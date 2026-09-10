FROM python:3.12-slim

WORKDIR /srv

# Apply available Debian security patches at build time. The base image ships
# OS packages (perl, glibc, sqlite3, gzip, zlib) flagged by AWS Inspector; most
# have no fixed Debian version yet, but upgrading pulls in whatever patches exist
# and keeps the image current. Rebuild regularly to pick up new fixes.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip: 25.0.1 has 6 CVEs (CVE-2025-8869, CVE-2026-1703, CVE-2026-3219,
# CVE-2026-6357, CVE-2026-8643, CVE-2026-13346), fixed in >=25.3.
RUN pip install --no-cache-dir --upgrade "pip>=25.3"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY bot/ bot/
COPY ingest/ ingest/
COPY scripts/ scripts/
COPY sql/ sql/

ENV PYTHONUNBUFFERED=1
# чтобы `python scripts/...` видел пакеты app/ и ingest/
ENV PYTHONPATH=/srv
EXPOSE 8080
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
