"""Reference OpenAI agent adapter — THE SWAP POINT.

================================================================================
  THIS IS A *REFERENCE* IMPLEMENTATION, NOT THE REAL TALOS AGENTS.
================================================================================

The real TALOS multi-agent rollout system (Planner / Critic / Monitor / Executor)
lives in the TALOS core repository. This sandbox file exists only to prove the
seam: it drives the SAME ``run_live(directive, backend, emit)`` interface that
``app.agents.__init__`` exposes, using the OpenAI Python SDK (``openai>=1.40``)
with function / tool calling bound to the §7 tool surface.

    >>> TODO (swap point): replace the body of ``run_live_openai`` with a thin
    >>> shim that imports and invokes the real TALOS agent graph. The real agents
    >>> already speak the same typed, allowlisted tool interface
    >>> (get_fleet_topology, get_health, capture_baseline, deploy_version,
    >>>  rollback, evaluate_gate) and already emit structured reasoning steps —
    >>> map those onto the §3 event envelope via ``emit`` and delete the
    >>> hand-rolled prompt loop below. No other sandbox file changes.

Design constraints honored here:
  * Model id comes from ``OPENAI_MODEL`` (default ``gpt-4o-mini``).
  * The agents are wired ONLY to the sim backend (passed in), never to anything
    real — blast containment (§9.7) is enforced by the backend, not by this file.
  * On ANY OpenAI API error the adapter degrades gracefully: it emits a
    ``narration`` error event then a ``done`` event so the front-end never hangs.
  * The OpenAI key is read by the SDK from the environment only; this file never
    logs it, never echoes it, never returns it.

The adapter uses the model for the *reasoning* events (directive framing, plan,
critique, verdict) and lets the model call the real backend tools for the
*action* events (deploy, gate eval, health). For each tool the model calls, the
adapter also emits the matching §3 event so the UI renders a live run identically
to a replay.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# The Emitter type is defined in the package __init__; import lazily inside
# functions to avoid a circular import at module load. We only need the duck
# type here (an object with an async ``send(...)`` method).


# --------------------------------------------------------------------------- #
# Availability check (used by run_live to decide whether to delegate here).
# --------------------------------------------------------------------------- #
def is_available() -> bool:
    """True iff the OpenAI SDK is importable AND an API key is present in env.

    Live mode's master switch (``LIVE_MODE_ENABLED``) is enforced by
    ``app.live.LiveGate`` upstream; this only checks that *this adapter* could
    actually make a call. ``LiveGate`` will not invoke ``run_live`` at all when
    live mode is disabled, so a missing key here simply means the deterministic
    fallback is used (e.g. during ``record_replays.py``).
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# OpenAI tool schema bound to the §7 FleetBackend surface.
# --------------------------------------------------------------------------- #
def _tool_schema() -> List[Dict[str, Any]]:
    """The allowlisted tool surface, as OpenAI function-calling definitions.

    These are the ONLY actions the live agents can take. The names and arg shapes
    mirror §7 exactly so the model's tool calls map 1:1 onto backend methods.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "get_fleet_topology",
                "description": "List all hosts with ring, ha_pair, version, and health.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_health",
                "description": "Get current health metrics for one host.",
                "parameters": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "capture_baseline",
                "description": "Snapshot a host's current metrics as its baseline.",
                "parameters": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                    "required": ["host"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "deploy_version",
                "description": "Deploy a version to a host; starts a health trajectory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "version": {"type": "string"},
                    },
                    "required": ["host", "version"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rollback",
                "description": "Roll a host back to a known-good version; resets to healthy.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "to_version": {"type": "string"},
                    },
                    "required": ["host", "to_version"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "evaluate_gate",
                "description": "Evaluate a named gate (canary_gate|ring_gate) against a host's metrics.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "gate": {"type": "string"},
                    },
                    "required": ["host", "gate"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "emit_reasoning",
                "description": (
                    "Record one structured reasoning step for the operator feed. Use this "
                    "to publish the plan, a critique, a verdict, a revision, a breach, or a "
                    "narration as you reason. This is how the human sees your decisions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "plan",
                                "critique",
                                "verdict",
                                "revision",
                                "breach",
                                "narration",
                            ],
                        },
                        "actor": {
                            "type": "string",
                            "enum": ["planner", "critic", "monitor", "executor", "system"],
                        },
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "data": {
                            "type": "object",
                            "description": "Type-specific payload matching the §3 envelope.",
                        },
                    },
                    "required": ["type", "actor", "title", "body"],
                },
            },
        },
    ]


_SYSTEM_PROMPT = """You are the TALOS rollout agent team operating a small fleet of Windows hosts.
The fleet: a canary host (TALOS-CANARY) and a production ring (ring1) containing an HA pair
(TALOS-R1A and TALOS-R1B). Baseline version is 2.1.3.

You act as four cooperating roles in sequence:
- PLANNER: author a ring rollout plan (canary first, then ring one host at a time, with soak gates).
- CRITIC: review the plan for blast radius. REJECT any plan that deploys both HA-pair members
  (TALOS-R1A AND TALOS-R1B) at the same time, or that skips the canary. Force a safer revision.
- EXECUTOR: deploy versions and roll back when a gate fails.
- MONITOR: capture baselines, evaluate gates, and watch health during soak.

Rules:
- ALWAYS publish your reasoning with emit_reasoning before acting: emit a `plan`, then a
  `critique`, then a `verdict`. If you reject, emit a `revision` then a new `verdict`.
- NEVER deploy TALOS-R1A and TALOS-R1B simultaneously. Canary always goes first.
- Use evaluate_gate after a soak. If a gate fails, emit a `breach`, then rollback the host,
  then verify health recovered.
- You only have the six fleet tools plus emit_reasoning. You cannot touch anything outside them.
- Keep it tight: this is a live demo. A handful of steps, not dozens.

When the rollout is complete (fleet healthy or regression caught and rolled back), stop calling
tools and reply with a one-sentence final summary.
"""


# --------------------------------------------------------------------------- #
# Main entrypoint delegated to from app.agents.run_live.
# --------------------------------------------------------------------------- #
async def run_live_openai(directive: str, backend: Any, em: Any) -> None:
    """Run the reference OpenAI agent loop, emitting §3 events via ``em.send``.

    ``em`` is an :class:`app.agents.Emitter`. Parameters match the swap-point
    interface so the real TALOS agents can drop in behind the same signature.

    Returns ``None``. On any OpenAI error, emits a ``narration`` error event and a
    ``done`` event, then returns (never raises to the caller).
    """
    from . import Emitter, classify_urgency  # local import: avoid circular load

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    urgency = classify_urgency(directive)

    # The directive and topology events are deterministic framing — emit them up
    # front so the tiles initialize even before the model responds.
    await em.send(
        "directive",
        "system",
        "Directive received (live)",
        directive.strip(),
        {"text": directive.strip(), "urgency": urgency},
    )
    topology = _call(backend.get_fleet_topology)
    await em.send(
        "topology",
        "system",
        "Fleet topology",
        f"{len(topology)} hosts: canary + ring1 HA pair.",
        {"hosts": topology},
        dt=0.5,
    )

    try:
        from openai import AsyncOpenAI
    except Exception as exc:  # SDK missing despite is_available — be safe.
        await _fail(em, f"OpenAI SDK unavailable: {type(exc).__name__}")
        return

    # The SDK reads OPENAI_API_KEY from the environment. We never pass it through
    # code or log it.
    client = AsyncOpenAI()

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Directive (urgency={urgency}): {directive.strip()}\n\n"
                f"Current topology: {json.dumps(topology)}\n"
                "Plan and execute the rollout now. Publish your reasoning as you go."
            ),
        },
    ]
    tools = _tool_schema()

    total_prompt_tokens = 0
    total_completion_tokens = 0
    max_turns = 16  # hard cap so a runaway model can't burn budget or time

    try:
        for _turn in range(max_turns):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0

            choice = response.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None)

            # Append the assistant turn (with any tool calls) to the transcript.
            messages.append(_assistant_message_dict(msg))

            if not tool_calls:
                # Model is done — emit its closing narration + done.
                final_text = (msg.content or "Rollout complete.").strip()
                await em.send(
                    "narration",
                    "system",
                    "Agent summary",
                    final_text,
                    {"text": final_text},
                    dt=0.3,
                )
                break

            # Execute each tool call against the backend, emit matching §3 events,
            # and feed the tool result back to the model.
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = await _dispatch_tool(name, args, backend, em)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": json.dumps(result, default=str),
                    }
                )
        else:
            # Loop exhausted without a natural stop.
            await em.send(
                "narration",
                "system",
                "Run truncated",
                "Reached the live turn limit; ending the run.",
                {"text": "live turn limit reached"},
                dt=0.2,
            )
    except Exception as exc:
        # ANY API/runtime error → graceful degradation.
        await _fail(em, f"OpenAI call failed: {type(exc).__name__}: {exc}")
        return

    # Report token usage so the LiveGate can estimate spend. report_usage mirrors
    # the totals onto the emit callable's ``usage`` dict (if present), which is how
    # app.live reads them back across the run_live boundary. Absence → flat estimate.
    try:
        em.report_usage(total_prompt_tokens, total_completion_tokens, model)
    except Exception:
        pass

    await em.send(
        "done",
        "system",
        "Live run complete",
        "The real model finished reasoning and acting against the simulated fleet.",
        {
            "outcome": "live_complete",
            "summary": "Live agent run complete (real model, simulated fleet).",
        },
        dt=0.4,
    )


# --------------------------------------------------------------------------- #
# Tool dispatch: map model tool calls onto §7 backend methods + §3 events.
# --------------------------------------------------------------------------- #
async def _dispatch_tool(
    name: str, args: Dict[str, Any], backend: Any, em: Any
) -> Dict[str, Any]:
    """Run one model tool call and emit the corresponding §3 event(s)."""
    if name == "emit_reasoning":
        rtype = args.get("type", "narration")
        actor = args.get("actor", "system")
        title = args.get("title", "")
        body = args.get("body", "")
        data = args.get("data") or {}
        # Normalise a couple of fields the model often omits, so the renderer is happy.
        if rtype == "narration" and "text" not in data:
            data = {"text": body, **data}
        await em.send(rtype, actor, title, body, data, dt=0.3)
        return {"ok": True}

    if name == "get_fleet_topology":
        topo = _call(backend.get_fleet_topology)
        return {"hosts": topo}

    if name == "get_health":
        host = args.get("host", "")
        metrics = _call(backend.get_health, host)
        await em.send(
            "health",
            "fleet",
            f"Health: {host}",
            f"{host}: mem {metrics.get('memory_mb')}MB, http {metrics.get('http_health')}, "
            f"errors {metrics.get('eventlog_errors')}.",
            {
                "host": host,
                "service_state": metrics.get("service_state", "Running"),
                "http_health": metrics.get("http_health", 200),
                "eventlog_errors": metrics.get("eventlog_errors", 0),
                "memory_mb": metrics.get("memory_mb", 800),
                "health": metrics.get("health", "green"),
            },
            dt=0.3,
        )
        return metrics

    if name == "capture_baseline":
        host = args.get("host", "")
        return _call(backend.capture_baseline, host)

    if name == "deploy_version":
        host = args.get("host", "")
        version = args.get("version", "")
        result = _call(backend.deploy_version, host, version)
        from_version = _current_version(backend, host) or "2.1.3"
        await em.send(
            "deploy",
            "executor",
            f"Deploying {host}",
            f"{host} → {version}",
            {"host": host, "from_version": from_version, "to_version": version},
            dt=0.5,
        )
        return result

    if name == "rollback":
        host = args.get("host", "")
        to_version = args.get("to_version", "")
        result = _call(backend.rollback, host, to_version)
        await em.send(
            "rollback",
            "executor",
            f"Rolling back {host}",
            f"Reverting {host} to {to_version}.",
            {
                "scope": "host",
                "hosts": [host],
                "to_version": to_version,
                "reason": "gate failure / regression",
            },
            dt=0.5,
        )
        return result

    if name == "evaluate_gate":
        host = args.get("host", "")
        gate = args.get("gate", "")
        result = _call(backend.evaluate_gate, host, gate)
        metrics = _call(backend.get_health, host)
        decision = result.get("result", "pass")
        await em.send(
            "gate_eval",
            "monitor",
            f"Gate {gate}: {decision.upper()} ({host})",
            f"{host} {gate} {decision}.",
            {
                "host": host,
                "gate": gate,
                "result": decision,
                "metrics": {
                    "service_state": metrics.get("service_state"),
                    "http_health": metrics.get("http_health"),
                    "eventlog_errors": metrics.get("eventlog_errors"),
                    "memory_mb": metrics.get("memory_mb"),
                },
                "violations": result.get("violations", []),
            },
            dt=0.4,
        )
        return result

    # Unknown tool name: return an error payload (model may recover).
    return {"error": f"unknown tool: {name}"}


# --------------------------------------------------------------------------- #
# Small utilities.
# --------------------------------------------------------------------------- #
def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a (sync) backend method. The sim backend is synchronous by contract."""
    return fn(*args, **kwargs)


def _current_version(backend: Any, host: str) -> Optional[str]:
    """Best-effort lookup of a host's current version from topology, for deploy events."""
    try:
        for row in backend.get_fleet_topology():
            if row.get("host") == host:
                return row.get("version")
    except Exception:
        return None
    return None


def _assistant_message_dict(msg: Any) -> Dict[str, Any]:
    """Serialize an OpenAI assistant message (with tool calls) for the next turn."""
    out: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ]
    return out


async def _fail(em: Any, reason: str) -> None:
    """Emit a graceful-degradation narration + done pair (never raises)."""
    await em.send(
        "narration",
        "system",
        "Live mode error",
        f"The live agent could not complete: {reason}. "
        "Falling back — try a recorded scenario for a guaranteed run.",
        {"text": reason},
        dt=0.2,
    )
    await em.send(
        "done",
        "system",
        "Live run ended (error)",
        "Live run ended early due to an upstream error.",
        {"outcome": "error", "summary": reason},
        dt=0.2,
    )


__all__ = ["is_available", "run_live_openai"]
