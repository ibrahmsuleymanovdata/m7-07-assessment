# Rollback Runbook — Scenario X: Personalized In-App Recommendations

**Owner:** On-Call Engineer
**Last updated:** 2026-06-07
**Estimated execution time:** 5–10 minutes

---

## Trigger Conditions

Initiate this runbook when **any** of the following alerts fire in Alertmanager:

| Alert | Threshold | Severity |
|---|---|---|
| `RecommenderAvailabilityCritical` | Error rate burn-rate > 14.4× (1h+5m windows) | Critical |
| `RecommenderLatencyCritical` | p95 latency burn-rate > 14.4× (1h+5m windows) | Critical |
| `RecommenderLatencyHardBreach` | p95 > 150 ms for 5 min | Critical |
| `RecommenderUnexpectedModelVersion` | Unknown `X-Model-Version` serving traffic | Warning |
| Manual decision | Product Lead or ML Engineer calls rollback | — |

> Thresholds above match `monitoring/alerts.yaml` exactly.
> Do not wait for a second alert — act on the first Critical page.

---

## Pre-Rollback Checklist

Before rolling back, confirm the issue is model-related (not infra):

- [ ] Check Grafana dashboard: https://grafana.internal/d/recommender-slos
- [ ] Confirm Redis is healthy: `kubectl get pods -n recommender-prod -l app=redis`
- [ ] Confirm Triton pods are running: `kubectl get pods -n recommender-prod -l app=triton`
- [ ] Check if a recent deployment occurred: `kubectl rollout history deployment/recommender-service -n recommender-prod`
- [ ] If infra issue (Redis down, Triton OOM) → do NOT rollback model; page platform team instead

---

## Rollback Steps

### Step 1 — Identify the previous Champion version

```bash
# Query MLflow for the last Archived pipeline version
python scripts/mlflow_fetch_artifact.py --stage Archived --latest

# Expected output:
# pipeline_version: 1.3.0
# retrieval_uri: s3://mlops-models/recommender/retrieval/v1.3.0/model.onnx
# ranker_uri:    s3://mlops-models/recommender/ranker/v1.3.0/model.onnx
# serving_image: recommender-service:a3f9d12-20260530
# archived_at:   2026-06-05T11:31:00Z
# artifact_retention_until: 2026-09-05
```

> Archived weights are retained for 90 days (see `lifecycle/model-registry.yaml`).
> If `artifact_retention_until` has passed, contact ML Engineer — weights may be gone.

---

### Step 2 — Update kubeconfig

```bash
aws eks update-kubeconfig --name mlops-prod --region us-east-1
```

---

### Step 3 — Roll back model weights (no image rebuild needed)

```bash
# Set MODEL_S3_PATH env vars to previous Champion's artifact URIs
kubectl set env deployment/recommender-service \
  -n recommender-prod \
  MODEL_VERSION="1.3.0" \
  MODEL_S3_PATH_RETRIEVAL="s3://mlops-models/recommender/retrieval/v1.3.0/model.onnx" \
  MODEL_S3_PATH_RANKER="s3://mlops-models/recommender/ranker/v1.3.0/model.onnx"

# Watch rollout
kubectl rollout status deployment/recommender-service \
  -n recommender-prod \
  --timeout=300s
```

> Init-container downloads ~440 MB from S3. Pod restart takes ~2–3 minutes.
> Old pods keep serving until new pods pass readiness probe.

---

### Step 4 — Verify rollback succeeded

```bash
# Check X-Model-Version header on a live request
curl -s -I \
  -H "Authorization: Bearer ${HEALTHCHECK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"usr_healthcheck","context":{"platform":"ios","screen":"home"}}' \
  https://api.company.com/v1/recommend \
  | grep X-Model-Version

# Expected: X-Model-Version: 1.3.0
```

```bash
# Confirm no unexpected model versions in Prometheus
# (alert RecommenderUnexpectedModelVersion should resolve within 5 min)
curl -s "http://prometheus.internal/api/v1/query" \
  --data-urlencode 'query=count by (model_version) (http_requests_total{service="recommender-api"})' \
  | python -m json.tool
```

---

### Step 5 — Update MLflow registry

```bash
# Demote current Champion back to Staging
python scripts/mlflow_set_stage.py \
  --pipeline-version 1.4.0 \
  --stage Staging \
  --reason "Rollback: latency SLO breach in production"

# Promote previous Archived version back to Champion
python scripts/mlflow_set_stage.py \
  --pipeline-version 1.3.0 \
  --stage Champion \
  --reason "Rollback target restored to Champion"
```

---

### Step 6 — Confirm alerts resolved

Wait up to 10 minutes, then confirm in Alertmanager:

- [ ] `RecommenderAvailabilityCritical` — resolved
- [ ] `RecommenderLatencyCritical` — resolved
- [ ] `RecommenderLatencyHardBreach` — resolved
- [ ] `RecommenderUnexpectedModelVersion` — resolved
- [ ] Grafana p95 latency back below 120 ms
- [ ] Error rate back below 0.1%

If alerts do not resolve within 10 minutes → escalate to ML Engineer + Platform Engineer.

---

### Step 7 — Notify stakeholders

```bash
curl -X POST $SLACK_WEBHOOK_URL \
  -H 'Content-type: application/json' \
  -d '{
    "text": "⚠️ Recommender rollback completed. Pipeline 1.4.0 → 1.3.0. Reason: SLO breach. Incident channel: #inc-recommender",
    "channel": "#ml-deploys"
  }'
```

Notify: ML Engineer, Product Lead, On-Call Lead.

---

## Post-Rollback Actions (within 24 hours)

- [ ] Open incident report with timeline and root cause
- [ ] ML Engineer investigates pipeline 1.4.0 failure mode
- [ ] Run offline evaluation on pipeline 1.4.0 with production traffic sample
- [ ] Do not re-promote 1.4.0 without root cause identified and fixed
- [ ] Update `lifecycle/model-registry.yaml` with rollback event notes

---

## Quick Reference

| What | Value |
|---|---|
| Production namespace | `recommender-prod` |
| EKS cluster | `mlops-prod` (us-east-1) |
| MLflow UI | http://mlflow.mlops.svc.cluster.local:5000 |
| Grafana SLO dashboard | https://grafana.internal/d/recommender-slos |
| Alertmanager | https://alertmanager.internal |
| Slack channel | `#ml-deploys`, `#ml-alerts` |
| S3 model bucket | `s3://mlops-models/recommender/` |
| Artifact retention | 90 days after archive |
