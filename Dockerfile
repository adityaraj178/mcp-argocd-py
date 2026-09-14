# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Resolve dependencies first so the layer is reused while only the source changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
# --no-editable: install the package into the venv itself, so the runtime stage
# only needs .venv and not the source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
EXPOSE 3000
# No MCP_BIND_ADDRESS here on purpose. The image keeps the loopback default, which
# a sidecar or any other container sharing this network namespace can reach.
# Publishing a port is the case that needs a wider bind, and that stays an explicit
# `-e MCP_BIND_ADDRESS=0.0.0.0` alongside an inbound credential. See "Network
# Exposure" in the README.
#
# Split so that overriding the command only replaces the arguments, not the
# interpreter: `docker run <image> http --stateless` works as written.
ENTRYPOINT [ "argocd-mcp" ]
CMD [ "http" ]
USER 1000
