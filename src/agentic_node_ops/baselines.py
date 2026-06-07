"""Host baseline learning from Prometheus metrics.

Nightly job to compute p50/p95 baselines from Prometheus query_range
and store them in the host_fingerprints table.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import urllib.parse
import urllib.request
from typing import Optional

from .database import Database

log = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")


def _query_prometheus_range(
    query: str, start: str, end: str, step: str = "1h"
) -> list[float]:
    """Query Prometheus for a range of values and return a flat list of floats."""
    url = (
        f"{PROMETHEUS_URL}/api/v1/query_range?"
        f"query={urllib.parse.quote(query)}&start={start}&end={end}&step={step}"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            result = data.get("data", {}).get("result", [])
            values = []
            for series in result:
                for point in series.get("values", []):
                    if len(point) == 2:
                        try:
                            values.append(float(point[1]))
                        except (ValueError, TypeError):
                            pass
            return values
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
    ) as e:
        log.warning("Prometheus range query failed for %s: %s", query, e)
        return []


def compute_percentiles(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    """Compute p50 and p95 from a list of values."""
    if not values:
        return None, None

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    # p50 (median)
    p50 = statistics.median(sorted_vals)

    # p95
    p95_idx = int(n * 0.95)
    if p95_idx >= n:
        p95_idx = n - 1
    p95 = sorted_vals[p95_idx]

    return p50, p95


def update_host_baselines(
    db: Database,
    host: str,
    metrics: list[str],
    start: str,
    end: str,
    step: str = "1h",
) -> dict[str, tuple[Optional[float], Optional[float]]]:
    """
    Query Prometheus for each metric, compute p50/p95, and upsert to DB.

    Returns a dict mapping metric name to (p50, p95).
    """
    results = {}
    for metric in metrics:
        # Add host label if not already in the query
        if "host=" not in metric and "instance=" not in metric:
            query = f'{metric}{{host="{host}"}}'
        else:
            query = metric

        values = _query_prometheus_range(query, start, end, step)
        p50, p95 = compute_percentiles(values)
        results[metric] = (p50, p95)

        if p50 is not None and p95 is not None:
            db.upsert_host_baseline(host, metric, p50, p95)
            log.info(
                "Updated baseline for %s on %s: p50=%.2f, p95=%.2f",
                metric,
                host,
                p50,
                p95,
            )
        else:
            log.warning(
                "No data found for %s on %s, skipping baseline update", metric, host
            )

    return results
