from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agent import AgentCancelledError, AgentInvocationError
from analysis import AnalysisEngine, resolve_json_pointer, split_text
from config import load_settings
from models import (
    AgentInvocation,
    AgentResponse,
    ChannelMetadata,
    VideoMetadata,
)


class FakeAgent:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
    ) -> AgentResponse:
        del output_schema
        self.calls.append(
            {
                "prompt": prompt,
                "enable_web_search": enable_web_search,
                "stage": stage,
                "sequence_number": sequence_number,
                "agent_input": agent_input,
            }
        )
        payload = self.responses.pop(0)
        invocation = _invocation(
            stage=stage,
            sequence_number=sequence_number,
            prompt=prompt,
            agent_input=agent_input,
            payload=payload,
        )
        return AgentResponse(
            payload=payload,
            thread_id=f"thread-{len(self.calls)}",
            usage={"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
            invocation=invocation,
        )


class FailingAgent:
    async def run(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
    ) -> AgentResponse:
        del output_schema, enable_web_search
        invocation = _invocation(
            stage=stage,
            sequence_number=sequence_number,
            prompt=prompt,
            agent_input=agent_input,
            payload=None,
            status="failed",
            error_message="agent failed",
        )
        raise AgentInvocationError("agent failed", invocation)


class CancelledAgent:
    async def run(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
    ) -> AgentResponse:
        del output_schema, enable_web_search
        invocation = _invocation(
            stage=stage,
            sequence_number=sequence_number,
            prompt=prompt,
            agent_input=agent_input,
            payload=None,
            status="cancelled",
            error_message="Codex invocation cancelled",
        )
        raise AgentCancelledError("Codex invocation cancelled", invocation)


def _invocation(
    *,
    stage: str,
    sequence_number: int,
    prompt: str,
    agent_input: dict[str, Any],
    payload: dict[str, Any] | None,
    status: str = "succeeded",
    error_message: str | None = None,
) -> AgentInvocation:
    now = datetime.now(UTC)
    return AgentInvocation(
        stage=stage,
        sequence_number=sequence_number,
        status=status,
        thread_id=f"thread-{sequence_number}",
        agent_input=agent_input,
        full_prompt=prompt,
        intermediate_events=[{"type": "turn.completed"}],
        final_response=None,
        output_payload=payload,
        usage={"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        error_message=error_message,
        started_at=now,
        finished_at=now,
    )


def _video() -> VideoMetadata:
    return VideoMetadata(
        youtube_video_id="video-id",
        channel=ChannelMetadata(
            youtube_channel_id="channel-id",
            title="Research Channel",
            channel_url="https://youtube.test/channel",
        ),
        title="AI Research Video",
        video_url="https://youtube.test/watch?v=video-id",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_split_text_preserves_order_and_limit() -> None:
    chunks = split_text("first line\nsecond line is long", 10)

    assert chunks == ["first line", "second lin", "e is long"]
    assert all(len(chunk) <= 10 for chunk in chunks)


def test_resolve_json_pointer_supports_escaped_tokens_and_arrays() -> None:
    payload = {"a/b": [{"~key": "value"}]}

    assert resolve_json_pointer(payload, "/a~1b/0/~0key") == "value"


@pytest.mark.asyncio
async def test_simplified_chinese_subtitle_is_copied_without_agent() -> None:
    settings = await load_settings()
    fake = FakeAgent([])
    engine = AnalysisEngine(settings, fake)

    saved: list[AgentInvocation] = []

    async def save(invocation: AgentInvocation) -> None:
        saved.append(invocation)

    result = await engine.translate(
        "zh-CN",
        "原始中文字幕",
        invocation_sink=save,
    )

    assert result.translated_text == "原始中文字幕"
    assert result.translated_language_code == "zh-CN"
    assert result.metadata["mode"] == "copied_chinese_source"
    assert fake.calls == []
    assert saved == []


@pytest.mark.asyncio
async def test_traditional_chinese_subtitle_is_translated_to_simplified() -> None:
    settings = await load_settings()
    fake = FakeAgent([{"translated_text": "简体中文字幕"}])
    engine = AnalysisEngine(settings, fake)

    result = await engine.translate("zh-Hant", "繁體中文字幕")

    assert result.translated_text == "简体中文字幕"
    assert result.translated_language_code == "zh-Hans"
    assert result.metadata["mode"] == "codex_translation"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_non_chinese_translation_uses_configured_projection() -> None:
    settings = await load_settings()
    fake = FakeAgent([{"translated_text": "第一段"}, {"translated_text": "第二段"}])
    engine = AnalysisEngine(settings, fake)
    text = "a" * settings.agent.translation_chunk_chars + "\nlast"

    saved: list[AgentInvocation] = []

    async def save(invocation: AgentInvocation) -> None:
        saved.append(invocation)

    result = await engine.translate("en", text, invocation_sink=save)

    assert result.translated_text == "第一段\n第二段"
    assert result.translated_language_code == "zh-Hans"
    assert result.metadata["chunk_count"] == 2
    assert all(not call["enable_web_search"] for call in fake.calls)
    assert [call["stage"] for call in fake.calls] == ["translation", "translation"]
    assert [call["sequence_number"] for call in fake.calls] == [1, 2]
    assert fake.calls[0]["agent_input"]["chunk_count"] == 2
    assert [invocation.sequence_number for invocation in saved] == [1, 2]


@pytest.mark.asyncio
async def test_analysis_projects_common_fields_and_preserves_payload() -> None:
    settings = await load_settings()
    payload = {
        "is_relevant": True,
        "filter_reason": "相关",
        "relevance_score": 91,
        "quality_score": 87.5,
        "summary": "摘要",
        "background_notes": "背景",
        "key_points": ["要点"],
        "tags": [
            {
                "name": "推理",
                "category": "主题",
                "description": "推理技术",
                "confidence": 95,
            }
        ],
        "sources": [],
    }
    fake = FakeAgent([payload.copy()])
    engine = AnalysisEngine(settings, fake)

    saved: list[AgentInvocation] = []

    async def save(invocation: AgentInvocation) -> None:
        saved.append(invocation)

    result = await engine.analyze(
        _video(),
        "en",
        "中文字幕",
        invocation_sink=save,
    )

    assert result.payload == payload
    assert result.projection.relevance_score == 91.0
    assert result.projection.tags[0]["name"] == "推理"
    assert fake.calls[0]["enable_web_search"] is True
    assert fake.calls[0]["stage"] == "analysis"
    assert fake.calls[0]["sequence_number"] == 1
    assert fake.calls[0]["agent_input"]["subtitle_text"] == "中文字幕"
    assert "中文字幕" in fake.calls[0]["prompt"]
    assert saved[0].stage == "analysis"


@pytest.mark.asyncio
async def test_failed_agent_invocation_is_sent_to_sink_before_reraising() -> None:
    settings = await load_settings()
    engine = AnalysisEngine(settings, FailingAgent())
    saved: list[AgentInvocation] = []

    async def save(invocation: AgentInvocation) -> None:
        saved.append(invocation)

    with pytest.raises(AgentInvocationError, match="agent failed"):
        await engine.translate("en", "source subtitle", invocation_sink=save)

    assert len(saved) == 1
    assert saved[0].status == "failed"
    assert saved[0].stage == "translation"
    assert saved[0].sequence_number == 1


@pytest.mark.asyncio
async def test_cancelled_agent_invocation_is_sent_to_sink_before_reraising() -> None:
    settings = await load_settings()
    engine = AnalysisEngine(settings, CancelledAgent())
    saved: list[AgentInvocation] = []

    async def save(invocation: AgentInvocation) -> None:
        saved.append(invocation)

    with pytest.raises(AgentCancelledError):
        await engine.analyze(_video(), "en", "中文字幕", invocation_sink=save)

    assert len(saved) == 1
    assert saved[0].status == "cancelled"
    assert saved[0].stage == "analysis"
