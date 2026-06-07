# ADR-0002: Model Weights Mounted at Runtime vs. Baked into Docker Image

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-06-07 |
| **Deciders** | ML Engineer, Platform Engineer |
| **Scenario** | Scenario X — Personalized In-App Recommendations |

---

## Context

The model server (Triton) needs access to two sets of model weights at runtime:
- Two-tower retrieval model: ~420 MB (ONNX)
- XGBoost ranker: ~18 MB (ONNX)

Total: ~440 MB of model artifacts per serving pod.

Two delivery strategies were evaluated:

**Option A — Bake weights into Docker image**
Model artifacts are copied into the image at build time (`COPY model/ /models/`).
Image is rebuilt whenever weights change.

**Option B — Mount weights at pod startup via S3 init-container**
The Docker image contains only the serving runtime (Triton + dependencies).
An init-container downloads model artifacts from S3 to a shared volume before the main
container starts. The S3 path is injected via environment variable.

---

## Decision

**Option B (runtime mount from S3) is chosen.**

---

## Rationale

### A/B Testing Velocity

The product team runs continuous A/B experiments. At any given time, two model versions are
in production simultaneously: Champion and Challenger (see `lifecycle/model-registry.yaml`).

With Option A (baked image):
- Each model version requires its own image build (~8 minutes in CI)
- Deploying a new Challenger requires a full image push (~4 GB layer transfer for two pods)
- Total time from model registration to live traffic: ~20–30 minutes

With Option B (mounted weights):
- A model swap is a Kubernetes rolling restart with a new `MODEL_S3_PATH` env var
- Init-container downloads ~440 MB from S3 in the same region: ~25–35 seconds
- Total time from model registration to live traffic: ~2–3 minutes

Faster iteration directly improves the product team's ability to run experiments.

### Image Size and Registry Cost

| Option | Image Size | Monthly Registry Transfer (4 deploys/week) |
|---|---|---|
| Baked | ~5.2 GB | ~85 GB — significant ECR cost |
| Mounted | ~780 MB | ~13 GB — minimal |

The baked image is impractical for a team deploying multiple times per week.

### Security and Auditability

With Option B, model artifacts are stored in S3 with versioned object keys tied to the MLflow
registry entry:

```
s3://mlops-models/recommender/retrieval/v{semver}/model.onnx
s3://mlops-models/recommender/ranker/v{semver}/model.onnx
```

Access is controlled via IAM roles bound to the EKS service account (IRSA). The serving pod
never holds credentials; it assumes the role at runtime.

With Option A, model weights are embedded in the image layer, making it harder to audit which
weights are actually running vs. what the registry records — the image and the registry can
drift silently.

### CI/CD Pipeline Alignment

The CI/CD pipeline (see `cicd/.github/workflows/deploy-model.yml`) tags images as:

```
recommender-service:{git-sha7}-{YYYYMMDD}
```

This tag reflects **code** changes only. Model promotions in the registry trigger a separate
deployment step that updates `MODEL_S3_PATH` without changing the image tag. This separation
of concerns keeps the image tag meaningful and the deployment history clean.

---

## Consequences

**Positive:**
- Model swaps in ~2–3 minutes vs. ~20–30 minutes (10× faster experimentation)
- Image size reduced from ~5.2 GB to ~780 MB
- Model artifacts auditable independently of image builds
- IAM-controlled S3 access — no credentials in image layers

**Negative:**
- Pod startup time increases by ~30 seconds (S3 download)
  Mitigated: rolling updates are graceful; old pods keep serving until new ones are Ready
- S3 availability becomes a dependency for pod restarts
  Mitigated: S3 SLA is 99.99%; init-container retries 3× with exponential backoff before failing
- Model artifact and serving image versions must be tracked together
  Mitigated: MLflow registry records both the artifact URI and the compatible image tag in
  model metadata (see `lifecycle/model-registry.yaml` — `serving_image` field)

---

## Revisit Criteria

Reopen this ADR if:
- Model size exceeds 2 GB (S3 download becomes a meaningful startup bottleneck — consider
  pre-warming via node-level DaemonSet cache)
- A/B testing frequency drops to < 1 experiment/month (baked image becomes simpler to operate)
- Air-gapped deployment requirements prevent S3 access from the serving cluster
