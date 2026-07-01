# TALOS Sandbox — developer convenience targets.
# Recipe lines are TAB-indented (Make requires real tabs, not spaces).

.PHONY: dev run test record up down logs

# Hot-reloading dev server (auto-restarts on code changes).
dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Plain server (no reload), as it runs in production-ish local use.
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run the test suite quietly.
test:
	pytest -q

# Record replay transcripts by running the agents vs. the sim fleet into
# replays/. Safe by default: existing curated transcripts are SKIPPED (the
# script prints what it skipped). To regenerate them from real agent runs:
#   python scripts/record_replays.py --all --live --force   (needs OPENAI_API_KEY)
record:
	python scripts/record_replays.py --all

# Build + start the full Docker stack (app + caddy) in the background.
up:
	docker compose up -d --build

# Stop and remove the stack.
down:
	docker compose down

# Tail logs from the running stack.
logs:
	docker compose logs -f
