"""Simulated fleet backend (CONTRACT §2, §7).

``SimFleet`` is an in-memory model of the same fleet shape as the real TALOS
lab: a canary plus a 2-host ring containing one HA pair. It implements the
typed, allowlisted ``FleetBackend`` tool surface the agents are restricted to,
so the *same* agent code drives either this simulator or the real WinRM
transport without knowing the difference.

Determinism is load-bearing: the sim clock can be advanced explicitly
(``tick``) or read from wall-clock time, and trajectory sampling plus the noise
function are deterministic given the sim-time. This keeps demos repeatable and
the gate boundaries crisp.

Tunables (env, with defaults):
  * ``REGRESSION_DELAY_S`` = 8 — sim-seconds the bad build holds baseline.
  * ``SIM_SPEED``          = 12 — wall→sim time compression for soak windows.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable, Optional

from tools import gates as gates_module

# ---------------------------------------------------------------------------
# Fixed fleet constants (CONTRACT §2). Sim and transcripts must agree exactly.
# ---------------------------------------------------------------------------

BASELINE_VERSION = "2.1.3"
GOOD_VERSION = "2.1.4"
REGRESSION_VERSION = "2.1.5"

# host -> (ring, ha_pair, role)
FLEET_LAYOUT: list[dict[str, Any]] = [
    {"host": "TALOS-CANARY", "ring": "canary", "ha_pair": None, "role": "canary"},
    {"host": "TALOS-R1A", "ring": "ring1", "ha_pair": "TALOS-R1B",
     "role": "production, HA pair member A"},
    {"host": "TALOS-R1B", "ring": "ring1", "ha_pair": "TALOS-R1A",
     "role": "production, HA pair member B"},
]

# Baseline healthy metrics (CONTRACT §2).
BASELINE_MEMORY_MB = 820          # ~ 780-860
BASELINE_HTTP = 200
BASELINE_EVENTLOG = 1             # in 0..2
BASELINE_SERVICE = "Running"

# Regression target envelope (CONTRACT §2).
REGRESSION_MEMORY_TARGET = 1900   # memory_mb climbs toward ~1900
REGRESSION_HTTP_BAD = 500         # http_health flips 200 -> 500
REGRESSION_EVENTLOG_PEAK = 14     # climbs past the gate threshold (5)

# How long (sim-seconds) the regression takes to ramp from delay-end to peak.
REGRESSION_RAMP_S = 6.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class _HostState:
    """Mutable per-host simulation state."""

    __slots__ = (
        "host", "ring", "ha_pair", "role",
        "version", "trajectory", "t_deploy", "armed",
    )

    def __init__(self, host: str, ring: str, ha_pair: Optional[str], role: str):
        self.host = host
        self.ring = ring
        self.ha_pair = ha_pair
        self.role = role
        self.version = BASELINE_VERSION
        # "healthy" or "regression"
        self.trajectory = "healthy"
        # sim-time at which the current version was deployed.
        self.t_deploy = 0.0
        # whether the regression trajectory is active on this host.
        self.armed = False


class SimFleet:
    """In-memory fleet backend implementing the §7 tool surface.

    The constructor seeds the §2 fleet at baseline ``2.1.3``, all green.

    Time handling: ``now()`` returns sim-seconds. By default it derives from
    wall-clock elapsed since construction multiplied by ``SIM_SPEED`` (so a
    120s soak completes in ~10s of wall time). For deterministic tests, pass a
    ``clock`` callable (returns sim-seconds directly) or drive ``tick(dt)`` and
    rely solely on the manual clock.
    """

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], float]] = None,
        regression_delay_s: Optional[float] = None,
        sim_speed: Optional[float] = None,
    ) -> None:
        self.regression_delay_s = (
            regression_delay_s
            if regression_delay_s is not None
            else _env_float("REGRESSION_DELAY_S", 8.0)
        )
        self.sim_speed = (
            sim_speed if sim_speed is not None else _env_float("SIM_SPEED", 12.0)
        )

        # Manual sim clock (advanced by tick); used when no external clock and
        # when wall-clock is disabled.
        self._manual_t = 0.0
        # If an explicit clock callable is supplied, it is authoritative and
        # wall-clock is ignored (fully deterministic for tests).
        self._clock = clock
        # Wall-clock origin for the default time source.
        self._wall_origin = time.monotonic()
        # When True, now() ignores wall-clock and uses only the manual clock.
        self._manual_only = clock is None and _env_truthy("SIM_MANUAL_CLOCK")

        self._hosts: dict[str, _HostState] = {}
        for spec in FLEET_LAYOUT:
            self._hosts[spec["host"]] = _HostState(
                host=spec["host"],
                ring=spec["ring"],
                ha_pair=spec["ha_pair"],
                role=spec["role"],
            )

    # -- clock -------------------------------------------------------------

    def now(self) -> float:
        """Current sim-time in seconds."""
        if self._clock is not None:
            return float(self._clock())
        if self._manual_only:
            return self._manual_t
        # Wall-clock since construction, compressed by sim_speed, plus any
        # manual ticks the caller has applied.
        wall = time.monotonic() - self._wall_origin
        return self._manual_t + wall * self.sim_speed

    def tick(self, dt: float) -> float:
        """Advance the manual sim clock by ``dt`` sim-seconds; returns now()."""
        self._manual_t += float(dt)
        return self.now()

    # -- topology ----------------------------------------------------------

    def get_fleet_topology(self) -> list[dict]:
        """Return [{host, ring, ha_pair, version, health}] for every host."""
        out: list[dict] = []
        for spec in FLEET_LAYOUT:
            st = self._hosts[spec["host"]]
            metrics = self.get_health(st.host)
            out.append(
                {
                    "host": st.host,
                    "ring": st.ring,
                    "ha_pair": st.ha_pair,
                    "version": st.version,
                    "health": metrics["health"],
                }
            )
        return out

    # -- health ------------------------------------------------------------

    def get_health(self, host: str) -> dict:
        """Sample the host trajectory at current sim-time.

        Returns ``{service_state, http_health, eventlog_errors, memory_mb,
        health}``. The ``health`` color is derived from the §8 gate thresholds:
        green=pass, amber=approaching, red=fail.
        """
        st = self._require_host(host)
        t = self.now()
        elapsed = max(0.0, t - st.t_deploy)

        if st.trajectory == "regression" and st.armed:
            metrics = self._regression_metrics(host, elapsed)
        else:
            metrics = self._healthy_metrics(host, elapsed)

        # Gate used for the color is the host's ring gate (canary vs ring).
        gate = "canary_gate" if st.ring == "canary" else "ring_gate"
        metrics["health"] = gates_module.health_color(gate, metrics)
        return metrics

    def _healthy_metrics(self, host: str, elapsed: float) -> dict:
        n = self._noise(host, elapsed)
        memory = BASELINE_MEMORY_MB + int(round(n * 30))  # +/- ~30 MB
        eventlog = BASELINE_EVENTLOG + (1 if n > 0.6 else 0)
        return {
            "service_state": BASELINE_SERVICE,
            "http_health": BASELINE_HTTP,
            "eventlog_errors": max(0, eventlog),
            "memory_mb": max(0, memory),
        }

    def _regression_metrics(self, host: str, elapsed: float) -> dict:
        delay = self.regression_delay_s
        if elapsed < delay:
            # Holds baseline before the breach arms.
            return self._healthy_metrics(host, elapsed)

        # Progress 0..1 across the ramp once past the delay.
        ramp = REGRESSION_RAMP_S if REGRESSION_RAMP_S > 0 else 1.0
        p = min(1.0, (elapsed - delay) / ramp)
        # Smooth (ease-in) curve toward the regression target.
        curve = p * p

        n = self._noise(host, elapsed)
        memory = BASELINE_MEMORY_MB + (REGRESSION_MEMORY_TARGET - BASELINE_MEMORY_MB) * curve
        memory_mb = int(round(memory + n * 25))

        # http flips to 500 once the curve clearly breaks (past ~25% of ramp).
        http_health = REGRESSION_HTTP_BAD if p >= 0.25 else BASELINE_HTTP

        eventlog = BASELINE_EVENTLOG + (REGRESSION_EVENTLOG_PEAK - BASELINE_EVENTLOG) * curve
        eventlog_errors = int(round(eventlog))

        return {
            "service_state": BASELINE_SERVICE,
            "http_health": http_health,
            "eventlog_errors": max(0, eventlog_errors),
            "memory_mb": max(0, memory_mb),
        }

    @staticmethod
    def _noise(host: str, elapsed: float) -> float:
        """Small bounded deterministic noise in roughly [-1, 1].

        Deterministic in (host, elapsed-bucket) so repeated reads at the same
        sim-time are stable, keeping the gate boundary crisp and demos
        repeatable.
        """
        # Bucket to 0.5s so tiny clock jitter doesn't produce flicker.
        bucket = math.floor(elapsed * 2.0)
        seed = (hash(host) & 0xFFFF) + bucket * 2654435761
        # Map to a smooth-ish bounded value without importing random.
        val = math.sin(seed * 0.001) * 0.5 + math.sin(seed * 0.013) * 0.5
        return max(-1.0, min(1.0, val))

    # -- baseline / deploy / rollback -------------------------------------

    def capture_baseline(self, host: str) -> dict:
        """Snapshot current metrics; returns the baseline."""
        metrics = self.get_health(host)
        # Return a plain snapshot (without mutating anything).
        return dict(metrics)

    def deploy_version(self, host: str, version: str) -> dict:
        """Deploy ``version`` to ``host`` and start its health trajectory.

        Returns ``{ok, host, version}``. The regression build (``2.1.5``) arms
        a regression trajectory; everything else stays healthy.
        """
        st = self._require_host(host)
        st.version = version
        st.t_deploy = self.now()
        if version == REGRESSION_VERSION:
            st.trajectory = "regression"
            st.armed = True
        else:
            st.trajectory = "healthy"
            st.armed = False
        return {"ok": True, "host": host, "version": version}

    def deploy_version_escape(self, host: str, version: str) -> dict:
        """S3b escape variant: regression only arms on ring1 hosts.

        Canary stays clean; the breach surfaces only after promotion to ring1.
        Used by the s3b transcript-recording path; the default tool surface is
        :meth:`deploy_version`.
        """
        st = self._require_host(host)
        st.version = version
        st.t_deploy = self.now()
        if version == REGRESSION_VERSION and st.ring != "canary":
            st.trajectory = "regression"
            st.armed = True
        else:
            st.trajectory = "healthy"
            st.armed = False
        return {"ok": True, "host": host, "version": version}

    def rollback(self, host: str, to_version: str) -> dict:
        """Roll ``host`` back to ``to_version`` and reset to a healthy curve.

        Returns ``{ok, host, version}``.
        """
        st = self._require_host(host)
        st.version = to_version
        st.t_deploy = self.now()
        st.trajectory = "healthy"
        st.armed = False
        return {"ok": True, "host": host, "version": to_version}

    # -- gate --------------------------------------------------------------

    def evaluate_gate(self, host: str, gate: str) -> dict:
        """Evaluate a gate against the host's current health.

        Delegates to :func:`tools.gates.evaluate` — the identical code path the
        real system uses. Returns the gates result dict augmented with the host
        and gate name for convenience.
        """
        metrics = self.get_health(host)
        result = gates_module.evaluate(gate, metrics)
        return {
            "host": host,
            "gate": gate,
            "result": result["result"],
            "violations": result["violations"],
            "thresholds": result["thresholds"],
            "metrics": {
                "service_state": metrics["service_state"],
                "http_health": metrics["http_health"],
                "eventlog_errors": metrics["eventlog_errors"],
                "memory_mb": metrics["memory_mb"],
            },
        }

    # -- internals ---------------------------------------------------------

    def _require_host(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            raise KeyError(
                f"unknown host: {host!r} (known: {sorted(self._hosts)})"
            )
        return st


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}
