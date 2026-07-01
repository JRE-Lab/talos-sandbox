"""Tests for tools.sim_backend.SimFleet (CONTRACT §2, §7, §8).

Uses an injected deterministic clock so sim-time is fully controllable and the
gate boundaries are crisp and repeatable.

Covered:
  * deploy healthy -> metrics steady & gate pass
  * deploy "2.1.5" then advance clock past REGRESSION_DELAY_S -> gate fail
    (memory high / http 500)
  * rollback resets to healthy
  * topology returns the 3 hosts with correct ring / ha_pair
"""

from __future__ import annotations

import pytest

from tools import get_backend
from tools.gates import GATES
from tools.sim_backend import (
    BASELINE_VERSION,
    GOOD_VERSION,
    REGRESSION_VERSION,
    SimFleet,
)


class FakeClock:
    """Manually advanced sim-clock for deterministic tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_fleet(delay: float = 8.0) -> tuple[SimFleet, FakeClock]:
    clock = FakeClock(0.0)
    fleet = SimFleet(clock=clock, regression_delay_s=delay, sim_speed=12.0)
    return fleet, clock


# -- topology --------------------------------------------------------------

def test_topology_three_hosts_correct_shape() -> None:
    fleet, _ = make_fleet()
    topo = fleet.get_fleet_topology()
    assert len(topo) == 3

    by_host = {h["host"]: h for h in topo}
    assert set(by_host) == {"TALOS-CANARY", "TALOS-R1A", "TALOS-R1B"}

    assert by_host["TALOS-CANARY"]["ring"] == "canary"
    assert by_host["TALOS-CANARY"]["ha_pair"] is None

    assert by_host["TALOS-R1A"]["ring"] == "ring1"
    assert by_host["TALOS-R1A"]["ha_pair"] == "TALOS-R1B"

    assert by_host["TALOS-R1B"]["ring"] == "ring1"
    assert by_host["TALOS-R1B"]["ha_pair"] == "TALOS-R1A"


def test_topology_seeds_baseline_all_green() -> None:
    fleet, _ = make_fleet()
    for row in fleet.get_fleet_topology():
        assert row["version"] == BASELINE_VERSION
        assert row["health"] == "green"


# -- healthy deploy --------------------------------------------------------

def test_deploy_healthy_metrics_steady_and_gate_pass() -> None:
    fleet, clock = make_fleet()
    res = fleet.deploy_version("TALOS-CANARY", GOOD_VERSION)
    assert res == {"ok": True, "host": "TALOS-CANARY", "version": GOOD_VERSION}

    # Sample across a long window; healthy build must stay green and pass.
    for _ in range(20):
        clock.advance(5.0)
        h = fleet.get_health("TALOS-CANARY")
        assert h["service_state"] == "Running"
        assert h["http_health"] == 200
        assert h["memory_mb"] <= GATES["canary_gate"]["max_memory_mb"]
        assert h["eventlog_errors"] <= GATES["canary_gate"]["max_eventlog_errors"]
        assert h["health"] == "green"
        gate = fleet.evaluate_gate("TALOS-CANARY", "canary_gate")
        assert gate["result"] == "pass"
        assert gate["violations"] == []


def test_capture_baseline_returns_snapshot() -> None:
    fleet, _ = make_fleet()
    base = fleet.capture_baseline("TALOS-R1A")
    assert base["service_state"] == "Running"
    assert base["http_health"] == 200
    assert "memory_mb" in base


# -- regression deploy -----------------------------------------------------

def test_regression_holds_baseline_before_delay() -> None:
    fleet, clock = make_fleet(delay=8.0)
    fleet.deploy_version("TALOS-CANARY", REGRESSION_VERSION)

    # Just before the delay, it should still look healthy and pass.
    clock.advance(7.0)
    h = fleet.get_health("TALOS-CANARY")
    assert h["http_health"] == 200
    assert h["memory_mb"] <= 1500
    assert fleet.evaluate_gate("TALOS-CANARY", "canary_gate")["result"] == "pass"


def test_regression_fails_after_delay() -> None:
    fleet, clock = make_fleet(delay=8.0)
    fleet.deploy_version("TALOS-CANARY", REGRESSION_VERSION)

    # Advance well past the delay + ramp so the breach is fully expressed.
    clock.advance(20.0)
    h = fleet.get_health("TALOS-CANARY")
    assert h["memory_mb"] > 1500
    assert h["http_health"] == 500
    assert h["eventlog_errors"] > 5
    assert h["health"] == "red"

    gate = fleet.evaluate_gate("TALOS-CANARY", "canary_gate")
    assert gate["result"] == "fail"
    assert any("memory_mb" in v for v in gate["violations"])
    assert any("http_health" in v for v in gate["violations"])


def test_regression_target_envelope() -> None:
    fleet, clock = make_fleet(delay=8.0)
    fleet.deploy_version("TALOS-R1A", REGRESSION_VERSION)
    clock.advance(40.0)
    h = fleet.get_health("TALOS-R1A")
    # memory climbs toward ~1900 (allow noise band).
    assert 1700 <= h["memory_mb"] <= 2000


# -- rollback --------------------------------------------------------------

def test_rollback_resets_to_healthy() -> None:
    fleet, clock = make_fleet(delay=8.0)
    fleet.deploy_version("TALOS-CANARY", REGRESSION_VERSION)
    clock.advance(20.0)
    assert fleet.evaluate_gate("TALOS-CANARY", "canary_gate")["result"] == "fail"

    res = fleet.rollback("TALOS-CANARY", BASELINE_VERSION)
    assert res == {"ok": True, "host": "TALOS-CANARY", "version": BASELINE_VERSION}

    # After rollback, time keeps moving but the host is healthy again.
    clock.advance(20.0)
    h = fleet.get_health("TALOS-CANARY")
    assert h["http_health"] == 200
    assert h["memory_mb"] <= 1500
    assert h["health"] == "green"
    assert fleet.evaluate_gate("TALOS-CANARY", "canary_gate")["result"] == "pass"

    topo = {r["host"]: r for r in fleet.get_fleet_topology()}
    assert topo["TALOS-CANARY"]["version"] == BASELINE_VERSION


# -- escape variant (S3b) --------------------------------------------------

def test_escape_variant_canary_clean_ring_breaches() -> None:
    fleet, clock = make_fleet(delay=8.0)
    # Escape deploy: regression only arms on ring1 hosts.
    fleet.deploy_version_escape("TALOS-CANARY", REGRESSION_VERSION)
    fleet.deploy_version_escape("TALOS-R1A", REGRESSION_VERSION)
    clock.advance(20.0)

    assert fleet.evaluate_gate("TALOS-CANARY", "canary_gate")["result"] == "pass"
    assert fleet.evaluate_gate("TALOS-R1A", "ring_gate")["result"] == "fail"


# -- selector --------------------------------------------------------------

def test_get_backend_sim_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    backend = get_backend()
    assert isinstance(backend, SimFleet)
    topo = backend.get_fleet_topology()
    assert len(topo) == 3


def test_get_backend_explicit_sim() -> None:
    backend = get_backend("sim")
    assert isinstance(backend, SimFleet)


def test_get_backend_unknown_raises() -> None:
    with pytest.raises(ValueError):
        get_backend("nope")


def test_get_backend_winrm_placeholder_raises() -> None:
    # winrm backend is a documented placeholder; constructing it must refuse.
    with pytest.raises(NotImplementedError):
        get_backend("winrm")


# -- determinism -----------------------------------------------------------

def test_health_deterministic_at_same_simtime() -> None:
    fleet, clock = make_fleet()
    fleet.deploy_version("TALOS-R1B", GOOD_VERSION)
    clock.advance(3.0)
    a = fleet.get_health("TALOS-R1B")
    b = fleet.get_health("TALOS-R1B")
    assert a == b


def test_unknown_host_raises() -> None:
    fleet, _ = make_fleet()
    with pytest.raises(KeyError):
        fleet.get_health("nonexistent")
