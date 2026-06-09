# Agentic Node Ops — Design Overview

> Single-operator · Docker/eth-docker · Read-only observability first · Minimal operational complexity

This document is the table of contents for the design documents. Each sub-document covers a specific aspect of the system.

---

## Core Principle

**Prometheus Detects. Hermes Reasons.**

Deterministic alerting for known failures. LLM-driven contextual analysis for everything else.

---

## Documents

| Document | Covers |
|---|---|
| [Architecture](architecture.md) | System diagram, four-plane architecture, deployment topology, socket-proxy pattern, tech choices, phased roadmap, design constraints |
| [Webhook Receiver Spec](webhook-receiver-spec.md) | Alert ingestion, queue design, single-writer boundary, deduplication logic, context snapshot fetch, alert storm protection, self-monitoring, normalized alert schema |
| [Slashing Protocol](slashing-protocol.md) | Slashing detection surface, detection rules, Hermes response protocol (4 steps), evidence preservation, what Hermes must NEVER do |
| [Runbook Spec](runbook-spec.md) | Three-tier action classification, approval state machine, approval fatigue prevention (cooldowns, escalation, grouping), approval message cadence rules |
| [Memory and Feedback](memory-and-feedback.md) | Database schema (incidents, host_fingerprints, operator_corrections, runbook_outcomes, action_proposals), context assembly for Hermes prompts, post-incident feedback collection, host baseline learning |
| [Alert Set](alert-set.md) | Initial alert set (consensus desync, duty misses, slashing risk, client crash), telemetry plane health alerts |
| [Notification Design](notification-design.md) | Two-tier notification routing (Discord + ntfy.sh), severity routing, Discord embed design, mention behaviour, ntfy.sh setup, environment variables |

---

## Phased Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Webhook receiver + alert normalization + jsonl queue | Design complete |
| 2 | Hermes integration + runbook matching + operator notifications | Design complete |
| 3 | Memory layer + feedback loop + host fingerprints | Design complete |
| 4 | Tier 2 suggested actions + approval state machine + socket-proxy migration + Discord Bot API migration | Design complete |
| 5 | Runbook synthesis from historical incidents | Design pending |
| 6 | Semi-autonomous remediation with confidence scoring | Future |

---

## Implementation Plan

See [hermes-implementation-plan.md](hermes-implementation-plan.md) for the task-by-task implementation roadmap with status tracking.
