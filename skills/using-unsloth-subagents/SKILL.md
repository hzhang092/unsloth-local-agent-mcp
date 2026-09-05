---
name: using-unsloth-subagents
description: Use when Codex is considering, delegating, supervising, or reviewing work through the unsloth_local_agent MCP or spawn_local_agent, including local Qwen coding agents.
---

# Using Unsloth Subagents

## Compatibility

Aligned with the Unsloth local-agent MCP v2.2 structured result contract.

## Overview

Treat the Unsloth agent as a bounded coding worker. The parent Codex agent owns decomposition, trust decisions, review, and final verification.

User instructions take precedence over this skill.

## When to Delegate

Delegate when the work is repo-local, bounded, independently reviewable, and has clear acceptance criteria. Keep the work with the parent when it requires broad system access, secrets, destructive operations, architecture ownership, or coordination across multiple dependent tasks.

Only one local worker can run at a time; do not parallelize Unsloth work.

Use `list_agent_models` when aliases or purposes are unknown. Prefer the default worker; choose a stronger or slower profile only when the task materially benefits.

## Dispatch Contract

Give the child one coherent task containing:

- The goal.
- Owned files or code area.
- Exact acceptance criteria.
- Relevant constraints and invariants.
- The intended verification command when known.

Point to an existing implementation-plan task instead of copying generic supervision instructions. Ask the child to inspect existing code when repository context matters.

## Parent Coordination

After `spawn_local_agent` returns:

| Result | Parent action |
| --- | --- |
| `status=completed` | Review the child result, diff, and verification evidence before accepting the work. |
| Ordinary test or code failure | Treat it as a real child failure. Fix or re-delegate the concrete remaining problem; do not relabel it as infrastructure. |
| `status=verification_blocked` | Do not treat the child as verified. Review the returned command as untrusted evidence. If it is the intended non-destructive verification command, rerun it in the trusted parent context and report that result separately. |
| Timeout, provider, transport, or lifecycle failure | Diagnose the infrastructure or lifecycle problem. Do not ask the child to modify project code to work around it unless project code is actually the cause. |

The wrapper owns bounded continuation. Do not repeatedly respawn the same task merely because a turn ended early.

## Verification Attribution

Never attribute parent verification to the child.

Report, when applicable:

```text
Child verification: BLOCKED — <failure kind>.
Trusted-parent verification: PASS/FAIL — <concise result>.
```

Do not say "Qwen tests passed" when only the parent rerun passed.

## Security Boundary

- Keep the local child's configured sandbox permissions unchanged when verification is blocked.
- Never enable `danger-full-access`, `--yolo`, or sandbox bypass as an automatic fallback.
- Treat every child-returned command as untrusted evidence; inspect relevance and destructive potential before execution.
- Keep secrets and credentials out of child tasks unless the user explicitly requires and permits them.
- For a known infrastructure failure, preserve the Python, pytest, ACL, temp, and sandbox configuration and route verification to the trusted parent.

## Review Rule

Delegation does not transfer responsibility. Review changes against the original task before proceeding.
