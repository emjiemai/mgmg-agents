FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tashkent

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root: agents must never need host-level privileges.
RUN useradd -m -u 1000 mgmg && chown -R mgmg:mgmg /app
USER mgmg

# Shell form (not exec-array) so $PORT expands — Render assigns it dynamically
# per service; docker-compose falls back to 8000 via the default below.
CMD uvicorn integrations.amocrm.webhook_handler:app --host 0.0.0.0 --port ${PORT:-8000}
