from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent as agent_module
from agent import (
    AgentCancelledError,
    AgentConfigurationError,
    AgentInvocationError,
    AgentOutputError,
    CodexStructuredAgent,
)
from codex_sessions import AGENT_SESSION_ORIGINATOR


class FakeCodex:
    options: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    thread_options: dict[str, Any] | None = None
    last_thread: FakeThread | None = None

    def __init__(self, options: dict[str, Any]) -> None:
        type(self).options = options

    def start_thread(self, options: dict[str, Any]) -> FakeThread:
        type(self).thread_options = options
        thread = FakeThread(type(self).events)
        type(self).last_thread = thread
        return thread


class FakeThread:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.id: str | None = None
        self._events = events
        self.prompt: str | None = None
        self.turn_options: dict[str, Any] | None = None

    async def run_streamed(
        self,
        prompt: str,
        turn_options: dict[str, Any],
    ) -> SimpleNamespace:
        self.prompt = prompt
        self.turn_options = turn_options

        async def iterate() -> AsyncGenerator[dict[str, Any], None]:
            for event in self._events:
                if event.get("type") == "thread.started":
                    self.id = event.get("thread_id")
                yield event

        return SimpleNamespace(events=iterate())


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        codex_path="",
        reasoning_effort="medium",
        analysis_web_search="disabled",
        model="",
        turn_timeout_seconds=2,
    )


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _success_events(response: object) -> list[dict[str, Any]]:
    return [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {"id": "reasoning-1", "type": "reasoning", "text": "checked"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "message-1",
                "type": "agent_message",
                "text": json.dumps(response),
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 4,
            },
        },
    ]


def test_codex_agent_disables_execution_and_configured_mcp_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.local_tools]\ncommand = "tool-server"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PIPELINE_TEST_INHERITED_ENV", "preserved")
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "foreign-originator")
    monkeypatch.setattr(agent_module, "Codex", FakeCodex)

    agent = CodexStructuredAgent(SimpleNamespace(codex_path=""))  # type: ignore[arg-type]
    try:
        assert FakeCodex.options is not None
        config = FakeCodex.options["config"]
        assert config["features"]["shell_tool"] is False
        assert config["features"]["unified_exec"] is False
        assert config["features"]["plugins"] is False
        assert config["mcp_servers"] == {"local_tools": {"enabled": False}}
        environment = FakeCodex.options["env"]
        assert environment["PIPELINE_TEST_INHERITED_ENV"] == "preserved"
        assert (
            environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"]
            == AGENT_SESSION_ORIGINATOR
        )
        assert Path(agent._working_directory).is_dir()
        assert not Path(agent._working_directory).is_relative_to(Path.cwd())
    finally:
        agent._workspace.cleanup()


def test_codex_agent_rejects_mcp_names_that_cannot_be_safely_overridden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers."unsafe.name"]\ncommand = "tool-server"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(AgentConfigurationError, match="MCP server names"):
        CodexStructuredAgent(SimpleNamespace(codex_path=""))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_codex_agent_collects_complete_success_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _success_events({"value": "result"})
    FakeCodex.events = events
    monkeypatch.setattr(agent_module, "Codex", FakeCodex)
    agent = CodexStructuredAgent(_settings())  # type: ignore[arg-type]

    try:
        response = await agent.run(
            "complete prompt",
            _schema(),
            enable_web_search=False,
            stage="translation",
            sequence_number=2,
            agent_input={"subtitle_text": "source", "chunk_index": 2},
        )
    finally:
        agent._workspace.cleanup()

    invocation = response.invocation
    assert invocation is not None
    assert response.payload == {"value": "result"}
    assert invocation.status == "succeeded"
    assert invocation.stage == "translation"
    assert invocation.sequence_number == 2
    assert invocation.thread_id == "thread-1"
    assert invocation.agent_input == {"subtitle_text": "source", "chunk_index": 2}
    assert invocation.full_prompt == "complete prompt"
    assert invocation.intermediate_events == events
    assert invocation.final_response == '{"value": "result"}'
    assert invocation.output_payload == {"value": "result"}
    assert invocation.usage == events[-1]["usage"]
    assert invocation.error_message is None
    assert invocation.started_at.tzinfo is not None
    assert invocation.finished_at >= invocation.started_at


@pytest.mark.asyncio
async def test_codex_agent_keeps_reconnect_error_event_until_turn_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _success_events({"value": "result"})
    events.insert(
        1,
        {"type": "error", "message": "Reconnecting after a transient upstream error"},
    )
    FakeCodex.events = events
    monkeypatch.setattr(agent_module, "Codex", FakeCodex)
    agent = CodexStructuredAgent(_settings())  # type: ignore[arg-type]

    try:
        response = await agent.run(
            "complete prompt",
            _schema(),
            enable_web_search=False,
            stage="analysis",
            sequence_number=1,
            agent_input={"subtitle_text": "source"},
        )
    finally:
        agent._workspace.cleanup()

    assert response.payload == {"value": "result"}
    assert response.invocation is not None
    assert response.invocation.intermediate_events == events


@pytest.mark.asyncio
async def test_codex_agent_exposes_cancelled_stream_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_started = asyncio.Event()

    class BlockingThread:
        async def run_streamed(
            self,
            prompt: str,
            turn_options: dict[str, Any],
        ) -> SimpleNamespace:
            del prompt, turn_options

            async def iterate() -> AsyncGenerator[dict[str, Any], None]:
                yield {"type": "thread.started", "thread_id": "thread-cancelled"}
                stream_started.set()
                await asyncio.Future()

            return SimpleNamespace(events=iterate())

    class BlockingCodex:
        def __init__(self, options: dict[str, Any]) -> None:
            del options

        def start_thread(self, options: dict[str, Any]) -> BlockingThread:
            del options
            return BlockingThread()

    monkeypatch.setattr(agent_module, "Codex", BlockingCodex)
    agent = CodexStructuredAgent(_settings())  # type: ignore[arg-type]
    task = asyncio.create_task(
        agent.run(
            "cancelled prompt",
            _schema(),
            enable_web_search=False,
            stage="translation",
            sequence_number=1,
            agent_input={"subtitle_text": "source"},
        )
    )

    try:
        await stream_started.wait()
        task.cancel()
        with pytest.raises(AgentCancelledError) as raised:
            await task
    finally:
        agent._workspace.cleanup()

    invocation = raised.value.invocation
    assert invocation.status == "cancelled"
    assert invocation.thread_id == "thread-cancelled"
    assert invocation.error_message == "Codex invocation cancelled"
    assert invocation.intermediate_events == [
        {"type": "thread.started", "thread_id": "thread-cancelled"}
    ]


@pytest.mark.asyncio
async def test_codex_agent_exposes_failed_stream_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "thread-failed"},
        {"type": "turn.failed", "error": {"message": "model unavailable"}},
    ]
    FakeCodex.events = events
    monkeypatch.setattr(agent_module, "Codex", FakeCodex)
    agent = CodexStructuredAgent(_settings())  # type: ignore[arg-type]

    try:
        with pytest.raises(AgentInvocationError, match="model unavailable") as raised:
            await agent.run(
                "failed prompt",
                _schema(),
                enable_web_search=True,
                stage="analysis",
                sequence_number=1,
                agent_input={"video_url": "https://youtube.test/video"},
            )
    finally:
        agent._workspace.cleanup()

    invocation = raised.value.invocation
    assert invocation.status == "failed"
    assert invocation.thread_id == "thread-failed"
    assert invocation.intermediate_events == events
    assert invocation.final_response is None
    assert invocation.output_payload is None
    assert invocation.error_message == "model unavailable"


@pytest.mark.asyncio
async def test_codex_agent_preserves_raw_and_parsed_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _success_events({"value": 3})
    FakeCodex.events = events
    monkeypatch.setattr(agent_module, "Codex", FakeCodex)
    agent = CodexStructuredAgent(_settings())  # type: ignore[arg-type]

    try:
        with pytest.raises(AgentOutputError, match="schema validation") as raised:
            await agent.run(
                "schema prompt",
                _schema(),
                enable_web_search=False,
                stage="translation",
                sequence_number=1,
                agent_input={"subtitle_text": "source"},
            )
    finally:
        agent._workspace.cleanup()

    invocation = raised.value.invocation
    assert invocation.status == "failed"
    assert invocation.final_response == '{"value": 3}'
    assert invocation.output_payload == {"value": 3}
    assert invocation.usage == events[-1]["usage"]
