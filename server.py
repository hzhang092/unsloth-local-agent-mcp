# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team.
# Modifications Copyright 2026 Henry Zhang.
from __future__ import annotations

import asyncio
import json
import logging
import msvcrt
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP
from unsloth_cli.claude_subagent_mcp import _bounded
from unsloth_cli.codex_subagent_mcp import _config
from unsloth_cli.commands.start import (
    _CODEX_ENV_KEY,
    _CODEX_ENV_UNSET,
    _CODEX_PROFILE,
    _CODEX_SUBAGENT_CONFIG_ENV,
    _CODEX_SUBAGENT_ROUTING_INSTRUCTIONS,
    _SUBAGENT_INSTRUCTIONS,
    _merge_wslenv,
    _prefer_windows_cmd_sibling,
    _resolved_launch_command,
    _wsl_shim_env,
)

_CANCEL_POLL_SECONDS = 0.1
_CHILD_TIMEOUT_SECONDS = 3500
_CHILD_INACTIVITY_SECONDS = 900
_MODELS_PATH = Path(__file__).with_name("models.json")
_LOCK_PATH = Path(__file__).with_name("qwen38.lock")
_LOGGER = logging.getLogger("unsloth-mcp")
_ASYNC_LOCK = asyncio.Lock()
_COMPLETION_MARKER = "LOCAL_AGENT_COMPLETE"
_COMPLETION_CONTRACT = (
    "When the assigned task and its verification are complete, put "
    f"{_COMPLETION_MARKER} on the final line of your final response."
)
_CONTINUATION_INSTRUCTION = f"""Continue the original assigned task. Your previous turn ended before satisfying
the completion contract. Execute the remaining work and verification. Do not
stop after describing the next action. Use {_COMPLETION_MARKER} only after the
original task is actually complete."""
_MAX_CONTINUATIONS = 2


class _TurnState(Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class _TurnResult:
    state: _TurnState
    thread_id: str
    message: str
    command_activity: int
    file_activity: int


class _TurnFailure(RuntimeError):
    def __init__(self, message: str, thread_id: str = "") -> None:
        super().__init__(message)
        self.thread_id = thread_id


def _task_prompt(task: str) -> str:
    return f"{_SUBAGENT_INSTRUCTIONS}\n\n{_COMPLETION_CONTRACT}\n\nTask: {task}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}.")
        result[key] = value
    return result


def _models() -> tuple[str, dict[str, dict[str, Any]]]:
    try:
        data = json.loads(
            _MODELS_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read {_MODELS_PATH}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise RuntimeError("models.json must contain a models object.")
    default = data.get("default_model")
    models = data["models"]
    if not isinstance(default, str) or default not in models:
        raise RuntimeError("default_model must name an approved model alias.")
    for alias, entry in models.items():
        if not isinstance(alias, str) or not alias.strip():
            raise RuntimeError("Every model alias must be a non-empty string.")
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise RuntimeError(f"Model alias {alias} must have a non-empty id.")
        if not isinstance(entry.get("purpose"), str) or not entry["purpose"].strip():
            raise RuntimeError(f"Model alias {alias} must have a non-empty purpose.")
        if not isinstance(entry.get("context_window"), int) or entry["context_window"] <= 0:
            raise RuntimeError(f"Model alias {alias} must have a positive context_window.")
    return default, models


def _resolve_model(alias: str | None) -> tuple[str, str, int]:
    default, models = _models()
    requested = alias or default
    if requested not in models:
        available = ", ".join(models)
        raise ValueError(
            f"Unknown local-agent model alias: {requested}. Available aliases: {available}."
        )
    entry = models[requested]
    return requested, entry["id"], entry["context_window"]


@asynccontextmanager
async def _single_flight() -> AsyncIterator[None]:
    if _ASYNC_LOCK.locked():
        raise RuntimeError("Local Qwen agent is busy; retry after the active call finishes.")
    await _ASYNC_LOCK.acquire()
    lock_file = None
    locked = False
    try:
        _LOCK_PATH.touch(exist_ok=True)
        lock_file = _LOCK_PATH.open("r+b")
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise RuntimeError(
                "Local Qwen agent is busy in another Codex task; retry after it finishes."
            ) from exc
        yield
    finally:
        if lock_file is not None:
            if locked:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()
        _ASYNC_LOCK.release()


def _failure(detail: str, alias: str, model_id: str) -> RuntimeError:
    lowered = detail.lower()
    if any(marker in lowered for marker in ("model not found", "model unavailable", "unknown model", "404")):
        return RuntimeError(f"Configured model unavailable ({alias} -> {model_id}): {_bounded(detail)}")
    if any(
        marker in lowered
        for marker in (
            "connection refused",
            "failed to connect",
            "response.failed",
            "stream disconnected",
            "server error",
            "502",
            "503",
        )
    ):
        return RuntimeError(f"Unsloth API/server failure: {_bounded(detail)}")
    return RuntimeError(f"Child Codex failure: {_bounded(detail)}")


def _validated_result_text(stdout: str) -> str:
    result = _parse_turn(stdout)
    if result.state is _TurnState.INCOMPLETE:
        raise RuntimeError(
            "Local agent exited before confirming task completion. "
            f"Last message: {_bounded(result.message)}"
        )
    return result.message


def _parse_turn(stdout: str) -> _TurnResult:
    thread_ids: list[str] = []
    messages: list[str] = []
    errors: list[str] = []
    turn_completed = False
    command_activity = 0
    file_activity = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_ids.append(event["thread_id"])
        if event_type == "turn.completed":
            turn_completed = True
        if event_type in ("error", "turn.failed"):
            detail = event.get("message") or event.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
            if detail:
                errors.append(str(detail))
        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict):
            item_type = item.get("type")
            if item_type in ("command_execution", "tool_call"):
                command_activity += 1
            if item_type == "file_change":
                file_activity += 1
        if (
            event_type == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
            and item["text"].strip()
        ):
            messages.append(item["text"].strip())
    thread_id = thread_ids[-1] if thread_ids else ""
    if errors:
        raise _TurnFailure(_bounded(errors[-1]), thread_id)
    last_message = messages[-1] if messages else ""
    if not thread_id:
        raise RuntimeError("Local agent turn returned no thread_id.")
    if not turn_completed:
        raise _TurnFailure(
            "Local agent exited without a turn.completed event. "
            f"Last message: {_bounded(last_message)}",
            thread_id,
        )
    lines = last_message.splitlines()
    completed = bool(lines and lines[-1].strip() == _COMPLETION_MARKER)
    message = "\n".join(lines[:-1]).strip() if completed else last_message
    return _TurnResult(
        state=_TurnState.COMPLETED if completed else _TurnState.INCOMPLETE,
        thread_id=thread_id,
        message=_bounded(
            message or ("Local agent completed the task." if completed else "")
        ),
        command_activity=command_activity,
        file_activity=file_activity,
    )


def _normalized_message(message: str) -> str:
    return " ".join(message.lower().split())


async def _drain_stream(
    stream: asyncio.StreamReader,
    parts: list[str],
    activity: list[float],
    context: Context | Any | None,
    *,
    report: bool,
) -> None:
    progress = 0
    while line := await stream.readline():
        decoded = line.decode("utf-8", errors="replace")
        parts.append(decoded)
        activity[0] = time.monotonic()
        if report and context is not None:
            progress += 1
            await context.report_progress(
                progress,
                message=f"Local Qwen agent is active ({progress} events received).",
            )


async def _stop_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _run_child_turn(
    prompt: str,
    alias: str,
    model_id: str,
    context_window: int,
    deadline: float,
    context: Context | Any | None = None,
    thread_id: str | None = None,
) -> _TurnResult:
    config = _config()
    executable = _prefer_windows_cmd_sibling(shutil.which("codex"))
    if executable is None:
        raise RuntimeError("`codex` is not installed or is not on PATH.")
    permissions = (
        ["--dangerously-bypass-approvals-and-sandbox"]
        if config.get("bypass_permissions") is True
        else ["--sandbox", "workspace-write", "--ask-for-approval", "never"]
    )
    command = [
        "codex",
        "--oss",
        "--profile",
        _CODEX_PROFILE,
        "--model",
        model_id,
        "--config",
        f"model_context_window={context_window}",
        *permissions,
        "exec",
    ]
    if thread_id is None:
        command.extend(["--json", "--skip-git-repo-check", prompt])
    else:
        command.extend(
            ["resume", "--json", "--skip-git-repo-check", thread_id, prompt]
        )
    local_env = {
        _CODEX_ENV_KEY: config["api_key"],
        "CODEX_HOME": config["codex_home"],
        "CODEX_SQLITE_HOME": config["codex_home"],
    }
    bridged, wsl_names = _wsl_shim_env(command, local_env, _CODEX_ENV_UNSET)
    child_env = dict(os.environ)
    if wsl_names:
        bridged = {**bridged, "PWD": os.getcwd()}
        child_env["WSLENV"] = _merge_wslenv(child_env.get("WSLENV", ""), wsl_names)
        for name in _CODEX_ENV_UNSET:
            child_env[name] = ""
    else:
        for name in _CODEX_ENV_UNSET:
            child_env.pop(name, None)
    child_env.update(bridged)
    popen_kwargs: dict[str, Any] = {
        "cwd": os.getcwd(),
        "env": child_env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    launch_command = _resolved_launch_command(executable, command[1:], child_env)
    _LOGGER.info("child start alias=%s model=%s", alias, model_id)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(*launch_command, **popen_kwargs)
    if process.stdout is None or process.stderr is None:
        await _stop_process_tree(process)
        raise RuntimeError("Local Codex output pipes were not created.")
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    activity = [started]
    readers = [
        asyncio.create_task(
            _drain_stream(process.stdout, stdout_parts, activity, context, report=True)
        ),
        asyncio.create_task(
            _drain_stream(process.stderr, stderr_parts, activity, context, report=False)
        ),
    ]
    try:
        while process.returncode is None:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(
                    f"Local-agent timeout after {_CHILD_TIMEOUT_SECONDS} seconds ({alias} -> {model_id})."
                )
            if now - activity[0] >= _CHILD_INACTIVITY_SECONDS:
                raise RuntimeError(
                    f"Local-agent inactivity timeout after {_CHILD_INACTIVITY_SECONDS} seconds "
                    f"({alias} -> {model_id})."
                )
            for reader in readers:
                if reader.done() and not reader.cancelled() and reader.exception() is not None:
                    raise reader.exception()  # type: ignore[misc]
            await asyncio.sleep(_CANCEL_POLL_SECONDS)
        await asyncio.gather(*readers)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
    except BaseException:
        for reader in readers:
            reader.cancel()
        await asyncio.shield(_stop_process_tree(process))
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    elapsed = time.monotonic() - started
    _LOGGER.info("child exit alias=%s status=%s elapsed=%.1fs", alias, process.returncode, elapsed)
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip()
        failure = _failure(
            detail or f"Local Codex exited with code {process.returncode}.",
            alias,
            model_id,
        )
        thread_id = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                value = event.get("thread_id")
                if isinstance(value, str):
                    thread_id = value
        raise _TurnFailure(str(failure), thread_id) from failure
    return _parse_turn(stdout)


async def _cleanup_session(thread_id: str) -> None:
    config = _config()
    executable = _prefer_windows_cmd_sibling(shutil.which("codex"))
    if executable is None:
        _LOGGER.warning("session cleanup skipped: codex executable not found")
        return
    command = ["codex", "delete", "--force", thread_id]
    local_env = {
        _CODEX_ENV_KEY: config["api_key"],
        "CODEX_HOME": config["codex_home"],
        "CODEX_SQLITE_HOME": config["codex_home"],
    }
    bridged, wsl_names = _wsl_shim_env(command, local_env, _CODEX_ENV_UNSET)
    child_env = dict(os.environ)
    if wsl_names:
        bridged = {**bridged, "PWD": os.getcwd()}
        child_env["WSLENV"] = _merge_wslenv(child_env.get("WSLENV", ""), wsl_names)
        for name in _CODEX_ENV_UNSET:
            child_env[name] = ""
    else:
        for name in _CODEX_ENV_UNSET:
            child_env.pop(name, None)
    child_env.update(bridged)
    launch_command = _resolved_launch_command(executable, command[1:], child_env)
    process = await asyncio.create_subprocess_exec(
        *launch_command,
        cwd=os.getcwd(),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), 30)
    except asyncio.TimeoutError:
        await _stop_process_tree(process)
        _LOGGER.warning("session cleanup timed out thread=%s", thread_id)
        return
    if process.returncode != 0:
        _LOGGER.warning("session cleanup failed thread=%s status=%s", thread_id, process.returncode)


async def _run_local_agent(
    task: str,
    alias: str,
    model_id: str,
    context_window: int,
    context: Context | Any | None = None,
) -> str:
    deadline = time.monotonic() + _CHILD_TIMEOUT_SECONDS
    thread_id = ""
    previous_incomplete: _TurnResult | None = None
    zero_progress_streak = 0
    try:
        try:
            result = await _run_child_turn(
                _task_prompt(task),
                alias,
                model_id,
                context_window,
                deadline,
                context,
                None,
            )
        except _TurnFailure as exc:
            thread_id = exc.thread_id
            raise
        thread_id = result.thread_id
        if result.state is _TurnState.COMPLETED:
            return result.message

        previous_incomplete = result
        for _ in range(_MAX_CONTINUATIONS):
            result = await _run_child_turn(
                _CONTINUATION_INSTRUCTION,
                alias,
                model_id,
                context_window,
                deadline,
                context,
                thread_id,
            )
            if result.thread_id != thread_id:
                raise RuntimeError(
                    f"Local agent resumed a different thread ({result.thread_id} != {thread_id})."
                )
            if result.state is _TurnState.COMPLETED:
                return result.message

            made_progress = result.command_activity > 0 or result.file_activity > 0
            zero_progress_streak = 0 if made_progress else zero_progress_streak + 1
            repeated_without_work = (
                previous_incomplete is not None
                and not made_progress
                and previous_incomplete.command_activity == 0
                and previous_incomplete.file_activity == 0
                and _normalized_message(previous_incomplete.message)
                == _normalized_message(result.message)
            )
            if zero_progress_streak >= 2 or repeated_without_work:
                raise RuntimeError(
                    "Local agent made no progress across two continuation turns. "
                    f"Last message: {_bounded(result.message)}"
                )
            previous_incomplete = result
        raise RuntimeError(
            "Local agent remained incomplete after two continuation turns. "
            f"Last message: {_bounded(result.message)}"
        )
    finally:
        if thread_id:
            try:
                await asyncio.shield(_cleanup_session(thread_id))
            except Exception as exc:
                _LOGGER.warning("session cleanup error thread=%s error=%s", thread_id, exc)


server = FastMCP(
    name="unsloth-local-agent",
    instructions=_CODEX_SUBAGENT_ROUTING_INSTRUCTIONS,
    log_level="WARNING",
)


@server.tool(description="List the approved local-agent model aliases.")
def list_agent_models() -> dict[str, Any]:
    default, models = _models()
    return {
        "default": default,
        "models": [
            {
                "alias": alias,
                "id": entry["id"],
                "context_window": entry["context_window"],
                "purpose": entry["purpose"],
            }
            for alias, entry in models.items()
        ],
    }


@server.tool(description="Run one local Codex child using an approved model alias.")
async def spawn_local_agent(task: str, ctx: Context, model: str | None = None) -> str:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("A non-empty task is required.")
    alias, model_id, context_window = _resolve_model(model)
    async with _single_flight():
        return await _run_local_agent(task.strip(), alias, model_id, context_window, ctx)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print(json.dumps(list_agent_models(), indent=2))
        return
    if len(sys.argv) > 1:
        os.environ[_CODEX_SUBAGENT_CONFIG_ENV] = sys.argv[1]
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
