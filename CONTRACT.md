# TALOS Sandbox — BUILD CONTRACT (authoritative)

This file is the single source of truth for every component. Every module, transcript,
and the front-end must conform to the exact names, shapes, and routes below. When in
doubt, this file wins over prose in `TALOS_SANDBOX.md`.

The product: a single-page dashboard + FastAPI backend that lets a hackathon judge drive
a demo of the real TALOS multi-agent rollout system against a **simulated fleet**.
Default mode is **replay** (recorded real-agent runs, zero external deps, bulletproof).
Optional mode is **live** (real OpenAI agents against the sim fleet, heavily guardrailed).

The honesty rule is load-bearing: the fleet is simulated, the agents are real. Say so,
visibly, everywhere. A persistent `SANDBOX — simulated fleet · real agents` badge and a
"Watch it run on real VMs → [video]" link must be present on the dashboard.

This is the **OpenAI** Agentic AI Hackathon — the live agent layer uses the OpenAI API,
not any other provider.

---

## 1. Repository layout & file ownership

```
TALOS-Sandbox/
├── README.md                    [G]  what it is, quickstart, modes, honesty note
├── CONTRACT.md                  [me] this file (already written — do not modify)
├── DEPLOY.md                    [G]  VPS runbook (Docker, Caddy, TLS, env)
├── AT-HOME.md                   [G]  what to wire before shipping (real agents, video URL, domain)
├── docker-compose.yml           [F]  app + caddy, one stack
├── Dockerfile                   [F]  python:3.12-slim, install reqs, run uvicorn
├── Caddyfile                    [F]  reverse proxy + automatic HTTPS to talos.<domain>
├── requirements.txt             [F]  fastapi, uvicorn[standard], pydantic>=2, openai>=1.40, sse-starlette, python-dotenv, pytest
├── .env.example                 [F]  every env var referenced anywhere, with safe defaults
├── .gitignore                   [F]  .env, __pycache__, *.pyc, .pytest_cache, venv
├── Makefile                     [F]  dev/run/test/record/up/down targets
├── app/
│   ├── __init__.py              [B]
│   ├── main.py                  [B]  FastAPI app: routes, static mount, SSE, scenarios, audit, live gate, healthz
│   ├── replay.py                [B]  load+validate transcripts; audit hash-chain compute/verify
│   ├── live.py                  [C]  live-run orchestration + ALL §9 guardrails
│   ├── models.py                [A]  pydantic models for events, transcripts, host state, gate results
│   └── agents/
│       ├── __init__.py          [C]  agent interface + run_live(directive, backend, emit) entrypoint
│       └── reference_openai.py  [C]  reference OpenAI Planner/Critic/Monitor/Executor (the SWAP POINT)
├── tools/                       ← the fleet-backend swap point (spec §2)
│   ├── __init__.py              [A]  get_backend(name) selector
│   ├── sim_backend.py           [A]  SimFleet + the typed tool functions (sandbox)
│   ├── winrm_backend.py         [A]  placeholder — real impl lives in the TALOS core repo
│   └── gates.py                 [A]  deterministic gate evaluation (shared with real system)
├── replays/
│   ├── s1_clean_rollout.json    [E]
│   ├── s2_block_bad_plan.json   [E]
│   ├── s3_catch_regression.json [E]
│   ├── s3b_escape_to_ring.json  [E]
│   └── s4_verify_audit.json     [E]
├── static/
│   ├── index.html               [D]
│   ├── style.css                [D]
│   └── app.js                   [D]
├── scripts/
│   └── record_replays.py        [C]  run real agents vs sim, dump transcripts to replays/
└── tests/
    ├── test_gates.py            [A]
    ├── test_sim_backend.py      [A]
    └── test_replay_schema.py    [B]
```

Letters = the build agent that owns each file. Agents write ONLY their files. Cross-module
references are resolved at runtime via the signatures below, so parallel authoring is safe.

---

## 2. Fleet shape (fixed constants — sim and transcripts must agree exactly)

Three hosts (canary + a 2-host ring containing one HA pair):

| host       | ring    | ha_pair  | role                         |
|------------|---------|----------|------------------------------|
| `TALOS-CANARY`| `canary`| `null`   | canary                       |
| `TALOS-R1A`  | `ring1` | `TALOS-R1B`| production, HA pair member A |
| `TALOS-R1B`  | `ring1` | `TALOS-R1A`| production, HA pair member B |

Versions: baseline `2.1.3`; good build `2.1.4`; regression build `2.1.5`.
Health colors: `green` | `amber` | `red`.

Baseline healthy metrics (per host): `service_state="Running"`, `http_health=200`,
`eventlog_errors` in 0..2, `memory_mb` ~ 780–860.

Regression dynamics (`2.1.5`): holds baseline for `REGRESSION_DELAY_S` sim-seconds, then
`memory_mb` climbs on a curve toward ~1900, `http_health` flips 200→500, `eventlog_errors`
climbs past the gate threshold. Deterministic, timer-driven on the sim clock.
Escape variant (S3b): the regression only arms on `ring1` hosts, so canary passes clean and
the breach surfaces only after promotion to ring1 → fleet-scale rollback.

---

## 3. Event envelope (THE shared shape — SSE frames and transcript `events[]` are identical)

Every streamed/recorded event is one JSON object:

```json
{
  "seq": 7,
  "t": 12.5,
  "type": "plan",
  "actor": "planner",
  "title": "Ring plan authored",
  "body": "Canary TALOS-CANARY first, soak 120s, then ring1 one host at a time.",
  "data": { }
}
```

- `seq` (int): monotonic from 0 within a run. `t` (float): sim-time seconds (for pacing/labels).
- `actor` ∈ `system` | `planner` | `critic` | `monitor` | `executor` | `fleet`.
- `title` (string, short). `body` (string, may be multi-sentence plain text).
- `data` (object): type-specific payload below.

### Event types and their `data` payloads

| type                | actor            | data fields |
|---------------------|------------------|-------------|
| `directive`         | system           | `{ "text": str, "urgency": "routine"\|"urgent"\|"critical" }` |
| `topology`          | system           | `{ "hosts": [ {host, ring, ha_pair, version, health} ] }` (inits tiles) |
| `plan`              | planner          | `{ "steps": [ {ring, action, hosts:[...], soak_s:int} ], "rationale": str, "risk": "low"\|"medium"\|"high" }` |
| `critique`          | critic           | `{ "concern": str, "severity": "info"\|"warning"\|"blocker", "blast_radius": str }` |
| `verdict`           | critic           | `{ "decision": "approve"\|"reject", "reasons": [str] }` |
| `revision`          | planner          | `{ "steps": [ ... same as plan.steps ... ], "note": str }` |
| `deploy`            | executor         | `{ "host": str, "from_version": str, "to_version": str }` |
| `gate_eval`         | monitor          | `{ "host": str, "gate": str, "result": "pass"\|"fail", "metrics": {service_state,http_health,eventlog_errors,memory_mb}, "violations": [str] }` |
| `health`            | fleet            | `{ "host": str, "service_state": str, "http_health": int, "eventlog_errors": int, "memory_mb": int, "health": "green"\|"amber"\|"red" }` |
| `soak`              | monitor          | `{ "ring": str, "duration_s": int, "remaining_s": int }` (drives the countdown timer) |
| `approval_required` | system           | `{ "ring": str, "prompt": str }` (front-end shows Approve button; gates progress) |
| `approved`          | system           | `{ "ring": str }` |
| `breach`            | monitor          | `{ "host": str, "metric": str, "value": (num\|str), "threshold": (num\|str) }` |
| `rollback`          | executor         | `{ "scope": "host"\|"ring"\|"fleet", "hosts": [str], "to_version": str, "reason": str }` |
| `narration`         | system           | `{ "text": str }` (caption line in the feed) |
| `done`              | system           | `{ "outcome": str, "summary": str }` |

The front-end MUST handle every type above. Unknown types render as a generic feed line
(never crash). Tile state updates come from `topology`, `health`, `deploy`, `rollback`.

---

## 4. Transcript file schema (`replays/*.json`)

```json
{
  "id": "s1",
  "name": "Clean rollout",
  "summary": "Routine update deploys canary→ring with a clean soak. Fleet ends green.",
  "moment": "the happy path — every gate passes",
  "events": [ <event>, <event>, ... ],
  "audit_records": [
    { "id": 1, "action": "deploy TALOS-CANARY 2.1.3 -> 2.1.4", "actor": "executor", "ts": "2026-07-02T15:00:01Z" }
  ]
}
```

- `events[]`: ordered by `seq`, conform to §3. First event SHOULD be `topology`; last SHOULD be `done`.
- `audit_records[]`: append-only list. Authors provide only `{id, action, actor, ts}` —
  the server computes the hash chain (§6), so hashes are NOT stored in the file.
- An event with `type:"approval_required"` is an interactive gate: in replay, the player
  pauses there until the judge clicks Approve.

Scenario content requirements (each transcript must actually demonstrate its moment):
- **s1 Clean rollout**: directive(routine 2.1.4) → topology → plan(canary→ring1, soak) →
  verdict(approve) → deploy TALOS-CANARY → soak(compressed) → gate_eval(pass) →
  approval_required(ring1) → approved → deploy TALOS-R1A → deploy TALOS-R1B → soak →
  gate_eval(pass) → health(all green) → done(fleet_green).
- **s2 Block the bad plan**: directive(urgent/critical, "patch all 3 now") → plan(risk:high,
  deploys TALOS-R1A AND TALOS-R1B simultaneously, no canary) → critique(severity:blocker,
  blast_radius names taking down both HA-pair members at once) → verdict(**reject**) →
  revision(canary first, then HA pair one at a time) → verdict(approve) → short safe rollout
  → done. The reject must be unmistakable on screen.
- **s3 Catch the regression**: directive(deploy 2.1.5) → plan → approve → deploy TALOS-CANARY
  2.1.5 → soak starts green → health amber → breach(memory_mb / http_health) →
  gate_eval(fail) → rollback(scope:host, TALOS-CANARY → 2.1.3) → health(green) →
  done(caught_at_canary). Include a visible soak timer and the breach time.
- **s3b Escape to ring**: like s3 but canary passes clean (regression armed only on ring1) →
  approval_required → promote to ring1 → breach on TALOS-R1A/TALOS-R1B after promotion →
  rollback(scope:fleet) → health(green) → done(caught_at_ring).
- **s4 Verify the audit**: a compact run (may mirror s1) whose `audit_records` are the
  point. The front-end Audit panel calls verify (chain OK) and tamper (chain FAILS). The
  events should narrate that every action was recorded to a tamper-evident chain.

---

## 5. HTTP API (FastAPI, all under the same origin; front-end served at `/`)

| method | path | returns |
|--------|------|---------|
| GET | `/healthz` | `{"status":"ok"}` (container health check) |
| GET | `/api/scenarios` | `[ {id, name, summary, moment} ]` (from replays/*.json) |
| GET | `/api/scenarios/{id}` | full transcript JSON (§4) — replay is client-paced |
| GET | `/api/audit/{id}` | `{ "records": [ {id, action, actor, ts, prev_hash, hash} ] }` (server-computed chain) |
| GET | `/api/audit/{id}/verify` | `{ "ok": true, "broken_at": null }` — recompute chain, report integrity |
| GET | `/api/audit/{id}/verify?tamper={record_id}` | `{ "ok": false, "broken_at": <record_id> }` — server flips that record's `action`, recomputes, reports first broken link |
| GET | `/api/live/status` | `{ "enabled": bool, "spend": float, "cap": float, "remaining_runs_session": int, "busy": bool }` |
| POST | `/api/live/run` | body `{ "directive": str }` → guardrail checks (§9). On accept: `{ "run_id": str }`. On refusal: HTTP 429/403 with `{ "reason": str, "fallback_scenario": str }` |
| GET | `/api/live/{run_id}/stream` | **SSE** stream of §3 events as the live agents produce them; terminates after a `done` event |

SSE framing: each frame is `event: message` with `data: <json of one §3 event>`. Use
`sse-starlette`'s `EventSourceResponse`. Heartbeat comment every 15s. Replay mode does NOT
use SSE — it fetches the full transcript and paces client-side (this is why replay has zero
runtime deps and cannot fall over).

Static: mount `static/` so `GET /` serves `index.html`, plus `/style.css`, `/app.js`.

---

## 6. Audit hash chain (server-computed; sandbox-local, no dependency on core M3)

For a transcript's `audit_records` (ordered by `id`):

```
prev_hash[0]   = "GENESIS"
canonical(r)   = f"{r.id}|{r.action}|{r.actor}|{r.ts}|{prev_hash}"
hash[i]        = sha256( canonical(records[i]) ).hexdigest()
prev_hash[i+1] = hash[i]
```

`verify`: recompute the chain; `ok=true`, `broken_at=null` if every recomputed hash matches.
`verify?tamper=K`: build a mutated copy where record K's `action` has `" [TAMPERED]"`
appended, recompute from scratch; the first index whose hash diverges from the untampered
chain is `broken_at` (= K). Return `ok=false, broken_at=K`. This makes the tamper-evidence
tactile without needing the real M3 SQLite audit log.

---

## 7. Tool interface (the agent↔fleet swap point — `tools/`)

`tools/__init__.py` exposes `get_backend(name: str) -> FleetBackend` where `name` comes from
env `SANDBOX_BACKEND` (default `"sim"`). A `FleetBackend` is any object providing these
methods (the typed, allowlisted tool surface the agents are restricted to):

```python
get_fleet_topology() -> list[dict]      # [{host, ring, ha_pair, version, health}]
get_health(host: str) -> dict           # {service_state, http_health, eventlog_errors, memory_mb, health}
capture_baseline(host: str) -> dict     # snapshot of current metrics; returns the baseline
deploy_version(host: str, version: str) -> dict   # {ok, host, version}; starts a health trajectory
rollback(host: str, to_version: str) -> dict      # {ok, host, version}; resets trajectory to healthy
evaluate_gate(host: str, gate: str) -> dict       # delegates to tools.gates.evaluate(gate, get_health(host))
```

`tools/sim_backend.py` implements `SimFleet(FleetBackend)`:
- Constructor seeds the §2 fleet at baseline `2.1.3`, all green.
- Holds a sim clock (monotonic seconds; advanced by `tick(dt)` and/or wall-clock since deploy).
- `deploy_version` records `(version, t_deploy)` per host and selects a trajectory:
  healthy build → metrics steady; `2.1.5` → regression curve after `REGRESSION_DELAY_S`.
- `get_health` samples the trajectory at current sim-time, adds small bounded noise, derives
  `health` color from the gate thresholds (green=pass, amber=approaching, red=fail).
- `evaluate_gate` imports and calls `tools.gates.evaluate` — identical code path to the real
  system (gate eval does not care whether metrics are real or simulated).
- Soak compression: expose a speed factor (env `SIM_SPEED`, default 12) so a 120s soak
  completes in ~10s of wall time. The video shows real timing; the sandbox compresses.

`tools/winrm_backend.py`: a small placeholder class raising `NotImplementedError` with a
docstring: "The real WinRM transport lives in the TALOS core repo; the sandbox never talks
to real hosts." Present so the swap point is visible and documented.

Tunables (env, with defaults): `REGRESSION_DELAY_S=8`, `SIM_SPEED=12`, `SANDBOX_BACKEND=sim`.

---

## 8. `tools/gates.py` (deterministic; shared with the real system conceptually)

```python
GATES = {
  "canary_gate": {"max_memory_mb": 1500, "require_http": 200, "max_eventlog_errors": 5, "require_service": "Running"},
  "ring_gate":   {"max_memory_mb": 1500, "require_http": 200, "max_eventlog_errors": 5, "require_service": "Running"},
}

def evaluate(gate: str, metrics: dict) -> dict:
    # returns {"result": "pass"|"fail", "violations": [str], "thresholds": dict}
```

A metric violates if memory_mb > max, http_health != require_http, eventlog_errors > max,
or service_state != require_service. `result="pass"` iff `violations == []`. Pure function,
no I/O — directly unit-testable. Health-color helper may live here or in sim_backend, but the
pass/fail boundary is exactly these thresholds.

---

## 9. Live mode & guardrails (`app/live.py`) — MANDATORY, all of them

A public endpoint backed by a personal OpenAI key is a real cost/abuse surface. `live.py`
owns an in-process `LiveGate` (single instance) enforcing, on every `POST /api/live/run`:

1. **Global budget kill-switch**: track cumulative estimated USD spend; if `spend >= LIVE_BUDGET_CAP`
   (env, default `25.0`), refuse with `fallback_scenario` (graceful → judge gets a replay).
2. **Global rate limit**: at most `LIVE_RUNS_PER_HOUR` (env, default `20`) accepted runs/hour across all callers.
3. **Per-session limit**: a session cookie/id gets at most `LIVE_RUNS_PER_SESSION` (env, default `3`) runs.
4. **Input bounds**: reject `directive` longer than `LIVE_MAX_DIRECTIVE_CHARS` (env, default `500`); strip control chars.
5. **Concurrency guard**: at most ONE live run in flight (`busy` flag / lock); concurrent requests are refused, not queued past a small bound.
6. **Key safety**: read `OPENAI_API_KEY` from env only; never log it, never send it to the client; if unset, live mode reports `enabled:false` and the UI hides the live box.
7. **Blast containment**: live agents are wired ONLY to the sim backend — a hostile prompt can at worst produce a weird plan against fake hosts.

Spend accounting: after each run, add an estimate from token usage (or a flat per-run
estimate if usage is unavailable). The gate is fail-safe: any uncertainty → refuse to live,
fall back to replay. Live mode is **off by default** unless `OPENAI_API_KEY` is set AND
`LIVE_MODE_ENABLED=true` (env).

`app/agents/__init__.py` defines the orchestration entrypoint:
```python
async def run_live(directive: str, backend, emit) -> None
```
where `emit(event: dict)` pushes a §3 event onto the SSE queue. It drives a
Planner→Critic→(Executor/Monitor) loop. `app/agents/reference_openai.py` is the reference
implementation using the OpenAI API with function-calling bound to the §7 tool surface; it is
clearly labeled the SWAP POINT for the real TALOS agents. It must degrade gracefully (emit a
`narration` error event + `done`) if the API call fails.

---

## 10. Front-end behavior contract (`static/`)

Single dark page, vanilla HTML/CSS/JS (no build step). Components:
- **Header**: title, persistent badge `SANDBOX — simulated fleet · real agents`, and a
  prominent link `Watch it run on real VMs → [video]` (href = env-injected or a clear
  `REPLACE_WITH_VIDEO_URL` placeholder).
- **Scenario picker**: buttons for s1, s2, s3, s3b, s4 (name + one-line moment).
- **Ring tiles**: one tile per host showing `host · version · health dot` (green/amber/red),
  grouped by ring; HA-pair members visually paired. Update live from tile-state events.
- **Reasoning/event feed**: scrolling list of feed lines rendered from §3 events, color-keyed
  by actor (planner/critic/monitor/executor/system). Plans, critiques, verdicts, gate evals,
  breaches, rollbacks each get a distinct, readable treatment. The Critic **reject** in s2 and
  the **rollback** in s3 must be visually emphatic.
- **Controls**: Step (next event), Play, Pause, Restart, and a Fast-forward/soak-skip. Replay
  is paced client-side from event `t` deltas (clamp long soaks; FF collapses them).
- **Approve button**: appears on `approval_required`; clicking advances past the gate (replay)
  or POSTs approval (not required for replay—local advance is fine). Disabled otherwise.
- **Soak timer**: visible countdown driven by `soak` events.
- **Live box**: a text input + "Run live" button, gated behind a clear affordance
  `Live — calls a real model, ~20s`. Calls `POST /api/live/run`; on accept, opens the SSE
  stream and feeds the SAME renderer. On refusal, shows the returned reason and offers the
  fallback scenario. Hidden entirely when `/api/live/status` reports `enabled:false`.
- **Audit panel**: lists audit records for the current scenario; a `Verify` button →
  `/api/audit/{id}/verify` (shows chain OK); a `Tamper a record` control → calls verify with
  `?tamper=K` and shows the chain FAILING at record K, highlighting the broken link.
- **Mode label**: always shows whether the feed is `Replaying a recorded run` or
  `Live — real model against the simulated fleet`.

Robustness bar: "can't fall over when mashed." Guard every fetch; never throw on an unknown
event type; the replay path must work with the API up and the model/network down.

---

## 11. Environment variables (single list — .env.example must contain all)

| var | default | meaning |
|-----|---------|---------|
| `SANDBOX_BACKEND` | `sim` | fleet backend selector |
| `SIM_SPEED` | `12` | soak/time compression factor |
| `REGRESSION_DELAY_S` | `8` | sim-seconds before the bad build degrades |
| `LIVE_MODE_ENABLED` | `false` | master switch for live mode |
| `OPENAI_API_KEY` | (unset) | live mode key; never logged/sent to client |
| `OPENAI_MODEL` | `gpt-4o-mini` | model for live agents |
| `LIVE_BUDGET_CAP` | `25.0` | USD hard cap; at cap → fall back to replay |
| `LIVE_RUNS_PER_HOUR` | `20` | global accepted live runs/hour |
| `LIVE_RUNS_PER_SESSION` | `3` | per-session live runs |
| `LIVE_MAX_DIRECTIVE_CHARS` | `500` | input bound |
| `VIDEO_URL` | `REPLACE_WITH_VIDEO_URL` | demo video link injected into the page |
| `TALOS_DOMAIN` | `talos.example.com` | subdomain for Caddy TLS |

---

## 12. Conventions

- Python 3.12, type hints, pydantic v2, `async def` for FastAPI handlers. Standard library
  `hashlib` for the audit chain. No database — transcripts are JSON files, audit chain is
  computed on the fly.
- Determinism: replay is fully deterministic. Sim noise is small and bounded; gate boundaries
  are crisp so demos are repeatable.
- Every file an agent writes must be syntactically complete and import-consistent with the
  signatures in this contract. No placeholders except the explicitly-named swap points
  (`winrm_backend.py`, the real-agents TODO in `reference_openai.py`, `VIDEO_URL`).
- Keep it shippable: `docker compose up` must serve the replay sandbox with no secrets set.
