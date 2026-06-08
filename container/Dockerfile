# =============================================================================
# Dockerfile — Recommendation Service (Scenario X)
# Multi-stage build: builder → runtime
#
# Image tag scheme: recommender-service:{git-sha7}-{YYYYMMDD}
# (matches CI/CD tagging in cicd/.github/workflows/deploy-model.yml
#  and MLflow registry serving_image field in lifecycle/model-registry.yaml)
#
# Model weights are NOT baked into this image.
# They are mounted at pod startup via S3 init-container.
# See: architecture/adr/0002-model-mount-vs-bake.md
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder
# Installs all dependencies into a virtual environment.
# The venv is then copied into the slim runtime image, keeping
# build tools (gcc, pip, wheel) out of the final layer.
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Prevent .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build dependencies only (not copied to runtime stage)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first — layer cache: only re-runs pip install
# when requirements change, not on every code change
COPY requirements.txt .

# Create an isolated venv so we can copy it cleanly into the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip --no-cache-dir && \
    pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# Slim final image: no build tools, no pip, no compilers.
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Non-root user — principle of least privilege
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Copy only the pre-built venv from builder — no build tools in this layer
COPY --from=builder /opt/venv /opt/venv

# Copy application source
COPY --chown=appuser:appgroup src/ ./src/

# Health-check endpoint — Kubernetes liveness/readiness probe target
# Kubernetes will call GET /health every 10 s; 3 failures → pod restart
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Runtime environment variables (non-secret defaults)
# Secrets (e.g. REDIS_PASSWORD) are injected via Kubernetes Secrets, not here
ENV APP_PORT=8000 \
    UVICORN_WORKERS=4 \
    LOG_LEVEL=info \
    # Model version is injected at deploy time from MLflow registry
    # — not hardcoded here (see ADR-0002 and lifecycle/model-registry.yaml)
    MODEL_VERSION="" \
    # S3 path injected at pod startup by init-container
    # e.g. s3://mlops-models/recommender/retrieval/v1.4.0/model.onnx
    MODEL_S3_PATH="" \
    # A/B split ratio — overridden per deployment for Champion vs Challenger
    AB_SPLIT_RATIO="50:50"

USER appuser

EXPOSE 8000

# 4 uvicorn workers per pod — matches capacity-plan.md throughput calculation
# (4 workers × 40 req/s per worker = 160 req/s per pod)
CMD ["uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--log-level", "info"]
