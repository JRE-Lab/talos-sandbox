"""Deterministic gate evaluation (CONTRACT §8).

This module is the pass/fail boundary for the rollout gates. It is conceptually
shared with the real TALOS system: gate evaluation does not care whether the
metrics it is handed came from a live WinRM host or the simulated fleet, so the
sandbox imports and calls exactly this code path.

Pure functions, no I/O — directly unit-testable.
"""

from __future__ import annotations

from typing import Any

# Gate thresholds. canary_gate and ring_gate share the same boundary today, but
# they are kept as separate named gates so the agents can reference them by ring
# and so the real system can diverge them later without a contract change.
GATES: dict[str, dict[str, Any]] = {
    "canary_gate": {
        "max_memory_mb": 1500,
        "require_http": 200,
        "max_eventlog_errors": 5,
        "require_service": "Running",
    },
    "ring_gate": {
        "max_memory_mb": 1500,
        "require_http": 200,
        "max_eventlog_errors": 5,
        "require_service": "Running",
    },
}


def evaluate(gate: str, metrics: dict) -> dict:
    """Evaluate a gate against a metrics snapshot.

    Args:
        gate: gate name; must be a key in ``GATES``.
        metrics: a dict with keys ``service_state``, ``http_health``,
            ``eventlog_errors``, ``memory_mb`` (extra keys such as ``health``
            are ignored).

    Returns:
        ``{"result": "pass"|"fail", "violations": [str], "thresholds": dict}``.

    A metric violates if:
      * ``memory_mb`` > ``max_memory_mb``
      * ``http_health`` != ``require_http``
      * ``eventlog_errors`` > ``max_eventlog_errors``
      * ``service_state`` != ``require_service``

    ``result == "pass"`` iff there are no violations.

    Raises:
        KeyError: if ``gate`` is not a known gate.
    """
    if gate not in GATES:
        raise KeyError(f"unknown gate: {gate!r} (known: {sorted(GATES)})")

    thresholds = GATES[gate]
    violations: list[str] = []

    service_state = metrics.get("service_state")
    http_health = metrics.get("http_health")
    eventlog_errors = metrics.get("eventlog_errors")
    memory_mb = metrics.get("memory_mb")

    require_service = thresholds["require_service"]
    require_http = thresholds["require_http"]
    max_eventlog_errors = thresholds["max_eventlog_errors"]
    max_memory_mb = thresholds["max_memory_mb"]

    if service_state != require_service:
        violations.append(
            f"service_state {service_state!r} != required {require_service!r}"
        )
    if http_health != require_http:
        violations.append(
            f"http_health {http_health} != required {require_http}"
        )
    if eventlog_errors is None or eventlog_errors > max_eventlog_errors:
        violations.append(
            f"eventlog_errors {eventlog_errors} > max {max_eventlog_errors}"
        )
    if memory_mb is None or memory_mb > max_memory_mb:
        violations.append(
            f"memory_mb {memory_mb} > max {max_memory_mb}"
        )

    result = "pass" if not violations else "fail"
    return {
        "result": result,
        "violations": violations,
        "thresholds": dict(thresholds),
    }


def health_color(gate: str, metrics: dict) -> str:
    """Derive a health color from the gate thresholds (CONTRACT §8 helper).

    The pass/fail boundary is exactly :func:`evaluate`. This helper adds an
    "approaching" band so tiles can show ``amber`` before a hard ``red`` fail:

      * ``green``  — passes the gate with comfortable margin.
      * ``amber``  — still passes, but at least one metric is within the
        warning band (approaching a threshold).
      * ``red``    — fails the gate.

    The warning band is a fixed fraction of the thresholds; it is advisory and
    never changes the pass/fail result returned by :func:`evaluate`.
    """
    res = evaluate(gate, metrics)
    if res["result"] == "fail":
        return "red"

    thresholds = GATES[gate]
    max_memory_mb = thresholds["max_memory_mb"]
    max_eventlog_errors = thresholds["max_eventlog_errors"]

    memory_mb = metrics.get("memory_mb", 0) or 0
    eventlog_errors = metrics.get("eventlog_errors", 0) or 0

    # Warning band: within 80% of the memory ceiling, or within one error of the
    # eventlog ceiling. Crisp and bounded so demos are repeatable.
    memory_warn = max_memory_mb * 0.8
    eventlog_warn = max_eventlog_errors - 1

    if memory_mb >= memory_warn or eventlog_errors >= eventlog_warn:
        return "amber"
    return "green"
