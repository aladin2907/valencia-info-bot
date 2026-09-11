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

# torch на CPU: обычное колесо тянет ~2.5 ГБ ради CUDA, которая на сервере не нужна
ENV PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# веса кладём в том, иначе они качаются заново при каждом пересоздании контейнера
ENV HF_HOME=/models-cache
ENV OMP_NUM_THREADS=4

EXPOSE 8081
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8081"]
