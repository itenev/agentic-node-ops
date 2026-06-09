# Phase 5 — Runbook Synthesis Design

> Advisory only · Human review gate mandatory · Implementation gated on production data

---

## 1. Objective

Analyse historical incident data accumulated during Phases 1–4 to automatically generate
draft YAML runbooks for alert types that recur with consistent resolution patterns.
Drafts are placed in `runbooks/drafts/` and require explicit operator review and promotion
before they influence live alert handling.

**Synthesis never modifies live runbooks and never executes actions.**

---

## 2. Core Principles

- **Advisory, not autonomous.** Drafts are suggestions. An operator must read, edit,
  and promote each one.
- **Conservative thresholds.** Better to produce no draft than a low-quality one.
  Premature synthesis on thin data produces dangerous runbooks.
- **Transparent provenance.** Every draft carries a metadata header showing exactly
  what data it was derived from. An operator should be able to reproduce the reasoning.
- **Slashing safety carve-out.** Slashing-adjacent alert types
  (`ValidatorDoubleInstance`, `SlashingProtectionDBError`, `ClockSkewExcessive`) are
  excluded from action synthesis permanently. Diagnostics may be synthesised;
  `suggested_actions` may not.

---

## 3. Minimum Data Requirements

Synthesis for a given `(alert_type, host)` pair is attempted only when all of the
following are satisfied:

| Requirement | Threshold | Rationale |
|---|---|---|
| Resolved incidents | >= 10 | Minimum for statistical validity |
| Resolution rate | >= 70% | Pattern must be reliably solvable |
| Action consistency | >= 60% | One action must dominate resolutions |
| Data age | >= 7 days old | Prevents synthesis from a transient burst |

**Resolution rate** = resolved incidents / total incidents for this pair.

**Action consistency** = count of incidents resolved with the dominant action /
total resolved incidents. Computed over `runbook_outcomes.action_taken` joined to
resolved incidents.

If any threshold is not met, synthesis is skipped for that pair and a debug log
entry is written. No notification is sent for skipped pairs.

---

## 4. Synthesis Algorithm

### Step 1 — Identify candidate patterns

```sql
SELECT
    alert_type,
    host,
    COUNT(*)                                                        AS total_incidents,
    SUM(CASE WHEN outcome = 'resolved' THEN 1 ELSE 0 END)          AS resolved_count,
    CAST(
        SUM(CASE WHEN outcome = 'resolved' THEN 1 ELSE 0 END) AS FLOAT
    ) / COUNT(*)                                                    AS resolution_rate,
    MIN(fired_at)                                                   AS first_seen,
    MAX(fired_at)                                                   AS last_seen
FROM incidents
WHERE
    -- Exclude slashing-adjacent alert types (never synthesise actions)
    alert_type NOT IN (
        'ValidatorDoubleInstance',
        'SlashingProtectionDBError',
        'ClockSkewExcessive'
    )
    -- Exclude incidents from the last 7 days (require data age)
    AND fired_at < datetime('now', '-7 days')
GROUP BY alert_type, host
HAVING
    COUNT(*) >= 10
    AND resolution_rate >= 0.70
ORDER BY resolution_rate DESC, total_incidents DESC;
```

### Step 2 — Extract the dominant action

For each candidate pair, query `runbook_outcomes` for resolved incidents:

```python
def get_dominant_action(alert_type: str, host: str, db: Database) -> dict | None:
    """
    Returns the most frequently successful action for this pair,
    or None if action_consistency < 0.60.
    """
    rows = db.execute("""
        SELECT
            ro.action_taken,
            COUNT(*)                                                AS usage_count,
            SUM(CASE WHEN ro.outcome = 'resolved' THEN 1 ELSE 0 END) AS resolved_count
        FROM runbook_outcomes ro
        JOIN incidents i ON i.runbook_used = ro.runbook_id
        WHERE i.alert_type = ?
          AND i.host       = ?
          AND i.outcome    = 'resolved'
          AND ro.action_taken IS NOT NULL 
          AND ro.action_taken != ''
        GROUP BY ro.action_taken
        ORDER BY resolved_count DESC
        LIMIT 1
    """, (alert_type, host)).fetchall()

    if not rows:
        return None

    dominant    = rows[0]
    total_resolved = db.execute(
        "SELECT COUNT(*) FROM incidents WHERE alert_type=? AND host=? AND outcome='resolved'",
        (alert_type, host)
    ).fetchone()[0]

    action_consistency = dominant["resolved_count"] / total_resolved
    if action_consistency < 0.60:
        log.debug(
            "Skipping synthesis for %s/%s: action_consistency %.2f < 0.60",
            alert_type, host, action_consistency
        )
        return None

    return {
        "action_id":          dominant["action_taken"],
        "action_consistency": action_consistency,
        "usage_count":        dominant["usage_count"],
    }
```

### Step 3 — Compute confidence score

```python
def compute_confidence(
    resolution_rate:    float,
    action_consistency: float,
    total_incidents:    int,
) -> float:
    """
    Weighted confidence in [0.0, 1.0].

    Weights:
      50% — resolution rate   (does fixing this alert type work reliably?)
      30% — action consistency (is there one dominant fix?)
      20% — sample size        (capped at 20 incidents, diminishing returns beyond)
    """
    sample_weight = min(total_incidents / 20.0, 1.0)
    return round(
        resolution_rate    * 0.50 +
        action_consistency * 0.30 +
        sample_weight      * 0.20,
        3,
    )
```

### Step 4 — Collect operator corrections

```python
corrections = db.execute("""
    SELECT correction, created_at
    FROM operator_corrections
    WHERE alert_type = ? AND host = ?
    ORDER BY created_at ASC
""", (alert_type, host)).fetchall()
```

Corrections are included as YAML comments in the draft so the operator can see exactly
what past feedback influenced the synthesis.

### Step 5 — Compute resolution time baseline

```python
avg_resolve_seconds = db.execute("""
    SELECT AVG(duration_to_resolve)
    FROM incidents
    WHERE alert_type = ? AND host = ? AND outcome = 'resolved'
      AND duration_to_resolve IS NOT NULL
""", (alert_type, host)).fetchone()[0]
```

Used to populate `approval_timeout` with a data-driven default rather than a hardcoded
30 minutes.

### Step 6 — Generate draft YAML

```python
def render_draft(
    alert_type:         str,
    host:               str,
    dominant_action:    dict,
    corrections:        list,
    resolution_rate:    float,
    action_consistency: float,
    total_incidents:    int,
    avg_resolve_seconds: float | None,
) -> str:
    confidence    = compute_confidence(resolution_rate, action_consistency, total_incidents)
    approval_mins = max(30, int((avg_resolve_seconds or 1800) / 60) + 15)

    correction_block = ""
    if corrections:
        correction_block = "\n# Operator corrections incorporated:\n"
        for c in corrections:
            correction_block += f"#   [{c['created_at'][:10]}] {c['correction']}\n"

    return f"""\
# synthesis_metadata:
#   synthesized_at:          {datetime.utcnow().isoformat()}Z
#   alert_type:              {alert_type}
#   host:                    {host}
#   based_on_incidents:      {total_incidents}
#   resolution_rate:         {resolution_rate:.0%}
#   action_consistency:      {action_consistency:.0%}
#   confidence:              {confidence}
#   corrections_incorporated:{len(corrections)}
#   status:                  draft
#
# REVIEW REQUIRED before promotion. Do not promote without:
#   1. Verifying the suggested action is safe for this host.
#   2. Adding appropriate pre_conditions.
#   3. Confirming the diagnostics cover the failure mode.
#
# Promote with: python -m agentic_node_ops.synthesis promote {alert_type}
{correction_block}
id: {alert_type}
triggers:
  - alert_type: {alert_type}
    min_severity: high  # Review: adjust if lower severity is appropriate

diagnostics:
  # Review: add host-specific diagnostic commands before promoting
  - id: check_container
    cmd: "docker inspect {alert_type.replace('_', '-')} --format '{{{{.State.Status}}}}'"
    timeout: 5s
    description: "Verify container state"

suggested_actions:
  - id: {dominant_action['action_id']}
    description: "Synthesised from {total_incidents} historical incidents ({resolution_rate:.0%} resolution rate)"
    cmd: ""  # REQUIRED: add the actual command before promoting
    risk: low         # Review: confirm risk level
    reversible: true  # Review: confirm reversibility
    requires_approval: true
    approval_timeout: {approval_mins}m
    pre_conditions: []  # Review: add pre_conditions (e.g. container_status == running)

privileged_actions: []
"""
```

---

## 5. Draft File Management

### Naming convention

```
runbooks/drafts/{alert_type}.yaml
```

One file per `alert_type`, not per `(alert_type, host)`. If synthesis has data from
multiple hosts for the same alert type, it uses the host with the highest confidence
score and notes the others in the metadata comment.

Rationale: runbooks in the live directory are keyed by `alert_type` (the matcher uses
`alert_type` as the runbook `id`). Drafts should mirror this convention so promotion
is a simple file copy.

### Overwrite policy

```python
def should_overwrite_draft(draft_path: Path, new_confidence: float) -> bool:
    """
    Overwrite an existing draft only if the new confidence is strictly higher.
    Prevents lower-quality re-synthesis from replacing a well-regarded draft
    that the operator hasn't yet had time to review.
    """
    if not draft_path.exists():
        return True
    existing = yaml.safe_load(draft_path.read_text())
    # Parse confidence from metadata comment block
    existing_confidence = _parse_confidence_from_comments(draft_path.read_text())
    return new_confidence > existing_confidence
```

### Staleness

Drafts older than 30 days without operator action are flagged in the daily Discord
notification as stale. They are never auto-deleted — stale detection is advisory only.

---

## 6. Human Review Gate

### Daily synthesis run

The synthesis job runs as a cron task at 02:00 UTC. It is non-blocking — if it fails,
it logs an error and exits without affecting alert processing.

```
Synthesis job
    │
    ├── Query candidates (Step 1)
    ├── For each candidate:
    │     ├── Get dominant action (Step 2)
    │     ├── Compute confidence (Step 3)
    │     ├── Collect corrections (Step 4)
    │     ├── Compute resolve time baseline (Step 5)
    │     └── Write/overwrite draft if confidence improves (Step 6)
    │
    └── Send Discord summary
```

### Discord notification format

```
📋 Runbook Synthesis — daily report

New drafts:
  • consensus_desync (confidence: 0.81, based on 14 incidents)

Updated drafts (confidence improved):
  • client_crash (0.67 → 0.74, 3 new incidents)

Stale drafts (> 30 days, no operator action):
  • validator_duty_misses (synthesised 2025-01-01, confidence: 0.71)

Review drafts: runbooks/drafts/
Promote a draft: python -m agentic_node_ops.synthesis promote <alert_type>
```

### Promotion CLI

```bash
# Review a draft
cat runbooks/drafts/consensus_desync.yaml

# Promote (copies to runbooks/, strips synthesis metadata comments,
#          creates a backup of the existing runbook if one exists)
python -m agentic_node_ops.synthesis promote consensus_desync

# Discard a draft (removes from drafts/, records discard in DB)
python -m agentic_node_ops.synthesis discard consensus_desync --reason "not enough context"
```

**Promotion behaviour:**

1. Read `runbooks/drafts/{alert_type}.yaml`
2. Strip `# synthesis_metadata:` comment block from the top
3. If `runbooks/{alert_type}.yaml` already exists, move it to
   `runbooks/archive/{alert_type}_{timestamp}.yaml` before overwriting
4. Write promoted file to `runbooks/{alert_type}.yaml`
5. Record the promotion in a new `synthesis_events` table:
   ```sql
   INSERT INTO synthesis_events
     (alert_type, host, confidence, proposed_action, event_type, created_at)
   VALUES (?, ?, ?, ?, 'promoted', CURRENT_TIMESTAMP);
   ```
6. Notify operator via Discord: "✅ Promoted `consensus_desync` runbook (confidence: 0.81)"

**Discard behaviour:**

1. Remove `runbooks/drafts/{alert_type}.yaml`
2. Record in `synthesis_events` with `event_type = 'discarded'` and the reason
3. Increment a discard counter. If a pattern is discarded 3 times, it is added to a
   `synthesis_blocklist` table and will never be synthesised again without manual removal.

---

## 7. Refinement of Existing Runbooks

When a candidate pattern already has a live runbook in `runbooks/`, synthesis computes
whether the historical data suggests a different action than what the runbook currently
specifies:

```python
def should_refine(alert_type: str, existing_runbook: Runbook, dominant_action: dict) -> bool:
    existing_action_ids = [a.id for a in existing_runbook.suggested_actions]
    # Refinement is suggested if the dominant historical action is NOT
    # already in the existing runbook's suggested_actions
    return dominant_action["action_id"] not in existing_action_ids
```

If refinement is suggested, the draft is written to
`runbooks/drafts/{alert_type}_refined.yaml` (not `{alert_type}.yaml`) to avoid
overwriting the existing live runbook's draft slot.

The Discord notification explicitly labels refined drafts:
```
🔄 Refined drafts (existing runbook may need updating):
  • consensus_desync_refined (dominant action differs from live runbook)
```

---

## 8. Edge Case Handling

| Situation | Behaviour |
|---|---|
| Action consistency < 60% (contradictory outcomes) | Skip synthesis for this pair; log debug. Threshold not met. |
| `action_taken` is empty/null (operator fixed manually) | Exclude from action analysis. Count in resolution_rate but not action_consistency. |
| Dominant action involves a slashing-adjacent alert type | Synthesis of `suggested_actions` is blocked permanently. Diagnostics-only draft may still be generated if diagnostic commands can be inferred from `hermes_analysis`. |
| Two hosts have same alert_type, different dominant actions | Synthesise separately per host, use highest-confidence host for `runbooks/drafts/{alert_type}.yaml`. Note the other host in metadata comments. |
| Draft already exists with higher confidence than new synthesis | Do not overwrite. Log info: "Existing draft has higher confidence, skipping." |
| Pattern discarded 3+ times | Add to `synthesis_blocklist`. Never re-synthesise without manual intervention. |
| `avg_resolve_seconds` is NULL (no timing data) | Default `approval_timeout` to 30 minutes. |
| Synthesis job fails mid-run | Partial drafts are not written. Each draft is written atomically (tempfile + rename). |

---

## 9. New Database Schema

```sql
CREATE TABLE synthesis_events (
    id                TEXT PRIMARY KEY,
    alert_type        TEXT NOT NULL,
    host              TEXT NOT NULL,
    confidence        REAL,
    proposed_action   TEXT,           -- The action_id that was promoted/discarded
    event_type        TEXT NOT NULL,  -- 'synthesised' | 'promoted' | 'discarded' | 'skipped'
    reason            TEXT,           -- populated on 'discarded' and 'skipped'
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE synthesis_blocklist (
    alert_type  TEXT NOT NULL,
    host        TEXT NOT NULL,
    reason      TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (alert_type, host)
);

CREATE INDEX idx_synthesis_events_alert_host
    ON synthesis_events(alert_type, host);
```

---

## 10. New Source Files

| File | Purpose |
|---|---|
| `src/agentic_node_ops/synthesis.py` | Core synthesis algorithm, draft generation, promote/discard CLI |
| `src/agentic_node_ops/scheduler.py` | Cron runner wiring synthesis job to 02:00 UTC daily |
| `runbooks/drafts/.gitkeep` | Ensure directory exists in repo |
| `tests/test_synthesis.py` | Unit tests for candidate query, confidence scoring, draft rendering, edge cases |

---

## 11. Implementation Gate

**Do not implement until:**

- Functional testing is complete and the stack is running in production
- At minimum 10 resolved incidents of at least one `alert_type` have been recorded
  in the production SQLite database
- The `runbook_outcomes` table is being populated (requires the approval flow in Phase 4
  to have been exercised at least once)

The synthesis code can be written and tested against synthetic fixture data, but the
confidence thresholds should be tuned against real data before the first production
synthesis run.

---

## 12. Key Design Constraints

- Synthesis is a background job — it never blocks alert processing
- Drafts are written atomically (tempfile + rename) — no partial files on crash
- Slashing-adjacent alert types are permanently excluded from action synthesis
- Promotion requires explicit operator CLI action — no auto-promotion under any condition
- A pattern discarded 3 times is blocklisted permanently without manual override
- Confidence score is always written in the draft metadata — operator must see the basis
  for synthesis before deciding to promote