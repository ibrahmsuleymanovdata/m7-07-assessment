# Capacity Plan — Scenario X: Personalized In-App Recommendations

## Traffic Profile

| Metric | Value | Source |
|---|---|---|
| Peak RPS | 800 | Product requirement |
| Sustained (off-peak) | ~200 RPS | ~25% of peak, estimated |
| p95 end-to-end latency budget | 120 ms | Product requirement |
| Inference budget (both stages) | 60 ms | See `architecture/architecture.md` — Latency Budget |
| Avg response payload | ~2 KB | 20 item IDs + metadata, JSON |
| Peak inbound bandwidth | ~1.6 MB/s | 800 RPS × 2 KB |

---

## Recommendation Service (FastAPI) — Replica Sizing

**Pod spec:** 2 vCPU / 4 GB RAM, 4 uvicorn workers per pod

Each worker handles one request at a time during the synchronous feature-fetch + gRPC call
sequence. Effective concurrency per pod = 4 workers.

Observed p95 handling time per worker (excluding Triton inference): ~25 ms
→ Max throughput per worker ≈ 1000 ms / 25 ms = **40 req/s**
→ Max throughput per pod (4 workers) = **160 req/s**

| Scenario | RPS | Replicas Needed | Replicas Provisioned | Headroom |
|---|---|---|---|---|
| Sustained | 200 | 2 | 12 | 6× |
| Peak | 800 | 5 | 12 | 2.4× |
| Peak × 1.5 spike | 1,200 | 8 | 12 | 1.5× |

**12 replicas** provisioned. HPA configured to scale between 6 (off-peak) and 16 (burst) based
on CPU utilisation target 60% and custom metric `recommender_requests_in_flight > 6 per pod`.

---

## Model Server (Triton) — Replica Sizing

**Pod spec:** g5.xlarge — 1× NVIDIA A10G (24 GB VRAM), 4 vCPU, 16 GB RAM

Both models are served from the same Triton instance per pod:
- Two-tower retrieval: ~420 MB VRAM, p95 inference ≤ 40 ms
- XGBoost ranker: ~18 MB VRAM, p95 inference ≤ 20 ms
- Total VRAM per pod: ~440 MB active + ~2 GB Triton overhead — well within 24 GB

**Dynamic batching:** max batch size 64, max queue delay 2 ms.

At 800 RPS with dynamic batching, average batch size ≈ 12–15 requests.
Each batch inference (retrieval + ranker) takes ≤ 60 ms.
GPU utilisation per pod at peak: ~55–65%.

| Scenario | Effective RPS into Triton | Replicas Needed | Replicas Provisioned | GPU Util |
|---|---|---|---|---|
| Sustained | 200 | 1 | 4 | ~15% |
| Peak | 800 | 2 | 4 | ~60% |
| Peak × 1.5 spike | 1,200 | 3 | 4 | ~85% |

**4 replicas** provisioned — handles 1.5× peak spike before GPU saturation.

---

## Feature Store (Redis) — Sizing

- **Cluster:** 3 primary + 3 replica nodes, r7g.large (2 vCPU, 16 GB RAM each)
- **Key size:** ~1.5 KB per user (128-dim float32 vector + category vector + metadata)
- **Active user keys at peak:** estimated 5 million concurrent sessions × 1.5 KB = **~7.5 GB**
  comfortably within 16 GB per primary node
- **Read throughput:** r7g.large sustains ~100,000 ops/s; peak demand at 800 RPS is ~800 ops/s —
  negligible load
- **TTL:** 12 h — keys expire naturally; no eviction pressure expected

---

## Latency Budget Validation

The 120 ms end-to-end p95 budget is allocated as follows (consistent with `architecture/architecture.md`):

| Segment | Budget | Validated By |
|---|---|---|
| Network (client → gateway) | 15 ms | CDN edge SLA |
| API Gateway overhead | 5 ms | AWS HTTP API p99 < 5 ms |
| Feature retrieval (Redis) | 10 ms | Redis r7g p99 < 5 ms; 5 ms margin |
| gRPC transport (service → Triton) | 5 ms | In-cluster same-AZ measurement |
| Two-tower retrieval (Triton) | 40 ms | Triton benchmark, A10G, batch≤64 |
| XGBoost ranker (Triton) | 20 ms | Triton benchmark, A10G, batch≤64 |
| Response serialization + network back | 15 ms | ~2 KB JSON payload |
| **Total** | **110 ms** | **10 ms headroom** |

All SLOs declared in `serving/slos.yaml` reference these segment budgets.

---

## Monthly Cost Estimate

| Component | AWS Service | Count | Unit Cost | Monthly |
|---|---|---|---|---|
| Rec Service pods | EKS on m6i.xlarge nodes | 12 pods (3 nodes) | $140/node | $420 |
| Triton pods | EKS on g5.xlarge nodes | 4 pods (4 nodes) | $560/node | $2,240 |
| Redis cluster | ElastiCache r7g.large | 6 nodes | $120/node | $720 |
| RDS PostgreSQL | db.t3.medium + read replica | 2 | $60/instance | $120 |
| S3 storage + transfer | Standard | ~500 GB | ~$12 | $12 |
| API Gateway | HTTP API | 800 RPS × 2.6 B req/mo | $1/M req | $260 |
| Data transfer + misc | — | — | — | ~$430 |
| **Total** | | | | **~$4,200/month** |

*Spot instances for Rec Service pods reduce compute cost by ~40% with graceful draining on
interruption. Triton pods use On-Demand due to GPU availability constraints.*

---

## Scaling Triggers

| Metric | Scale-Out Threshold | Scale-In Threshold | Component |
|---|---|---|---|
| CPU utilisation | > 60% (2-min avg) | < 30% (5-min avg) | Rec Service HPA |
| In-flight requests per pod | > 6 | < 2 | Rec Service HPA |
| GPU utilisation | > 75% (2-min avg) | < 30% (5-min avg) | Triton HPA |
| Redis memory usage | > 70% | — | Manual review |

---

## Cold-Start Traffic Estimate

Cold-start requests (no Redis key) bypass Triton entirely and are served from DynamoDB.
Estimated cold-start rate: 5% of requests at peak = ~40 RPS.
DynamoDB on-demand capacity handles this with sub-10 ms read latency — no separate sizing needed.
