"""
Load test result assertion script — Scenario X: Personalized In-App Recommendations
Reads the Locust CSV output and validates against SLO thresholds from serving/slos.yaml.

Usage:
    python tests/load/assert_results.py results/load_test_stats.csv

Exit codes:
    0 — all pass criteria met
    1 — one or more criteria failed (blocks CI/CD promotion step)

Pass criteria (from serving/load-test-plan.md, Scenario 2):
    - p95 latency    <= 120 ms  (SLO-2: serving/slos.yaml)
    - error rate      < 0.1%   (SLO-1: serving/slos.yaml)
"""

import csv
import sys


# ---------------------------------------------------------------------------
# Thresholds — must match serving/slos.yaml and serving/load-test-plan.md
# ---------------------------------------------------------------------------
P95_LATENCY_THRESHOLD_MS = 120.0   # SLO-2
ERROR_RATE_THRESHOLD_PCT = 0.1     # SLO-1 (percentage, not fraction)


def parse_locust_stats(csv_path: str) -> dict:
    """
    Parse the Locust *_stats.csv file.
    Returns a dict with aggregated metrics for the 'POST /v1/recommend' row.
    Falls back to the 'Aggregated' row if the specific endpoint is not found.
    """
    rows = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row.get("Name", "")] = row

    # Prefer the specific endpoint row; fall back to Aggregated
    target = rows.get("POST /v1/recommend") or rows.get("Aggregated")
    if not target:
        print(f"ERROR: No usable row found in {csv_path}")
        sys.exit(1)

    return target


def main():
    if len(sys.argv) != 2:
        print("Usage: python assert_results.py <path/to/load_test_stats.csv>")
        sys.exit(1)

    csv_path = sys.argv[1]
    print(f"Reading load test results from: {csv_path}\n")

    row = parse_locust_stats(csv_path)

    # ------------------------------------------------------------------
    # Extract metrics
    # Locust CSV columns: "95%", "# Requests", "# Failures"
    # Latency values are in milliseconds.
    # ------------------------------------------------------------------
    try:
        p95_ms = float(row["95%"])
        total_requests = int(row["Request Count"])
        total_failures = int(row["Failure Count"])
    except (KeyError, ValueError) as exc:
        print(f"ERROR: Could not parse CSV columns — {exc}")
        print(f"Available columns: {list(row.keys())}")
        sys.exit(1)

    error_rate_pct = (total_failures / total_requests * 100) if total_requests else 0.0

    print("=" * 55)
    print("  Load Test Results — Scenario 2 (Peak Load, 800 RPS)")
    print("=" * 55)
    print(f"  Total requests : {total_requests:,}")
    print(f"  Total failures : {total_failures:,}")
    print(f"  Error rate     : {error_rate_pct:.3f}%  (threshold: < {ERROR_RATE_THRESHOLD_PCT}%)")
    print(f"  p95 latency    : {p95_ms:.1f} ms  (threshold: <= {P95_LATENCY_THRESHOLD_MS} ms)")
    print("=" * 55)

    # ------------------------------------------------------------------
    # Assert pass criteria
    # ------------------------------------------------------------------
    failures = []

    if p95_ms > P95_LATENCY_THRESHOLD_MS:
        failures.append(
            f"FAIL  p95 latency {p95_ms:.1f} ms exceeds {P95_LATENCY_THRESHOLD_MS} ms SLO (SLO-2)"
        )
    else:
        print(f"  PASS  p95 latency {p95_ms:.1f} ms <= {P95_LATENCY_THRESHOLD_MS} ms")

    if error_rate_pct >= ERROR_RATE_THRESHOLD_PCT:
        failures.append(
            f"FAIL  error rate {error_rate_pct:.3f}% >= {ERROR_RATE_THRESHOLD_PCT}% threshold (SLO-1)"
        )
    else:
        print(f"  PASS  error rate {error_rate_pct:.3f}% < {ERROR_RATE_THRESHOLD_PCT}%")

    print()
    if failures:
        print("LOAD TEST FAILED — promotion gate blocked:")
        for msg in failures:
            print(f"  {msg}")
        print()
        print("See serving/load-test-plan.md for remediation guidance.")
        print("Rollback procedure: runbooks/rollback.md")
        sys.exit(1)
    else:
        print("LOAD TEST PASSED — all SLO pass criteria met.")
        print("Promotion gate: cleared for deploy-production step.")
        sys.exit(0)


if __name__ == "__main__":
    main()
