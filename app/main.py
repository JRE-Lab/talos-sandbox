"""FastAPI application — routes, static mount, SSE, scenarios, audit, live gate.

Implements EXACTLY the CONTRACT §5 HTTP surface. The replay path has zero runtime
dependencies (no model, no network); the live path is wired to ``app.live`` and is
imported **lazily and defensively** so the app still boots for replay-only use even
when the OpenAI deps or key are absent.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles

from app import replay

# sse-starlette is a hard dependency (requirements.txt). Import at module load.
from sse_starlette.sse import EventSourceResponse

# Load a local .env when running outside Docker (e.g. `uvicorn app.main:app`).
# In Docker, Compose's env_file already populates the environment; load_dotenv
# does not override existing vars, so this is a no-op there. Best-effort: if
# python-dotenv is absent the app still runs on process env alone.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = REPO_ROOT / "static"

SESSION_COOKIE: str = "talos_sid"
SSE_HEARTBEAT_S: int = 15  # CONTRACT §5: heartbeat comment every 15s.

VIDEO_URL_PLACEHOLDER: str = "REPLACE_WITH_VIDEO_URL"

app = FastAPI(title="TALOS Sandbox", version="1.0.0")


# ---------------------------------------------------------------------------
# Live gate — imported lazily/defensively so replay-only boots without OpenAI.
# ---------------------------------------------------------------------------


def _get_live() -> Any:
    """Return the ``app.live`` module, or ``None`` if it cannot be imported.

    Live mode is optional. If ``app.live`` (or its OpenAI deps) is unavailable,
    the whole live surface degrades to "disabled" rather than crashing the app.
    """
    try:
        from app import live as live_module  # type: ignore

        return live_module
    except Exception:
        return None


def _live_gate() -> Any:
    """Return the shared ``live_gate`` singleton, or ``None`` if live is absent."""
    live_module = _get_live()
    if live_module is None:
        return None
    return getattr(live_module, "live_gate", None)


def _live_refused_exc() -> Optional[type]:
    """Return the ``LiveRefused`` exception type, or ``None`` if live is absent."""
    live_module = _get_live()
    if live_module is None:
        return None
    return getattr(live_module, "LiveRefused", None)


# ---------------------------------------------------------------------------
# Session cookie management
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, sid: str) -> None:
    """Attach the ``talos_sid`` session cookie to ``response``."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sid,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24,  # 1 day
    )


def _read_or_mint_sid(request: Request) -> tuple[str, bool]:
    """Return ``(sid, is_new)`` — the existing cookie value or a freshly minted one."""
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        return sid, False
    return uuid.uuid4().hex, True


def _ensure_session(request: Request, response: Response) -> str:
    """Read the ``talos_sid`` cookie or mint a new one (set on the response)."""
    sid, is_new = _read_or_mint_sid(request)
    if is_new:
        _set_session_cookie(response, sid)
    return sid


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    """Container health check (§5)."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Scenarios (§5)
# ---------------------------------------------------------------------------


@app.get("/api/scenarios")
async def get_scenarios() -> Any:
    """List scenario summaries: ``[{id, name, summary, moment}]`` (§5)."""
    return replay.list_scenarios()


@app.get("/api/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str) -> Any:
    """Return the full transcript JSON for a scenario, or 404 (§5)."""
    transcript = replay.get_scenario(scenario_id)
    if transcript is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown scenario '{scenario_id}'"},
        )
    return transcript


# ---------------------------------------------------------------------------
# Audit (§5 / §6)
# ---------------------------------------------------------------------------


@app.get("/api/audit/{scenario_id}")
async def get_audit(scenario_id: str) -> Any:
    """Return the server-computed audit hash chain for a scenario (§5)."""
    records = replay.get_audit_records(scenario_id)
    if records is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown scenario '{scenario_id}'"},
        )
    return {"records": records}


@app.get("/api/audit/{scenario_id}/verify")
async def verify_audit(
    scenario_id: str,
    tamper: Optional[int] = Query(default=None),
) -> Any:
    """Recompute the chain and report integrity; optional ``?tamper=K`` (§5/§6)."""
    transcript = replay.get_scenario(scenario_id)
    if transcript is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown scenario '{scenario_id}'"},
        )
    records = transcript.get("audit_records", []) or []
    return replay.verify_chain(records, tamper=tamper)


# ---------------------------------------------------------------------------
# Live mode (§5 / §9) — wired to app.live.live_gate
# ---------------------------------------------------------------------------


@app.get("/api/live/status")
async def live_status(request: Request, response: Response) -> Any:
    """Report live-mode availability + budget/usage (§5).

    ``remaining_runs_session`` reflects THIS caller's actual remaining runs (read
    from the ``talos_sid`` cookie), not the configured ceiling — the displayed
    count must not overstate availability in a tool whose whole pitch is honesty.
    When ``app.live`` is unavailable, report a safe disabled status so the UI
    hides the live box rather than erroring.
    """
    disabled = {
        "enabled": False,
        "spend": 0.0,
        "cap": 0.0,
        "remaining_runs_session": 0,
        "busy": False,
    }
    gate = _live_gate()
    if gate is None:
        return disabled
    try:
        status = dict(gate.status())
        # Override the per-session count with this caller's real remaining runs.
        sid = _ensure_session(request, response)
        remaining = getattr(gate, "remaining_runs_for_session", None)
        if callable(remaining):
            status["remaining_runs_session"] = remaining(sid)
        return status
    except Exception:
        return disabled


@app.post("/api/live/run")
async def live_run(request: Request) -> Any:
    """Start a guardrailed live run (§5/§9).

    On accept → ``{"run_id": str}``. On any guardrail refusal (``LiveRefused``) →
    HTTP 429 ``{"reason", "fallback_scenario"}``. The ``talos_sid`` cookie is the
    session id passed to the gate.
    """
    sid, is_new = _read_or_mint_sid(request)

    def _finish(resp: Response) -> Response:
        # Persist the session cookie on whichever response we return.
        if is_new:
            _set_session_cookie(resp, sid)
        return resp

    gate = _live_gate()
    refused = _live_refused_exc()
    if gate is None:
        # Live mode is not available at all → behave like a refusal with a fallback.
        return _finish(
            JSONResponse(
                status_code=429,
                content={
                    "reason": "Live mode is unavailable.",
                    "fallback_scenario": "s1",
                },
            )
        )

    try:
        body = await request.json()
    except Exception:
        body = {}
    directive = ""
    if isinstance(body, dict):
        directive = body.get("directive", "") or ""
    if not isinstance(directive, str):
        directive = str(directive)

    try:
        run_id = await gate.start_run(directive, sid)
    except Exception as exc:  # includes LiveRefused
        if refused is not None and isinstance(exc, refused):
            return _finish(
                JSONResponse(
                    status_code=429,
                    content={
                        "reason": getattr(exc, "reason", "Live run refused."),
                        "fallback_scenario": getattr(exc, "fallback_scenario", "s1"),
                    },
                )
            )
        # Unexpected failure — fail safe to a replay fallback, never 500 the demo.
        return _finish(
            JSONResponse(
                status_code=429,
                content={
                    "reason": "Live run could not start.",
                    "fallback_scenario": "s1",
                },
            )
        )

    return _finish(JSONResponse(content={"run_id": run_id}))


@app.get("/api/live/{run_id}/stream")
async def live_stream(run_id: str) -> Any:
    """SSE stream of §3 events for a live run; ends after a ``done`` event (§5)."""
    gate = _live_gate()
    if gate is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "live mode unavailable"},
        )

    async def event_generator():
        import json as _json

        try:
            async for event in gate.stream(run_id):
                yield {"event": "message", "data": _json.dumps(event)}
        except Exception as exc:
            # Never tear the connection without a terminal frame the UI can read.
            err = {
                "seq": -1,
                "t": 0.0,
                "type": "narration",
                "actor": "system",
                "title": "Live stream error",
                "body": f"The live stream ended unexpectedly: {exc}",
                "data": {"text": f"Live stream error: {exc}"},
            }
            yield {"event": "message", "data": _json.dumps(err)}
            done = {
                "seq": -1,
                "t": 0.0,
                "type": "done",
                "actor": "system",
                "title": "Run ended",
                "body": "The live run ended.",
                "data": {"outcome": "error", "summary": "Live stream error."},
            }
            yield {"event": "message", "data": _json.dumps(done)}

    return EventSourceResponse(event_generator(), ping=SSE_HEARTBEAT_S)


# ---------------------------------------------------------------------------
# Static front-end (§5 / §10)
# ---------------------------------------------------------------------------


def _inject_video_url(html: str) -> str:
    """Replace the front-end's VIDEO_URL placeholder with the env value, if set.

    The contract allows either env injection here OR the front-end falling back
    to its own placeholder. We inject when ``VIDEO_URL`` is set to a real value;
    otherwise we leave the page untouched so the front-end shows its placeholder.
    """
    video_url = os.environ.get("VIDEO_URL", "").strip()
    if not video_url or video_url == VIDEO_URL_PLACEHOLDER:
        return html
    return html.replace(VIDEO_URL_PLACEHOLDER, video_url)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    """Serve the dashboard, injecting ``VIDEO_URL`` when available (§10)."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.is_file():
        return PlainTextResponse(
            "TALOS Sandbox: static/index.html not found.", status_code=500
        )
    html = index_path.read_text(encoding="utf-8")
    html = _inject_video_url(html)

    response = HTMLResponse(content=html)
    # Ensure the visitor has a session cookie as soon as they load the page.
    _ensure_session(request, response)
    return response


# Serve /style.css and /app.js so the dashboard resolves its assets (§5). These
# handlers guard at request time, so they register even if agent D's files arrive
# later; index.html is served by the handler above (which injects VIDEO_URL).
@app.get("/style.css")
async def style_css() -> Any:
    path = STATIC_DIR / "style.css"
    if not path.is_file():
        return PlainTextResponse("/* not found */", status_code=404)
    return FileResponse(path, media_type="text/css")


@app.get("/app.js")
async def app_js() -> Any:
    path = STATIC_DIR / "app.js"
    if not path.is_file():
        return PlainTextResponse("// not found", status_code=404)
    return FileResponse(path, media_type="application/javascript")


# Also expose the whole static dir under /static for any extra assets (icons,
# fonts, etc.). Mounting requires the directory to exist at import time.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
