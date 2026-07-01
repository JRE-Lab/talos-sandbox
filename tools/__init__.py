"""Fleet-backend swap point (CONTRACT §7).

The agent layer acts only through a typed, allowlisted tool surface
(``get_fleet_topology``, ``get_health``, ``capture_baseline``,
``deploy_version``, ``rollback``, ``evaluate_gate``). That surface is the seam
between the agents and the fleet. The sandbox implements it twice:

  * ``sim``   -> :class:`tools.sim_backend.SimFleet` (in-memory, sandbox).
  * ``winrm`` -> :class:`tools.winrm_backend.WinRMFleet` (placeholder here;
    the real transport lives in the TALOS core repo — the sandbox never talks
    to real hosts).

``get_backend(name)`` selects the backend by name, defaulting to the value of
env ``SANDBOX_BACKEND`` (default ``"sim"``).
"""

from __future__ import annotations

import os
from typing import Any, Optional


def get_backend(name: Optional[str] = None) -> Any:
    """Return a fleet backend instance.

    Args:
        name: backend selector. If ``None``, read env ``SANDBOX_BACKEND``
            (default ``"sim"``).

    Returns:
        A backend object providing the §7 tool surface.

    Raises:
        ValueError: if ``name`` is not a known backend.
    """
    if name is None:
        name = os.environ.get("SANDBOX_BACKEND", "sim")
    key = (name or "sim").strip().lower()

    if key == "sim":
        from tools.sim_backend import SimFleet

        return SimFleet()
    if key == "winrm":
        from tools.winrm_backend import WinRMFleet

        return WinRMFleet()

    raise ValueError(
        f"unknown SANDBOX_BACKEND {name!r} (known: 'sim', 'winrm')"
    )


__all__ = ["get_backend"]
