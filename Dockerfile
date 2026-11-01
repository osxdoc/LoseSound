# LoseSound service image
FROM python:3.15-slim

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="LoseSound" \
      org.opencontainers.image.description="Local preset and stream helper for Bose SoundTouch" \
      org.opencontainers.image.source="https://github.com/osxdoc/LoseSound" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    APP_VERSION=${APP_VERSION} \
    CONFIG_PATH=/data/config.json

WORKDIR /app

RUN useradd --create-home --uid 10001 losesound \
    && mkdir -p /data \
    && chown -R losesound:losesound /data

COPY src/ /app/src/
RUN chown -R losesound:losesound /app/src

USER losesound
EXPOSE 8092

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8092/api/health', timeout=3)"]

CMD ["python", "-m", "losesound"]
