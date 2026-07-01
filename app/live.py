"""Live-run orchestration + ALL §9 guardrails.

A public endpoint backed by a personal OpenAI key is a real cost/abuse surface,
so every guardrail in §9 is enforced here, in one place, by a single in-process
:class:`LiveGate` instance (the module-level singleton ``live_gate``).

Public surface (``app.main`` and this module must agree exactly — see the
contract's "SHARED app/live.py PUBLIC SURFACE")::

    live_gate = LiveGate()                       # module-level singleton

    class LiveRefused(Exception):
        def __init__(self, reason: str, fallback_scenario: str): ...

    class LiveGate:
        def status(self) -> dict                 # {enabled, spend, cap,
                                                  #  remaining_runs_session, busy}
        async def start_run(self, directive: str, session_id: str) -> str
        async def stream(self, run_id: str)       # async generator of §3 events,
                                                  # ends after a 'done' event

``main.py`` manages a ``talos_sid`` cookie and passes it as ``session_id``. On
``LiveRefused`` it returns HTTP 429 with ``{reason, fallback_scenario}``.

The guardrails (§9), all fail-safe (any uncertainty → refuse → fall back to replay):

  1. Global budget kill-switch  — ``spend >= LIVE_BUDGET_CAP`` (USD, default 25) → refuse.
  2. Global rate limit          — at most ``LIVE_RUNS_PER_HOUR`` (default 20) accepted/hour, rolling window.
  3. Per-session limit          — at most ``LIVE_RUNS_PER_SESSION`` (default 3) per ``session_id``.
  4. Input bounds               — reject directive > ``LIVE_MAX_DIRECTIVE_CHARS`` (default 500); strip control chars.
  5. Concurrency guard          — a single in-flight run (asyncio lock/flag); extra requests refused.
  6. Key safety                 — ``OPENAI_API_KEY`` from env only; never logged or returned.
  7. Blast containment          — live agents are wired ONLY to the sim backend (enforced by get_backend).

Spend is estimated from token usage when the run reports it (the reference adapter
stashes ``prompt_tokens`` / ``completion_tokens`` on the Emitter), else a flat
per-run estimate is charged. Spend accumulates *before* the next run is allowed.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from typing import Any, AsyncGenerator, Deque, Dict, Optional

# The agent entrypoint and the backend selector. Imported at module load; both
# live in sibling packages and have no import-time side effects on this module.
from app.agents import run_live
from tools import get_backend

# Sentinel pushed onto a run's queue to signal the stream is finished. The 'done'
# §3 event is delivered first, then this sentinel closes the generator.
_STREAM_END = object()

# Per-1K-token price estimates (USD) for spend accounting. These are intentionally
# conservative for a small model (gpt-4o-mini-class). They only drive the budget
# kill-switch — they are NOT billed to anyone — so erring high is the safe choice.
_PRICE_PER_1K_PROMPT = float(os.environ.get("LIVE_PRICE_PER_1K_PROMPT", "0.0006"))
_PRICE_PER_1K_COMPLETION = float(os.environ.get("LIVE_PRICE_PER_1K_COMPLETION", "0.0024"))
# Flat fallback charge when a run reports no usage (fail-safe: assume it cost something).
_FLAT_RUN_ESTIMATE = float(os.environ.get("LIVE_FLAT_RUN_ESTIMATE", "0.05"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _strip_control_chars(text: str) -> str:
    """Remove ASCII control chars except common whitespace (\\t, \\n, \\r)."""
    return "".join(
        ch for ch in text if ch in ("\t", "\n", "\r") or ord(ch) >= 0x20
    )


class LiveRefused(Exception):
    """Raised by :meth:`LiveGate.start_run` when a guardrail blocks a live run.

    Carries a ``fallback_scenario`` (a replay id like ``"s1"``) so the front-end
    can offer the judge a recorded run instead. ``main.py`` turns this into an
    HTTP 429 with ``{reason, fallback_scenario}``.
    """

    def __init__(self, reason: str, fallback_scenario: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fallback_scenario = fallback_scenario


class _Run:
    """Internal state for a single live run."""

    __slots__ = ("run_id", "session_id", "queue", "task", "done")

    def __init__(self, run_id: str, session_id: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: Optional[asyncio.Task] = None
        self.done = False


class LiveGate:
    """In-process gate enforcing every §9 guardrail around live agent runs.

    A single instance (``live_gate``) is shared across the process. All mutating
    operations are guarded by an asyncio lock so concurrent requests cannot race
    the budget / rate / concurrency counters.
    """

    def __init__(self) -> None:
        # Config (read once at construction; env is process-stable in the container).
        self.cap: float = _env_float("LIVE_BUDGET_CAP", 25.0)
        self.runs_per_hour: int = _env_int("LIVE_RUNS_PER_HOUR", 20)
        self.runs_per_session: int = _env_int("LIVE_RUNS_PER_SESSION", 3)
        self.max_directive_chars: int = _env_int("LIVE_MAX_DIRECTIVE_CHARS", 500)
        self.backend_name: str = os.environ.get("SANDBOX_BACKEND", "sim")

        # Mutable accounting state.
        self.spend: float = 0.0
        self._session_counts: Dict[str, int] = {}
        self._accept_times: Deque[float] = deque()  # epoch seconds of accepted runs
        self._busy: bool = False
        self._runs: Dict[str, _Run] = {}
        self._lock = asyncio.Lock()

        # The replay shown when we refuse. s1 is the safe, always-green happy path.
        self._fallback_scenario = "s1"

    # ------------------------------------------------------------------ #
    # Enablement / key safety (§9.6).
    # ------------------------------------------------------------------ #
    def _key_present(self) -> bool:
        """True iff an OpenAI key is set in the environment. Never returns the key."""
        return bool(os.environ.get("OPENAI_API_KEY"))

    @property
    def enabled(self) -> bool:
        """Live mode is enabled only when LIVE_MODE_ENABLED=true AND a key is present."""
        return _env_bool("LIVE_MODE_ENABLED", False) and self._key_present()

    # ------------------------------------------------------------------ #
    # Status (§ shared surface).
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        """Report live-mode status. Safe to call any time; never touches the key value.

        ``remaining_runs_session`` is the global per-session ceiling here (the
        gate cannot know the caller's session_id at status time — main.py reads
        the cookie). It is the configured max; per-session decrement happens at
        ``start_run``. Returned shape matches the contract exactly.
        """
        return {
            "enabled": self.enabled,
            "spend": round(self.spend, 4),
            "cap": self.cap,
            "remaining_runs_session": self.runs_per_session,
            "busy": self._busy,
        }

    def remaining_runs_for_session(self, session_id: str) -> int:
        """Per-session remaining runs for a specific session id (used by main.py if desired)."""
        used = self._session_counts.get(session_id, 0)
        return max(0, self.runs_per_session - used)

    # ------------------------------------------------------------------ #
    # Rate-limit bookkeeping (§9.2).
    # ------------------------------------------------------------------ #
    def _prune_rate_window(self, now: float) -> None:
        """Drop accepted-run timestamps older than one hour from the rolling window."""
        cutoff = now - 3600.0
        while self._accept_times and self._accept_times[0] < cutoff:
            self._accept_times.popleft()

    # ------------------------------------------------------------------ #
    # start_run (§ shared surface) — runs ALL guardrails, then launches the task.
    # ------------------------------------------------------------------ #
    async def start_run(self, directive: str, session_id: str) -> str:
        """Validate every §9 guardrail and, on success, launch the live run.

        Returns the ``run_id`` (also the key for :meth:`stream`). Raises
        :class:`LiveRefused` on any guardrail block — fail-safe, so any unexpected
        condition refuses rather than spends.
        """
        async with self._lock:
            # --- §9.6 / enablement: key + master switch -------------------- #
            if not self.enabled:
                raise LiveRefused(
                    "Live mode is disabled (no OpenAI key or LIVE_MODE_ENABLED is off).",
                    self._fallback_scenario,
                )

            # --- §9.4 input bounds ---------------------------------------- #
            if directive is None:
                raise LiveRefused("Empty directive.", self._fallback_scenario)
            cleaned = _strip_control_chars(str(directive)).strip()
            if not cleaned:
                raise LiveRefused("Empty directive.", self._fallback_scenario)
            if len(cleaned) > self.max_directive_chars:
                raise LiveRefused(
                    f"Directive too long (>{self.max_directive_chars} chars).",
                    self._fallback_scenario,
                )

            # --- §9.1 global budget kill-switch --------------------------- #
            if self.spend >= self.cap:
                raise LiveRefused(
                    f"Live budget cap reached (${self.spend:.2f} of ${self.cap:.2f}). "
                    "Falling back to a recorded run.",
                    self._fallback_scenario,
                )

            # --- §9.5 concurrency guard ----------------------------------- #
            if self._busy:
                raise LiveRefused(
                    "A live run is already in progress. Try a recorded scenario, "
                    "or retry in a moment.",
                    self._fallback_scenario,
                )

            # --- §9.2 global rate limit (rolling hour) -------------------- #
            now = time.time()
            self._prune_rate_window(now)
            if len(self._accept_times) >= self.runs_per_hour:
                raise LiveRefused(
                    f"Global live rate limit reached ({self.runs_per_hour}/hour). "
                    "Falling back to a recorded run.",
                    self._fallback_scenario,
                )

            # --- §9.3 per-session limit ----------------------------------- #
            sid = session_id or "anon"
            used = self._session_counts.get(sid, 0)
            if used >= self.runs_per_session:
                raise LiveRefused(
                    f"Per-session live limit reached ({self.runs_per_session} runs). "
                    "Falling back to a recorded run.",
                    self._fallback_scenario,
                )

            # All guardrails passed — reserve the slot atomically.
            self._busy = True
            self._accept_times.append(now)
            self._session_counts[sid] = used + 1

            run_id = uuid.uuid4().hex
            run = _Run(run_id, sid)
            self._runs[run_id] = run

        # Launch outside the lock so the agent loop doesn't hold it.
        run.task = asyncio.create_task(self._drive_run(run, cleaned))
        return run_id

    # ------------------------------------------------------------------ #
    # The background agent loop: feeds the run's queue, then accounts spend.
    # ------------------------------------------------------------------ #
    async def _drive_run(self, run: _Run, directive: str) -> None:
        """Run the agents, pushing each §3 event onto the run's queue.

        On completion (or error), estimates spend, releases the concurrency slot,
        and pushes the stream-end sentinel. Always emits a terminal 'done' if the
        agents did not (so the front-end never hangs).
        """
        saw_done = False

        # Usage sink bridged across the run_live boundary: the agents' Emitter
        # mirrors token totals onto ``emit.usage`` (see Emitter.report_usage), so
        # we can read them back here for spend accounting after the run.
        usage: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "model": None}

        async def emit(event: Dict[str, Any]) -> None:
            nonlocal saw_done
            if event.get("type") == "done":
                saw_done = True
            await run.queue.put(event)

        # Attach the sink as an attribute so Emitter.report_usage can find it.
        emit.usage = usage  # type: ignore[attr-defined]

        try:
            # §9.7 blast containment: agents are wired ONLY to the sim backend.
            backend = get_backend(self.backend_name)
            # run_live(directive, backend, emit): the public agent entrypoint. It
            # wraps `emit` in its own Emitter and the reference adapter reports
            # token usage through it onto `emit.usage`, which we read below.
            await run_live(directive, backend, emit)
        except Exception as exc:  # never let the task die silently
            try:
                await run.queue.put(
                    {
                        "seq": 9_999,
                        "t": 0.0,
                        "type": "narration",
                        "actor": "system",
                        "title": "Live run error",
                        "body": f"The live run failed: {type(exc).__name__}.",
                        "data": {"text": f"{type(exc).__name__}"},
                    }
                )
            except Exception:
                pass
        finally:
            # Guarantee a terminal 'done' so stream() always ends cleanly.
            if not saw_done:
                try:
                    await run.queue.put(
                        {
                            "seq": 10_000,
                            "t": 0.0,
                            "type": "done",
                            "actor": "system",
                            "title": "Live run ended",
                            "body": "Run ended.",
                            "data": {"outcome": "ended", "summary": "Live run ended."},
                        }
                    )
                except Exception:
                    pass

            # --- Spend accounting (§9 spend, fail-safe) ------------------- #
            self._account_spend(usage)

            # Release concurrency + mark the run done, then close the stream.
            async with self._lock:
                self._busy = False
                run.done = True
            await run.queue.put(_STREAM_END)

    def _account_spend(self, usage: Dict[str, Any]) -> None:
        """Add this run's estimated USD cost to cumulative spend.

        Prefers token usage reported by the reference adapter (mirrored into the
        ``usage`` dict via ``Emitter.report_usage``); else charges the flat
        per-run estimate. Always charges *something* (fail-safe: a run that
        reports nothing is assumed to have cost the flat estimate).
        """
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        if prompt_tokens or completion_tokens:
            cost = (
                prompt_tokens / 1000.0 * _PRICE_PER_1K_PROMPT
                + completion_tokens / 1000.0 * _PRICE_PER_1K_COMPLETION
            )
            # Never charge less than a tiny floor for a run that did call the API.
            cost = max(cost, 0.001)
        else:
            cost = _FLAT_RUN_ESTIMATE
        self.spend += cost

    # ------------------------------------------------------------------ #
    # stream (§ shared surface): yield events until 'done', then stop.
    # ------------------------------------------------------------------ #
    async def stream(self, run_id: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Async generator yielding §3 event dicts for ``run_id``.

        Yields each event the agent loop produced, terminating after the ``done``
        event (the internal sentinel that follows it closes the generator). If
        ``run_id`` is unknown, yields a single error 'done' so the SSE endpoint
        closes cleanly rather than hanging.
        """
        run = self._runs.get(run_id)
        if run is None:
            yield {
                "seq": 0,
                "t": 0.0,
                "type": "done",
                "actor": "system",
                "title": "Unknown run",
                "body": "No such live run.",
                "data": {"outcome": "error", "summary": "unknown run_id"},
            }
            return

        try:
            while True:
                item = await run.queue.get()
                if item is _STREAM_END:
                    break
                yield item
                if isinstance(item, dict) and item.get("type") == "done":
                    # Drain the sentinel that follows 'done' so the run can be GC'd,
                    # then stop. (The sentinel is the very next item.)
                    try:
                        tail = await asyncio.wait_for(run.queue.get(), timeout=2.0)
                        if tail is not _STREAM_END:
                            # Unexpected extra event after done; ignore it.
                            pass
                    except asyncio.TimeoutError:
                        pass
                    break
        finally:
            # Best-effort cleanup so completed runs don't accumulate.
            if run.done:
                self._runs.pop(run_id, None)


# Module-level singleton — imported as ``from app.live import live_gate``.
live_gate = LiveGate()


__all__ = ["live_gate", "LiveGate", "LiveRefused"]
