## Summary
<!-- 1-2 sentence overview of what this PR accomplishes -->

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## Pre-Review Findings and Resolutions
<!-- Hermes pre-review checklist. Fill this out before requesting human review. -->
- [ ] Linting/formatting (`ruff check --fix && ruff format`) applied *before* pre-review.
- [ ] Any runbook loading/YAML changes include an end-to-end test loading the physical file.
- [ ] Design contracts verified (single-writer boundary, offset-after-commit, fail-open dedup, Connection: close, no `for` clause on slashing).
- **Findings flagged by Hermes**: 
  - <!-- e.g., "Added missing 'description' field to RunbookDiagnostic" -->
- **Resolutions applied**:
  - <!-- e.g., "Patched dataclass and added test_load_actual_consensus_desync_runbook" -->

## Testing
- [ ] All existing tests pass (`pytest -v --cov=agentic_node_ops --cov=webhook_receiver`)
- [ ] New tests added for new functionality
- [ ] Manual verification steps (if applicable): <!-- e.g., "Tested webhook receiver with mock Alertmanager payload" -->

## Checklist
- [ ] My code follows the style guidelines of this project
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings or errors