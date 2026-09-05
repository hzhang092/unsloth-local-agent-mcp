# Unsloth Local-Agent MCP — Issues and Resolutions

Last updated: 2026-09-04

This is the living troubleshooting record for the persistent Unsloth Qwen
subagent MCP used by Codex. Update this file whenever the setup, failure mode,
fix, or verification status changes.

## Current status

- MCP name: `unsloth_local_agent`
- Tools: `spawn_local_agent`, `list_agent_models`
- Default model: `qwen38-q4`
- Optional model: `qwen38-q5`
- Default reasoning effort: `medium`
- Concurrency: one Qwen worker at a time
- Completion: marker plus `turn.completed`
- Premature completion: resume the same child session up to two times
- Focused tests: 43 passing on 2026-09-04
- Verification fallback: live qualified on 2026-09-04
- Normal-success v2.2 MCP smoke: live qualified on 2026-09-04
- Live test of the new bounded continuation path: not yet recorded

## Ownership and file locations

Codex-owned source repository:

```text
D:\self_study\github_projects\unsloth-local-agent-mcp
```

Deployed MCP wrapper:

```text
C:\Users\henry\.codex\unsloth_mcp
```

Global Codex registration:

```text
C:\Users\henry\.codex\config.toml
```

Unsloth-owned authentication and child runtime:

```text
C:\Users\henry\.unsloth\studio\auth\agents\codex-subagent
```

The Unsloth directory contains generated authentication, child Codex state,
configuration, logs, caches, sandbox data, and SQLite databases. Keep it in
place. Do not copy or commit `subagent.json`; it contains authentication
material.

## Current registration

```toml
[mcp_servers.unsloth_local_agent]
command = 'C:\Users\henry\.unsloth\studio\unsloth_studio\Scripts\python.exe'
args = [
  'C:\Users\henry\.codex\unsloth_mcp\server.py',
  'C:\Users\henry\.unsloth\studio\auth\agents\codex-subagent\subagent.json',
]
required = true
enabled_tools = ["spawn_local_agent", "list_agent_models"]
default_tools_approval_mode = "approve"
startup_timeout_sec = 15
tool_timeout_sec = 3600
```

Codex must be fully restarted after changing this block or replacing the
deployed wrapper.

## Current model routing

| Alias | Model | Context | Purpose |
| --- | --- | ---: | --- |
| `qwen38-q4` | `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL` | 152832 | Default coding worker |
| `qwen38-q5` | `unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL` | 89600 | Higher-quality worker |

The allowlist is stored in `models.json`. Unknown aliases are rejected before
a child process starts.

## Issue history

### 1. Direct Unsloth bridge could not select between models

**Symptom**

The generated `unsloth_cli.codex_subagent_mcp` bridge exposed only:

```text
spawn_local_agent(task)
```

It had no model argument or model-listing tool.

**Cause**

The generated bridge was designed for one configured child model.

**Resolution**

Created the Codex-owned `server.py` wrapper and `models.json`. The wrapper adds
an optional approved alias, passes the selected model and fitted context to
`codex exec`, and leaves the installed Unsloth package unchanged.

**Status:** Resolved and qualified for Q4 and Q5 switching.

### 2. Native Qwen worker was routed through the ChatGPT account

**Symptom**

The native worker failed with:

```text
The 'Unsloth-studio/unsloth-Qwen3.8-27B-GGUF' model is not supported when
using Codex with a ChatGPT account.
```

**Cause**

The native agent definition named the local model but did not select the
`unsloth_api` provider or receive the Unsloth authentication token. Codex sent
the model name through the parent ChatGPT account route.

**Resolution**

Removed `C:\Users\henry\.codex\agents\qwen38-worker.toml`. Qwen, Unsloth, and
local-agent delegation now use only `unsloth_local_agent/spawn_local_agent`.
The MCP injects authentication only into the child process and explicitly uses
the child `unsloth_api` profile.

**Status:** Resolved. Do not recreate the native Qwen worker.

### 3. Child PowerShell commands were blocked by the Windows sandbox

**Symptom**

The child could reach the model but failed to start or use `pwsh.exe`, including
`rejected: blocked by policy` behavior.

**Cause**

The generated child runtime needed the native Windows unelevated sandbox
backend and project trust configuration.

**Resolution**

The dedicated child `config.toml` uses:

```toml
[windows]
sandbox = "unelevated"

[projects.'d:\self_study\github_projects\novel-agent']
trust_level = "trusted"
```

The child still runs with `workspace-write` and approval policy `never`.
Qualification confirmed in-workspace commands and writes succeed while an
outside-workspace write is denied.

**Status:** Resolved for the qualified project.

### 4. Loading a different Studio model did not reliably change the worker

**Symptom**

Changing the model in Unsloth Studio did not necessarily change the model used
by the Codex child.

**Cause**

The child profile was pinned independently, and the generated bridge did not
override the model per invocation.

**Resolution**

The custom wrapper resolves an approved alias and passes both:

```text
--model <model-id>
--config model_context_window=<context>
```

Q4-to-Q5 and Q5-to-Q4 switching were exercised successfully.

**Status:** Resolved.

### 5. Q4 and Q5 could not safely share one context-window value

**Symptom**

The two quantizations fit different runtime contexts on the available hardware.

**Cause**

Although both models share the same architecture limit, available memory and
quantization determine the locally fitted runtime context.

**Resolution**

Store a conservative context per alias in `models.json`: 152832 for Q4 and
89600 for Q5. Pass it as a per-run Codex override.

**Status:** Resolved for the currently qualified hardware.

### 6. Default reasoning effort was unclear

**Symptom**

The MCP had no explicit reasoning-effort field, so it was unclear whether Qwen
used `none`, `low`, `medium`, `high`, or `xhigh`.

**Cause**

Reasoning effort belongs to the child Codex/provider request, not the MCP model
allowlist. With no override, the Qwen template could apply its own default.

**Resolution**

Qualified the Studio choices `none`, `low`, `medium`, `high`, and `xhigh` on
Q4. Set the shared child default in the dedicated child `config.toml`:

```toml
model_reasoning_effort = "medium"
```

`high` is normalized to Qwen's `xhigh` branch. `Preserve thinking` is a separate
history-retention control, not another effort level.

**Status:** Resolved for the shared default. Per-call effort remains out of
scope.

### 7. Forced model-server termination left stale Studio state

**Symptom**

After manually killing `llama-server`, Studio sometimes reported the model as
loaded even though no server process existed. Delegation then failed.

**Cause**

The running Studio supervisor retained stale process/model state after the
forced termination. The original bridge failed in the same condition.

**Resolution**

Restart Unsloth Studio cleanly. Once Studio correctly reported the model as
unloaded, a new MCP request started `llama-server`, loaded Q4, and completed.

**Status:** Operational workaround verified. Do not use forced server
termination as a normal model-switch procedure.

### 8. Concurrent calls could queue invisibly or interfere with model switching

**Symptom**

Overlapping requests could wait without visible progress, and Q4/Q5 share the
same local server resources.

**Cause**

The first implementation waited indefinitely for a global lock. Multiple MCP
processes also needed one cross-process coordination point.

**Resolution**

Use an in-process async lock plus the Windows `qwen38.lock` file. A second call
now fails quickly with a busy error instead of queuing. Q4 and Q5 share exactly
one worker slot.

**Status:** Resolved and covered by tests.

### 9. Child output buffering caused opaque stalls and pipe risk

**Symptom**

The parent received no useful activity while the child ran. Repeated
`communicate(timeout=...)` handling could lose captured output, while poll-only
handling could block if stderr filled its pipe.

**Cause**

The synchronous wrapper buffered child output until exit and did not drain both
pipes continuously.

**Resolution**

Make `spawn_local_agent` asynchronous, launch with
`asyncio.create_subprocess_exec`, and continuously drain stdout and stderr.
Report only safe activity counts through MCP progress notifications; do not
expose child reasoning or output contents.

**Status:** Resolved and covered by tests.

### 10. Cancelling the MCP could leave the child running

**Symptom**

Cancelling the parent MCP call did not reliably stop the child Codex process,
which could retain the lock and block later work.

**Cause**

The synchronous bridge did not propagate async cancellation to the full child
process tree.

**Resolution**

Catch cancellation and failure, stop the complete Windows process tree, await
reader shutdown, and release both locks in every exit path.

**Status:** Resolved. Real child-process cancellation is covered by tests.

### 11. A progress sentence could be returned as successful completion

**Symptom**

Qwen ended a turn with:

```text
Now let me create the test file.
```

The child emitted `turn.completed` and exited with code `0`, but the requested
test file was missing and no test had run.

**Cause**

`turn.completed` means the model finished that Codex turn. It does not prove
that the coding objective was fulfilled. The original Unsloth parser returned
the last assistant message and could therefore report a progress sentence as a
successful tool result.

Official OpenAI documentation similarly separates response/tool lifecycle
events from application-level acceptance criteria: [Responses API
reference](https://developers.openai.com/api/reference/cli/resources/beta/subresources/responses).

**First resolution**

Require both `turn.completed` and this exact final marker:

```text
LOCAL_AGENT_COMPLETE
```

The wrapper strips the marker before returning the result. This correctly
turned the observed incomplete task into an MCP failure.

**Follow-up problem**

The marker prevented false success but did not let Qwen continue after a
prematurely ended turn.

**Current resolution**

The wrapper now creates a resumable child session. If the turn ends without the
marker, it resumes the same thread with a minimal continuation instruction. It
allows at most two continuation turns, shares one global timeout across all
turns, rejects a changed thread ID, stops on explicit errors, and fails early
after repeated no-progress turns.

**Status:** Implemented in both the repository and deployed wrapper. Unit tests
pass. A live Qwen run that intentionally triggers continuation still needs to
be recorded.

### 12. Resumable sessions create child SQLite/state records

**Symptom**

The earlier one-shot child used `--ephemeral`. Same-thread continuation requires
a persisted Codex session, which writes normal child state into the dedicated
child Codex home and its SQLite databases.

**Cause**

`codex exec resume <thread-id>` needs the prior session state. SQLite files are
runtime databases, not disposable test probes.

**Resolution**

The wrapper removes `--ephemeral`, captures `thread.started`, resumes only that
thread, and calls:

```text
codex delete --force <thread-id>
```

in `finally` after success, terminal failure, or cancellation. Cleanup has a
30-second limit. Cleanup failure is logged but does not replace an otherwise
successful task result.

Do not delete the shared SQLite database files manually.

**Status:** Implemented and covered by cleanup tests. Live database cleanup
should be checked during the live continuation qualification.

### 13. MCP tests failed under an unrelated Python environment

**Symptom**

Running tests from a Python environment without the MCP package failed to import
`mcp`. Adding only the Unsloth `site-packages` path to a different interpreter
then failed to load the compiled `pydantic_core` extension.

**Cause**

Compiled Python packages must match the interpreter/environment that installed
them.

**Resolution**

Run this MCP's tests with the same Python environment used by the registration:

```powershell
& 'C:\Users\henry\.unsloth\studio\unsloth_studio\Scripts\python.exe' `
  -m unittest -v test_server.py
```

Do not combine the Unsloth `site-packages` directory with a different Python
interpreter.

**Status:** Resolved. All 21 tests passed on 2026-09-02. A non-fatal
`IncompleteFieldDefinitionWarning` from `pydantic_settings` remains upstream
runtime noise.

### 14. Authoritative source workspace was relocated

**Change**

On 2026-09-03, the authoritative source workspace moved from:

```text
C:\Users\henry\Documents\ChatGPT\unsloth-mcp
```

to:

```text
D:\self_study\github_projects\unsloth-local-agent-mcp
```

The destination already contained the same Git history plus newer uncommitted
changes to `README.md`, `server.py`, and `test_server.py`. Those changes and the
existing `docs/` and `work/` directories were preserved. `AGENTS.md` and this
report were moved into the destination, and their workspace pointers were
updated.

Windows could not recycle the old clone because this running Codex task still
had it open. No old files were deleted by the failed cleanup attempts.

**Status:** New workspace active. Close this Codex task or its old workspace,
then send `C:\Users\henry\Documents\ChatGPT\unsloth-mcp` to the Recycle Bin.

### 15. Windows pytest verification is blocked by private-directory ACLs

**Symptom**

The Qwen child runs this exact command under `workspace-write`:

```powershell
conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v
```

Pytest collects four tests but all error during `tmp_path` setup with:

```text
PermissionError: [WinError 5] Access is denied: ...\pytest-of-henry
```

The trusted parent runs the same command successfully with four passing tests.
An attempted post-deployment qualification returned `Transport closed` before
creating a child session because the active MCP stdio process had been stopped
to reload the wrapper.

**Cause**

The child uses Codex's Windows restricted-token sandbox. Python's owner-only
private-directory ACL cannot be re-entered by that token. The MCP registration
also points to the deployed copy under `C:\Users\henry\.codex\unsloth_mcp`, while
the authoritative D: source contained newer uncommitted v2.1 stream hardening.
The first v2.2 deployment was therefore not source-first and lacked that local
hardening.

**Resolution**

Merged v2.2 into the authoritative D: source while preserving incremental JSONL
parsing, the 16 MiB event limit, bounded stderr, cancellation, continuation, and
cleanup. The wrapper now retains only an 8 KiB command-output tail, classifies
only the confirmed Windows pytest signature, returns `verification_blocked`,
and asks the trusted parent to review and rerun the command with separate
attribution. Qwen permissions remain `workspace-write` with approval `never`.

Affected source files:

- `server.py`
- `test_server.py`
- `README.md`
- `SETUP_ISSUES_AND_RESOLUTIONS.md`
- `.ai-bridge/2026-09-04-codex-mcp-windows-sandbox.md`
- `docs/superpowers/plans/2026-09-04-windows-sandbox-verification-fallback.md`
- `skills/using-unsloth-subagents/SKILL.md`

The preserved investigation report and canonical implementation plan were
copied from the retired workspace into these repository locations on
2026-09-04. Their normalized text matches the source documents exactly; only
line endings were normalized by the repository patch operation. This
documentation-only copy requires no Codex restart.

The repository also includes the parent-coordination skill with an explicit
v2.2 compatibility marker. Its installed and repository copies have identical
normalized text.

Verification command:

```powershell
& 'C:\Users\henry\.unsloth\studio\unsloth_studio\Scripts\python.exe' `
  -m unittest -v test_server.py
```

Result: `Ran 43 tests ... OK` on 2026-09-04.

**Deployment:** `server.py`, `test_server.py`, and `README.md` were copied to
`C:\Users\henry\.codex\unsloth_mcp`. The deployed suite also passed all 43
tests, and the deployed permission branch remains exactly
`--sandbox workspace-write --ask-for-approval never`.

**Live qualification after restart**

The one permitted Q4 call used the exact project command above and returned a
normal structured MCP response with:

- thread ID `01a06e88-9839-7861-9eb0-3b1c5ac792e0`;
- status `verification_blocked`;
- child verification status `infrastructure_blocked`;
- failure kind `windows_restricted_token_private_acl`;
- exit code `1`;
- the exact PowerShell-wrapped pytest command; and
- bounded evidence containing `PermissionError`, `WinError 5`, and
  `C:\Users\henry\AppData\Local\Temp\pytest-of-henry`.

The MCP call completed normally without the old oversized-JSONL failure,
timeout, or continuation retries. After reviewing the returned command as
untrusted evidence, the trusted parent ran:

```powershell
conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v
```

from `D:\self_study\github_projects\novel-agent`. Result: `4 passed in 0.33s`
with Python 3.14.4 and pytest 9.0.3. Attribution:

```text
Child verification: BLOCKED — windows_restricted_token_private_acl.
Trusted-parent verification: PASS — 4 passed in 0.33s.
```

The target repository status was identical before and after qualification.
The child session file was absent after the call and no child `codex exec`
process remained. The failed pytest temp root still exists; an exact-path
PowerShell removal was rejected by the host command policy before execution,
and no bypassing deletion method was attempted.

The separate normal-success Q4 smoke ran `git status --short` read-only in the
upload repository. Thread `01a06ebb-df78-7453-8d96-50b671efcc95` returned:

- status `completed`;
- child verification status `completed`; and
- no `parent_verification_request`.

The parent confirmed the reported dirty working-tree state was unchanged. No
matching file remained in the child `sessions` directory after the call; the
thread remains referenced only by shared SQLite WAL state as expected.

**Status:** Both live v2.2 result paths are qualified: the Windows pytest path
returns `verification_blocked`, while the harmless Q4 path returns `completed`
without requesting trusted-parent verification.

## Current completion flow

```text
Parent Codex calls spawn_local_agent
    ↓
Wrapper resolves an approved model alias
    ↓
Single-flight lock is acquired
    ↓
Child Codex starts a persisted JSONL session
    ↓
Marker present + turn.completed → return success
    ↓ otherwise
Resume the same thread, at most twice
    ↓
Success, explicit error, no-progress failure, or global timeout
    ↓
Delete the temporary child session and release the lock
```

## Verification record

On 2026-09-02:

```powershell
& 'C:\Users\henry\.unsloth\studio\unsloth_studio\Scripts\python.exe' `
  -m unittest -v test_server.py
```

Result:

```text
Ran 21 tests in 0.906s
OK
```

Covered behavior includes:

- async MCP tool shape;
- fail-fast single-flight locking;
- JSONL activity reporting without content leakage;
- real child-process cancellation;
- marker and `turn.completed` validation;
- same-thread continuation;
- no continuation after success or explicit failure;
- two-continuation limit;
- no-progress detection;
- shared timeout across turns;
- cancellation during resume;
- session cleanup after success and failure;
- cleanup failure not masking success.

`server.py --check` also passed and returned Q4 as the default plus the approved
Q5 alias.

The deployed `server.py` and `test_server.py` contain the same functional code
as this repository. The repository copies additionally contain SPDX copyright
headers.

## Remaining work

1. Run one live Q4 task designed to produce a premature progress-ending turn.
2. Confirm the wrapper resumes the same thread and eventually receives
   `LOCAL_AGENT_COMPLETE`.
3. Confirm the temporary child session is absent afterward while the shared
   SQLite databases remain healthy.
4. Confirm cancellation during a live resumed turn leaves no child Codex
   process and releases `qwen38.lock`.
5. Decide whether to qualify `--output-schema` with the Unsloth Qwen Responses
   adapter. Structured output may improve parsing, but it cannot replace
   parent-side diff and test verification.

## Rules that must remain true

- Never print, copy, or commit the key in `subagent.json`.
- Keep authentication and generated child runtime under the Unsloth-owned
  directory.
- Keep custom source under Codex/user ownership.
- Keep Q4 as the default; use Q5 only when explicitly requested.
- Keep one global Qwen worker slot.
- Never silently fall back to another model.
- Never treat process exit or `turn.completed` alone as task success.
- Keep continuation bounded; never retry forever.
- Preserve cancellation, timeouts, process-tree cleanup, and parent-side
  verification.

## How to update this report

For every new incident, add:

1. exact symptom and error text, with secrets redacted;
2. affected task/session ID;
3. root cause or current hypothesis;
4. files changed;
5. resolution;
6. exact verification command and result;
7. whether Codex or Unsloth must be restarted;
8. remaining unverified behavior.
