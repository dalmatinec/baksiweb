FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data WEB_HOST=0.0.0.0 WEB_PORT=8080 MALLOC_ARENA_MAX=2

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 baksi

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY baksi ./baksi
COPY modules ./modules
RUN rm -rf modules/*/tests && mkdir -p /data && chown -R baksi:baksi /data /app

USER baksi
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
STOPSIGNAL SIGTERM
CMD ["python", "-m", "baksi"]
