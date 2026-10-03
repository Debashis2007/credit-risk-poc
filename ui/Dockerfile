# Lifecycle console on mocked AWS (local mode), for hosting the demo. Build from the repo root:
#   docker build -f ui/Dockerfile -t credit-risk-console .
FROM node:20-slim AS frontend
WORKDIR /app/ui/frontend
COPY ui/frontend/package.json ui/frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY ui/frontend/ ./
RUN npm run build

FROM python:3.11-slim
ARG MLP_PLATFORM_REPOSITORY=Debashis2007/ML-Platform
ARG MLP_PLATFORM_REF=feature/v7-alignment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/.platform:/app/ui/backend \
    MLP_CONSOLE_MODE=local \
    CONSOLE_AUTH_REQUIRED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 --branch "${MLP_PLATFORM_REF}" "https://github.com/${MLP_PLATFORM_REPOSITORY}.git" .platform \
    && rm -rf .platform/.git \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r .platform/requirements.txt -r requirements.txt -r requirements-dev.txt

COPY model.yaml train.py evaluate.py preprocess.py inference.py ./
COPY scripts/ scripts/
COPY ui/backend/ ui/backend/
COPY --from=frontend /app/ui/frontend/dist ui/frontend/dist

RUN useradd --create-home console && chown -R console /app
USER console
CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
