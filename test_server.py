# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Henry Zhang.

from __future__ import annotations

import asyncio
import ctypes
import inspect
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import server


class FakeContext:
    def __init__(self) -> None:
        self.messages: list[str | None] = []
        self.activity = asyncio.Event()

    async def report_progress(
        self,
        progress: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        self.messages.append(message)
        self.activity.set()


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    def test_spawn_tool_is_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(server.spawn_local_agent))

    async def test_single_flight_fails_fast_when_busy(self) -> None:
        async with server._single_flight():
            with self.assertRaisesRegex(RuntimeError, "busy"):
                async with server._single_flight():
                    self.fail("A second worker must not enter the critical section")

    async def test_cancellation_reaches_child_runner_and_releases_lock(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_runner(*args: object, **kwargs: object) -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch.object(server, "_run_local_agent", side_effect=blocked_runner):
            call = asyncio.create_task(
                server.spawn_local_agent("test cancellation", FakeContext())
            )
            await asyncio.wait_for(started.wait(), 1)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            self.assertTrue(cancelled.is_set())

        async with server._single_flight():
            pass

    async def test_jsonl_activity_reports_progress_without_exposing_content(self) -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(b'{"type":"item.completed","text":"secret"}\n')
        stream.feed_eof()
        context = FakeContext()
        parts: list[str] = []
        activity = [0.0]

        await server._drain_stream(stream, parts, activity, context, report=True)

        self.assertEqual(parts, ['{"type":"item.completed","text":"secret"}\n'])
        self.assertEqual(len(context.messages), 1)
        self.assertNotIn("secret", context.messages[0] or "")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    async def test_cancellation_stops_real_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "child.pid"
            script = (
                "import json, os, pathlib, time; "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "print(json.dumps({'type':'turn.started'}), flush=True); "
                "time.sleep(60)"
            )
            context = FakeContext()
            config = {
                "api_key": "test-only",
                "codex_home": temp_dir,
                "bypass_permissions": False,
            }
            launch = [sys.executable, "-u", "-c", script]

            with (
                patch.object(server, "_config", return_value=config),
                patch.object(server, "_prefer_windows_cmd_sibling", return_value=sys.executable),
                patch.object(server, "_wsl_shim_env", return_value=({}, [])),
                patch.object(server, "_resolved_launch_command", return_value=launch),
            ):
                call = asyncio.create_task(
                    server._run_local_agent("test", "qwen38-q4", "test-model", 1024, context)
                )
                await asyncio.wait_for(context.activity.wait(), 5)
                pid = int(pid_path.read_text())
                call.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await call

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    self.assertEqual(ctypes.windll.kernel32.WaitForSingleObject(handle, 0), 0)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
