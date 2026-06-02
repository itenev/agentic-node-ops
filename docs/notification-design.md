# Notification Design — Two-Tier

| Tier | Channel | Trigger | Purpose |
|---|---|---|---|
| 1 | Discord | All severities | Rich embed with full context, runbook link, diagnostics, approval prompts |
| 2 | ntfy.sh | Critical + slashing only | Urgent push notification bypassing iOS/Android silent mode |

## Severity Routing

```
low / medium / high  →  Discord only
critical             →  Discord + ntfy (Priority: urgent)
slashing alerts      →  Discord + ntfy (Priority: urgent), regardless of severity field
                        (meta-category: ValidatorDoubleInstance,
                         SlashingProtectionDBError, ClockSkewExcessive)
```

## Discord

Primary operator interface. Renders rich embeds with severity colour, diagnostics as inline fields, proposed actions, and forensic evidence paths for slashing incidents.

**Mention behaviour:**

- `@here` for `critical` severity alerts — notifies online members only
- `@here` for slashing alerts — same; do not use `@everyone`

`@everyone` notifies all server members regardless of online status or their own notification settings. For a single-operator server this has no immediate effect, but it becomes a problem the moment anyone else is added to the server. `@here` is sufficient to trigger push notifications on mobile for the operator and is the correct default. If `@everyone` is explicitly wanted for slashing, make it an opt-in environment variable (`DISCORD_SLASHING_MENTION=everyone`), defaulting to `@here`.

**Current implementation:** webhook (send-only). Returns Discord message ID for future edits (marking resolved).

**Phase 4 migration:** swap webhook transport for Bot API to support interaction buttons for the approval flow. Payload shape (content + embeds) is identical — only the transport changes. The Bot token must be provisioned before Phase 4 rollout.

## ntfy.sh

Single HTTP POST per critical alert. `Priority: urgent` bypasses iOS Do Not Disturb and Android priority settings. Topic name acts as the shared secret — use a long random string.

```bash
# Example: what ntfy sends for a critical alert
curl -d "Lighthouse is 184 slots behind — validator-01" \
     -H "Title: CONSENSUS DESYNC — validator-01" \
     -H "Priority: urgent" \
     -H "Tags: rotating_light,consensus_desync" \
     https://ntfy.sh/${NTFY_TOPIC}
```

Self-hosted ntfy is supported — set `NTFY_SERVER_URL` to your own instance.

## Environment Variables

```bash
DISCORD_WEBHOOK_URL        # required — Discord incoming webhook URL
DISCORD_SLASHING_MENTION   # optional — "everyone" to use @everyone for slashing; default: "here"
NTFY_TOPIC                 # required — random string, acts as shared secret
NTFY_SERVER_URL            # optional — defaults to https://ntfy.sh
NTFY_USERNAME              # optional — for authenticated self-hosted ntfy
NTFY_PASSWORD              # optional
```

---

## Related Documents

- [Architecture](architecture.md) — system design, socket-proxy pattern
- [Slashing Protocol](slashing-protocol.md) — slashing notification requirements
- [Webhook Receiver Spec](webhook-receiver-spec.md) — alert ingestion pipeline
- [Runbook Spec](runbook-spec.md) — approval flow (Phase 4 Bot API migration)
