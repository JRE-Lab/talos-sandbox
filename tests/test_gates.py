"""Tests for tools.gates (CONTRACT §8).

Covers the pass/fail boundaries for every threshold:
  * memory just under / just over 1500
  * http 200 vs 500
  * eventlog under / over 5
  * service Running vs Stopped
"""

from __future__ import annotations

import pytest

from tools import gates


def _healthy() -> dict:
    return {
        "service_state": "Running",
        "http_health": 200,
        "eventlog_errors": 1,
        "memory_mb": 820,
    }


@pytest.mark.parametrize("gate", ["canary_gate", "ring_gate"])
def test_healthy_passes(gate: str) -> None:
    res = gates.evaluate(gate, _healthy())
    assert res["result"] == "pass"
    assert res["violations"] == []
    assert res["thresholds"]["max_memory_mb"] == 1500


def test_gates_dict_shape() -> None:
    for name in ("canary_gate", "ring_gate"):
        g = gates.GATES[name]
        assert g["max_memory_mb"] == 1500
        assert g["require_http"] == 200
        assert g["max_eventlog_errors"] == 5
        assert g["require_service"] == "Running"


# -- memory boundary: > 1500 fails; 1500 and below pass --------------------

def test_memory_just_under_threshold_passes() -> None:
    m = _healthy()
    m["memory_mb"] = 1499
    assert gates.evaluate("ring_gate", m)["result"] == "pass"


def test_memory_at_threshold_passes() -> None:
    # Violation is strictly greater-than, so exactly 1500 still passes.
    m = _healthy()
    m["memory_mb"] = 1500
    assert gates.evaluate("ring_gate", m)["result"] == "pass"


def test_memory_just_over_threshold_fails() -> None:
    m = _healthy()
    m["memory_mb"] = 1501
    res = gates.evaluate("ring_gate", m)
    assert res["result"] == "fail"
    assert any("memory_mb" in v for v in res["violations"])


def test_memory_regression_high_fails() -> None:
    m = _healthy()
    m["memory_mb"] = 1900
    assert gates.evaluate("ring_gate", m)["result"] == "fail"


# -- http boundary: 200 passes, anything else fails ------------------------

def test_http_200_passes() -> None:
    m = _healthy()
    m["http_health"] = 200
    assert gates.evaluate("canary_gate", m)["result"] == "pass"


def test_http_500_fails() -> None:
    m = _healthy()
    m["http_health"] = 500
    res = gates.evaluate("canary_gate", m)
    assert res["result"] == "fail"
    assert any("http_health" in v for v in res["violations"])


# -- eventlog boundary: > 5 fails; 5 and below pass ------------------------

def test_eventlog_just_under_threshold_passes() -> None:
    m = _healthy()
    m["eventlog_errors"] = 4
    assert gates.evaluate("ring_gate", m)["result"] == "pass"


def test_eventlog_at_threshold_passes() -> None:
    m = _healthy()
    m["eventlog_errors"] = 5
    assert gates.evaluate("ring_gate", m)["result"] == "pass"


def test_eventlog_just_over_threshold_fails() -> None:
    m = _healthy()
    m["eventlog_errors"] = 6
    res = gates.evaluate("ring_gate", m)
    assert res["result"] == "fail"
    assert any("eventlog_errors" in v for v in res["violations"])


# -- service boundary: Running passes, Stopped fails -----------------------

def test_service_running_passes() -> None:
    m = _healthy()
    m["service_state"] = "Running"
    assert gates.evaluate("ring_gate", m)["result"] == "pass"


def test_service_stopped_fails() -> None:
    m = _healthy()
    m["service_state"] = "Stopped"
    res = gates.evaluate("ring_gate", m)
    assert res["result"] == "fail"
    assert any("service_state" in v for v in res["violations"])


# -- multiple violations accumulate ----------------------------------------

def test_multiple_violations_all_reported() -> None:
    m = {
        "service_state": "Stopped",
        "http_health": 500,
        "eventlog_errors": 14,
        "memory_mb": 1900,
    }
    res = gates.evaluate("ring_gate", m)
    assert res["result"] == "fail"
    assert len(res["violations"]) == 4


def test_missing_metrics_fail_safe() -> None:
    # Absent numeric metrics should be treated as violations, not pass.
    res = gates.evaluate("ring_gate", {"service_state": "Running", "http_health": 200})
    assert res["result"] == "fail"


def test_unknown_gate_raises() -> None:
    with pytest.raises(KeyError):
        gates.evaluate("does_not_exist", _healthy())


# -- health_color helper ---------------------------------------------------

def test_health_color_green_when_comfortable() -> None:
    assert gates.health_color("ring_gate", _healthy()) == "green"


def test_health_color_red_on_fail() -> None:
    m = _healthy()
    m["memory_mb"] = 1900
    m["http_health"] = 500
    assert gates.health_color("ring_gate", m) == "red"


def test_health_color_amber_when_approaching() -> None:
    # 80% of 1500 = 1200; still passes the gate but should be amber.
    m = _healthy()
    m["memory_mb"] = 1300
    assert gates.evaluate("ring_gate", m)["result"] == "pass"
    assert gates.health_color("ring_gate", m) == "amber"
