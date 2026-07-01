# DEPLOY.md — TALOS Sandbox VPS runbook

How to put the sandbox on a public URL behind automatic HTTPS, so it can be pasted into the
submission's Live Agent Link field. The whole stack is one `docker compose` invocation:
**app (FastAPI/uvicorn) + Caddy (reverse proxy + automatic TLS)**.

> Replay mode needs no secrets and no external services. You can deploy the entire judge
> experience with nothing but a domain. Live mode is opt-in and covered at the end.

---

## 1. Prerequisites

- A Linux VPS you control (see §7 on whether to reuse the existing Kalshi box).
- **Docker Engine + the Compose plugin** (`docker compose`, not the legacy `docker-compose`).
- **A domain** you can add a DNS record to. The sandbox is served at a subdomain
  `talos.<yourdomain>` and Caddy provisions a TLS cert for exactly that name.
- Inbound **ports 80 and 443** open to the VPS (Caddy needs 80 for the ACME HTTP challenge
  and 443 for HTTPS). No other inbound ports are required.

---

## 2. DNS

Create a single **A record** pointing the sandbox subdomain at the VPS public IP:

```
talos.<yourdomain>.   A   <VPS_PUBLIC_IP>
```

(If the VPS has an IPv6 address, add the matching `AAAA` record too.) Wait for it to
resolve before bringing the stack up — Caddy's automatic TLS depends on the name pointing at
the box so the ACME challenge can complete:

```bash
dig +short talos.<yourdomain>     # should print <VPS_PUBLIC_IP>
```

---

## 3. Configure the environment

Copy the example env file and set the domain. Every variable the app reads has a safe default
in `.env.example`; for a replay-only deploy you only need to change `TALOS_DOMAIN`.

```bash
cp .env.example .env
```

Edit `.env`:

```ini
# Required: the subdomain Caddy will request a TLS cert for.
TALOS_DOMAIN=talos.<yourdomain>

# Recommended: point the "Watch it run on real VMs" link at the real demo video.
VIDEO_URL=https://<your-demo-video-url>

# Leave live mode OFF for the public default deploy (see §6 to enable it):
LIVE_MODE_ENABLED=false
# OPENAI_API_KEY stays UNSET for replay-only.
```

The remaining variables (`SIM_SPEED`, `REGRESSION_DELAY_S`, the live caps, `OPENAI_MODEL`,
etc.) have correct defaults in `.env.example` and the contract's §11 table. **Never commit
`.env`** — it is in `.gitignore`.

`TALOS_DOMAIN` is consumed by the Caddyfile (it is the site address Caddy gets a cert for),
so it must match the DNS A record exactly.

---

## 4. Bring the stack up

```bash
docker compose up -d --build
```

This builds the app image (`python:3.12-slim`, installs `requirements.txt`, runs uvicorn) and
starts two services — the app and Caddy — on one network. Caddy reverse-proxies
`talos.<yourdomain>` to the app and **provisions a TLS certificate automatically** on first
request via Let's Encrypt. The first HTTPS hit may take a few seconds while the cert is
issued; after that it is cached on a Docker volume and renews itself.

Watch it come up:

```bash
docker compose ps
docker compose logs -f caddy     # look for the cert being obtained for talos.<yourdomain>
docker compose logs -f app       # uvicorn "Application startup complete"
```

---

## 5. Verify

The app exposes a container health check at `/healthz`:

```bash
# straight at the app through Caddy over TLS:
curl -s https://talos.<yourdomain>/healthz
# → {"status":"ok"}
```

Then in a browser open **`https://talos.<yourdomain>/`** and confirm:

- the **`SANDBOX — simulated fleet · real agents`** badge is present,
- the **`Watch it run on real VMs → [video]`** link is present (and points at your video if
  you set `VIDEO_URL`),
- the six scenario buttons (`s1`, `s2`, `s3`, `s3b`, `s3c`, `s4`) load and step through,
- the Audit panel's **Verify** shows the chain OK and **Tamper a record** shows it failing,
- the live box is **hidden** (it should be, with live mode off).

A quick sanity check of the scenario API:

```bash
curl -s https://talos.<yourdomain>/api/scenarios | head
```

---

## 6. Enabling live mode safely (optional)

Live mode runs the real OpenAI agents against the sim backend on a judge's own directive. It
is a real cost/abuse surface, so it is **off by default** and stays hidden until **both** of
these are true:

1. `OPENAI_API_KEY` is set, **and**
2. `LIVE_MODE_ENABLED=true`.

To turn it on, edit `.env`:

```ini
LIVE_MODE_ENABLED=true
OPENAI_API_KEY=sk-...               # read from env only; never logged, never sent to client
OPENAI_MODEL=gpt-4o-mini            # default is fine

# Guardrail caps — keep these conservative on a public box:
LIVE_BUDGET_CAP=25.0                # USD hard cap; at cap the gate falls back to replay
LIVE_RUNS_PER_HOUR=20               # global accepted runs/hour across all callers
LIVE_RUNS_PER_SESSION=3             # per-session cap
LIVE_MAX_DIRECTIVE_CHARS=500        # input length bound
```

Then recreate the app container so it picks up the new env:

```bash
docker compose up -d --build
curl -s https://talos.<yourdomain>/api/live/status
# → {"enabled":true,"spend":0.0,"cap":25.0,"remaining_runs_session":3,"busy":false}
```

The `LiveGate` enforces, on every `POST /api/live/run`: the global budget kill-switch, the
global and per-session rate limits, the directive length bound (with control chars stripped),
and a single-run concurrency lock. Any refusal returns a reason **and** a `fallback_scenario`
so the judge still gets a recorded run rather than an error. The gate is fail-safe — any
uncertainty about spend or capacity means it refuses to go live and falls back to replay.

**Key hygiene:** the key lives only in the VPS `.env` (which is gitignored and not baked into
the image). It is never written to logs and never returned to the client. If you rotate it,
update `.env` and `docker compose up -d` again. To turn live mode back off, set
`LIVE_MODE_ENABLED=false` (or remove the key) and recreate — the UI hides the live box again.

---

## 7. Resilience

- **Restart policy.** Both services run with `restart: unless-stopped` (in
  `docker-compose.yml`), so the app and Caddy come back after a crash or a host reboot.
- **The replay path has no external dependencies.** Transcripts are JSON files baked into the
  image and the audit chain is computed in-process with `hashlib`. There is no database, no
  outbound network call, and no API key needed for replay. So the judge experience stays up
  **regardless** of OpenAI API state, rate limits, or network weather — the path that can
  call out (live mode) is exactly the path that is optional and gated.
- **Health check.** `/healthz` returns `{"status":"ok"}`; wire it into compose's healthcheck
  and/or your uptime monitor.
- **TLS renewal** is automatic via Caddy; the cert and ACME account persist on a Docker
  volume, so a redeploy does not re-request a cert or hit rate limits.
- **Logs.** `docker compose logs -f app` / `caddy`. The app never logs the OpenAI key.

To update after a code or transcript change:

```bash
git pull
docker compose up -d --build
```

To take it down:

```bash
docker compose down
```

---

## 8. Reuse the Kalshi VPS, or stand up a separate box? (spec §9 open item)

The spec leaves this as an open item to confirm before deploy. The footprint is small (one
Python container + Caddy, replay path is in-memory JSON), so headroom is rarely the blocker —
but **confirm it explicitly** rather than assuming:

**Check headroom on the existing box** (the Kalshi ensemble VPS):

```bash
free -h                 # spare RAM — the app idles in the low hundreds of MB
df -h /                 # spare disk for the image + Caddy cert volume
docker ps               # what's already running / port pressure
nproc                   # CPU count
cat /etc/os-release     # confirm the Linux flavor for exact steps
```

**Decision guide:**

- **Reuse the Kalshi box if:** ports **80 and 443 are free** (or you can run Caddy on the
  existing reverse proxy), there is spare RAM/disk per the checks above, and the Kalshi
  workload is not latency-sensitive enough that a second container disturbs it. This is the
  cheapest path and the ops are already familiar.
- **Stand up a separate box if:** the Kalshi system already owns 80/443 with its own proxy
  you don't want to reconfigure, the box is tight on resources, or you want the public,
  judge-facing endpoint isolated from the trading system on principle (recommended if in
  doubt — blast-radius isolation for a public URL is worth a cheap separate droplet).

Either way, record the chosen box and its Linux flavor so the exact deploy steps above (Docker
install, firewall rules) match the distro. If the existing proxy is nginx rather than fresh
Caddy, you can instead proxy `talos.<yourdomain>` from that nginx to the app container's port
and skip the Caddy service — but the default, simplest path is the bundled Caddy with
automatic TLS.
