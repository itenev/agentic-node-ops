# Runbooks for Ethereum node monitoring

# See docs/hermes-agent-design.md §5 for the runbook schema

# Example runbook (to be implemented)

# id: consensus_desync

# triggers

# - alert_type: consensus_desync

# min_severity: high

# diagnostics

# - id: fetch_sync_status

# cmd: "curl -s <http://consensus:5052/eth/v1/node/syncing>"

# suggested_actions

# - id: restart_consensus_client

# cmd: "docker restart consensus"

# requires_approval: true
