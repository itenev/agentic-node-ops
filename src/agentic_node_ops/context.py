"""Context assembly for Hermes prompts."""

import json

from .database import Database
from .types import NotificationPayload


def build_hermes_context(payload: NotificationPayload, db: Database) -> str:
    """
    Assemble the context prompt for Hermes analysis.

    Combines current state, recent incident history, operator corrections,
    runbook performance, and host baselines into a single structured prompt.
    """
    # Recent incidents
    recent_incidents = db.get_recent_incidents(
        alert_type=payload.alert_type, host=payload.host, limit=5
    )
    if recent_incidents:
        incidents_text = "\n".join(
            f"- {inc['fired_at']}: {inc['outcome'] or 'unresolved'} "
            f"(severity: {inc['severity']}, analysis: {(inc.get('hermes_analysis') or 'N/A')[:100]}{'...' if len(inc.get('hermes_analysis') or '') > 100 else ''})"
            for inc in recent_incidents
        )
    else:
        incidents_text = (
            "- No prior incidents recorded for this alert type on this host."
        )

    # Operator corrections
    corrections = db.get_corrections(alert_type=payload.alert_type, host=payload.host)
    if corrections:
        corrections_text = "\n".join(f"- {c}" for c in corrections)
    else:
        corrections_text = "- No operator corrections recorded."

    # Runbook stats
    runbook_id = payload.runbook_id or "unknown"
    stats = db.get_runbook_stats(runbook_id)
    success_rate_pct = stats["success_rate"] * 100
    failed_cases_text = "\n".join(f"- {case}" for case in stats["failed_cases"])

    # Host baselines
    baselines = db.get_host_baselines(host=payload.host)
    if baselines:
        baselines_text = "\n".join(
            f"- {metric}: p50={data['p50']}, p95={data['p95']}"
            for metric, data in baselines.items()
        )
    else:
        baselines_text = "- No baselines recorded for this host."

    # Diagnostics snapshot
    diagnostics_text = (
        json.dumps(payload.diagnostics, indent=2)
        if payload.diagnostics
        else "No diagnostics available."
    )

    prompt = f"""You are analyzing a {payload.alert_type} alert on {payload.host}.

CURRENT STATE:
{diagnostics_text}

RECENT INCIDENT HISTORY (last 5 similar incidents on this host):
{incidents_text}

OPERATOR CORRECTIONS FOR THIS ALERT TYPE ON THIS HOST:
{corrections_text}

RUNBOOK PERFORMANCE:
Runbook '{runbook_id}' has resolved this alert type {success_rate_pct:.0f}% of the time.
Known failure cases:
{failed_cases_text}

HOST BASELINES:
{baselines_text}

Your job: explain what is likely happening, why, and what the operator should do.
Be specific. If this matches a pattern from history, say so explicitly.
"""
    return prompt
