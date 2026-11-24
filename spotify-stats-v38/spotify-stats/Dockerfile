# The recipe for the container image. GitHub Actions builds it (for the Raspberry Pis' arm64
# and for normal amd64 PCs) and pushes it to ghcr.io/dycastcoast/spotify-stats:latest.
FROM python:3.13-slim

# Don't write .pyc files (the container's files are read-only in the cluster), and print
# logs straight away so `kubectl logs` shows them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Packages first, code second: Docker reuses the slow package step on rebuilds
# as long as requirements.txt hasn't changed.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an ordinary user, never root (the cluster refuses root containers anyway).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

# Where things that change are kept: /data is the mounted storage (the database),
# /tmp is scratch space (uploads while importing). Everything else is read-only.
ENV DATA_DIR=/data \
    UPLOAD_DIR=/tmp/uploads \
    HOME=/tmp

EXPOSE 8080
# gunicorn: a proper web server. ONE worker process, so every request shares the same
# memory (the page caches), with 8 threads so a slow page or the background poll never
# blocks other visitors. --timeout: a cold page on a Pi can take a while.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "300", "--access-logfile", "-", "app:app"]
