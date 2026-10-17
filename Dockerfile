FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/jiemo9527/news-admin" \
      org.opencontainers.image.title="News Admin" \
      org.opencontainers.image.description="Authenticated AI news aggregation and scheduled digests"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    NEWS_DATA_DIR=/app/data \
    NEWS_ENV_PATH=/app/data/config.env
WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 news \
    && useradd --uid 10001 --gid news --no-create-home news \
    && mkdir -p /app/data \
    && chown news:news /app/data
COPY pyproject.toml README.md ./
COPY news_admin ./news_admin
RUN pip install --no-cache-dir --no-deps .

USER 10001:10001
EXPOSE 5080
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5080/healthz', timeout=3)"
CMD ["python", "-m", "news_admin", "serve", "--host", "0.0.0.0", "--port", "5080"]
