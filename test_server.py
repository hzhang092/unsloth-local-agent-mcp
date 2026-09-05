# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026 Henry Zhang.
from __future__ import annotations

import asyncio
import ctypes
from contextlib import contextmanager
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from typing import Iterator
from unittest.mock import AsyncMock, patch

import server


def turn_stdout(
    thread_id: str = "thread-1",
    message: str = "Still working.",
    *,
    marker: bool = False,
    tool_event: bool = False,
    command: str | None = None,
    output: str = "",
    exit_code: int | None = 0,
    status: str = "completed",
) -> str:
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
    ]
    if tool_event or command is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "command-1",
                    "type": "command_execution",
                    "command": command or "test",
                    "aggregated_output": output,
                    "exit_code": exit_code,
                    "status": status,
                },
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


def blocked_turn(*, marker: bool = False) -> server._TurnResult:
    return turn_result(
        message="Pytest could not create its private temp directory.",
        marker=marker,
        command="conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v",
        output=(
            r"PermissionError: [WinError 5] Access is denied: "
            r"'C:\Users\henry\AppData\Local\Temp\pytest-of-henry'"
        ),
        exit_code=1,
        status="failed",
    )


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


@contextmanager
def fake_codex_child(script: str) -> Iterator[None]:
    config = {
        "api_key": "test-only",
        "codex_home": os.getcwd(),
        "bypass_permissions": False,
    }
    with (
        patch.object(server, "_config", return_value=config),
        patch.object(server, "_prefer_windows_cmd_sibling", return_value=sys.executable),
        patch.object(server, "_wsl_shim_env", return_value=({}, [])),
        patch.object(
            server,
            "_resolved_launch_command",
            return_value=[sys.executable, "-u", "-c", script],
        ),
    ):
        yield


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    def test_spawn_tool_is_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(server.spawn_local_agent))

    def test_public_completed_result_has_no_parent_verification_request(self) -> None:
        payload = server._public_agent_result(
            server._AgentResult(server._AgentState.COMPLETED, "Done.", "thread-1")
        )

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["child_verification"], {"status": "completed"})
        self.assertNotIn("parent_verification_request", payload)

    def test_public_blocked_result_separates_child_and_parent_verification(self) -> None:
        command = "conda activate fourteen; python -m pytest tests/test_project_lifecycle.py -v"
        payload = server._public_agent_result(
            server._AgentResult(
                server._AgentState.VERIFICATION_BLOCKED,
                "Verification blocked.",
                "thread-1",
                server._VerificationBlock(
                    server._WINDOWS_PRIVATE_ACL_KIND,
                    command,
                    1,
                    "x" * 5000 + " PermissionError WinError 5 pytest-of-henry",
                ),
            )
        )

        self.assertEqual(payload["status"], "verification_blocked")
        self.assertEqual(payload["child_verification"]["status"], "infrastructure_blocked")
        self.assertEqual(payload["child_verification"]["failure_kind"], server._WINDOWS_PRIVATE_ACL_KIND)
        self.assertEqual(payload["parent_verification_request"]["executor"], "trusted_parent")
        self.assertEqual(payload["parent_verification_request"]["command"], command)
        self.assertLessEqual(len(payload["child_verification"]["evidence"]), server._MAX_VERIFICATION_EVIDENCE)

    async def test_spawn_tool_returns_structured_result(self) -> None:
        agent_result = server._AgentResult(
            server._AgentState.COMPLETED, "Done.", "thread-1"
        )
        with (
            patch.object(server, "_resolve_model", return_value=("alias", "model", 1024)),
            patch.object(server, "_run_local_agent", new_callable=AsyncMock, return_value=agent_result),
        ):
            payload = await server.spawn_local_agent("task", FakeContext())

        self.assertEqual(payload["status"], "completed")

    def test_parent_instruction_requires_separate_attribution(self) -> None:
        instructions = server.server.instructions
        self.assertIn("untrusted evidence", instructions)
        self.assertIn("trusted parent execution context", instructions)
        self.assertIn("separate from the child's infrastructure-blocked verification", instructions)

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

    def test_parse_turn_captures_completed_command_details(self) -> None:
        result = turn_result(
            command="python -m pytest test_example.py -v",
            output="test output",
            exit_code=1,
            status="failed",
        )

        self.assertEqual(
            result.commands,
            (server._CommandResult("python -m pytest test_example.py -v", "test output", 1, "failed"),),
        )

    def test_windows_pytest_private_acl_is_classified_as_infrastructure_failure(self) -> None:
        block = server._find_verification_block(blocked_turn().commands, is_windows=True)

        self.assertIsNotNone(block)
        self.assertEqual(block.kind, server._WINDOWS_PRIVATE_ACL_KIND)
        self.assertEqual(block.exit_code, 1)
        self.assertIn("pytest-of-henry", block.evidence)

    def test_pytest_assertion_failure_is_not_classified_as_infrastructure_failure(self) -> None:
        result = turn_result(
            command="python -m pytest test_example.py -v",
            output="AssertionError\nFAILED",
            exit_code=1,
            status="failed",
        )
        self.assertIsNone(server._find_verification_block(result.commands, is_windows=True))

    def test_non_pytest_permission_error_is_not_classified_as_verification_failure(self) -> None:
        result = turn_result(
            command="python app.py",
            output="PermissionError: [WinError 5] Access is denied: C:\\Temp\\pytest-of-henry",
            exit_code=1,
            status="failed",
        )
        self.assertIsNone(server._find_verification_block(result.commands, is_windows=True))

    def test_private_acl_classifier_is_disabled_off_windows(self) -> None:
        self.assertIsNone(
            server._find_verification_block(blocked_turn().commands, is_windows=False)
        )

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

    def test_thread_started_updates_run_state_immediately(self) -> None:
        state = server._ChildRunState()

        server._consume_stdout_event(
            state,
            {"type": "thread.started", "thread_id": "thread-A"},
        )

        self.assertEqual(state.thread_id, "thread-A")

    def test_latest_agent_message_replaces_previous_message(self) -> None:
        state = server._ChildRunState()

        server._consume_stdout_event(
            state,
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "First update."},
            },
        )
        server._consume_stdout_event(
            state,
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Latest update."},
            },
        )

        self.assertEqual(state.last_agent_message, "Latest update.")

    def test_turn_completed_updates_run_state(self) -> None:
        state = server._ChildRunState()

        server._consume_stdout_event(state, {"type": "turn.completed"})

        self.assertTrue(state.turn_completed)

    def test_command_output_is_not_retained_in_run_state(self) -> None:
        state = server._ChildRunState()
        output = "command output that must not be retained" * 10_000

        server._consume_stdout_event(
            state,
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "diagnostic-command",
                    "aggregated_output": output,
                },
            },
        )

        self.assertEqual(state.command_activity, 1)
        self.assertNotIn(output, repr(state))
        for value in vars(state).values():
            if isinstance(value, str):
                self.assertNotIn(output, value)
            elif isinstance(value, list):
                self.assertNotIn(output, value)

    async def test_stdout_accepts_jsonl_event_larger_than_default_asyncio_limit(self) -> None:
        payload = "x" * 200_000
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "diagnostic-command",
                "aggregated_output": payload,
            },
        }
        self.assertGreater(len(json.dumps(event).encode()), 65_536)
        script = "\n".join(
            [
                "import json",
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-large'}), flush=True)",
                "payload = 'x' * 200_000",
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'diagnostic-command', 'aggregated_output': payload}}), flush=True)",
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Done.\\nLOCAL_AGENT_COMPLETE'}}), flush=True)",
                "print(json.dumps({'type': 'turn.completed'}), flush=True)",
            ]
        )
        state = server._ChildRunState()

        with fake_codex_child(script):
            result = await server._run_child_turn(
                "task",
                "qwen38-q4",
                "test-model",
                1024,
                time.monotonic() + 30,
                None,
                None,
                state,
            )

        self.assertEqual(result.thread_id, "thread-large")
        self.assertEqual(result.state, server._TurnState.COMPLETED)
        self.assertEqual(state.command_activity, 1)

    async def test_stdout_accepts_multi_mib_jsonl_event_below_limit(self) -> None:
        payload_size = 4 * 1024 * 1024
        script = "\n".join(
            [
                "import json",
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-multi-mib'}), flush=True)",
                f"payload = 'x' * {payload_size}",
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': payload}}), flush=True)",
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Done.\\nLOCAL_AGENT_COMPLETE'}}), flush=True)",
                "print(json.dumps({'type': 'turn.completed'}), flush=True)",
            ]
        )
        state = server._ChildRunState()

        with fake_codex_child(script):
            result = await server._run_child_turn(
                "task",
                "qwen38-q4",
                "test-model",
                1024,
                time.monotonic() + 30,
                state=state,
            )

        self.assertEqual(result.state, server._TurnState.COMPLETED)
        self.assertEqual(state.command_activity, 1)
        self.assertNotIn("x" * payload_size, repr(state))

    async def test_stdout_rejects_jsonl_event_over_16_mib_with_protocol_error(self) -> None:
        payload_size = server._JSONL_EVENT_LIMIT_BYTES + 1024
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "diagnostic-command",
                "aggregated_output": "x" * payload_size,
            },
        }
        self.assertGreater(
            len(json.dumps(event).encode()), server._JSONL_EVENT_LIMIT_BYTES
        )
        script = "\n".join(
            [
                "import json",
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-over-limit'}), flush=True)",
                f"payload = 'x' * {payload_size}",
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'diagnostic-command', 'aggregated_output': payload}}), flush=True)",
            ]
        )
        cleanup = AsyncMock()

        with (
            fake_codex_child(script),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "JSONL event exceeding the 16 MiB transport limit",
            ) as raised:
                await server.spawn_local_agent("task", FakeContext())

        self.assertNotIn("Separator is found", str(raised.exception))
        cleanup.assert_awaited_once_with("thread-over-limit")
        async with server._single_flight():
            pass

    async def test_stderr_accepts_large_output_without_newline(self) -> None:
        stream = asyncio.StreamReader()
        data = b"x" * (server._STDERR_CAPTURE_LIMIT_BYTES + 65_536)
        stream.feed_data(data)
        stream.feed_eof()
        tail = bytearray()
        activity = [0.0]

        await server._drain_stderr(stream, tail, activity)

        self.assertEqual(len(tail), server._STDERR_CAPTURE_LIMIT_BYTES)
        self.assertEqual(bytes(tail), data[-server._STDERR_CAPTURE_LIMIT_BYTES :])
        self.assertGreater(activity[0], 0.0)

    async def test_thread_is_cleaned_up_when_stream_fails_after_thread_started(self) -> None:
        cleanup = AsyncMock()

        async def failing_runner(*args: object, **kwargs: object) -> server._TurnResult:
            state = kwargs.get("state")
            if state is None and len(args) > 7:
                state = args[7]
            self.assertIsInstance(state, server._ChildRunState)
            server._consume_stdout_event(
                state,
                {"type": "thread.started", "thread_id": "thread-stream-failure"},
            )
            raise RuntimeError("stream failed after thread.started")

        with (
            patch.object(server, "_run_child_turn", side_effect=failing_runner),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            with self.assertRaisesRegex(RuntimeError, "stream failed after thread.started"):
                await server._run_local_agent(
                    "task",
                    "qwen38-q4",
                    "test-model",
                    1024,
                )

        cleanup.assert_awaited_once_with("thread-stream-failure")

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

        self.assertEqual(result.message, "Done.")
        self.assertIs(result.state, server._AgentState.COMPLETED)
        self.assertEqual(run_turn.await_count, 2)
        self.assertIsNone(run_turn.await_args_list[0].args[6])
        self.assertEqual(run_turn.await_args_list[1].args[6], "thread-1")

    async def test_blocked_verification_stops_before_continuation(self) -> None:
        run_turn = AsyncMock(return_value=blocked_turn())
        with (
            patch.object(server.os, "name", "nt"),
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            result = await server._run_local_agent("task", "alias", "model", 1024)

        run_turn.assert_awaited_once()
        self.assertIs(result.state, server._AgentState.VERIFICATION_BLOCKED)

    async def test_blocked_verification_overrides_completion_marker(self) -> None:
        with (
            patch.object(server.os, "name", "nt"),
            patch.object(server, "_run_child_turn", new_callable=AsyncMock, return_value=blocked_turn(marker=True)),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            result = await server._run_local_agent("task", "alias", "model", 1024)

        self.assertIs(result.state, server._AgentState.VERIFICATION_BLOCKED)

    async def test_blocked_verification_still_cleans_session_once(self) -> None:
        cleanup = AsyncMock()
        with (
            patch.object(server.os, "name", "nt"),
            patch.object(server, "_run_child_turn", new_callable=AsyncMock, return_value=blocked_turn()),
            patch.object(server, "_cleanup_session", cleanup),
        ):
            await server._run_local_agent("task", "alias", "model", 1024)

        cleanup.assert_awaited_once_with("thread-1")

    async def test_normal_incomplete_turn_still_uses_existing_continuation(self) -> None:
        run_turn = AsyncMock(side_effect=[turn_result(), turn_result(message="Done.", marker=True)])
        with (
            patch.object(server.os, "name", "nt"),
            patch.object(server, "_run_child_turn", run_turn),
            patch.object(server, "_cleanup_session", new_callable=AsyncMock),
        ):
            result = await server._run_local_agent("task", "alias", "model", 1024)

        self.assertIs(result.state, server._AgentState.COMPLETED)
        self.assertEqual(run_turn.await_count, 2)

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

        self.assertEqual(result.message, "Done.")
        self.assertIs(result.state, server._AgentState.COMPLETED)

    async def test_jsonl_activity_reports_progress_without_exposing_content(self) -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(
            b'{"type":"item.completed","item":{"type":"agent_message","text":"secret"}}\n'
        )
        stream.feed_eof()
        context = FakeContext()
        state = server._ChildRunState()
        activity = [0.0]

        await server._drain_jsonl_stdout(stream, state, activity, context)

        self.assertEqual(state.last_agent_message, "secret")
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
