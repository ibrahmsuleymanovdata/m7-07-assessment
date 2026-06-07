# Architecture Justification — Scenario X: Personalized In-App Recommendations

## Chosen Pattern

**Microservice serving layer + two-stage ML pipeline (retrieval → ranker) + pre-computed feature store**

---

## Why This Pattern?

### 1. Two-Stage Pipeline (Retrieval → Ranker) instead of a Single Model

A single large model (e.g., session-based Transformer) scoring all catalogue items per request
is infeasible at 800 RPS with a 120 ms p95 budget. The catalogue can contain millions of items;
scoring each one per request would require hundreds of milliseconds on its own.

The two-stage pattern is the industry standard for this constraint (used by YouTube, Pinterest, LinkedIn):

| Stage | Model | Output | Latency |
|---|---|---|---|
| Retrieval | Two-tower neural (ONNX) | Top-500 candidate IDs | ≤ 40 ms |
| Ranking | XGBoost scorer (ONNX) | Top-20 final results | ≤ 20 ms |

The retrieval stage narrows the search space cheaply using approximate nearest-neighbour search
over pre-indexed item embeddings. The ranker then scores only those 500 candidates with richer
features, which fits comfortably in the remaining latency budget.

**Trade-off accepted:** Two models to maintain, version, and monitor instead of one. Mitigated by
shared Triton serving infrastructure and a unified registry entry per pipeline version.

---

### 2. Pre-Computed Feature Store (Redis) instead of On-the-Fly Feature Engineering

Computing user features (embedding vectors, purchase history aggregates) at request time would add
50–100 ms of database query and computation — blowing the latency budget before the model is
even called.

Pre-computing features every 4 hours and caching in Redis (p99 < 5 ms reads) keeps the hot
path to a simple key lookup. TTL is set to 12 hours (3× the refresh interval) so a delayed
Spark job never causes a cache miss storm.

**Trade-off accepted:** Features are up to 4 hours stale. For product recommendations this is
acceptable; the user's taste does not change in minutes. A real-time event stream (Kafka → Flink)
would reduce staleness but add significant operational complexity that is not justified by the
business requirement.

---

### 3. Triton Inference Server instead of Custom FastAPI Model Endpoint

Triton provides:
- Dynamic request batching (max batch 64, max delay 2 ms) — critical for GPU utilisation at 800 RPS
- Multi-model serving from a single GPU instance (retrieval + ranker on the same g5.xlarge)
- ONNX runtime support for both PyTorch and XGBoost exports
- Built-in Prometheus metrics per model

A custom FastAPI endpoint wrapping PyTorch would require manual batching logic and offers no
native multi-model support. Triton's overhead over a raw PyTorch call is negligible (< 2 ms).

**Trade-off accepted:** Triton adds operational complexity (model repository layout, protobuf
config files). Mitigated by storing configs in the same S3 path as model weights.

---

### 4. Model Weights Mounted at Runtime (S3 init-container) instead of Baked into Image

With two active model versions in flight simultaneously (Champion + Challenger for A/B testing),
baking weights into the image would require a full image rebuild and re-deployment for every
model swap — a multi-minute operation that blocks experimentation velocity.

Mounting from S3 at pod startup decouples the serving image lifecycle from the model lifecycle:
- Image rebuilds happen only when serving code changes
- Model swaps are a Kubernetes rolling restart (weights path env var change), taking ~2–3 minutes
- The same image serves both Champion and Challenger pods

**Trade-off accepted:** Pod startup time increases by ~30 seconds (S3 download of ~440 MB total
weights). This is acceptable for a stateless serving pod with graceful rolling updates.

---

### 5. Separate Recommendation Service (FastAPI) in Front of Triton

Triton's gRPC interface is not suitable for direct mobile client consumption. A thin FastAPI
service provides:
- REST/JSON interface for the mobile app
- A/B cohort routing logic (`X-AB-Cohort` → model version mapping)
- Cold-start detection and DynamoDB fallback orchestration
- `X-Model-Version` header attachment on responses
- Business-level observability (CTR signals, cold-start rate)

**Trade-off accepted:** Extra network hop (in-cluster gRPC, ~5 ms). The abstraction layer is
worth the cost because it isolates ML serving concerns from API contract concerns.

---

## Patterns Considered and Rejected

| Pattern | Reason Rejected |
|---|---|
| Single session-based Transformer | p95 inference > 150 ms — exceeds budget alone |
| On-the-fly feature computation | Adds 50–100 ms; blows latency budget |
| Real-time Kafka feature pipeline | Justified only if staleness < 1 min is required; 4 h is acceptable |
| Baked model weights in Docker image | Blocks A/B testing velocity; image size ~1 GB+ |
| Direct Triton exposure to mobile clients | gRPC/protobuf not suitable for mobile; no business logic layer |
| Serverless inference (Lambda/Cloud Run) | GPU cold-start latency incompatible with 120 ms SLO |

---

## Summary

The chosen architecture is the minimal system that satisfies all three hard constraints simultaneously:

1. **120 ms p95 latency** — achieved via pre-computed features + two-stage pipeline within budget
2. **800 RPS throughput** — achieved via horizontal scaling of stateless pods + GPU dynamic batching
3. **Continuous A/B testing** — achieved via runtime-mounted weights + cohort routing in serving layer
