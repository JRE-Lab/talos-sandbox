# AT-HOME.md — pre-ship checklist

The steps to run **at home in Claude Code**, on the real TALOS workstation, before submitting.
The staged sandbox ships with a reference OpenAI agent adapter and placeholder transcripts so
it is import-consistent and runnable out of the box — but the submission-grade version wires in
the *real* TALOS agents, records transcripts from *real* runs, and points at the *real* video.

This is the **SB1 → SB4** build sequence from the spec, as a checklist:

- **SB1 — Replay sandbox (local):** steps 1–3 (real agents wired, transcripts recorded). The
  shippable core.
- **SB2 — VPS deploy:** steps 4 and 6 (domain + DNS, deploy behind Caddy).
- **SB3 — Live mode (optional):** step 5 (decide on/off, set keys + caps). Only if core + video
  are locked.
- **SB4 — Interactive audit:** already wired in the sandbox (the `s4` verify/tamper control);
  just confirm it during the step-7 smoke test.

Work top to bottom. Each step says how to confirm it before moving on.

---

## 1. Swap the real TALOS agents into `app/agents/reference_openai.py`

`app/agents/__init__.py` defines the orchestration entrypoint the rest of the app calls:

```python
async def run_live(directive: str, backend, emit) -> None
```

`reference_openai.py` is the **SWAP POINT** — a reference OpenAI Planner/Critic/Monitor/
Executor loop with function-calling bound to the §7 tool surface. There is a clearly-labeled
real-agents `TODO` in that file. You have two valid choices:

- **(a) Keep the reference adapter.** It is real OpenAI agents against the sim backend — fully
  honest, just not the production TALOS agent code. Fine for submission if the core agents
  aren't cleanly importable into this repo.
- **(b) Swap in the real TALOS agents.** Wire the production Planner/Critic/Monitor/Executor in
  behind the same `run_live(directive, backend, emit)` interface, emitting the **same §3 event
  envelope** via `emit(event: dict)`. The agents must act **only** through the `backend` tool
  surface (`get_fleet_topology`, `get_health`, `capture_baseline`, `deploy_version`,
  `rollback`, `evaluate_gate`) — that is what keeps them swappable and contained.

Whichever you choose, the agents must still emit §3 events and **degrade gracefully** (a
`narration` error event + a `done` event) if an API call fails. Do not change the
`run_live` signature or the event shapes — six other modules and the front-end depend on them.

**Confirm:** run a live call locally against the sim backend and watch the feed render the same
event types as a replay.

---

## 2. Re-record the curated transcripts from real agent runs

The placeholder transcripts in `replays/*.json` exist so the sandbox runs day one. For the
submission, **re-record them from real agent runs** so the "real agents" claim is literally
true of every replay:

```bash
python scripts/record_replays.py
```

This runs the real agents against `sim_backend` once per curated scenario and dumps a
conforming transcript (`CONTRACT.md` §4) to `replays/`:

- `s1_clean_rollout.json` — every gate passes, fleet ends green
- `s2_block_bad_plan.json` — the Critic **reject** must be unmistakable on screen
- `s3_catch_regression.json` — breach during soak → host rollback, caught at canary
- `s3b_escape_to_ring.json` — clean canary → breach after promotion → fleet rollback
- `s4_verify_audit.json` — compact run whose `audit_records` are the point

**Confirm:** each scenario still demonstrates its `moment` after re-recording (s1 all-green,
s2 visible reject + revision, s3 breach + rollback, s3b escape-to-ring, s4 audit records
present). First event of each is `topology`, last is `done`.

---

## 3. Set `VIDEO_URL` to the real demo video

The dashboard's **`Watch it run on real VMs → [video]`** link reads `VIDEO_URL`. Until set it
is the literal placeholder `REPLACE_WITH_VIDEO_URL`. Put the real, public demo-video URL in
`.env`:

```ini
VIDEO_URL=https://<your-real-demo-video-url>
```

This link is load-bearing for the honesty rule — the video is where the judge sees the **real
fleet**. Do not ship with the placeholder.

**Confirm:** load the page, click the link, confirm it opens the real video.

---

## 4. Set `TALOS_DOMAIN` + DNS

Pick the public subdomain and point it at the VPS:

- Add the DNS **A record** `talos.<yourdomain> → <VPS_PUBLIC_IP>` and wait for it to resolve
  (`dig +short talos.<yourdomain>`).
- Set `TALOS_DOMAIN=talos.<yourdomain>` in the VPS `.env` (Caddy requests a TLS cert for
  exactly this name).

Full details in `DEPLOY.md` §2–§3.

**Confirm:** `dig +short talos.<yourdomain>` returns the VPS IP before you deploy.

---

## 5. Decide live mode on/off (and set keys + caps if on)

Live mode is **off by default** and the live box is hidden unless **both** `OPENAI_API_KEY` is
set **and** `LIVE_MODE_ENABLED=true`. Per the spec, the default is **replay-only first**; only
ship live mode if the core and the video are already locked.

- **Off (recommended default):** leave `LIVE_MODE_ENABLED=false` and `OPENAI_API_KEY` unset.
  The replay sandbox is complete and bulletproof on its own.
- **On (the "prove it's real" stretch):** set `LIVE_MODE_ENABLED=true`, set `OPENAI_API_KEY`,
  and keep the guardrail caps conservative:

  ```ini
  LIVE_MODE_ENABLED=true
  OPENAI_API_KEY=sk-...
  OPENAI_MODEL=gpt-4o-mini
  LIVE_BUDGET_CAP=25.0
  LIVE_RUNS_PER_HOUR=20
  LIVE_RUNS_PER_SESSION=3
  LIVE_MAX_DIRECTIVE_CHARS=500
  ```

  The key is read from env only — never logged, never sent to the client. See `DEPLOY.md` §6.

**Confirm:** `GET /api/live/status` reports the state you intend (`enabled:false` with the box
hidden, or `enabled:true` with the caps you set).

---

## 6. `docker compose up` on the VPS behind Caddy

Deploy the stack (app + Caddy, one container build) and let Caddy provision automatic TLS:

```bash
# on the VPS, in the repo root, with .env set:
docker compose up -d --build
curl -s https://talos.<yourdomain>/healthz      # → {"status":"ok"}
```

Then smoke-test in a browser: badge present, video link correct, all five scenarios step
through, the Audit panel's Verify/Tamper works, and the live box is shown/hidden per your
step-5 decision. Full runbook and resilience notes in `DEPLOY.md` (§4–§7), including the
**Kalshi-VPS-vs-separate-box** decision (spec §9) in `DEPLOY.md` §8.

**Confirm:** the public HTTPS URL loads cleanly from a machine that is not the VPS.

---

## 7. Paste the public URL into the submission

Put the public URL into **TALOS_SUBMISSION Field 11 (Live Agent Link)**:

```
https://talos.<yourdomain>/
```

And add the one-line honesty note in Access Instructions:

> Labeled sandbox; the real-fleet run is in the video.

This preserves the load-bearing honesty rule at the submission layer too: the link is a
**labeled sandbox** (simulated fleet, real agents), and the real Windows-VM run is in the demo
video.

**Confirm:** open the submitted URL one final time as a judge would, from a clean browser, and
verify the badge, the video link, and at least one full scenario step-through before calling it
done.

---

### Final gate

Before you submit, all seven boxes checked:

- [ ] real agents (or the reference adapter) wired behind `run_live`
- [ ] transcripts re-recorded from real runs, each still demonstrating its moment
- [ ] `VIDEO_URL` set to the real video (no placeholder)
- [ ] `TALOS_DOMAIN` + DNS A record resolving
- [ ] live mode decision made; keys + caps set if on
- [ ] `docker compose up` on the VPS, `/healthz` ok, TLS green, smoke test passes
- [ ] public URL + honesty note in TALOS_SUBMISSION Field 11
