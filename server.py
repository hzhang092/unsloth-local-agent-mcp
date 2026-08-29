# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team.
# Modifications Copyright 2026 Henry Zhang.

from __future__ import annotations

import json
import logging
import msvcrt
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mcp.server.fastmcp import FastMCP
from unsloth_cli.claude_subagent_mcp import _bounded, _stop_child
from unsloth_cli.codex_subagent_mcp import _config, _result_text
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
_MODELS_PATH = Path(__file__).with_name("models.json")
_LOCK_PATH = Path(__file__).with_name("qwen38.lock")
_LOGGER = logging.getLogger("unsloth-mcp")
_THREAD_LOCK = threading.Lock()


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


@contextmanager
def _single_flight() -> Iterator[None]:
    with _THREAD_LOCK:
        _LOCK_PATH.touch(exist_ok=True)
        with _LOCK_PATH.open("r+b") as lock_file:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(_CANCEL_POLL_SECONDS)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


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


def _run_local_agent(task: str, alias: str, model_id: str, context_window: int) -> str:
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
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        f"{_SUBAGENT_INSTRUCTIONS}\n\nTask: {task}",
    ]
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
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    launch_command = _resolved_launch_command(executable, command[1:], child_env)
    _LOGGER.info("child start alias=%s model=%s", alias, model_id)
    started = time.monotonic()
    process = subprocess.Popen(launch_command, **popen_kwargs)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def drain(stream: Any, parts: list[str]) -> None:
        parts.append(stream.read() or "")

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_parts), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_parts), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None:
            if time.monotonic() - started >= _CHILD_TIMEOUT_SECONDS:
                _stop_child(process)
                raise RuntimeError(
                    f"Local-agent timeout after {_CHILD_TIMEOUT_SECONDS} seconds ({alias} -> {model_id})."
                )
            time.sleep(_CANCEL_POLL_SECONDS)
        for reader in readers:
            reader.join()
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
    except BaseException:
        if process.poll() is None:
            _stop_child(process)
        raise
    elapsed = time.monotonic() - started
    _LOGGER.info("child exit alias=%s status=%s elapsed=%.1fs", alias, process.returncode, elapsed)
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip()
        raise _failure(detail or f"Local Codex exited with code {process.returncode}.", alias, model_id)
    return _result_text(stdout)


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
def spawn_local_agent(task: str, model: str | None = None) -> str:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("A non-empty task is required.")
    alias, model_id, context_window = _resolve_model(model)
    with _single_flight():
        return _run_local_agent(task.strip(), alias, model_id, context_window)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        print(json.dumps(list_agent_models(), indent=2))
        return
    if len(sys.argv) > 1:
        os.environ[_CODEX_SUBAGENT_CONFIG_ENV] = sys.argv[1]
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

