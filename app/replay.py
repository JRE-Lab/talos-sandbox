"""Replay engine — load + validate recorded transcripts and compute the audit chain.

Owns:
  * Discovery / loading / validation of ``replays/*.json`` against ``app.models``.
  * ``list_scenarios`` / ``get_scenario`` for the HTTP layer.
  * The sandbox-local, tamper-evident audit hash chain (CONTRACT §6).

Replay mode has **zero runtime dependencies** beyond the standard library + pydantic:
no network, no model, no database. It cannot fall over.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# replay.py lives at <repo>/app/replay.py — the transcripts live at <repo>/replays.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
REPLAYS_DIR: Path = REPO_ROOT / "replays"

# ---------------------------------------------------------------------------
# Contract constants (§3 / §6) — kept here so replay.py is self-validating even
# if app.models is unavailable at import time (e.g. parallel authoring).
# ---------------------------------------------------------------------------

#: The complete set of event ``type`` values defined by the contract (§3).
VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "directive",
        "topology",
        "plan",
        "critique",
        "verdict",
        "revision",
        "deploy",
        "gate_eval",
        "health",
        "soak",
        "approval_required",
        "approved",
        "breach",
        "rollback",
        "narration",
        "done",
    }
)

#: The seed of the audit hash chain (§6).
GENESIS: str = "GENESIS"


class TranscriptValidationError(ValueError):
    """Raised when a transcript file does not conform to the contract schema."""


# ---------------------------------------------------------------------------
# Validation against app.models (defensive: degrade to structural checks)
# ---------------------------------------------------------------------------


def _validate_with_models(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate ``raw`` against ``app.models`` if a transcript model is present.

    Agent A owns ``app.models`` (pydantic v2 models for events, transcripts, host
    state, gate results). The contract does not pin the exact class name, so we
    look for a small set of conventional names and use the first that exists.
    If none is found (or the import fails — e.g. during parallel authoring), we
    fall back to the structural validation in :func:`_validate_structure`, which
    enforces exactly the CONTRACT §4 schema. The structural pass always runs too,
    so the guarantees the rest of the system relies on hold regardless.
    """
    try:
        from app import models as _models  # type: ignore
    except Exception:
        return _validate_structure(raw)

    candidate = None
    for name in ("Transcript", "TranscriptModel", "ReplayTranscript", "Scenario"):
        candidate = getattr(_models, name, None)
        if candidate is not None and hasattr(candidate, "model_validate"):
            break
        candidate = None

    if candidate is not None:
        try:
            # pydantic v2: validate then re-dump to a plain dict so downstream
            # consumers always see JSON-safe primitives.
            model = candidate.model_validate(raw)
            validated = model.model_dump(mode="json")
            # Belt-and-suspenders: the model may not carry every field through,
            # so still run the structural pass on the original payload.
            _validate_structure(raw)
            return validated
        except TranscriptValidationError:
            raise
        except Exception as exc:  # pydantic.ValidationError or anything else
            raise TranscriptValidationError(
                f"transcript failed app.models validation: {exc}"
            ) from exc

    # No usable model class — fall back to structural checks only.
    return _validate_structure(raw)


def _validate_structure(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the CONTRACT §4 transcript schema with stdlib-only checks."""
    if not isinstance(raw, dict):
        raise TranscriptValidationError("transcript root must be a JSON object")

    for key in ("id", "name", "summary", "moment", "events"):
        if key not in raw:
            raise TranscriptValidationError(f"transcript missing required key '{key}'")

    if not isinstance(raw["id"], str) or not raw["id"]:
        raise TranscriptValidationError("transcript 'id' must be a non-empty string")

    events = raw["events"]
    if not isinstance(events, list) or not events:
        raise TranscriptValidationError("transcript 'events' must be a non-empty list")

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise TranscriptValidationError(f"event[{idx}] must be a JSON object")
        for key in ("seq", "t", "type", "actor"):
            if key not in ev:
                raise TranscriptValidationError(
                    f"event[{idx}] missing required key '{key}'"
                )
        etype = ev["type"]
        if etype not in VALID_EVENT_TYPES:
            raise TranscriptValidationError(
                f"event[{idx}] has unknown type '{etype}'"
            )

    # audit_records is optional; when present it must be a list of objects with
    # the four author-provided fields (§4). Hashes are NOT stored in the file.
    records = raw.get("audit_records", [])
    if records is not None and not isinstance(records, list):
        raise TranscriptValidationError("'audit_records' must be a list when present")
    for idx, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            raise TranscriptValidationError(f"audit_records[{idx}] must be an object")
        for key in ("id", "action", "actor", "ts"):
            if key not in rec:
                raise TranscriptValidationError(
                    f"audit_records[{idx}] missing required key '{key}'"
                )

    return raw


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_file(path: Path) -> Dict[str, Any]:
    """Read + parse + validate a single transcript file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise TranscriptValidationError(f"{path.name}: invalid JSON: {exc}") from exc
    return _validate_with_models(raw)


def load_all() -> Dict[str, Dict[str, Any]]:
    """Load every ``replays/*.json`` transcript, keyed by its scenario id.

    Sorted by filename for deterministic ordering. Raises
    :class:`TranscriptValidationError` if any file is malformed — fail loud at
    startup rather than serving a broken scenario.
    """
    scenarios: Dict[str, Dict[str, Any]] = {}
    if not REPLAYS_DIR.is_dir():
        return scenarios
    for path in sorted(REPLAYS_DIR.glob("*.json")):
        transcript = _load_file(path)
        sid = transcript["id"]
        if sid in scenarios:
            raise TranscriptValidationError(
                f"duplicate scenario id '{sid}' (from {path.name})"
            )
        scenarios[sid] = transcript
    return scenarios


def list_scenarios() -> List[Dict[str, str]]:
    """Return the picker summary list: ``[{id, name, summary, moment}]`` (§5)."""
    out: List[Dict[str, str]] = []
    for transcript in load_all().values():
        out.append(
            {
                "id": transcript["id"],
                "name": transcript.get("name", transcript["id"]),
                "summary": transcript.get("summary", ""),
                "moment": transcript.get("moment", ""),
            }
        )
    return out


def get_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    """Return the full validated transcript dict for ``scenario_id`` or ``None``."""
    return load_all().get(scenario_id)


# ---------------------------------------------------------------------------
# Audit hash chain (CONTRACT §6)
# ---------------------------------------------------------------------------


def _canonical(record: Dict[str, Any], prev_hash: str) -> str:
    """Build the canonical pre-image string for a record (§6).

    ``f"{id}|{action}|{actor}|{ts}|{prev_hash}"``
    """
    return f"{record['id']}|{record['action']}|{record['actor']}|{record['ts']}|{prev_hash}"


def _hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_audit_chain(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compute the hash chain over ``records`` (ordered by ``id``) per §6.

    Returns NEW record dicts (the inputs are not mutated), each carrying the
    author-provided fields plus ``prev_hash`` and ``hash``. ``prev_hash`` of the
    first record is ``"GENESIS"``; each subsequent ``prev_hash`` is the previous
    record's ``hash``.
    """
    ordered = sorted(records, key=lambda r: r["id"])
    chained: List[Dict[str, Any]] = []
    prev_hash = GENESIS
    for record in ordered:
        canonical = _canonical(record, prev_hash)
        h = _hash(canonical)
        chained.append(
            {
                "id": record["id"],
                "action": record["action"],
                "actor": record["actor"],
                "ts": record["ts"],
                "prev_hash": prev_hash,
                "hash": h,
            }
        )
        prev_hash = h
    return chained


def verify_chain(
    records: List[Dict[str, Any]], tamper: Optional[int] = None
) -> Dict[str, Any]:
    """Verify the audit chain; optionally tamper a record to prove detection (§6).

    With ``tamper=None``: recompute the chain from the author-provided fields and
    confirm it is internally consistent → ``{"ok": True, "broken_at": None}``.

    With ``tamper=K``: build a mutated copy where record ``K``'s ``action`` has
    ``" [TAMPERED]"`` appended, recompute from scratch, and find the first index
    whose hash diverges from the untampered chain. That index's record id is
    ``broken_at`` → ``{"ok": False, "broken_at": K}``.
    """
    ordered = sorted(records, key=lambda r: r["id"])
    baseline = compute_audit_chain(ordered)

    if tamper is None:
        return {"ok": True, "broken_at": None}

    # Build the mutated copy: append the tamper marker to record `tamper`'s action.
    mutated: List[Dict[str, Any]] = []
    tampered_any = False
    for record in ordered:
        copy = dict(record)
        if record["id"] == tamper:
            copy["action"] = f"{record['action']} [TAMPERED]"
            tampered_any = True
        mutated.append(copy)

    if not tampered_any:
        # Tamper target does not exist — nothing diverges, chain stays intact.
        return {"ok": True, "broken_at": None}

    mutated_chain = compute_audit_chain(mutated)

    broken_at: Optional[Any] = None
    for base_rec, mut_rec in zip(baseline, mutated_chain):
        if base_rec["hash"] != mut_rec["hash"]:
            broken_at = mut_rec["id"]
            break

    if broken_at is None:
        # Defensive: a change must perturb the chain; if not, report consistent.
        return {"ok": True, "broken_at": None}

    return {"ok": False, "broken_at": broken_at}


def get_audit_records(scenario_id: str) -> Optional[List[Dict[str, Any]]]:
    """Return the chained audit records for a scenario, or ``None`` if unknown."""
    transcript = get_scenario(scenario_id)
    if transcript is None:
        return None
    return compute_audit_chain(transcript.get("audit_records", []) or [])
