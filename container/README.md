# Container Plan — Recommendation Service

## Overview

This directory contains the `Dockerfile` for the **Recommendation Service** only.
The Model Server (Triton Inference Server) uses NVIDIA's official image
`nvcr.io/nvidia/tritonserver:24.03-py3` and is not built here.

---

## Images in the Serving Stack

| Service | Image | Built Here |
|---|---|---|
| Recommendation Service | `recommender-service:{git-sha7}-{YYYYMMDD}` | ✅ Yes |
| Triton Inference Server | `nvcr.io/nvidia/tritonserver:24.03-py3` | ❌ Upstream |
| S3 Init-Container | `amazon/aws-cli:2.15` | ❌ Upstream |

---

## Image Tag Scheme

```
recommender-service:{git-sha7}-{YYYYMMDD}

Example: recommender-service:a3f9c12-20260607
```

- `git-sha7` — first 7 characters of the commit SHA that triggered the build
- `YYYYMMDD` — build date for human readability

This scheme is used consistently across:
- `cicd/.github/workflows/deploy-model.yml` (image build + push step)
- `lifecycle/model-registry.yaml` (`serving_image` field per model version)
- `architecture/architecture.md` (Component Descriptions — Recommendation Service)

**Important:** The image tag reflects **code changes only**. Model promotions
(Champion → Challenger swaps) update the `MODEL_S3_PATH` environment variable
without changing the image tag. See ADR-0002 for rationale.

---

## Bake-vs-Mount Decision

**Model weights are mounted at runtime — not baked into this image.**

Full rationale: [`architecture/adr/0002-model-mount-vs-bake.md`](../architecture/adr/0002-model-mount-vs-bake.md)

Summary:

| Approach | Image Size | Model Swap Time | Chosen |
|---|---|---|---|
| Bake weights into image | ~5.2 GB | ~20–30 min (full rebuild) | ❌ |
| Mount from S3 at startup | ~780 MB | ~2–3 min (rolling restart) | ✅ |

At pod startup, a Kubernetes init-container (`amazon/aws-cli:2.15`) downloads
model weights from S3 to a shared `emptyDir` volume before the main container
starts:

```
s3://mlops-models/recommender/retrieval/v{semver}/model.onnx   (~420 MB)
s3://mlops-models/recommender/ranker/v{semver}/model.onnx      (~18 MB)
```

S3 access uses IAM Roles for Service Accounts (IRSA) — no credentials in the
image or environment variables.

---

## Multi-Stage Build

The `Dockerfile` uses two stages:

| Stage | Base | Purpose |
|---|---|---|
| `builder` | `python:3.12-slim` | Install dependencies into `/opt/venv` |
| `runtime` | `python:3.12-slim` | Copy venv only — no build tools |

**Why multi-stage?**
Build tools (`gcc`, `build-essential`) are needed to compile some Python
packages but must not be present in the final image (attack surface reduction).
The builder stage compiles everything; the runtime stage copies only the
finished virtual environment.

---

## Image Size Estimate

| Layer | Estimated Size |
|---|---|
| `python:3.12-slim` base | ~130 MB |
| Python venv (FastAPI, uvicorn, grpcio, prometheus-client, boto3, pydantic) | ~210 MB |
| Application source (`src/`) | ~2 MB |
| **Total (uncompressed)** | **~342 MB** |
| **Total (compressed, ECR)** | **~780 MB** |

This is consistent with the ADR-0002 comparison table and the capacity-plan.md
monthly ECR transfer estimate (~13 GB/month at 4 deploys/week).

---

## Security Posture

| Control | Implementation |
|---|---|
| Non-root user | `appuser` (UID 1001) — defined in Dockerfile |
| No secrets in image | All secrets injected via Kubernetes Secrets at runtime |
| No model weights in image | S3 mount via IRSA (no credentials stored) |
| Minimal base image | `python:3.12-slim` — no package manager, no shell utilities |
| Image vulnerability scan | Trivy scan in CI/CD pipeline (blocks on CRITICAL CVEs) |
| Read-only filesystem | Enforced via Kubernetes `securityContext.readOnlyRootFilesystem: true` |

---

## Key Environment Variables

These are set at deploy time (not hardcoded in the image):

| Variable | Source | Description |
|---|---|---|
| `MODEL_VERSION` | MLflow registry | Active model version string (e.g. `v1.4.0`) — used to set `X-Model-Version` response header |
| `MODEL_S3_PATH` | MLflow registry | S3 URI for model weights, injected by init-container |
| `AB_SPLIT_RATIO` | Deployment config | Champion/Challenger traffic split (default `50:50`) |
| `REDIS_URL` | Kubernetes Secret | Redis cluster connection string |
| `REDIS_PASSWORD` | Kubernetes Secret | Redis auth token |

`MODEL_VERSION` is the value attached to the `X-Model-Version` response header
on every non-cold-start response, as described in `architecture/architecture.md`
and monitored in `monitoring/alerts.yaml`.

---

## Local Development (Without Kubernetes)

```bash
# Build
docker build -t recommender-service:dev .

# Run with mock environment variables
docker run --rm \
  -e MODEL_VERSION=dev-local \
  -e AB_SPLIT_RATIO=100:0 \
  -e REDIS_URL=redis://localhost:6379 \
  -p 8000:8000 \
  recommender-service:dev
```

Model weights are not needed for local development if the service is run with
a stub Triton client (set `TRITON_STUB=true` to activate mock inference responses).
