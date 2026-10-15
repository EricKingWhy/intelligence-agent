# Build image for the Anthropic OSS Scanner enrollment.
#
# How the scanner uses it (anthropics/oss-scanner, tools/check):
#   1. clones the enrolled repo anonymously at the pinned ref,
#   2. builds this Dockerfile with that checkout as the build context
#      (network IS available during the build),
#   3. then runs the security audit with NO network access.
# Everything the build or the tests need must be fetched inside this Dockerfile.
#
# Versions mirror CI (.github/workflows/gate0.yml):
#   python 3.13 / node 22 / pnpm 10.32.1 / uv 0.10.9
#   `uv sync --locked --all-extras` + `pnpm install --frozen-lockfile`

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_VERSION=0.10.9 \
    PNPM_VERSION=10.32.1

# System deps + Node.js 22 (nodesource, same major as CI's setup-node 22).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv + pnpm, pinned to the CI versions.
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && npm install -g "pnpm@${PNPM_VERSION}"

WORKDIR /src

# --- Python dependencies first (best layer cache hit) ---
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --all-extras

# --- Frontend dependencies ---
COPY web/package.json web/pnpm-lock.yaml ./web/
RUN cd web && pnpm install --frozen-lockfile

# --- Rest of the repository ---
COPY . /src

# --- Frontend production build (tsc -b && vite build) ---
RUN cd web && pnpm build

# Smoke check: the package imports under the synced venv.
RUN .venv/bin/python -c "import agent_harness; print('import ok:', agent_harness.__file__)"
