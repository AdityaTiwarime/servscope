# One image for the ServScope API and the demo services; the compose command picks which runs.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH

WORKDIR /app
# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000 8080
CMD ["python", "-m", "servscope.app"]
