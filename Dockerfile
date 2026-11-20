FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY rebound ./rebound
COPY assets ./assets
RUN useradd --create-home --uid 10001 rebound && mkdir -p /app/data && chown -R rebound:rebound /app
USER rebound
ENV REBOUND_DB=/app/data/rebound.sqlite3
EXPOSE 8765
# Hosts such as Render, Railway and Fly inject PORT; local runs keep 8765.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn rebound.app:app --host 0.0.0.0 --port \"${PORT:-8765}\" --proxy-headers --forwarded-allow-ips '*'"]
