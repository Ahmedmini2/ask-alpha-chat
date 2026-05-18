FROM python:3.13-slim

# System deps: build tools for native wheels (asyncpg, pypdf occasionally), libpq for asyncpg fallback,
# tini as init so Ctrl-C / SIGTERM signals reach our processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first so layers cache.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Then the app.
COPY app ./app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command runs the API. The Telegram bot service overrides this in Railway:
#   python -m app.integrations.telegram.bot
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
