"""Schema + audit-chain tests for the recorded transcripts (``replays/*.json``).

For each transcript these tests assert the CONTRACT §3/§4 invariants:
  * it validates (against ``app.models`` when available, else structurally);
  * the first event is ``topology`` and the last is ``done``;
  * every event ``type`` is one the contract defines (§3);
  * the audit chain verifies clean, and tampering an existing record makes the
    chain fail at exactly that record id (§6).

These run against the transcripts agent E authors in the same tree. They are
parametrized over whatever ``replays/*.json`` files exist; if none exist yet a
single guard test reports the directory as empty rather than silently passing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app import replay


REPLAYS_DIR: Path = replay.REPLAYS_DIR
TRANSCRIPT_PATHS: List[Path] = sorted(REPLAYS_DIR.glob("*.json")) if REPLAYS_DIR.is_dir() else []
TRANSCRIPT_IDS: List[str] = [p.stem for p in TRANSCRIPT_PATHS]


def _read(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_replays_directory_present() -> None:
    """The replays directory must exist (transcripts are baked into the image)."""
    assert REPLAYS_DIR.is_dir(), f"replays dir missing: {REPLAYS_DIR}"


def test_at_least_one_transcript() -> None:
    """There must be at least one transcript to demo (agent E provides 5)."""
    assert TRANSCRIPT_PATHS, (
        "no replays/*.json transcripts found — agent E authors these in the same tree"
    )


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_transcript_validates(path: Path) -> None:
    """Each transcript loads + validates against app.models (or structurally)."""
    raw = _read(path)
    # Should not raise — _validate_with_models enforces the §4 schema.
    validated = replay._validate_with_models(raw)
    assert validated["id"], "transcript must carry a non-empty id"


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_first_event_is_topology(path: Path) -> None:
    """First event SHOULD initialize tiles via a ``topology`` event (§4)."""
    raw = _read(path)
    events = raw["events"]
    assert events, "transcript has no events"
    assert events[0]["type"] == "topology", (
        f"{path.name}: first event is '{events[0]['type']}', expected 'topology'"
    )


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_last_event_is_done(path: Path) -> None:
    """Last event SHOULD be a terminal ``done`` event (§4)."""
    raw = _read(path)
    events = raw["events"]
    assert events, "transcript has no events"
    assert events[-1]["type"] == "done", (
        f"{path.name}: last event is '{events[-1]['type']}', expected 'done'"
    )


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_every_event_type_is_known(path: Path) -> None:
    """Every event ``type`` must be one the contract defines (§3)."""
    raw = _read(path)
    for idx, ev in enumerate(raw["events"]):
        assert ev["type"] in replay.VALID_EVENT_TYPES, (
            f"{path.name}: event[{idx}] has unknown type '{ev['type']}'"
        )


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_event_seq_monotonic(path: Path) -> None:
    """``seq`` is monotonic from 0 within a run (§3) — sanity for the renderer."""
    raw = _read(path)
    seqs = [ev["seq"] for ev in raw["events"]]
    assert seqs == sorted(seqs), f"{path.name}: event seq values are not monotonic"


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_audit_chain_verifies(path: Path) -> None:
    """The untampered audit chain verifies clean: ``ok=True, broken_at=None`` (§6)."""
    raw = _read(path)
    records = raw.get("audit_records", []) or []
    if not records:
        pytest.skip(f"{path.name} has no audit_records")
    result = replay.verify_chain(records, tamper=None)
    assert result["ok"] is True, f"{path.name}: clean chain did not verify"
    assert result["broken_at"] is None


@pytest.mark.skipif(not TRANSCRIPT_PATHS, reason="no transcripts present yet")
@pytest.mark.parametrize("path", TRANSCRIPT_PATHS, ids=TRANSCRIPT_IDS)
def test_audit_chain_tamper_detected(path: Path) -> None:
    """Tampering an existing record fails the chain at exactly that record id (§6)."""
    raw = _read(path)
    records = raw.get("audit_records", []) or []
    if not records:
        pytest.skip(f"{path.name} has no audit_records")
    # Pick a real record id to tamper.
    target_id = records[0]["id"]
    result = replay.verify_chain(records, tamper=target_id)
    assert result["ok"] is False, (
        f"{path.name}: tamper of record {target_id} was not detected"
    )
    assert result["broken_at"] == target_id, (
        f"{path.name}: broken_at={result['broken_at']}, expected {target_id}"
    )


def test_compute_chain_seeds_genesis() -> None:
    """First record's ``prev_hash`` is the GENESIS seed (§6) — unit check."""
    records = [
        {"id": 1, "action": "deploy TALOS-CANARY 2.1.3 -> 2.1.4", "actor": "executor", "ts": "2026-07-02T15:00:01Z"},
        {"id": 2, "action": "gate_eval canary_gate pass", "actor": "monitor", "ts": "2026-07-02T15:00:11Z"},
    ]
    chained = replay.compute_audit_chain(records)
    assert chained[0]["prev_hash"] == replay.GENESIS
    assert chained[1]["prev_hash"] == chained[0]["hash"]
    # Every hash is a 64-char sha256 hex digest.
    for rec in chained:
        assert len(rec["hash"]) == 64
        int(rec["hash"], 16)  # parses as hex


def test_verify_tamper_on_synthetic_chain() -> None:
    """End-to-end tamper detection on a hand-built chain (independent of agent E)."""
    records = [
        {"id": 1, "action": "deploy TALOS-CANARY 2.1.3 -> 2.1.4", "actor": "executor", "ts": "2026-07-02T15:00:01Z"},
        {"id": 2, "action": "gate_eval canary_gate pass", "actor": "monitor", "ts": "2026-07-02T15:00:11Z"},
        {"id": 3, "action": "approval ring1 granted", "actor": "system", "ts": "2026-07-02T15:00:20Z"},
    ]
    assert replay.verify_chain(records)["ok"] is True
    tampered = replay.verify_chain(records, tamper=2)
    assert tampered["ok"] is False
    assert tampered["broken_at"] == 2
    # Tampering a non-existent record leaves the chain intact.
    assert replay.verify_chain(records, tamper=999)["ok"] is True
