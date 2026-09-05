# Windows Sandbox Verification Fallback v2.2 Implementation Plan

Date: 2026-09-04

## Goal

Keep local Qwen children inside Codex `workspace-write` while detecting the confirmed Windows/Python private-directory ACL verification failure and handing the exact blocked verification back to the trusted parent for separately attributed rerun.

## Architecture

Preserve the existing `codex exec` child, bounded continuation, cleanup, single-flight, and stream-hardening architecture. Extend JSONL parsing only enough to retain bounded command-result evidence, classify only the confirmed Windows pytest private-ACL signature, and convert that condition into a non-error `verification_blocked` agent result. The MCP never broadens Qwen permissions or executes verification outside the sandbox; it asks the already-trusted parent to review and rerun the blocked verification.

The approved specification is `.ai-bridge/2026-09-04-codex-mcp-windows-sandbox.md`. It is read-only during this implementation.

## Global constraints

- Keep `bypass_permissions=false` as `--sandbox workspace-write --ask-for-approval never`.
- Do not add `danger-full-access`, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, or automatic permission broadening.
- Do not monkeypatch or configure around Python/pytest, temp roots, or `--basetemp`.
- Do not migrate to app-server/thread runtime in v2.2.
- Do not add an MCP-hosted privileged verifier.
- Never execute a command merely because the child returned it. The trusted parent must inspect it first.
- Classify only the confirmed Windows pytest restricted-token/private-ACL signature, named `windows_restricted_token_private_acl`.
- Preserve exact command, exit code, thread ID, and bounded error/path evidence.
- Preserve all local v2.1 stream, continuation, timeout, cleanup, cancellation, and single-flight behavior.
- Never expose the API key from Unsloth `subagent.json`.
- Modify only `server.py`, `test_server.py`, and `README.md`; do not modify `models.json`.

## Interfaces

Add `_CommandResult`, `_VerificationBlock`, `_AgentState`, and `_AgentResult`. Extend `_TurnResult` with `commands: tuple[_CommandResult, ...]`.

Add constants:

```python
_WINDOWS_PRIVATE_ACL_KIND = "windows_restricted_token_private_acl"
_MAX_COMMAND_OUTPUT_TAIL = 8192
_MAX_VERIFICATION_EVIDENCE = 4096
```

Add `_command_result()`, `_find_verification_block()`, and `_public_agent_result()`.

The classifier matches only when all of these hold:

- Windows execution.
- Command contains `pytest`, case-insensitively.
- Exit code is non-zero or status is `failed`.
- Output contains `PermissionError`.
- Output contains `WinError 5` or `Access is denied`.
- Output contains `pytest-of-`.

It must not classify ordinary pytest assertion failures, application permission bugs, non-pytest commands, or non-Windows execution.

## Public result contract

Completed:

```json
{
  "status": "completed",
  "message": "child final message",
  "thread_id": "...",
  "child_verification": {"status": "completed"}
}
```

Infrastructure blocked:

```json
{
  "status": "verification_blocked",
  "message": "child final/progress message",
  "thread_id": "...",
  "child_verification": {
    "status": "infrastructure_blocked",
    "failure_kind": "windows_restricted_token_private_acl",
    "command": "conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v",
    "exit_code": 1,
    "evidence": "PermissionError: [WinError 5] Access is denied: '...pytest-of-henry'..."
  },
  "parent_verification_request": {
    "executor": "trusted_parent",
    "command": "conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v",
    "instruction": "Review the command first. If it is the intended non-destructive verification command, rerun it in the intended project workspace using the trusted parent context and report the outcome as parent verification, not child verification."
  }
}
```

Do not synthesize a future parent result, and do not invent a command cwd.

## Task 1 — Capture command evidence and classify the ACL failure

- [ ] Extend `turn_stdout()` to emit completed command events with command, aggregated output, exit code, and status while preserving fixture compatibility.
- [ ] Add parser and classifier tests for positive Windows pytest ACL failure, ordinary pytest assertion failure, non-pytest permission failure, and non-Windows execution.
- [ ] Run the focused tests and confirm RED.
- [ ] Project completed command events into bounded `_CommandResult` values without retaining unlimited aggregated output.
- [ ] Scan newest-first and return bounded `_VerificationBlock` evidence for only the specified signature.
- [ ] Run focused tests and parser/transport regressions to GREEN.
- [ ] Commit as `feat: classify Windows pytest sandbox failures`.

## Task 2 — Make verification blockage a terminal agent outcome

- [ ] Add lifecycle tests proving a block prevents continuation, overrides `LOCAL_AGENT_COMPLETE`, cleans once, and leaves ordinary continuation unchanged.
- [ ] Confirm RED.
- [ ] Add `_AgentState` and `_AgentResult`.
- [ ] Check every successful child turn for a verification block before accepting completion or scheduling continuation.
- [ ] Preserve ordinary completion and all unrelated error paths.
- [ ] Keep the existing single outer cleanup path.
- [ ] Run focused and full wrapper tests to GREEN.
- [ ] Commit as `feat: surface blocked child verification`.

## Task 3 — Expose the structured MCP contract

- [ ] Add completed/blocked serialization, spawn return, and parent-attribution tests.
- [ ] Confirm RED.
- [ ] Implement `_public_agent_result()` and reject malformed blocked results.
- [ ] Change `spawn_local_agent()` to return `dict[str, Any]` while preserving validation, model resolution, and single-flight behavior.
- [ ] Add routing instructions requiring safety review and separately attributed trusted-parent verification, without adding any verifier tool.
- [ ] Run focused and full wrapper tests to GREEN.
- [ ] Confirm the response has exact command/thread/exit code and bounded evidence, not full output.
- [ ] Commit as `feat: request trusted parent verification`.

## Task 4 — Documentation and Windows qualification

- [ ] Document `completed`, `verification_blocked`, child versus parent verification, the security boundary, and Windows pytest ACL troubleshooting in `README.md`.
- [ ] Explain Codex issue #19791 and that the measured sandbox is a restricted token, not an AppContainer.
- [ ] Run the full wrapper suite.
- [ ] Audit `_run_child_turn()` and the diff for unchanged sandbox policy and absence of forbidden workarounds.
- [ ] Run the real Q4 qualification in `D:\self_study\github_projects\novel-agent` with the exact pytest command.
- [ ] Review the returned command, then run it in the trusted parent context and record child BLOCKED versus trusted-parent PASS attribution.
- [ ] Exercise the narrow normal-success contract.
- [ ] Check wrapper and target working trees, cleanup, and absence of diagnostic artifacts.
- [ ] Commit as `docs: explain trusted parent verification fallback`.

## Final verification gate

- [ ] Classifier requires Windows + pytest + failed/nonzero + `PermissionError` + WinError/access denied + `pytest-of-`.
- [ ] Ordinary pytest failures are not classified.
- [ ] Non-pytest WinError 5 failures are not classified.
- [ ] A recognized block overrides the completion marker.
- [ ] A recognized block prevents continuation retries.
- [ ] Qwen remains under `workspace-write`.
- [ ] The MCP never launches trusted-parent verification.
- [ ] Exact command, exit code, thread ID, and bounded evidence reach the parent.
- [ ] The MCP never represents future parent verification as passed.
- [ ] Existing stream, timeout, cancellation, cleanup, routing, and locking tests remain green.
- [ ] Real Qwen returns `verification_blocked` and trusted-parent rerun passes the same workload.
- [ ] README accurately describes the trust boundary.

STOP after v2.2 implementation and verification. Do not design or implement another version.
