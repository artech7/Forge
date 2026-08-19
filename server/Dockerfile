FROM python:3.12-slim

# ffprobe is the only outside program the server uses — it inspects files to
# decide what needs doing. It never encodes; that happens on worker machines.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Nothing is written next to the code, so the image runs on a read-only
# root filesystem. The database lives on a mounted volume.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORGE_DATA=/data \
    MEDIA_ROOTS=/media

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf /app/data /app/__pycache__

# Runs unprivileged. Override with `user:` in compose to match the account
# that owns your media, otherwise finished files land with the wrong owner.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin forge \
    && mkdir -p /data && chown forge:forge /data
USER forge

EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8420/api/health || exit 1

# --proxy-headers makes Forge trust X-Forwarded-For and X-Forwarded-Proto,
# so logs show the real client rather than the proxy. Only sensible because
# the container is not meant to be reachable except through the proxy.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8420", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
