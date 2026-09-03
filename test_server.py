# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Henry Zhang.
from __future__ import annotations

import asyncio
import ctypes
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import server


def turn_stdout(
    thread_id: str = "thread-1",
    message: str = "Still working.",
    *,
    marker: bool = False,
    tool_event: bool = False,
) -> str:
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
    ]
    if tool_event:
        events.append(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "test"},
            }
        )
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": message + ("\nLOCAL_AGENT_COMPLETE" if marker else ""),
                },
            },
            {"type": "turn.completed"},
        ]
    )
    return "\n".join(json.dumps(event) for event in events)


def turn_result(*args: object, **kwargs: object) -> server._TurnResult:
    return server._parse_turn(turn_stdout(*args, **kwargs))


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

    def test_task_prompt_uses_only_minimal_completion_contract(self) -> None:
        self.assertEqual(
            server._task_prompt("do the work"),
            "You are a local coding subagent powered by Unsloth. Complete the assigned "
            "task directly, use the available tools when useful, verify your work, and "
            "return a concise result to the parent agent.\n\n"
            "When the assigned task and its verification are complete, put "
            "LOCAL_AGENT_COMPLETE on the final line of your final response.\n\n"
            "Task: do the work",
        )

    def test_result_rejects_progress_message_as_completion(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "Let me inspect the remaining files.",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "before confirming task completion"):
            server._validated_result_text(stdout)

    def test_parse_turn_returns_incomplete_without_marker(self) -> None:
        result = server._parse_turn(turn_stdout(message="Now let me run the test."))

        self.assertIs(result.state, server._TurnState.INCOMPLETE)
        self.assertEqual(result.thread_id, "thread-1")
        self.assertEqual(result.message, "Now let me run the test.")

    def test_result_rejects_marker_without_completed_turn(self) -> None:
        stdout = json.dumps(
            {"type": "thread.started", "thread_id": "thread-1"}
        ) + "\n" + json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Implemented and tested.\nLOCAL_AGENT_COMPLETE",
                },
            }
        )

        with self.assertRaisesRegex(RuntimeError, "without a turn.completed event"):
            server._validated_result_text(stdout)

    def test_result_accepts_completed_turn_and_removes_marker(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "Implemented and tested.\nLOCAL_AGENT_COMPLETE",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        )

        self.assertEqual(
            server._validated_result_text(stdout),
            "Implemented and tested.",
        )

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

    async def test_incomplete_first_turn_resumes_same_thread(self) -> None:
        run_turn = AsyncMock(
            side_effect=[turn_result(), turn_result(message="Done.", marker=True)]
        )
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            result = await server._run_local_agent("task", "alias", "model", 1024)

        self.assertEqual(result, "Done.")
        self.assertEqual(run_turn.await_count, 2)
        self.assertIsNone(run_turn.await_args_list[0].args[6])
        self.assertEqual(run_turn.await_args_list[1].args[6], "thread-1")

    async def test_completed_first_turn_does_not_resume(self) -> None:
        run_turn = AsyncMock(return_value=turn_result(message="Done.", marker=True))
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            await server._run_local_agent("task", "alias", "model", 1024)

        run_turn.assert_awaited_once()

    async def test_resume_thread_id_mismatch_fails(self) -> None:
        run_turn = AsyncMock(
            side_effect=[turn_result("thread-1"), turn_result("thread-2")]
        )
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            with self.assertRaisesRegex(RuntimeError, "different thread"):
                await server._run_local_agent("task", "alias", "model", 1024)

    async def test_explicit_failed_turn_does_not_resume(self) -> None:
        run_turn = AsyncMock(side_effect=server._TurnFailure("broken", "thread-1"))
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken"):
                await server._run_local_agent("task", "alias", "model", 1024)

        run_turn.assert_awaited_once()

    async def test_continuation_stops_after_two_retries(self) -> None:
        run_turn = AsyncMock(return_value=turn_result(tool_event=True))
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete after two continuation"):
                await server._run_local_agent("task", "alias", "model", 1024)

        self.assertEqual(run_turn.await_count, 3)

    async def test_zero_progress_continuations_fail_early(self) -> None:
        run_turn = AsyncMock(
            side_effect=[
                turn_result(message="Initial incomplete."),
                turn_result(message="First continuation."),
                turn_result(message="Second continuation."),
            ]
        )
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            with self.assertRaisesRegex(RuntimeError, "no progress"):
                await server._run_local_agent("task", "alias", "model", 1024)

        self.assertEqual(run_turn.await_count, 3)

    async def test_global_timeout_is_shared_across_continuations(self) -> None:
        run_turn = AsyncMock(
            side_effect=[turn_result(), turn_result(message="Done.", marker=True)]
        )
        with (
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            await server._run_local_agent("task", "alias", "model", 1024)

        self.assertEqual(
            run_turn.await_args_list[0].args[4], run_turn.await_args_list[1].args[4]
        )

    async def test_cancellation_during_resume_stops_child_and_releases_lock(self) -> None:
        resumed = asyncio.Event()
        cleanup = AsyncMock()

        async def runner(*args: object) -> server._TurnResult:
            if args[6] is None:
                return turn_result()
            resumed.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with (
            patch.object(server, "_run_child_turn", side_effect=runner),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            call = asyncio.create_task(
                server.spawn_local_agent("task", FakeContext())
            )
            await asyncio.wait_for(resumed.wait(), 1)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

        cleanup.assert_awaited_once_with("thread-1")

        async with server._single_flight():
            pass

    async def test_session_cleanup_runs_after_success(self) -> None:
        cleanup = AsyncMock()
        with (
            patch.object(
                server,
                "_run_child_turn",
                new_callable=AsyncMock,
                return_value=turn_result(message="Done.", marker=True),
            ),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            await server._run_local_agent("task", "alias", "model", 1024)

        cleanup.assert_awaited_once_with("thread-1")

    async def test_session_cleanup_runs_after_terminal_failure(self) -> None:
        cleanup = AsyncMock()
        with (
            patch.object(
                server,
                "_run_child_turn",
                new_callable=AsyncMock,
                side_effect=server._TurnFailure("broken", "thread-1"),
            ),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken"):
                await server._run_local_agent("task", "alias", "model", 1024)

        cleanup.assert_awaited_once_with("thread-1")

    async def test_session_cleanup_failure_does_not_override_success(self) -> None:
        with (
            patch.object(
                server,
                "_run_child_turn",
                new_callable=AsyncMock,
                return_value=turn_result(message="Done.", marker=True),
            ),
            patch.object(
                server,
                "_cleanup_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("cleanup failed"),
            ),
        ):
            result = await server._run_local_agent("task", "alias", "model", 1024)

        self.assertEqual(result, "Done.")

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
