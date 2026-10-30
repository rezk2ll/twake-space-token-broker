FROM node:24-slim AS pages
WORKDIR /app
COPY package.json package-lock.json ./
RUN mkdir -p src/twake_space_token_broker/page && npm ci

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
COPY --from=pages /app/src/twake_space_token_broker/page/twake-css-*.css ./src/twake_space_token_broker/page/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin broker
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER 10001
EXPOSE 8080
# No access log: the callback's query string holds the user's authorization code
CMD ["uvicorn", "--factory", "twake_space_token_broker.app:create_app_from_env", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
