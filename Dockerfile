FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tashkent

WORKDIR /app

# libpango*: WeasyPrint lays out the report PDFs/images with Pango
# (integrations/reports/; the fonts themselves are bundled there).
# gnupg + postgresql-client-16: the encrypted backups (integrations/backup/).
# pg_dump must be at least the database's version (Render mgmg-db: 16), and
# Debian's own client may be older — so it comes from PostgreSQL's official
# apt repository (postgresql.org/download/linux/debian), for whichever
# Debian release this image is built on.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates gnupg fonts-dejavu-core libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root: agents must never need host-level privileges.
RUN useradd -m -u 1000 mgmg && chown -R mgmg:mgmg /app
USER mgmg

# Shell form (not exec-array) so $PORT expands — Render assigns it dynamically
# per service; docker-compose falls back to 8000 via the default below.
CMD uvicorn integrations.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
