"""Pydantic v2 models (CONTRACT §3, §4, §7, §8).

These models describe the shared shapes that flow between the backend, the
transcripts, and the front-end:

  * :class:`Event`       — the §3 event envelope (identical for SSE frames and
    transcript ``events[]``).
  * :class:`Transcript`  — a §4 replay file.
  * :class:`AuditRecord` — one §4 audit record (author-supplied fields only;
    the server computes the hash chain).
  * :class:`HostState`   — a §7 topology / health row.
  * :class:`GateResult`  — a §8 gate evaluation result.

The models are intentionally permissive: ``Event.data`` is a free-form dict so
the authored transcripts (which carry type-specific payloads per §3) load
without per-type schemas, and unknown event types never break loading.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# Actors and event types per §3. Kept as Literals for documentation/validation,
# but Event.type also accepts arbitrary strings so unknown types don't crash
# loading (the front-end renders unknown types as a generic feed line).
Actor = Literal["system", "planner", "critic", "monitor", "executor", "fleet"]
Health = Literal["green", "amber", "red"]


class Event(BaseModel):
    """The §3 event envelope. SSE frames and transcript events share this shape."""

    model_config = ConfigDict(extra="allow")

    seq: int = Field(..., description="monotonic from 0 within a run")
    t: float = Field(..., description="sim-time seconds (for pacing/labels)")
    type: str = Field(..., description="event type (see §3 table)")
    actor: str = Field(..., description="system|planner|critic|monitor|executor|fleet")
    title: str = Field("", description="short title")
    body: str = Field("", description="plain text, may be multi-sentence")
    data: dict[str, Any] = Field(default_factory=dict, description="type-specific payload")


class AuditRecord(BaseModel):
    """A §4 audit record as authored in the transcript.

    Authors provide only ``{id, action, actor, ts}``; the server computes the
    hash chain (§6), so ``prev_hash``/``hash`` are NOT stored in the file. They
    are accepted here as optional so a server-augmented record can also validate.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    action: str
    actor: str
    ts: str
    prev_hash: Optional[str] = None
    hash: Optional[str] = None


class Transcript(BaseModel):
    """A §4 replay transcript file (``replays/*.json``)."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    summary: str
    moment: str
    events: List[Event] = Field(default_factory=list)
    audit_records: List[AuditRecord] = Field(default_factory=list)


class HostState(BaseModel):
    """A §7 topology / health row.

    Covers both the topology shape ``{host, ring, ha_pair, version, health}``
    and the richer health sample ``{service_state, http_health, eventlog_errors,
    memory_mb, health}`` — all metric fields are optional so either form loads.
    """

    model_config = ConfigDict(extra="allow")

    host: str
    ring: Optional[str] = None
    ha_pair: Optional[str] = None
    version: Optional[str] = None
    health: Optional[str] = None
    service_state: Optional[str] = None
    http_health: Optional[int] = None
    eventlog_errors: Optional[int] = None
    memory_mb: Optional[int] = None


class GateResult(BaseModel):
    """A §8 gate evaluation result."""

    model_config = ConfigDict(extra="allow")

    result: Literal["pass", "fail"]
    violations: List[str] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    host: Optional[str] = None
    gate: Optional[str] = None
    metrics: Optional[dict[str, Any]] = None


__all__ = [
    "Actor",
    "Health",
    "Event",
    "AuditRecord",
    "Transcript",
    "HostState",
    "GateResult",
]
