# Load Test Plan — Scenario X: Personalized In-App Recommendations

## Purpose

Validate that the deployed system meets the SLOs declared in `serving/slos.yaml`
and that the replica counts in `serving/capacity-plan.md` are sufficient before
any Champion model is promoted to production.

Load tests are a required gate in the CI/CD pipeline
(see `cicd/.github/workflows/deploy-model.yml`, stage `load-test`).
A model version may not be promoted from `Staging` to `Champion` if any
threshold in this plan is violated.

---

## Tool

**[Locust](https://locust.io)** — Python-native, scriptable, integrates with CI.

Alternative: **k6** (if the platform team prefers Go-based tooling).
Both produce Prometheus-compatible metrics for comparison against SLO dashboards.

---

## Target Environment

| Parameter | Value |
|---|---|
| Environment | `staging` (mirrors production topology) |
| Staging cluster | EKS — same instance types as production |
| Rec Service replicas | 12 (same as production — capacity-plan.md) |
| Triton replicas | 4 × g5.xlarge (same as production) |
| Redis | Separate staging cluster, same r7g.large spec |
| Model versions | Staging Champion + Staging Challenger (A/B split 50:50) |
| Test runner location | Same AWS region as cluster (us-east-1) to exclude WAN latency |

---

## Test Scenarios

### Scenario 1 — Sustained Load (Baseline)

**Goal:** Confirm the system handles normal traffic without degradation.

| Parameter | Value |
|---|---|
| RPS | 200 (off-peak sustained) |
| Duration | 10 minutes |
| User type | Mix: 95% known users (Redis hit), 5% cold-start |
| Concurrency | 50 virtual users |

**Pass criteria:**
- p95 latency ≤ 120 ms (SLO-2)
- Error rate < 0.1% (SLO-1)
- Cold-start rate ≤ 10% (SLO-5)
- No Redis key misses for known users > 1%

---

### Scenario 2 — Peak Load

**Goal:** Confirm the system meets SLOs at the declared peak of 800 RPS.

| Parameter | Value |
|---|---|
| RPS | 800 |
| Duration | 15 minutes |
| Ramp-up | 5 minutes (0 → 800 RPS linearly) |
| User type | Mix: 95% known users, 5% cold-start |
| Concurrency | 200 virtual users |

**Pass criteria:**
- p95 latency ≤ 120 ms (SLO-2) — measured at the API Gateway, not the load runner
- p99 latency ≤ 200 ms (informational — not an SLO breach, but flagged)
- Error rate < 0.1% (SLO-1)
- GPU utilisation on Triton pods ≤ 75% (capacity-plan.md scaling trigger)
- CPU utilisation on Rec Service pods ≤ 60% (HPA threshold)
- X-Model-Version header present on 100% of non-cold-start responses (SLO-3)

---

### Scenario 3 — Spike (1.5× Peak)

**Goal:** Confirm graceful degradation under a traffic spike before HPA scales out.

| Parameter | Value |
|---|---|
| RPS | 1,200 (1.5× peak) |
| Duration | 5 minutes at spike, then return to 800 RPS for 5 minutes |
| Ramp-up | 2 minutes (800 → 1,200 RPS) |
| User type | Same mix as Scenario 2 |

**Pass criteria:**
- p95 latency ≤ 150 ms during spike (20% relaxation — graceful degradation, not SLO breach)
- Error rate < 0.5% during spike (relaxed; 0.1% after return to 800 RPS)
- HPA triggers within 2 minutes of spike start (observed in Kubernetes events)
- System recovers to SLO-compliant latency within 3 minutes of return to 800 RPS

**Failure criteria (hard stop):**
- Error rate > 2% at any point — indicates queue saturation, not graceful degradation
- p95 > 500 ms — indicates Triton queue backup, must investigate before promotion

---

### Scenario 4 — Cold-Start Storm

**Goal:** Validate the DynamoDB fallback path does not cascade into failures when
Redis is unavailable (e.g., Spark pipeline delay causes mass TTL expiry).

| Parameter | Value |
|---|---|
| RPS | 400 (50% peak) |
| Duration | 5 minutes |
| User type | 100% cold-start (Redis keys cleared in staging before test) |
| Concurrency | 100 virtual users |

**Pass criteria:**
- p95 latency ≤ 120 ms (DynamoDB fallback must be as fast as normal path)
- Error rate < 0.1%
- `cold_start: true` in 100% of responses
- Triton pods receive 0 inference requests (confirms bypass logic works)
- DynamoDB read latency p99 ≤ 10 ms

---

### Scenario 5 — A/B Split Validation

**Goal:** Confirm that traffic is split correctly between Champion and Challenger
models and that both versions respond within SLO.

| Parameter | Value |
|---|---|
| RPS | 400 |
| Duration | 10 minutes |
| `X-AB-Cohort` distribution | 50% cohort-A, 50% cohort-B |
| Expected model split | 50% Champion, 50% Challenger |

**Pass criteria:**
- X-Model-Version header distribution: 50% ± 5% per model version
- p95 latency ≤ 120 ms for **each** model version independently (checked via
  Prometheus label `model_version`)
- No cross-contamination: cohort-A requests must not receive Challenger responses

---

## Metrics Collected During Tests

All metrics are collected via Prometheus and visible in Grafana during the test run.
The load runner also exports its own latency histogram for comparison.

| Metric | Source | SLO |
|---|---|---|
| `recommender_request_duration_seconds` | Rec Service | SLO-2 |
| `recommender_requests_total{status}` | Rec Service | SLO-1 |
| `recommender_requests_total{cold_start}` | Rec Service | SLO-5 |
| `recommender_requests_total{model_version}` | Rec Service | SLO-3 |
| `triton_inference_request_duration_ms` | Triton | Capacity plan |
| `feature_vector_age_seconds` | Feature API | SLO-4 |
| `redis_connected_clients` | Redis exporter | Capacity plan |
| GPU utilisation | DCGM exporter | Capacity plan |

---

## CI/CD Integration

The load test runs as a CI/CD pipeline stage (`load-test`) after the staging
deployment and before the `promote-to-champion` gate. See
`cicd/.github/workflows/deploy-model.yml`.

```yaml
# Excerpt — full definition in cicd/.github/workflows/deploy-model.yml
- name: Run load test (Scenario 2 — Peak)
  run: |
    locust -f tests/load/locustfile.py \
      --host https://staging.recommender.internal \
      --users 200 --spawn-rate 40 --run-time 15m \
      --headless --only-summary \
      --csv results/load_test
  env:
    TARGET_RPS: "800"

- name: Assert pass criteria
  run: python tests/load/assert_results.py results/load_test_stats.csv
```

`assert_results.py` reads the Locust CSV output and exits non-zero if:
- p95 latency > 120 ms
- Error rate > 0.1%

A non-zero exit blocks the promotion step and posts the failure summary to the
PR as a comment.

---

## Baseline and Regression Tracking

After each successful test, results are stored in S3:

```
s3://mlops-load-tests/recommender/{model_version}/{YYYYMMDD-HHMMSS}/
  load_test_stats.csv
  load_test_failures.csv
  prometheus_snapshot.tar.gz
```

Before each test run, `assert_results.py` compares the new p95 latency against
the stored baseline for the current Champion version. A regression of > 10 ms
above the baseline is flagged as a warning (not a hard failure) and included in
the promotion PR comment.

---

## Rollback on Load Test Failure

If Scenario 2 or Scenario 3 exceeds hard failure criteria:
1. The `promote-to-champion` CI/CD step does not execute (gate blocked)
2. The Challenger version remains in `Staging` state in the MLflow registry
3. The on-call is notified via Alertmanager (alert `LoadTestFailed`)
4. Full rollback procedure: see `runbooks/rollback.md`
