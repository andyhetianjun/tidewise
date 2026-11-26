FROM python:3.12-slim

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev

# Cache lives in a volume so repeated questions do not hit the API again.
ENV TIDEWISE_CACHE_DIR=/cache
VOLUME /cache

# MCP clients talk to the server over stdin/stdout.
ENTRYPOINT ["uv", "run", "--no-sync", "tidewise"]
CMD ["serve"]
