from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChannelMetadata:
    youtube_channel_id: str
    title: str
    channel_url: str
    handle: str | None = None
    description: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    id: UUID
    youtube_channel_id: str
    title: str
    channel_url: str
    is_subscribed: bool
    initial_backfill_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    youtube_video_id: str
    channel: ChannelMetadata
    title: str
    video_url: str
    description: str | None = None
    duration_seconds: int | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    language_code: str
    language_name: str | None
    source_format: str
    is_auto_generated: bool
    url: str | None
    inline_data: str | None
    http_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DownloadedSubtitle:
    language_code: str
    language_name: str | None
    source_format: str
    is_auto_generated: bool
    raw_text: str
    normalized_text: str | None


@dataclass(frozen=True, slots=True)
class FetchedVideo:
    metadata: VideoMetadata
    subtitle: DownloadedSubtitle | None


class SubtitleDownloadStatus(IntEnum):
    PENDING = 0
    DOWNLOADED = 1
    FAILED = 2


@dataclass(frozen=True, slots=True)
class VideoReference:
    youtube_video_id: str
    video_url: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class VideoDiscoveryResult:
    references: list[VideoReference]
    source_exhausted: bool
    stopped_at_known: bool


@dataclass(frozen=True, slots=True)
class SubtitleDownloadTask:
    channel_id: UUID
    youtube_video_id: str
    video_url: str
    title: str
    status: SubtitleDownloadStatus


@dataclass(frozen=True, slots=True)
class StoredVideo:
    video_id: UUID
    subtitle_track_id: UUID | None
    fetched: FetchedVideo
    subtitle_status: str
    subtitle_download_status: SubtitleDownloadStatus
    translated_text: str | None
    translated_language_code: str | None
    translation_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    stage: str
    sequence_number: int
    status: str
    thread_id: str | None
    agent_input: dict[str, Any]
    full_prompt: str
    intermediate_events: list[dict[str, Any]]
    final_response: str | None
    output_payload: object | None
    usage: dict[str, Any] | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class AgentResponse:
    payload: dict[str, Any]
    thread_id: str | None
    usage: dict[str, Any] | None
    invocation: AgentInvocation | None = None


@dataclass(frozen=True, slots=True)
class AnalysisProjection:
    is_relevant: bool
    relevance_score: float | None
    quality_score: float | None
    summary: str | None
    translated_summary: str | None
    background_notes: str | None
    key_points: list[Any]
    tags: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translated_text: str
    translated_language_code: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    payload: dict[str, Any]
    projection: AnalysisProjection
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    video_url: str
    youtube_video_id: str | None
    status: str
    detail: str | None = None
