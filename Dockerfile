FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Runtime utilities used by some skills/tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p feibot && touch feibot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf feibot

# Copy the full source and install
COPY feibot/ feibot/
RUN uv pip install --system --no-cache .

# Create config directory
RUN mkdir -p /root/.feibot

# Gateway default port
EXPOSE 18790

ENTRYPOINT ["feibot"]
CMD ["--help"]
