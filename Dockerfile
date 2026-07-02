# TALOS Sandbox — application image
# python:3.12-slim, install deps, run uvicorn. No secrets are baked in; all
# runtime config (OPENAI_API_KEY, caps, toggles) arrives via env at run time.

FROM python:3.12-slim

# Keep Python lean and unbuffered so logs stream straight to Docker.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. The replay sandbox needs only these directories;
# tests/, docs/, and the compose/Caddy files stay out of the image.
COPY app/ ./app/
COPY tools/ ./tools/
COPY static/ ./static/
COPY replays/ ./replays/
COPY scripts/ ./scripts/

EXPOSE 8000

# Listen on $PORT when a PaaS (Render/Railway/Fly) injects one; default 8000 for
# the VPS + Caddy path (Caddy proxies to app:8000).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
