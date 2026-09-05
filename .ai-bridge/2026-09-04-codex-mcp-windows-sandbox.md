# Codex MCP Windows sandbox investigation

Date: 2026-09-04

## Executive conclusion

Built-in Luna succeeds because the installed Codex 0.149.1 `spawn_agent` path
inherits the root thread's live permission state. In this run that state was
`sandbox_policy = danger-full-access` and `permission_profile = disabled`.
Luna therefore ran pytest under the ordinary user token, not under the Windows
restricted-token sandbox.

MCP/Qwen does not inherit that state. `server.py::_run_child_turn()` constructs a
fresh command with `--sandbox workspace-write --ask-for-approval never` whenever
`bypass_permissions` is false. Codex resolves that to a managed, restricted
permission profile and executes commands with its Windows restricted-token
sandbox. Python 3.14's `os.mkdir(path, mode=0o700)` creates an owner-only ACL that
this restricted token cannot use immediately afterward. Pytest's `tmp_path`
setup uses that mode, so all four tests fail before their bodies run.

The decisive single-variable experiment changed only a fresh CLI child's sandbox
from `workspace-write` to the root/Luna-equivalent `danger-full-access`. The exact
test changed from four errors to four passes. Thus the cause is the effective
permission profile, not the fresh `codex exec` boundary, private Unsloth
`CODEX_HOME`, MCP transport, or asyncio process launch.

This is **Outcome C — Luna merely had broader permissions**. Codex has not fixed
the restricted-sandbox problem. A fresh MCP child can be made to behave like
Luna only by giving it the same broad execution policy; that does not preserve
the intended security boundary and is not a fix.

## Environment

| Item | Value |
| --- | --- |
| Windows | Windows 11 25H2 build 26200.9168 (`ProductName` remains the legacy `Windows 10 Pro` registry value) |
| Codex | `codex-cli 0.149.1` |
| Shell default Python | 3.11.14; pytest not installed |
| Workload Python | 3.14.4, `D:\Softwares\anaconda\envs\fourteen\python.exe` |
| Workload pytest | 9.0.3 |
| Unsloth | 2026.8.22 |
| Unsloth Zoo | 2026.8.16 |
| `uv` | 0.12.2; its selected Python was 3.11.14 and pytest was not installed |
| Wrapper commit | `4a320d899524965003d41a0c56c899fd90352d26` |
| Wrapper dirty state before investigation | clean |
| Wrapper profile | `bypass_permissions=false`; private API key never read into output |

Baseline commands also located Codex at
`C:\Users\henry\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe`.
The shell resolved Python first to `D:\Softwares\anaconda\python.exe`.

The exact recovered workload was the real Qwen-created test in
`D:\self_study\github_projects\novel-agent`:

```powershell
conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v
```

The target repository was already dirty before this investigation:

```text
 M app/ui/main_window.py
 M pyproject.toml
 M tests/test_main_window_bible_navigation.py
?? app/application/projects.py
?? tests/test_project_lifecycle.py
```

That state was unchanged afterward.

## Baseline reproduction

The normal/root control passed all four tests in 0.31–0.37 seconds. The fresh
MCP call reproduced four errors in `tmp_path` fixture setup:

```text
PermissionError: [WinError 5] Access is denied:
'C:\Users\henry\AppData\Local\Temp\pytest-of-henry'
```

The MCP child was `01a06cea-d893-75a2-ae91-3b38e6c5b3cf`. Its JSONL stream
contained a normal `thread.started`, tool activity, final completion marker, and
`turn.completed`; the MCP call returned normally. The wrapper then removed the
child rollout/session. This was not the prior oversized-event transport bug.

## Execution matrix

All rows used the same test file and pytest command.

| ID | Path | Result | Python / pytest | Effective execution state | Token evidence |
| --- | --- | --- | --- | --- | --- |
| A | Normal host Windows PowerShell | **PASS**, 4 passed | 3.14.4 / 9.0.3 | No Codex sandbox | Ordinary medium-integrity user |
| B | Main/root Codex command execution | **PASS**, 4 passed | 3.14.4 / 9.0.3 | `danger-full-access`; profile `disabled`; approval `never` | `TokenIsAppContainer=False` |
| C | Native `spawn_agent` Luna child | **PASS**, 4 passed | 3.14.4 / 9.0.3 | inherited `danger-full-access`; profile `disabled`; approval `never` | `TokenIsAppContainer=False` |
| D | MCP `spawn_local_agent` → Qwen → nested `codex exec` | **FAIL**, 4 setup errors | 3.14.4 / 9.0.3 | `workspace-write`; managed restricted profile; approval `never` | Restricted-token path; Qwen's class-34 result was invalid and excluded |
| E | Standalone fresh `codex --sandbox workspace-write --ask-for-approval never exec` | **FAIL**, 4 setup errors | 3.14.4 / 9.0.3 | `workspace-write`; managed restricted profile; approval `never` | corrected class 29: `TokenIsAppContainer=False` |
| F | Current `_run_child_turn()` launched directly without MCP | **FAIL**, 4 setup errors | 3.14.4 / 9.0.3 | exact Qwen Q4/private-home command, `workspace-write`, approval `never` | same restricted-token implementation; direct `0o700` access loss reproduced |

Common workload state was cwd/rootdir
`D:\self_study\github_projects\novel-agent`, with both `TEMP` and `TMP` equal
to `C:\Users\henry\AppData\Local\Temp`.

Row D's MCP server had been started from the wrapper repository, so the stored
child session cwd was the wrapper repository even though Qwen changed to the
target repository before running the exact command. Row F started directly from
the target repository, matching the original failure's cwd and writable root,
and failed identically. This rules out that cwd difference as the pytest cause.

Row F's pytest and `0o700` results completed, but the Qwen turn then repeatedly
spawned malformed ctypes diagnostics instead of finishing. It was terminated as
an unhealthy diagnostic turn. The process tree, child session, and its exact
probe directory were cleaned afterward. This does not affect row F's PASS/FAIL
classification for the requested workload.

## Permission comparison

| Field | Root | Built-in Luna | MCP/Qwen |
| --- | --- | --- | --- |
| Codex version | 0.149.1 | 0.149.1 | 0.149.1 |
| Thread/session source | `vscode` root | `subagent.thread_spawn` | `exec` |
| Parent thread | none | `01a06ce9-033b-73b0-80fb-40780f737e27` | none in child session metadata |
| Thread id | `01a06ce9-033b-73b0-80fb-40780f737e27` | `01a06cee-9949-7151-9d92-cce78ffc80d2` | `01a06cea-d893-75a2-ae91-3b38e6c5b3cf` |
| Provider/model | OpenAI / `gpt-5.6-sol` | OpenAI / `gpt-5.6-luna` | Unsloth / Qwen3.8 27B Q4 |
| Session cwd | wrapper repository | inherited wrapper repository | wrapper repository for row D; target repository for row F |
| Approval policy | `never` | `never` | `never` |
| Sandbox policy | `danger-full-access` | `danger-full-access` | `workspace-write`, network restricted |
| Permission profile | `disabled` | `disabled` | managed restricted filesystem/profile |
| AppContainer measured | false | false | false on the equivalent standalone restricted path |
| Windows security mechanism | ordinary token | ordinary token | `CreateRestrictedToken`, capability/restricting SIDs |
| Pytest | PASS | PASS | FAIL |

### Does Luna inherit the parent profile?

Yes, in the installed version. Runtime rollout evidence records Luna's effective
turn as:

```text
model=gpt-5.6-luna
approval_policy=never
sandbox_policy={type: danger-full-access}
permission_profile={type: disabled}
```

The source at Codex tag `rust-v0.149.1` confirms the behavior:

- `build_agent_spawn_config()` starts from `turn.config.clone()` and refreshes
  live provider/model/reasoning state.
- `apply_spawn_agent_runtime_overrides()` copies the live approval policy, cwd,
  and `permission_profile_state().snapshot()` into the child config.
- `AgentControl::spawn_agent_with_metadata()` receives that completed config and
  starts a child thread rather than a new CLI process.

Therefore Luna's success is explained by inheritance, but specifically by
inheritance of the root's **disabled/broad** profile.

## Windows-token evidence

### Root and Luna

Both reported:

```text
user=henrydesktop\henry
integrity=Medium Mandatory Level
TokenIsAppContainer=False
```

`BUILTIN\Administrators` was deny-only, as expected for an unelevated normal
user token. Both could create a `0o700` directory, create/read/list a file inside
it, and remove it.

### Workspace-write fresh child

Row E reported the same user and medium integrity, but the corrected
`GetTokenInformation(TokenIsAppContainer=29)` probe returned false. Thus the
process is **not an AppContainer**.

Codex 0.149.1 Windows source directly identifies the actual mechanism:
`windows-sandbox-rs/src/token.rs` calls `CreateRestrictedToken` with
`DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED` and capability/restricting
SIDs. The observed process is therefore a non-AppContainer Windows restricted
token.

Qwen's row-D probe queried class 34 and labeled it `TokenIsAppContainer`; class
34 is not `TokenIsAppContainer`, so that result was discarded. Row E measured
the same Codex 0.149.1 `workspace-write` execution path correctly. Row F's
runtime metadata and captured process command confirm it used that same path.

## `0o700` probe and ACL evidence

| Path | `os.mkdir(..., 0o700)` | Immediate file write/read/list | Cleanup in sandbox |
| --- | --- | --- | --- |
| Root | PASS | PASS | PASS |
| Luna | PASS | PASS | PASS |
| Fresh workspace-write CLI | directory created | **FAIL** on file creation; read/list unavailable | **FAIL**, host cleanup required |
| Direct exact Qwen command | directory created | **FAIL** on file creation and list | **FAIL**, host cleanup required |

Host-side `icacls` for the row-E probe showed:

```text
NT AUTHORITY\SYSTEM:(OI)(CI)(F)
BUILTIN\Administrators:(OI)(CI)(F)
OWNER RIGHTS:(OI)(CI)(F)
```

The existing `C:\Users\henry\AppData\Local\Temp\pytest-of-henry` root had the
same ACL shape. The unrestricted host could inspect and remove the newly created
probe directory; the restricted process that created it could not create a file
inside it. This is the same underlying private-ACL failure as pytest, not merely
a similar error string.

## Single-variable hypothesis test

The controlled change was:

```text
fresh codex exec + workspace-write + exact test = 4 errors
fresh codex exec + danger-full-access + exact test = 4 passed
```

Approval policy (`never`), Codex version, provider/model, cwd, Python, pytest,
test file, command, TEMP, and TMP were otherwise unchanged.

Result: permission-policy parity is sufficient for behavioral parity, but only
by removing the restricted sandbox. There is no supported restricted profile
tested here that both preserves the intended boundary and makes Python 3.14
private directories usable.

## Runtime-path findings

### Confirmed

- Root and native Luna use `danger-full-access` with a disabled permission
  profile and pass.
- MCP/Qwen reconstructs `workspace-write` with a managed restricted profile and
  fails.
- Standalone fresh `codex exec` reproduces without MCP.
- The exact current `_run_child_turn()` command reproduces without MCP.
- Changing only the fresh CLI sandbox to `danger-full-access` passes.
- The Windows sandbox is a `CreateRestrictedToken` design, not AppContainer.

### Ruled out

- MCP JSONL transport and the previous oversized-event bug.
- MCP's asyncio process-launch flags, pipes, or stdio setup.
- The private Unsloth `CODEX_HOME` and provider profile: ordinary Codex home also
  fails under `workspace-write`.
- Fresh `codex exec` as an inherently different execution architecture: a fresh
  process passes under the Luna-equivalent policy.
- TEMP relocation as a root cause or solution: the direct workspace-local
  `0o700` probe fails after creation too.

### Still uncertain

- Whether a future Codex restricted-token implementation can retain the current
  boundary while making Python owner-only ACLs usable.
- Whether CPython's AppContainer-specific proposed change would help Codex's
  distinct restricted-token/capability design. Its current description only adds
  an AppContainer SID, while this process is not an AppContainer.
- The best product contract for parent-owned trusted verification needs a v2.2
  design review; it was not designed or implemented here.

## App-server/thread-runtime feasibility

The installed Codex exposes the public, experimental `codex app-server` JSON-RPC
interface over stdio. Version-matched documentation and CLI help show public
methods for `thread/start`, `thread/resume`, `thread/fork`, `turn/start`, streamed
item/turn notifications, and permission-profile selection.

An external bridge could therefore run a future feasibility experiment without
accessing private Rust `AgentControl` APIs. It could potentially reduce wrapper
ownership of large JSONL lines, thread-id recovery, session cleanup, child
process lifecycle, and continuation reconstruction. None of those benefits is
proven yet.

App-server is **not the preferred response to this root cause**. If it starts a
Qwen thread with the same managed `workspace-write` profile, the same Windows
restricted-token/ACL failure is expected. Native `spawn_agent` passed because of
the inherited broad profile, not because thread runtime bypasses a correctly
equivalent restricted profile.

## Upstream status as of 2026-09-04

- [openai/codex#19791](https://github.com/openai/codex/issues/19791) — **OPEN**,
  last updated 2026-07-16. It reports the same Windows Codex sandbox plus
  pytest/private-directory failure.
- [python/cpython#134587](https://github.com/python/cpython/issues/134587) —
  **OPEN**, last updated 2026-04-20. It confirms Python 3.12.4+ owner-only temp
  directories fail under Windows AppContainer after the `0o700` ACL change.
- [python/cpython#148804](https://github.com/python/cpython/pull/148804) —
  **OPEN**, last updated 2026-07-25 and marked stale. It proposes adding the
  AppContainer SID to Python's `0o700` SDDL.

The CPython issue and PR validate the broader private-ACL mechanism, but they are
not a confirmed fix for Codex because Codex's measured token is not an
AppContainer. The direct open dependency for this environment remains Codex
#19791 or an equivalent Codex restricted-token fix.

## v2.2 recommendation

Preferred direction: **Outcome C — explicit infrastructure-failure handling
with trusted parent verification**.

V2.2 should recognize this known Windows sandbox/private-ACL failure as an
infrastructure verification failure, preserve the Qwen child's restricted
boundary, and return enough structured evidence for the already trusted parent
runtime to rerun the exact failed verification command. The parent must report
its result separately; it must not rewrite the Qwen result as if the child had
passed. Broad execution should be an explicit parent security policy, not a
hidden child bypass.

Do not pursue these as the primary fix:

- Do not set `bypass_permissions`, `--yolo`,
  `--dangerously-bypass-approvals-and-sandbox`, or `danger-full-access` for Qwen
  merely to make pytest green. That reproduces Luna's broader security posture
  rather than fixing sandbox correctness.
- Do not patch pytest, xdist, `tempfile`, `os.mkdir`, `Path.mkdir`, TEMP, or
  `--basetemp`. The direct probe proves the underlying token/ACL mismatch.
- Do not treat app-server migration as the pytest fix. Evaluate it separately
  only for lifecycle/transport simplification after defining the permission
  contract.
- Do not focus on MCP environment or `CREATE_NEW_PROCESS_GROUP`; row F rules that
  out.
- Do not wait silently for CPython #148804 as the only plan; its proposed change
  is AppContainer-specific and may not cover Codex's token.

Required future regression coverage should include the same exact command under
the restricted child, classification of the known infrastructure signature,
preservation of the original traceback/path/thread id, parent verification as a
separately attributed result, no automatic broadening of child permissions,
healthy JSONL/turn completion, and cleanup after success, infrastructure failure,
timeout, and cancellation.

## No-change confirmation

No production source, pytest configuration, global Codex configuration, Unsloth
profile, or target-project file was changed.

Temporary artifacts were:

- `.f_probe.py` in the wrapper repository — removed.
- Three Qwen diagnostic scripts in `%TEMP%` — removed.
- Two disposable `0o700` probe directories from rows E/F — inspected with
  `icacls`, then removed by the unrestricted host.
- A temporary Codex 0.149.1 source clone in `%TEMP%` — removed.
- Fresh CLI and direct-Qwen diagnostic sessions — deleted/cleaned.

The wrapper working tree was restored to clean state before adding only this
report. The target repository's pre-existing dirty state remained byte/status
unchanged.

## V2.2 PLANNING INPUT

Confirmed root cause: Luna inherits the root's disabled/danger-full-access
permission profile; MCP/Qwen reconstructs workspace-write and therefore runs
pytest under Codex's Windows restricted token, which cannot use Python 3.14
`0o700` directories after creating them.

Evidence: A/B/C pass; D/E/F fail; class-29 probes show root/Luna and the fresh
restricted child are not AppContainers; Codex 0.149.1 source uses
`CreateRestrictedToken`; `icacls` and direct `0o700` probes reproduce immediate
access loss; changing only fresh CLI policy to danger-full-access passes.

Recommended architecture: Keep Qwen restricted and add explicit infrastructure
failure reporting plus separately attributed verification by the trusted parent
runtime. Consider app-server later as an independent lifecycle/transport
feasibility experiment, not as the sandbox fix.

Do not do: Do not broaden Qwen permissions by default; do not monkeypatch Python
or pytest; do not relocate temp roots; do not treat app-server or CLI JSONL
changes as a solution to the ACL problem.

Required regression coverage: Restricted-child failure classification; exact
command/trace/path/thread preservation; trusted-parent rerun attribution; no
implicit permission broadening; JSONL health; cleanup on all terminal paths.

Open upstream dependency: openai/codex#19791. CPython #134587/#148804 document an
adjacent AppContainer mechanism but are not yet proven to fix Codex's
non-AppContainer restricted token.
