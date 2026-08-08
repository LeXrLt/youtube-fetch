from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from agent import (
    AgentCancelledError,
    AgentInvocationError,
    StructuredAgent,
)
from config import RuntimeSettings
from models import (
    AgentInvocation,
    AgentResponse,
    AnalysisOutcome,
    AnalysisProjection,
    TranslationResult,
    VideoMetadata,
)
from subtitles import is_chinese_language


class AnalysisInputError(ValueError):
    """Raised when transcript or configured projections are invalid."""


InvocationSink = Callable[[AgentInvocation], Awaitable[None]]


def resolve_json_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise AnalysisInputError(f"JSON Pointer must start with '/': {pointer}")

    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise AnalysisInputError(f"JSON Pointer does not exist: {pointer}")
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise AnalysisInputError(f"Invalid array JSON Pointer: {pointer}") from exc
        else:
            raise AnalysisInputError(f"JSON Pointer traverses a scalar value: {pointer}")
    return current


def split_text(text: str, max_chars: int) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not text.strip():
        raise AnalysisInputError("Subtitle text is empty")

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line:
            continue
        pieces = [line[index : index + max_chars] for index in range(0, len(line), max_chars)]
        for piece in pieces:
            separator_length = 1 if current else 0
            if current and current_length + separator_length + len(piece) > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
                separator_length = 0
            current.append(piece)
            current_length += separator_length + len(piece)
    if current:
        chunks.append("\n".join(current))
    return chunks


class AnalysisEngine:
    def __init__(self, settings: RuntimeSettings, agent: StructuredAgent) -> None:
        self._settings = settings
        self._agent = agent

    async def translate(
        self,
        source_language: str,
        subtitle_text: str,
        *,
        invocation_sink: InvocationSink | None = None,
    ) -> TranslationResult:
        copied_translation = self.copy_chinese_source(
            source_language,
            subtitle_text,
        )
        if copied_translation is not None:
            return copied_translation

        chunks = split_text(subtitle_text, self._settings.agent.translation_chunk_chars)
        translated_chunks: list[str] = []
        thread_ids: list[str] = []
        usage: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            agent_input = {
                "source_language": source_language,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "subtitle_text": chunk,
            }
            prompt = self._settings.prompts.translation.render(**agent_input)
            response = await self._run_agent(
                prompt,
                self._settings.translation_schema,
                enable_web_search=False,
                stage="translation",
                sequence_number=index,
                agent_input=agent_input,
                invocation_sink=invocation_sink,
            )
            translated = resolve_json_pointer(
                response.payload,
                self._settings.projection.translated_text,
            )
            if not isinstance(translated, str) or not translated.strip():
                raise AnalysisInputError(
                    "Translated subtitle projection must be a non-empty string"
                )
            translated_chunks.append(translated.strip())
            if response.thread_id:
                thread_ids.append(response.thread_id)
            if response.usage:
                usage.append(response.usage)

        return TranslationResult(
            translated_text="\n".join(translated_chunks),
            translated_language_code="zh-Hans",
            metadata={
                "mode": "codex_translation",
                "prompt_version": self._settings.prompts.translation.version,
                "translation_schema_sha256": self._settings.translation_schema_sha256,
                "chunk_count": len(chunks),
                "thread_ids": thread_ids,
                "usage": usage,
            },
        )

    def copy_chinese_source(
        self,
        source_language: str,
        subtitle_text: str,
    ) -> TranslationResult | None:
        if not is_chinese_language(source_language):
            return None
        return TranslationResult(
            translated_text=subtitle_text,
            translated_language_code=source_language,
            metadata={
                "mode": "copied_chinese_source",
                "prompt_version": self._settings.prompts.translation.version,
                "translation_schema_sha256": self._settings.translation_schema_sha256,
                "thread_ids": [],
                "usage": [],
            },
        )

    async def analyze(
        self,
        video: VideoMetadata,
        source_language: str,
        translated_text: str,
        *,
        invocation_sink: InvocationSink | None = None,
    ) -> AnalysisOutcome:
        if len(translated_text) > self._settings.agent.analysis_input_max_chars:
            raise AnalysisInputError(
                "Translated subtitle exceeds analysis_input_max_chars; "
                "increase the configured limit or add a map-reduce profile"
            )

        agent_input = {
            "title": video.title,
            "channel_title": video.channel.title,
            "published_at": (
                video.published_at.isoformat() if video.published_at else "unknown"
            ),
            "video_url": video.video_url,
            "source_language": source_language,
            "subtitle_text": translated_text,
        }
        prompt = self._settings.prompts.analysis.render(**agent_input)
        response = await self._run_agent(
            prompt,
            self._settings.analysis_schema,
            enable_web_search=True,
            stage="analysis",
            sequence_number=1,
            agent_input=agent_input,
            invocation_sink=invocation_sink,
        )
        projection = self._project(response.payload)
        return AnalysisOutcome(
            payload=response.payload,
            projection=projection,
            metadata={
                "thread_id": response.thread_id,
                "usage": response.usage,
                "profile_name": self._settings.agent.profile_name,
                "schema_version": self._settings.agent.schema_version,
                "schema_sha256": self._settings.analysis_schema_sha256,
            },
        )

    async def _run_agent(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
        invocation_sink: InvocationSink | None,
    ) -> AgentResponse:
        try:
            response = await self._agent.run(
                prompt,
                output_schema,
                enable_web_search=enable_web_search,
                stage=stage,
                sequence_number=sequence_number,
                agent_input=agent_input,
            )
        except AgentCancelledError as exc:
            if invocation_sink is not None:
                await asyncio.shield(invocation_sink(exc.invocation))
            raise
        except AgentInvocationError as exc:
            if invocation_sink is not None:
                await invocation_sink(exc.invocation)
            raise

        if invocation_sink is not None:
            if response.invocation is None:
                raise RuntimeError("StructuredAgent returned no invocation audit record")
            await invocation_sink(response.invocation)
        return response

    def _project(self, payload: dict[str, Any]) -> AnalysisProjection:
        paths = self._settings.projection
        is_relevant = resolve_json_pointer(payload, paths.is_relevant)
        relevance_score = resolve_json_pointer(payload, paths.relevance_score)
        quality_score = resolve_json_pointer(payload, paths.quality_score)
        summary = resolve_json_pointer(payload, paths.summary)
        translated_summary = resolve_json_pointer(payload, paths.translated_summary)
        background_notes = resolve_json_pointer(payload, paths.background_notes)
        key_points = resolve_json_pointer(payload, paths.key_points)
        raw_tags = resolve_json_pointer(payload, paths.tags)

        if not isinstance(is_relevant, bool):
            raise AnalysisInputError("is_relevant projection must be a boolean")
        projected_tags = self._project_tags(raw_tags)
        return AnalysisProjection(
            is_relevant=is_relevant,
            relevance_score=_optional_number(relevance_score, "relevance_score"),
            quality_score=_optional_number(quality_score, "quality_score"),
            summary=_optional_string(summary, "summary"),
            translated_summary=_optional_string(translated_summary, "translated_summary"),
            background_notes=_optional_string(background_notes, "background_notes"),
            key_points=_list_value(key_points, "key_points"),
            tags=projected_tags,
        )

    def _project_tags(self, raw_tags: object) -> list[dict[str, Any]]:
        tags = _list_value(raw_tags, "tags")
        projected: list[dict[str, Any]] = []
        paths = self._settings.projection
        for tag in tags:
            if not isinstance(tag, Mapping):
                raise AnalysisInputError("Each projected tag must be an object")
            projected.append(
                {
                    "name": _required_string(
                        resolve_json_pointer(tag, paths.tag_name),
                        "tag_name",
                    ),
                    "category": _optional_string(
                        resolve_json_pointer(tag, paths.tag_category),
                        "tag_category",
                    ),
                    "description": _optional_string(
                        resolve_json_pointer(tag, paths.tag_description),
                        "tag_description",
                    ),
                    "confidence": _optional_number(
                        resolve_json_pointer(tag, paths.tag_confidence),
                        "tag_confidence",
                    ),
                }
            )
        return projected


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AnalysisInputError(f"{name} projection must be numeric or null")
    return float(value)


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnalysisInputError(f"{name} projection must be a string or null")
    return value


def _required_string(value: object, name: str) -> str:
    projected = _optional_string(value, name)
    if not projected:
        raise AnalysisInputError(f"{name} projection must be a non-empty string")
    return projected


def _list_value(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise AnalysisInputError(f"{name} projection must be an array")
    return value
