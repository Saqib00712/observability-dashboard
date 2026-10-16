# obsdash -- single small image. No build tools needed: every dependency ships wheels.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY obsdash ./obsdash
COPY scripts ./scripts
COPY config ./config

# Run as a non-root user. /data is the only writable place (SQLite file).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin obsdash \
 && mkdir /data && chown obsdash:obsdash /data
USER obsdash

# Inside a container the app must listen on all interfaces; the compose file
# publishes the port on the HOST loopback only (see docker-compose.yml).
ENV HOST=0.0.0.0 \
    PORT=8100 \
    SQLITE_PATH=/data/obsdash.db
VOLUME ["/data"]
EXPOSE 8100

# /health needs no token. Uses only the stdlib so the image needs no curl.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=2).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "obsdash.main:app", "--host", "0.0.0.0", "--port", "8100"]
