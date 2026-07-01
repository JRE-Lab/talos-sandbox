#!/usr/bin/env python3
"""Record agent runs to replay transcripts.

Runs the TALOS agent loop (``app.agents.run_live``) against the simulated fleet
backend for one or more directives/scenarios and dumps the captured §3 events
(plus a derived ``audit_records`` list) to ``replays/<id>.json`` in the §4 schema.

This is how curated transcripts are regenerated from real agent runs: point it at
a directive, it drives the same code path the live endpoint uses, and writes a
deterministic JSON file the front-end can replay with zero runtime deps.

By default it records the DETERMINISTIC path (no OpenAI API), which is what makes
the captured transcripts reproducible and free. Pass ``--live`` to record an
actual OpenAI run instead (requires ``OPENAI_API_KEY``); note that live captures
are non-deterministic and may not match the curated scenario shapes.

Usage
-----
    # Record all built-in curated scenarios into replays/:
    python scripts/record_replays.py --all

    # Record a single built-in scenario by id:
    python scripts/record_replays.py --scenario s3

    # Record an ad-hoc directive to a chosen id/name:
    python scripts/record_replays.py \
        --id myrun --name "My run" \
        --directive "deploy 2.1.4 to the fleet" \
        --summary "ad-hoc" --moment "custom directive"

    # Record an actual live OpenAI run (non-deterministic):
    OPENAI_API_KEY=sk-... python scripts/record_replays.py --scenario s1 --live

Output
------
Each file conforms to the §4 transcript schema::

    {
      "id": "...", "name": "...", "summary": "...", "moment": "...",
      "events": [ <§3 event>, ... ],
      "audit_records": [ {id, action, actor, ts}, ... ]
    }

The server computes the audit hash chain at request time, so hashes are NOT
written here (per §4/§6).

Note: the canonical replays/s1..s4 transcripts are authored by hand (agent E) to
guarantee they demonstrate each "moment" exactly. This recorder is the tool that
lets those transcripts be *regenerated* from agent runs, and is the path the team
uses once the real TALOS agents are wired in behind ``run_live``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make the project root importable when run as a script (scripts/ is one level
# below the repo root that holds app/ and tools/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.agents import run_deterministic, run_live, Emitter  # noqa: E402
from tools import get_backend  # noqa: E402

REPLAYS_DIR = _REPO_ROOT / "replays"

# Canonical filename per scenario id. Re-recording writes to the SAME file
# (overwrites in place) rather than creating e.g. ``s1.json`` alongside the
# curated ``s1_clean_rollout.json`` — two files with the same scenario ``id``
# would make ``replay.load_all()`` fail loud on a duplicate id and break the
# served scenario set. Unknown ids fall back to ``<id>.json``.
CANONICAL_FILENAMES: Dict[str, str] = {
    "s1": "s1_clean_rollout.json",
    "s2": "s2_block_bad_plan.json",
    "s3": "s3_catch_regression.json",
    "s3b": "s3b_escape_to_ring.json",
    "s4": "s4_verify_audit.json",
}

# Base timestamp for synthesizing audit-record ts values. Deterministic so reruns
# produce identical files.
_BASE_TS = datetime.datetime(2026, 7, 2, 15, 0, 0, tzinfo=datetime.timezone.utc)


# Built-in curated scenario directives. The deterministic orchestrator branches
# on the directive text (urgency / version / aggressiveness), so these strings
# steer it toward each scenario's "moment". s4 mirrors s1 (audit is the point).
BUILTIN_SCENARIOS: Dict[str, Dict[str, str]] = {
    "s1": {
        "id": "s1",
        "name": "Clean rollout",
        "summary": "Routine update deploys canary→ring with a clean soak. Fleet ends green.",
        "moment": "the happy path — every gate passes",
        "directive": "Deploy version 2.1.4 to the fleet as a routine update.",
    },
    "s2": {
        "id": "s2",
        "name": "Block the bad plan",
        "summary": "An urgent directive yields an aggressive plan; the Critic rejects it on HA-pair blast radius, forces a revision, then approves.",
        "moment": "the Critic rejects the unsafe plan, live",
        "directive": "Critical zero-day — patch all 3 hosts now, immediately, version 2.1.4.",
    },
    "s3": {
        "id": "s3",
        "name": "Catch the regression",
        "summary": "A bad build (2.1.5) goes green then breaches during canary soak; auto-rollback fires and the fleet returns green.",
        "moment": "the regression is caught at the canary and rolled back",
        "directive": "Deploy version 2.1.5 to the fleet.",
    },
    # s3b and s4 share the deterministic engine's s3/s1 shapes respectively here;
    # the hand-authored canonical transcripts cover their full distinct moments.
    "s3b": {
        "id": "s3b",
        "name": "Escape to ring",
        "summary": "The regression passes canary clean but breaches after promotion to ring1, triggering a fleet-scale rollback.",
        "moment": "the regression escapes the canary and is caught at the ring",
        "directive": "Deploy version 2.1.5 to the fleet.",
    },
    "s4": {
        "id": "s4",
        "name": "Verify the audit",
        "summary": "A compact clean run whose every action is recorded to a tamper-evident audit chain.",
        "moment": "every action is recorded to a tamper-evident chain",
        "directive": "Deploy version 2.1.4 to the fleet as a routine update.",
    },
}


def _ts(offset_seconds: float) -> str:
    """Deterministic ISO-8601 UTC timestamp at base + offset (seconds, rounded)."""
    dt = _BASE_TS + datetime.timedelta(seconds=int(round(offset_seconds)))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _derive_audit_records(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build §4 ``audit_records`` from the actionable events in a run.

    Deploys and rollbacks are the audited actions (the executor mutating the
    fleet). Each record is ``{id, action, actor, ts}``; the server computes the
    hash chain (§6), so no hashes are written here.
    """
    records: List[Dict[str, Any]] = []
    next_id = 1
    for ev in events:
        etype = ev.get("type")
        data = ev.get("data", {}) or {}
        actor = ev.get("actor", "system")
        t = float(ev.get("t", 0.0) or 0.0)
        if etype == "deploy":
            host = data.get("host", "?")
            frm = data.get("from_version", "?")
            to = data.get("to_version", "?")
            action = f"deploy {host} {frm} -> {to}"
        elif etype == "rollback":
            hosts = ",".join(data.get("hosts", []) or []) or "?"
            to = data.get("to_version", "?")
            action = f"rollback {data.get('scope', 'host')} [{hosts}] -> {to}"
        else:
            continue
        records.append({"id": next_id, "action": action, "actor": actor, "ts": _ts(t)})
        next_id += 1
    return records


async def _capture_run(directive: str, live: bool) -> List[Dict[str, Any]]:
    """Drive one agent run and collect the ordered §3 events it emits.

    With ``live=False`` (default) the deterministic, no-API path is used so the
    transcript is reproducible. With ``live=True`` the full ``run_live`` selector
    is used, which delegates to the OpenAI reference adapter when a key is present.
    """
    events: List[Dict[str, Any]] = []

    async def emit(event: Dict[str, Any]) -> None:
        events.append(event)

    backend = get_backend(os.environ.get("SANDBOX_BACKEND", "sim"))

    if live:
        # Full selector: OpenAI reference adapter if key present, else deterministic.
        await run_live(directive, backend, emit)
    else:
        # Force the deterministic path regardless of any key in the environment,
        # so recorded transcripts are reproducible and free.
        em = Emitter(emit)
        await run_deterministic(directive, backend, em)

    # Ensure seq ordering is monotonic and contiguous (the engine assigns seq,
    # but if a live run interleaves we normalize defensively).
    events.sort(key=lambda e: e.get("seq", 0))
    return events


def _build_transcript(meta: Dict[str, str], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble a §4 transcript dict from scenario metadata + captured events."""
    return {
        "id": meta["id"],
        "name": meta["name"],
        "summary": meta["summary"],
        "moment": meta["moment"],
        "events": events,
        "audit_records": _derive_audit_records(events),
    }


def _write_transcript(
    transcript: Dict[str, Any], out_dir: Path, force: bool
) -> Optional[Path]:
    """Write a transcript to its canonical ``<out_dir>/<file>`` path.

    Returns the path written, or ``None`` if the file already exists and
    ``force`` is false (the curated transcripts are richer than the
    deterministic-engine output, so we never silently overwrite them).
    """
    sid = transcript["id"]
    filename = CANONICAL_FILENAMES.get(sid, f"{sid}.json")
    path = out_dir / filename
    if path.exists() and not force:
        print(
            f"skip {path.name} (exists; pass --force to regenerate it from agent runs)"
        )
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
    return path


async def _record_one(
    meta: Dict[str, str], live: bool, out_dir: Path, force: bool
) -> Optional[Path]:
    """Capture and write a single transcript; return the output path (or None if skipped)."""
    events = await _capture_run(meta["directive"], live=live)
    transcript = _build_transcript(meta, events)
    return _write_transcript(transcript, out_dir, force)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Record TALOS agent runs to replay transcripts (replays/<id>.json).",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--all", action="store_true", help="Record every built-in curated scenario.")
    src.add_argument(
        "--scenario",
        choices=sorted(BUILTIN_SCENARIOS.keys()),
        help="Record a single built-in curated scenario by id.",
    )
    src.add_argument("--directive", help="Record an ad-hoc directive (requires --id and --name).")

    p.add_argument("--id", help="Transcript id for an ad-hoc --directive run.")
    p.add_argument("--name", help="Display name for an ad-hoc --directive run.")
    p.add_argument("--summary", default="", help="Summary line for an ad-hoc run.")
    p.add_argument("--moment", default="", help="The 'moment' this run demonstrates.")
    p.add_argument(
        "--live",
        action="store_true",
        help="Record an actual OpenAI live run (non-deterministic; needs OPENAI_API_KEY).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing transcripts. Without it, files that already exist are skipped "
        "so the curated replays/*.json are never clobbered by deterministic output.",
    )
    p.add_argument(
        "--out-dir",
        default=str(REPLAYS_DIR),
        help=f"Output directory for transcripts (default: {REPLAYS_DIR}).",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)

    if args.all:
        written: List[Path] = []
        skipped = 0
        for sid in sorted(BUILTIN_SCENARIOS.keys()):
            path = await _record_one(BUILTIN_SCENARIOS[sid], args.live, out_dir, args.force)
            if path is None:
                skipped += 1
                continue
            written.append(path)
            print(f"wrote {path}")
        print(f"done: {len(written)} written, {skipped} skipped (use --force to overwrite)")
        return 0

    if args.scenario:
        path = await _record_one(
            BUILTIN_SCENARIOS[args.scenario], args.live, out_dir, args.force
        )
        if path is not None:
            print(f"wrote {path}")
        return 0

    # Ad-hoc directive.
    if not args.id or not args.name:
        print("error: --directive requires --id and --name", file=sys.stderr)
        return 2
    meta = {
        "id": args.id,
        "name": args.name,
        "summary": args.summary,
        "moment": args.moment,
        "directive": args.directive,
    }
    path = await _record_one(meta, args.live, out_dir, args.force)
    if path is not None:
        print(f"wrote {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
