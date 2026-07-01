# TALOS Sandbox

An interactive, judge-facing demo of the **real TALOS multi-agent rollout system**
driving a **simulated fleet** of Windows hosts. Built for the HCLTech–OpenAI Agentic AI
Hackathon to fill the (optional) Live Agent Link field with something you can actually
*touch* — not just watch.

Pick a scenario, step through it, and watch the Planner author a ring plan, the Critic
reject a reckless one, the Monitor catch a regression mid-soak, and the Executor roll it
back — then verify the tamper-evident audit chain with one click.

---

## The honesty rule (load-bearing — read this first)

> **The fleet is simulated. The agents are real.**

This is stated, visibly, everywhere — and it is not a disclaimer, it is the whole point.
The differentiator of TALOS is that it drives **real Windows hosts over real WinRM** in the
production system. Exposing that to the public internet would be reckless, so the sandbox
swaps in an in-memory `sim_backend` behind the *exact same* typed tool interface the agents
already use. The agents don't know or care which backend is behind the seam.

The dashboard therefore carries, at all times:

- a persistent badge: **`SANDBOX — simulated fleet · real agents`**
- a prominent link: **`Watch it run on real VMs → [video]`**
- a mode label that always reads either **`Replaying a recorded run`** or
  **`Live — real model against the simulated fleet`**

A judge must never mistake the sandbox for the real system. The pairing is the strength:
*play with the simulation here; watch the identical flow run against real Windows VMs in the
demo video.* Do not remove or soften these labels — doing so inverts the Technical
Excellence score the honesty is meant to earn.

The demo video is the artifact that shows the real fleet. Its URL is injected into the page
via the `VIDEO_URL` env var (placeholder `REPLACE_WITH_VIDEO_URL` until set — see `AT-HOME.md`).

**Provider note:** the live agent layer uses the **OpenAI** API (this is the OpenAI Agentic
AI Hackathon), not any other provider.

---

## Quickstart — local replay (zero external deps)

Replay mode needs no API key, no network, and no fleet. It loads the recorded transcripts
from `replays/*.json` and paces them client-side. This is the default and it cannot fall over.

```bash
# from the repo root
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open **http://localhost:8000** and pick a scenario.

No secrets are required for replay. The same command serves everything the judge sees in
replay mode; live mode stays hidden unless you opt in (see "Replay vs live" below).

---

## The scenarios

Six curated replays, each demonstrating one moment. They are recordings of the *real*
agents run against the sim backend, captured once and replayed deterministically.

| Scenario | id | What it demonstrates |
|----------|------|----------------------|
| **Clean rollout** | `s1` | The happy path: a routine `2.1.4` update deploys canary → ring with a clean soak, every gate passes, fleet ends green. Establishes the baseline flow and the step-through controls. |
| **Block the bad plan** | `s2` | The Innovation moment, interactive. An urgent directive asks to "patch all 3 now"; the Planner proposes a high-risk plan that takes down **both HA-pair members at once**; the Critic issues an unmistakable **reject** naming the blast radius; the Planner revises (canary first, HA pair one at a time); the Critic approves. |
| **Catch the regression** | `s3` | Deploy a bad build (`2.1.5`). Health goes green, then breaches during soak — `memory_mb` climbs, `http_health` flips 200→500, eventlog errors climb past the gate. The gate fails and the Executor **auto-rolls-back** canary to `2.1.3`. Caught at the canary, with a visible soak timer and breach time. |
| **Escape to ring** | `s3b` | Same regression, but armed only on `ring1` hosts — so the canary passes clean, the rollout is approved and promoted, and the breach surfaces only *after* promotion, triggering a **fleet-scale rollback**. Shows that the gates catch it even when it slips the canary. |
| **Regression → auto-heal** | `s3c` | The demo headline. Same regression as `s3`, but the loop doesn't stop at "undo": caught → rolled back to `2.1.3` → a **Diagnostician** diagnoses the software regression → TALOS **auto-heals** the fleet forward to the patched `2.1.6`, with no human in the loop. Every step lands in the audit chain. |
| **Verify the audit** | `s4` | The cryptographic-accountability moment, made tactile. A compact run whose point is its audit records. Click **Verify** → the hash chain is OK. Click **Tamper a record** → the server flips one record and the chain **FAILS** at exactly that link, highlighted on screen. |

---

## Replay vs live

**Replay (default, always available).** The front-end fetches the full transcript and paces
it client-side from each event's sim-time. No SSE, no API calls, no external dependencies at
judge time. It survives dead WiFi, a rate limit, or a model outage. This alone is a complete,
shippable interactive sandbox.

**Live (optional, opt-in, heavily capped).** A "Run live" box lets a judge type their own
directive; the **real OpenAI agents** respond live against the simulated fleet and stream
their reasoning over SSE into the same renderer. This is the "prove it's real" feature.

Live mode is **off by default**. The live box is hidden entirely unless the server reports it
enabled, which requires **both** `OPENAI_API_KEY` to be set **and** `LIVE_MODE_ENABLED=true`.
When enabled it is wrapped in the guardrails in `CONTRACT.md` §9: a global USD budget
kill-switch, global and per-session rate limits, directive length bounds, a single-run
concurrency lock, and key safety (the key is read from env only — never logged, never sent to
the client). The agents are wired **only** to the sim backend, so a hostile prompt can at
worst produce a weird plan against fake hosts. Any uncertainty in the gate → it refuses and
falls back to a replay. See `DEPLOY.md` for how to turn it on safely.

---

## Architecture (short version)

```
  Dashboard (single dark page)        static/index.html · style.css · app.js
        │  HTTP + SSE
        ▼
  FastAPI app                         app/main.py  (routes, static, SSE, healthz)
        ├── replay engine             app/replay.py  (load+validate transcripts, audit chain)
        └── live gate + orchestration app/live.py   (§9 guardrails) → app/agents/
                                      app/agents/run_live → reference_openai (OpenAI, SWAP POINT)
        │  typed, allowlisted tools (the swap point)
        ▼
  Fleet backend                       tools/get_backend(name)
        ├── sim_backend.py            in-memory hosts + health trajectories (sandbox default)
        └── winrm_backend.py          placeholder — real transport lives in the TALOS core repo
```

The seam that makes this clean: the agents act only through a typed, allowlisted tool
interface (`get_fleet_topology`, `get_health`, `capture_baseline`, `deploy_version`,
`rollback`, `evaluate_gate`). That interface is the swap point — implemented as `sim_backend`
here and as `winrm_backend` in the core repo. **No agent code is forked.** The same
`tools/gates.py` deterministic gate evaluation is used in both worlds, so the sandbox passes
and fails on exactly the thresholds the real system does.

Replay = zero-dependency default: a transcript is just a JSON file of events, and the audit
hash chain is computed on the fly with the standard-library `hashlib`. No database.

---

## How to add a scenario

Two ways, both ending in a `replays/*.json` transcript that conforms to `CONTRACT.md` §4:

1. **Drop in a transcript.** Write a `replays/<id>_<name>.json` file with the schema in
   `CONTRACT.md` §4: an `{id, name, summary, moment, events[], audit_records[]}` object. The
   `events[]` array uses the shared event envelope in §3 (first event should be `topology`,
   last should be `done`). Authors provide only `{id, action, actor, ts}` for each audit
   record — the server computes the hash chain. The scenario picker reads the directory on
   startup, so a new file shows up as a new button automatically.

2. **Re-record from real agent runs.** Run the real agents against the sim backend and dump
   the resulting transcript with the recorder:

   ```bash
   python scripts/record_replays.py
   ```

   This is how the curated transcripts are meant to be produced for the submission — see
   `AT-HOME.md` step 2. Recording from real runs is what keeps the "real agents" claim honest.

---

## Source of truth

`CONTRACT.md` is the single authoritative spec — every file name, data shape, route,
signature, and constant lives there and **wins over any prose**, including this README. If
anything here disagrees with `CONTRACT.md`, the contract is correct.

Companion docs:

- **`DEPLOY.md`** — VPS runbook (Docker + Caddy + automatic TLS, env, resilience, enabling
  live mode safely).
- **`AT-HOME.md`** — the pre-ship checklist to run before submitting (swap in the real
  agents, re-record transcripts, set the video URL and domain, decide on live mode, deploy,
  paste the public URL into the submission).
