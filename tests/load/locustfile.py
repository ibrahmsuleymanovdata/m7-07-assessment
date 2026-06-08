"""
Locust load test — Scenario X: Personalized In-App Recommendations
Implements Scenario 2 (Peak Load) from serving/load-test-plan.md.

Run:
    locust -f tests/load/locustfile.py \\
        --host https://api-staging.company.com \\
        --users 200 --spawn-rate 40 --run-time 15m \\
        --headless --only-summary --csv results/load_test

Pass criteria (checked by assert_results.py):
    - p95 latency <= 120 ms  (SLO-2)
    - error rate   < 0.1%    (SLO-1)
"""

import os
import random
import uuid

from locust import HttpUser, between, task


# ---------------------------------------------------------------------------
# Sample user pool — 95% "warm" users with known Redis keys,
# 5% cold-start users whose IDs will not be in the feature store.
# ---------------------------------------------------------------------------
WARM_USER_IDS = [f"usr_{uuid.uuid4().hex[:24]}" for _ in range(500)]
COLD_USER_IDS = [f"usr_NEW_{uuid.uuid4().hex[:20]}" for _ in range(50)]

PLATFORMS = ["ios", "android", "web"]
SCREENS = ["home", "category", "pdp", "cart"]

AUTH_TOKEN = os.environ.get("LOAD_TEST_TOKEN", "test-token-placeholder")


class RecommenderUser(HttpUser):
    """Simulates a mobile app user requesting recommendations."""

    wait_time = between(0.1, 0.5)  # ~200 VU → ~800 RPS at peak

    def on_start(self):
        self.client.headers.update(
            {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "Content-Type": "application/json",
            }
        )

    @task(19)  # 95% of requests — warm users (Redis hit expected)
    def recommend_warm_user(self):
        user_id = random.choice(WARM_USER_IDS)
        self._post_recommend(user_id)

    @task(1)  # 5% of requests — cold-start users (no Redis key)
    def recommend_cold_start_user(self):
        user_id = random.choice(COLD_USER_IDS)
        self._post_recommend(user_id)

    def _post_recommend(self, user_id: str):
        payload = {
            "user_id": user_id,
            "context": {
                "platform": random.choice(PLATFORMS),
                "screen": random.choice(SCREENS),
                "session_id": f"sess_{uuid.uuid4().hex[:10]}",
            },
            "max_results": 20,
        }
        with self.client.post(
            "/v1/recommend",
            json=payload,
            catch_response=True,
            name="POST /v1/recommend",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 429:
                # Rate-limited — treat as expected, not a failure
                resp.success()
            else:
                resp.failure(
                    f"Unexpected status {resp.status_code}: {resp.text[:200]}"
                )
