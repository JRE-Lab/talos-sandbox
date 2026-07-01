"""Real WinRM fleet backend — placeholder (CONTRACT §7).

The real WinRM transport lives in the TALOS core repo; the sandbox never talks
to real hosts. This class exists so the fleet-backend swap point is visible and
documented in the sandbox: ``get_backend("winrm")`` resolves here, and every
tool method raises ``NotImplementedError`` to make the boundary explicit.

To run the agents against real Windows VMs, deploy the core build (which ships
the real ``WinRMFleet``) — not this sandbox.
"""

from __future__ import annotations

from typing import Any

_MESSAGE = (
    "The real WinRM transport lives in the TALOS core repo; "
    "the sandbox never talks to real hosts."
)


class WinRMFleet:
    """Placeholder real-fleet backend. Every method raises NotImplementedError.

    Mirrors the §7 ``FleetBackend`` tool surface so the swap point is type-shaped
    identically to :class:`tools.sim_backend.SimFleet`, but performs no I/O.
    """

    def __init__(self) -> None:
        raise NotImplementedError(_MESSAGE)

    def get_fleet_topology(self) -> list[dict]:
        raise NotImplementedError(_MESSAGE)

    def get_health(self, host: str) -> dict:
        raise NotImplementedError(_MESSAGE)

    def capture_baseline(self, host: str) -> dict:
        raise NotImplementedError(_MESSAGE)

    def deploy_version(self, host: str, version: str) -> dict:
        raise NotImplementedError(_MESSAGE)

    def rollback(self, host: str, to_version: str) -> dict:
        raise NotImplementedError(_MESSAGE)

    def evaluate_gate(self, host: str, gate: str) -> dict:
        raise NotImplementedError(_MESSAGE)
