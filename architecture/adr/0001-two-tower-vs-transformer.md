# ADR-0001: Two-Stage Pipeline (Retrieval + Ranker) vs. Single Session-Based Transformer

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-06-07 |
| **Deciders** | ML Engineer, Platform Engineer, Product Lead |
| **Scenario** | Scenario X — Personalized In-App Recommendations |

---

## Context

The recommendation system must return personalised results on every home-screen load.
Hard constraints:

- **p95 latency ≤ 120 ms** end-to-end
- **~800 RPS** at peak
- Product catalogue size: ~2 million items
- A/B tests run continuously — model swap must not require downtime

Two candidate architectures were evaluated:

**Option A — Single session-based Transformer**
One large model (e.g., SASRec or BERT4Rec) ingests the user's last 30 days of events and
scores items in a single forward pass.

**Option B — Two-stage pipeline: Two-tower retrieval → XGBoost ranker**
Stage 1: Two-tower model produces a user embedding; ANN search over pre-indexed item embeddings
retrieves top-500 candidates in ≤ 40 ms.
Stage 2: Lightweight XGBoost ranker scores the 500 candidates with richer features, returning
top-20 in ≤ 20 ms.

---

## Decision

**Option B (two-stage pipeline) is chosen.**

---

## Rationale

### Latency

| Model | Items Scored | Estimated p95 Inference |
|---|---|---|
| Session Transformer (full catalogue) | ~2 M | > 500 ms — infeasible |
| Session Transformer (pre-filtered 1 K) | 1,000 | ~90 ms — leaves < 30 ms for everything else |
| Two-tower retrieval (ANN over 2 M) | 2 M (ANN) | ≤ 40 ms |
| XGBoost ranker (500 candidates) | 500 | ≤ 20 ms |

The Transformer cannot score 2 million items within the budget. Even with a pre-filter, it
consumes the entire latency budget on inference alone, leaving no room for feature retrieval,
network, or serialisation.

The two-stage pipeline fits within 60 ms of inference time, leaving 60 ms for all other segments
(see `architecture/architecture.md` — Latency Budget Breakdown).

### Throughput and Cost

A session Transformer large enough to produce good recommendations requires a minimum of an
higher-cost multi-GPU deployment per replica to meet latency targets. At 800 RPS, 6+ replicas would be
needed — approximately $12,000/month in GPU compute alone.

The two-tower + XGBoost pipeline runs on `g5.xlarge` (1× A10G) instances. Four replicas handle
800 RPS with headroom, at ~$4,200/month total infrastructure.

### A/B Testing Compatibility

Both stages can be versioned independently in the model registry. A Challenger retrieval model
can be tested against the Champion ranker, or vice versa, without coupling model lifetimes.
A single monolithic Transformer version would need to be swapped atomically.

---

## Consequences

**Positive:**
- Inference pipeline fits within 120 ms p95 budget with 10 ms headroom
- Lower GPU cost (~65% reduction vs. Transformer option)
- Independent versioning of retrieval and ranking stages
- XGBoost ranker is interpretable — easier to debug recommendation quality issues

**Negative:**
- Two models to train, evaluate, register, and monitor
- Retrieval quality caps ranking quality — a poor top-500 set cannot be recovered by the ranker
- ANN index for item embeddings must be refreshed when item catalogue changes (nightly job)

**Mitigations:**
- Shared Triton instance serves both models — no additional serving infrastructure
- Registry enforces pipeline version bundles: retrieval v{N} is always tested with ranker v{M}
  before promotion (see `lifecycle/model-registry.yaml`)
- ANN index refresh is a lightweight offline job, independent of the serving path

---

## Revisit Criteria

Reopen this ADR if:
- Catalogue size drops below 100 K items (Transformer becomes viable within budget)
- A distilled Transformer model achieves p95 < 50 ms at 800 RPS on g5.xlarge (benchmark quarterly)
- Business requires sub-second personalisation on events < 1 minute old (session Transformer
  handles recency better than pre-computed embeddings)
