FROM python:3.13-slim

# System deps: build tools for native wheels (asyncpg, pypdf occasionally), libpq for asyncpg fallback,
# tini as init so Ctrl-C / SIGTERM signals reach our processes, ffmpeg for media decoding, and
# curl/gnupg to add the NodeSource repo (Node powers the Remotion caption renderer).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        ca-certificates \
        tini \
        ffmpeg \
        curl \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first so layers cache.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Headless Chromium for brochure PDF rendering (playwright). --with-deps pulls the
# shared libraries Chromium needs on slim Debian — Remotion's Chrome Headless Shell
# (installed below) reuses the same libraries.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Remotion caption renderer: install node deps and pre-download Chrome Headless Shell
# so the first render at runtime doesn't have to fetch it.
COPY remotion ./remotion
RUN cd remotion \
    && npm ci --omit=dev --no-audit --no-fund \
    && npx remotion browser ensure

# Then the app.
COPY app ./app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command runs the API. The Telegram bot service overrides this in Railway:
#   python -m app.integrations.telegram.bot
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
