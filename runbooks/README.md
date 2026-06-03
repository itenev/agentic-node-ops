# Runbooks for Ethereum Node Monitoring

See [docs/runbook-spec.md](../docs/runbook-spec.md) for the runbook schema.

## Available Runbooks

| Runbook | Alert Type | Severity |
|---|---|---|
| [consensus_desync.yaml](consensus_desync.yaml) | consensus_desync | high+ |

## Adding New Runbooks

1. Create `<alert_name>.yaml` in this directory
2. Follow the schema in `../docs/runbook-spec.md`
3. Include: `id`, `triggers`, `diagnostics`, `suggested_actions`
4. `privileged_actions` are locked until Phase 4+
