from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from main import _parser
from models import (
    AgentInvocation,
    AnalysisOutcome,
    AnalysisProjection,
    ChannelMetadata,
    ChannelRecord,
    DownloadedSubtitle,
    FetchedVideo,
    ProcessResult,
    StoredVideo,
    SubtitleDownloadStatus,
    SubtitleDownloadTask,
    TranslationResult,
    VideoMetadata,
    VideoReference,
)
from service import PipelineService

VIDEO_ID = uuid4()
SUBTITLE_ID = uuid4()
RUN_ID = uuid4()
CHANNEL_ID = uuid4()
VIDEO_URL = "https://www.youtube.com/watch?v=test-video"
CHANNEL_URL = "https://www.youtube.com/@test-channel"


def _invocation(stage: str) -> AgentInvocation:
    now = datetime.now(UTC)
    return AgentInvocation(
        stage=stage,
        sequence_number=1,
        status="succeeded",
        thread_id=f"{stage}-thread",
        agent_input={"stage": stage},
        full_prompt=f"{stage} prompt",
        intermediate_events=[{"type": "turn.completed"}],
        final_response="{}",
        output_payload={},
        usage={"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        error_message=None,
        started_at=now,
        finished_at=now,
    )


TRANSLATION_INVOCATION = _invocation("translation")
ANALYSIS_INVOCATION = _invocation("analysis")


def _settings(*, download_concurrency: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(
            profile_name="research-v1",
            schema_version="2026-08-04",
            model="gpt-test",
        ),
        analysis_schema_sha256="schema-digest",
        prompt_version="translation:t1;analysis:a1",
        prompt_sha256="prompt-digest",
        translation_schema_sha256="translation-schema-digest",
        prompts=SimpleNamespace(translation=SimpleNamespace(version="t1")),
        pipeline=SimpleNamespace(download_concurrency=download_concurrency),
    )


def _fetched_video(
    *,
    with_subtitle: bool = True,
    normalized_text: str | None = "Original subtitle",
    is_auto_generated: bool = False,
) -> FetchedVideo:
    metadata = VideoMetadata(
        youtube_video_id="test-video",
        channel=ChannelMetadata(
            youtube_channel_id="test-channel",
            title="Test Channel",
            channel_url="https://www.youtube.com/@test-channel",
        ),
        title="Test Video",
        video_url=VIDEO_URL,
    )
    subtitle = None
    if with_subtitle:
        subtitle = DownloadedSubtitle(
            language_code="en",
            language_name="English",
            source_format="vtt",
            is_auto_generated=is_auto_generated,
            raw_text="WEBVTT\n\nOriginal subtitle",
            normalized_text=normalized_text,
        )
    return FetchedVideo(metadata=metadata, subtitle=subtitle)


def _translation() -> TranslationResult:
    return TranslationResult(
        translated_text="translated subtitle",
        translated_language_code="zh-Hans",
        metadata={
            "mode": "codex_translation",
            "prompt_version": "t1",
            "translation_schema_sha256": "translation-schema-digest",
            "thread_ids": ["translation-thread"],
            "usage": [{"input": 12}],
        },
    )


def _outcome() -> AnalysisOutcome:
    return AnalysisOutcome(
        payload={"result": {"summary": "analysis"}},
        projection=AnalysisProjection(
            is_relevant=True,
            relevance_score=0.9,
            quality_score=0.8,
            summary="analysis",
            translated_summary="分析",
            background_notes=None,
            key_points=["point"],
            tags=[],
        ),
        metadata={"thread_id": "analysis-thread"},
    )


def _stored_video(
    fetched: FetchedVideo,
    *,
    subtitle_status: str = "fetched",
    subtitle_download_status: SubtitleDownloadStatus | None = None,
    translation: TranslationResult | None = None,
) -> StoredVideo:
    if subtitle_download_status is None:
        subtitle_download_status = (
            SubtitleDownloadStatus.PENDING
            if subtitle_status == "pending"
            else SubtitleDownloadStatus.DOWNLOADED
        )
    return StoredVideo(
        video_id=VIDEO_ID,
        subtitle_track_id=SUBTITLE_ID if fetched.subtitle is not None else None,
        fetched=fetched,
        subtitle_status=subtitle_status,
        subtitle_download_status=subtitle_download_status,
        translated_text=translation.translated_text if translation is not None else None,
        translated_language_code=(
            translation.translated_language_code if translation is not None else None
        ),
        translation_metadata=translation.metadata if translation is not None else {},
    )


def _channel_record(*, backfill_completed: bool = False) -> ChannelRecord:
    return ChannelRecord(
        id=CHANNEL_ID,
        youtube_channel_id="test-channel",
        title="Test Channel",
        channel_url=CHANNEL_URL,
        is_subscribed=True,
        initial_backfill_completed_at=(
            datetime.now(UTC) if backfill_completed else None
        ),
    )


def _download_task(
    youtube_video_id: str,
    status: SubtitleDownloadStatus,
    *,
    channel_id: UUID = CHANNEL_ID,
    title: str | None = None,
) -> SubtitleDownloadTask:
    return SubtitleDownloadTask(
        channel_id=channel_id,
        youtube_video_id=youtube_video_id,
        video_url=f"https://youtube.test/watch?v={youtube_video_id}",
        title=title or f"Video {youtube_video_id}",
        status=status,
    )


@dataclass
class FakeYoutube:
    fetched: FetchedVideo
    references: list[VideoReference] = field(default_factory=list)
    failing_channel_ids: set[str] = field(default_factory=set)
    failing_video_urls: set[str] = field(default_factory=set)
    event_log: list[str] | None = None
    fetch_delay: float = 0
    source_exhausted: bool = True
    stopped_at_known: bool = False
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    active_fetches: int = 0
    peak_fetches: int = 0

    async def fetch_video(self, video_url: str) -> FetchedVideo:
        self.calls.append(("fetch_video", (video_url,)))
        self.active_fetches += 1
        self.peak_fetches = max(self.peak_fetches, self.active_fetches)
        if self.event_log is not None:
            self.event_log.append(f"download:start:{video_url}")
        try:
            if self.fetch_delay:
                await asyncio.sleep(self.fetch_delay)
            if video_url in self.failing_video_urls:
                raise RuntimeError("video download failed")
            return self.fetched
        finally:
            self.active_fetches -= 1
            if self.event_log is not None:
                self.event_log.append(f"download:end:{video_url}")

    async def inspect_channel(self, channel_url: str) -> ChannelMetadata:
        self.calls.append(("inspect_channel", (channel_url,)))
        return self.fetched.metadata.channel

    async def discover_channel_videos(
        self,
        youtube_channel_id: str,
        limit: int | None,
        *,
        known_video_ids: set[str],
        stop_at_known: bool,
    ) -> SimpleNamespace:
        self.calls.append(
            (
                "discover_channel_videos",
                (
                    youtube_channel_id,
                    limit,
                    frozenset(known_video_ids),
                    stop_at_known,
                ),
            )
        )
        if youtube_channel_id in self.failing_channel_ids:
            raise RuntimeError("channel discovery failed")
        return SimpleNamespace(
            references=self.references,
            source_exhausted=self.source_exhausted,
            stopped_at_known=self.stopped_at_known,
        )


class FakeRepository:
    def __init__(
        self,
        *,
        matching_analysis: bool = False,
        stored_video: StoredVideo | None = None,
        channels: list[ChannelRecord] | None = None,
        analysis_candidates: list[VideoReference] | None = None,
        download_tasks: list[SubtitleDownloadTask] | None = None,
        known_video_ids_by_channel: dict[UUID, set[str]] | None = None,
    ) -> None:
        self.matching_analysis = matching_analysis
        self.stored_video = stored_video
        self.channels = channels or []
        self.analysis_candidates = analysis_candidates or []
        self.download_tasks = list(download_tasks or [])
        self.known_video_ids_by_channel = known_video_ids_by_channel
        self.subtitle_download_errors: dict[str, str] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def get_stored_video(self, youtube_video_id: str) -> StoredVideo | None:
        self.calls.append(("get_stored_video", (youtube_video_id,), {}))
        if (
            self.stored_video is not None
            and self.stored_video.fetched.metadata.youtube_video_id == youtube_video_id
        ):
            return self.stored_video
        return None

    async def register_channel(
        self,
        channel: ChannelMetadata,
        researcher_name: str | None,
    ) -> UUID:
        self.calls.append(("register_channel", (channel, researcher_name), {}))
        return CHANNEL_ID

    async def get_channels(self, channel_ids: list[UUID]) -> list[ChannelRecord]:
        self.calls.append(("get_channels", (channel_ids,), {}))
        return self.channels

    async def list_active_channels(self) -> list[ChannelRecord]:
        self.calls.append(("list_active_channels", (), {}))
        return self.channels

    async def list_known_video_ids(
        self,
        channel_ids: list[UUID],
    ) -> dict[UUID, set[str]]:
        self.calls.append(("list_known_video_ids", (channel_ids,), {}))
        if self.known_video_ids_by_channel is not None:
            return {
                channel_id: set(self.known_video_ids_by_channel.get(channel_id, set()))
                for channel_id in channel_ids
            }
        known_video_ids = {
            channel_id: {
                task.youtube_video_id
                for task in self.download_tasks
                if task.channel_id == channel_id
            }
            for channel_id in channel_ids
        }
        if self.stored_video is not None and CHANNEL_ID in known_video_ids:
            known_video_ids[CHANNEL_ID].add(
                self.stored_video.fetched.metadata.youtube_video_id
            )
        return known_video_ids

    async def enqueue_subtitle_downloads(
        self,
        channel_id: UUID,
        references: list[VideoReference],
    ) -> dict[str, SubtitleDownloadStatus]:
        self.calls.append(("enqueue_subtitle_downloads", (channel_id, references), {}))
        tasks_by_id = {
            task.youtube_video_id: task for task in self.download_tasks
        }
        for reference in references:
            existing = tasks_by_id.get(reference.youtube_video_id)
            if existing is not None:
                replacement = SubtitleDownloadTask(
                    channel_id=channel_id,
                    youtube_video_id=reference.youtube_video_id,
                    video_url=reference.video_url,
                    title=reference.title or existing.title,
                    status=existing.status,
                )
                self.download_tasks[self.download_tasks.index(existing)] = replacement
                tasks_by_id[reference.youtube_video_id] = replacement
                continue
            status = (
                self.stored_video.subtitle_download_status
                if self.stored_video is not None
                and self.stored_video.fetched.metadata.youtube_video_id
                == reference.youtube_video_id
                else SubtitleDownloadStatus.PENDING
            )
            task = SubtitleDownloadTask(
                channel_id=channel_id,
                youtube_video_id=reference.youtube_video_id,
                video_url=reference.video_url,
                title=reference.title or reference.youtube_video_id,
                status=status,
            )
            self.download_tasks.append(task)
            tasks_by_id[reference.youtube_video_id] = task
        return {
            reference.youtube_video_id: tasks_by_id[reference.youtube_video_id].status
            for reference in references
        }

    async def list_subtitle_download_candidates(
        self,
        channel_ids: list[UUID],
        force_video_ids: list[str],
    ) -> list[SubtitleDownloadTask]:
        self.calls.append(
            (
                "list_subtitle_download_candidates",
                (channel_ids, force_video_ids),
                {},
            )
        )
        force_ids = set(force_video_ids)
        candidates = [
            task
            for task in self.download_tasks
            if task.channel_id in channel_ids
            and (
                task.status is not SubtitleDownloadStatus.DOWNLOADED
                or task.youtube_video_id in force_ids
            )
        ]
        return sorted(
            candidates,
            key=lambda task: task.status is SubtitleDownloadStatus.FAILED,
        )

    async def mark_subtitle_download_failed(
        self,
        youtube_video_id: str,
        error_message: str,
    ) -> None:
        self.calls.append(
            (
                "mark_subtitle_download_failed",
                (youtube_video_id, error_message),
                {},
            )
        )
        self.subtitle_download_errors[youtube_video_id] = error_message
        if (
            self.stored_video is not None
            and self.stored_video.fetched.metadata.youtube_video_id == youtube_video_id
        ):
            self.stored_video = replace(
                self.stored_video,
                subtitle_download_status=SubtitleDownloadStatus.FAILED,
            )
        for index, task in enumerate(self.download_tasks):
            if task.youtube_video_id == youtube_video_id:
                self.download_tasks[index] = SubtitleDownloadTask(
                    channel_id=task.channel_id,
                    youtube_video_id=task.youtube_video_id,
                    video_url=task.video_url,
                    title=task.title,
                    status=SubtitleDownloadStatus.FAILED,
                )
                return
        raise LookupError(youtube_video_id)

    async def list_analysis_candidates(
        self,
        *,
        profile_name: str,
        schema_version: str,
        prompt_version: str,
        prompt_sha256: str,
        translation_schema_sha256: str,
        schema_sha256: str,
        force: bool,
        limit: int | None,
    ) -> list[VideoReference]:
        self.calls.append(
            (
                "list_analysis_candidates",
                (),
                {
                    "profile_name": profile_name,
                    "schema_version": schema_version,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt_sha256,
                    "translation_schema_sha256": translation_schema_sha256,
                    "schema_sha256": schema_sha256,
                    "force": force,
                    "limit": limit,
                },
            )
        )
        candidates = self.analysis_candidates
        return candidates if limit is None else candidates[:limit]

    async def mark_channel_backfill_completed(self, channel_id: UUID) -> None:
        self.calls.append(("mark_channel_backfill_completed", (channel_id,), {}))
        self.channels = [
            replace(
                channel,
                initial_backfill_completed_at=(
                    channel.initial_backfill_completed_at or datetime.now(UTC)
                ),
            )
            if channel.id == channel_id
            else channel
            for channel in self.channels
        ]

    async def mark_channel_checked(self, channel_id: UUID) -> None:
        self.calls.append(("mark_channel_checked", (channel_id,), {}))

    async def save_fetched_video(
        self,
        video: VideoMetadata,
        subtitle: DownloadedSubtitle | None,
    ) -> tuple[UUID, UUID | None]:
        self.calls.append(("save_fetched_video", (video, subtitle), {}))
        self.stored_video = _stored_video(
            FetchedVideo(metadata=video, subtitle=subtitle),
            subtitle_status="fetched" if subtitle is not None else "unavailable",
        )
        self.subtitle_download_errors.pop(video.youtube_video_id, None)
        for index, task in enumerate(self.download_tasks):
            if task.youtube_video_id == video.youtube_video_id:
                self.download_tasks[index] = SubtitleDownloadTask(
                    channel_id=task.channel_id,
                    youtube_video_id=task.youtube_video_id,
                    video_url=video.video_url,
                    title=video.title,
                    status=SubtitleDownloadStatus.DOWNLOADED,
                )
                break
        return VIDEO_ID, SUBTITLE_ID if subtitle is not None else None

    async def has_matching_analysis(
        self,
        video_id: UUID,
        subtitle_track_id: UUID,
        *,
        profile_name: str,
        schema_version: str,
        prompt_version: str,
        prompt_sha256: str,
        translation_schema_sha256: str,
        schema_sha256: str,
        source_sha256: str,
    ) -> bool:
        self.calls.append(
            (
                "has_matching_analysis",
                (video_id, subtitle_track_id),
                {
                    "profile_name": profile_name,
                    "schema_version": schema_version,
                    "prompt_version": prompt_version,
                    "prompt_sha256": prompt_sha256,
                    "translation_schema_sha256": translation_schema_sha256,
                    "schema_sha256": schema_sha256,
                    "source_sha256": source_sha256,
                },
            )
        )
        return self.matching_analysis

    async def start_analysis_run(self, *args: Any, **kwargs: Any) -> UUID:
        self.calls.append(("start_analysis_run", args, kwargs))
        return RUN_ID

    async def save_translation(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("save_translation", args, kwargs))

    async def save_agent_invocation(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("save_agent_invocation", args, kwargs))

    async def complete_analysis_run(self, *args: Any, **kwargs: Any) -> UUID:
        self.calls.append(("complete_analysis_run", args, kwargs))
        return uuid4()

    async def fail_analysis_run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("fail_analysis_run", args, kwargs))

    async def cancel_analysis_run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("cancel_analysis_run", args, kwargs))


class FakeAnalysis:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        event_log: list[str] | None = None,
    ) -> None:
        self.fail_at = fail_at
        self.event_log = event_log
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def translate(self, *args: Any, **kwargs: Any) -> TranslationResult:
        self.calls.append(("translate", args))
        if self.event_log is not None:
            self.event_log.append("agent:translate")
        await kwargs["invocation_sink"](TRANSLATION_INVOCATION)
        if self.fail_at == "cancel":
            raise asyncio.CancelledError
        if self.fail_at == "translate":
            raise RuntimeError("translation agent failed")
        return _translation()

    async def analyze(self, *args: Any, **kwargs: Any) -> AnalysisOutcome:
        self.calls.append(("analyze", args))
        if self.event_log is not None:
            self.event_log.append("agent:analyze")
        await kwargs["invocation_sink"](ANALYSIS_INVOCATION)
        if self.fail_at == "analyze":
            raise RuntimeError("analysis agent failed")
        return _outcome()


def _service(
    repository: FakeRepository,
    analysis: FakeAnalysis,
    fetched: FetchedVideo,
) -> PipelineService:
    return PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeYoutube(fetched),  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_process_video_saves_translation_and_completes_analysis() -> None:
    fetched = _fetched_video()
    repository = FakeRepository()
    analysis = FakeAnalysis()

    result = await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    assert result.status == "analyzed"
    assert result.youtube_video_id == "test-video"
    source_sha256 = hashlib.sha256(b"Original subtitle").hexdigest()
    assert repository.calls == [
        ("save_fetched_video", (fetched.metadata, fetched.subtitle), {}),
        (
            "has_matching_analysis",
            (VIDEO_ID, SUBTITLE_ID),
            {
                "profile_name": "research-v1",
                "schema_version": "2026-08-04",
                "prompt_version": "translation:t1;analysis:a1",
                "prompt_sha256": "prompt-digest",
                "translation_schema_sha256": "translation-schema-digest",
                "schema_sha256": "schema-digest",
                "source_sha256": source_sha256,
            },
        ),
        (
            "start_analysis_run",
            (VIDEO_ID, SUBTITLE_ID),
            {
                "agent_model": "gpt-test",
                "prompt_version": "translation:t1;analysis:a1",
                "metadata": {
                    "profile_name": "research-v1",
                    "schema_version": "2026-08-04",
                    "prompt_version": "translation:t1;analysis:a1",
                    "prompt_sha256": "prompt-digest",
                    "translation_schema_sha256": "translation-schema-digest",
                    "schema_sha256": "schema-digest",
                    "source_sha256": source_sha256,
                    "source_language": "en",
                },
            },
        ),
        ("save_agent_invocation", (RUN_ID, TRANSLATION_INVOCATION), {}),
        ("save_translation", (SUBTITLE_ID, _translation()), {}),
        ("save_agent_invocation", (RUN_ID, ANALYSIS_INVOCATION), {}),
        (
            "complete_analysis_run",
            (RUN_ID, VIDEO_ID, SUBTITLE_ID, _outcome()),
            {
                "profile_name": "research-v1",
                "schema_version": "2026-08-04",
                "run_metadata": {
                    "translation": _translation().metadata,
                    "analysis": _outcome().metadata,
                },
            },
        ),
    ]
    assert analysis.calls == [
        ("translate", ("en", "Original subtitle")),
        ("analyze", (fetched.metadata, "en", "translated subtitle")),
    ]


@pytest.mark.asyncio
async def test_process_video_skips_when_matching_analysis_exists() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(matching_analysis=True)
    analysis = FakeAnalysis()

    result = await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    assert result.status == "skipped"
    assert result.detail == "Matching analysis already exists"
    assert [call[0] for call in repository.calls] == [
        "save_fetched_video",
        "has_matching_analysis",
    ]
    assert analysis.calls == []


@pytest.mark.asyncio
async def test_process_video_returns_no_subtitle_without_starting_analysis() -> None:
    fetched = _fetched_video(with_subtitle=False)
    repository = FakeRepository()
    analysis = FakeAnalysis()

    result = await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    assert result.status == "no_subtitle"
    assert result.detail == "No preferred original subtitle was available"
    assert repository.calls == [
        ("save_fetched_video", (fetched.metadata, None), {}),
    ]
    assert analysis.calls == []


@pytest.mark.asyncio
async def test_process_video_returns_invalid_subtitle_without_starting_analysis() -> None:
    fetched = _fetched_video(normalized_text=None)
    repository = FakeRepository()
    analysis = FakeAnalysis()

    result = await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    assert result.status == "invalid_subtitle"
    assert result.youtube_video_id == "test-video"
    assert repository.calls == [
        ("save_fetched_video", (fetched.metadata, fetched.subtitle), {}),
    ]
    assert analysis.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "message", "expected_failure_metadata", "translation_saved"),
    [
        ("translate", "translation agent failed", {}, False),
        (
            "analyze",
            "analysis agent failed",
            {"translation": _translation().metadata},
            True,
        ),
    ],
)
async def test_process_video_marks_started_run_failed(
    failure_stage: str,
    message: str,
    expected_failure_metadata: dict[str, Any],
    translation_saved: bool,
) -> None:
    fetched = _fetched_video()
    repository = FakeRepository()
    analysis = FakeAnalysis(fail_at=failure_stage)

    with pytest.raises(RuntimeError, match=message):
        await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    failed_calls = [call for call in repository.calls if call[0] == "fail_analysis_run"]
    assert failed_calls == [
        ("fail_analysis_run", (RUN_ID, message, expected_failure_metadata), {}),
    ]
    assert any(call[0] == "save_translation" for call in repository.calls) is translation_saved
    assert all(call[0] != "complete_analysis_run" for call in repository.calls)


@pytest.mark.asyncio
async def test_process_video_marks_started_run_cancelled() -> None:
    fetched = _fetched_video()
    repository = FakeRepository()
    analysis = FakeAnalysis(fail_at="cancel")

    with pytest.raises(asyncio.CancelledError):
        await _service(repository, analysis, fetched).process_video(VIDEO_URL)

    assert (
        "cancel_analysis_run",
        (RUN_ID, "Pipeline task cancelled", {}),
        {},
    ) in repository.calls
    assert all(call[0] != "fail_analysis_run" for call in repository.calls)
    assert all(call[0] != "complete_analysis_run" for call in repository.calls)


@pytest.mark.asyncio
async def test_process_video_skips_stored_matching_analysis_without_fetching() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        matching_analysis=True,
        stored_video=_stored_video(fetched),
    )
    analysis = FakeAnalysis()
    youtube = FakeYoutube(fetched)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    result = await service.process_video(VIDEO_URL, youtube_video_id="test-video")

    assert result.status == "skipped"
    assert youtube.calls == []
    assert [call[0] for call in repository.calls] == [
        "get_stored_video",
        "has_matching_analysis",
    ]
    assert analysis.calls == []


@pytest.mark.asyncio
async def test_process_video_resumes_analysis_with_stored_translation() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        stored_video=_stored_video(fetched, translation=_translation()),
    )
    analysis = FakeAnalysis()
    youtube = FakeYoutube(fetched)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    result = await service.process_video(VIDEO_URL, youtube_video_id="test-video")

    assert result.status == "analyzed"
    assert youtube.calls == []
    assert analysis.calls == [
        ("analyze", (fetched.metadata, "en", "translated subtitle")),
    ]
    assert all(
        call[0] not in {"save_fetched_video", "save_translation"}
        for call in repository.calls
    )


@pytest.mark.asyncio
async def test_process_video_retranslates_when_stored_translation_contract_changed() -> None:
    fetched = _fetched_video()
    stale_translation = TranslationResult(
        translated_text="stale translated subtitle",
        translated_language_code="zh-Hans",
        metadata={
            **_translation().metadata,
            "translation_schema_sha256": "stale-schema-digest",
        },
    )
    repository = FakeRepository(
        stored_video=_stored_video(fetched, translation=stale_translation),
    )
    analysis = FakeAnalysis()
    youtube = FakeYoutube(fetched)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    result = await service.process_video(VIDEO_URL, youtube_video_id="test-video")

    assert result.status == "analyzed"
    assert youtube.calls == []
    assert analysis.calls == [
        ("translate", ("en", "Original subtitle")),
        ("analyze", (fetched.metadata, "en", "translated subtitle")),
    ]
    assert any(call[0] == "save_translation" for call in repository.calls)


@pytest.mark.asyncio
async def test_process_video_reuses_stored_terminal_subtitle_state() -> None:
    fetched = _fetched_video(with_subtitle=False)
    repository = FakeRepository(
        stored_video=_stored_video(fetched, subtitle_status="unavailable"),
    )
    analysis = FakeAnalysis()
    youtube = FakeYoutube(fetched)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    result = await service.process_video(VIDEO_URL, youtube_video_id="test-video")

    assert result.status == "no_subtitle"
    assert youtube.calls == []
    assert [call[0] for call in repository.calls] == ["get_stored_video"]


@pytest.mark.asyncio
async def test_process_video_refetches_stored_pending_subtitle_state() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        matching_analysis=True,
        stored_video=_stored_video(fetched, subtitle_status="pending"),
    )
    analysis = FakeAnalysis()
    youtube = FakeYoutube(fetched)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    result = await service.process_video(VIDEO_URL, youtube_video_id="test-video")

    assert result.status == "skipped"
    assert youtube.calls == [("fetch_video", (VIDEO_URL,))]
    assert [call[0] for call in repository.calls] == [
        "get_stored_video",
        "save_fetched_video",
        "has_matching_analysis",
    ]
    assert analysis.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_urls", [None, []])
async def test_run_without_channels_uses_active_database_channels(
    channel_urls: list[str] | None,
) -> None:
    fetched = _fetched_video()
    channel = _channel_record()
    repository = FakeRepository(
        matching_analysis=True,
        stored_video=_stored_video(fetched),
        channels=[channel],
    )
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL)],
    )
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        channel_urls,
        max_videos_per_channel=None,
        force=False,
    )

    assert results == []
    assert youtube.calls == [
        (
            "discover_channel_videos",
            ("test-channel", None, frozenset({"test-video"}), False),
        ),
    ]
    assert [call[0] for call in repository.calls] == [
        "list_active_channels",
        "list_known_video_ids",
        "enqueue_subtitle_downloads",
        "mark_channel_backfill_completed",
        "mark_channel_checked",
        "list_subtitle_download_candidates",
        "list_analysis_candidates",
    ]


@pytest.mark.asyncio
async def test_run_without_channels_returns_empty_when_database_has_no_active_channels() -> None:
    fetched = _fetched_video()
    repository = FakeRepository()
    youtube = FakeYoutube(fetched, source_exhausted=False)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert results == []
    assert youtube.calls == []
    assert [call[0] for call in repository.calls] == [
        "list_active_channels",
        "list_analysis_candidates",
    ]


@pytest.mark.asyncio
async def test_explicit_channel_cap_does_not_mark_backfill_completed() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(fetched, source_exhausted=False)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        [CHANNEL_URL, CHANNEL_URL],
        max_videos_per_channel=3,
        force=False,
    )

    assert results == []
    assert youtube.calls == [
        ("inspect_channel", (CHANNEL_URL,)),
        (
            "discover_channel_videos",
            ("test-channel", 3, frozenset(), False),
        ),
    ]
    assert [call[0] for call in repository.calls] == [
        "register_channel",
        "get_channels",
        "list_known_video_ids",
        "enqueue_subtitle_downloads",
        "mark_channel_checked",
        "list_subtitle_download_candidates",
        "list_analysis_candidates",
    ]


@pytest.mark.asyncio
async def test_completed_backfill_stops_incremental_discovery_at_known_video(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = _channel_record(backfill_completed=True)
    repository = FakeRepository(
        channels=[channel],
        known_video_ids_by_channel={CHANNEL_ID: {"known-video"}},
    )
    youtube = FakeYoutube(
        _fetched_video(),
        references=[VideoReference("test-video", VIDEO_URL, "Test Video")],
        source_exhausted=False,
        stopped_at_known=True,
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO, logger="service")

    results = await service.download_channels(
        None,
        max_videos_per_channel=3,
        force=False,
    )

    assert [result.status for result in results] == ["downloaded"]
    assert youtube.calls[0] == (
        "discover_channel_videos",
        ("test-channel", 3, frozenset({"known-video"}), True),
    )
    assert all(
        call[0] != "mark_channel_backfill_completed" for call in repository.calls
    )
    assert "mode=incremental" in caplog.text
    assert "stop_reason=known video boundary reached" in caplog.text


@pytest.mark.asyncio
async def test_limited_initial_backfill_passes_first_batch_as_known_on_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(
        _fetched_video(),
        references=[
            VideoReference(
                "batch-one",
                "https://youtube.test/watch?v=batch-one",
                "Batch One",
            )
        ],
        source_exhausted=False,
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    async def download_video(
        video_url: str,
        *,
        youtube_video_id: str | None,
        force: bool,
    ) -> ProcessResult:
        return ProcessResult(
            video_url=video_url,
            youtube_video_id=youtube_video_id,
            status="downloaded",
            detail="language=en, source=manual, format=vtt",
        )

    monkeypatch.setattr(service, "download_video", download_video)

    await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=False,
    )
    youtube.references = [
        VideoReference(
            "batch-two",
            "https://youtube.test/watch?v=batch-two",
            "Batch Two",
        )
    ]
    youtube.source_exhausted = True
    await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=False,
    )

    discovery_calls = [
        call for call in youtube.calls if call[0] == "discover_channel_videos"
    ]
    assert discovery_calls == [
        (
            "discover_channel_videos",
            ("test-channel", 1, frozenset(), False),
        ),
        (
            "discover_channel_videos",
            ("test-channel", 1, frozenset({"batch-one"}), False),
        ),
    ]
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls


@pytest.mark.asyncio
async def test_download_channels_does_not_invoke_agent() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL)],
    )
    analysis = FakeAnalysis()
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    results = await service.download_channels(
        [CHANNEL_URL],
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["downloaded"]
    assert analysis.calls == []
    assert all(call[0] != "list_analysis_candidates" for call in repository.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetched", "stored_video", "expected_status", "expected_log", "level"),
    [
        (
            _fetched_video(),
            None,
            "downloaded",
            "Subtitle downloaded: Test Video [video_id=test-video, "
            "queue=pending]: language=en, source=manual, format=vtt",
            logging.INFO,
        ),
        (
            _fetched_video(is_auto_generated=True),
            None,
            "downloaded",
            "Subtitle downloaded: Test Video [video_id=test-video, "
            "queue=pending]: language=en, source=automatic, format=vtt",
            logging.INFO,
        ),
        (
            _fetched_video(with_subtitle=False),
            None,
            "no_subtitle",
            "No matching subtitle: Test Video [video_id=test-video, "
            "queue=pending]: subtitle check completed; no subtitle matched",
            logging.INFO,
        ),
        (
            _fetched_video(normalized_text=None),
            None,
            "invalid_subtitle",
            "Subtitle downloaded but could not be normalized: Test Video "
            "[video_id=test-video, queue=pending]",
            logging.WARNING,
        ),
        (
            _fetched_video(),
            _stored_video(_fetched_video()),
            "skipped",
            "Subtitle download skipped: Test Video [video_id=test-video, "
            "queue=pending]: Subtitle download state already exists",
            logging.INFO,
        ),
    ],
)
async def test_subtitle_terminal_status_has_business_log(
    caplog: pytest.LogCaptureFixture,
    fetched: FetchedVideo,
    stored_video: StoredVideo | None,
    expected_status: str,
    expected_log: str,
    level: int,
) -> None:
    repository = FakeRepository(
        channels=[_channel_record(backfill_completed=True)],
        stored_video=stored_video,
        download_tasks=[
            _download_task(
                "test-video",
                SubtitleDownloadStatus.PENDING,
                title="Test Video",
            )
        ],
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeYoutube(fetched),  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO, logger="service")

    results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == [expected_status]
    record = next(record for record in caplog.records if expected_log in record.message)
    assert record.levelno == level


@pytest.mark.asyncio
async def test_failed_retry_has_clear_error_and_debug_traceback_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = _download_task(
        "retry-video",
        SubtitleDownloadStatus.FAILED,
        title="Retry Video",
    )
    repository = FakeRepository(
        channels=[_channel_record(backfill_completed=True)],
        download_tasks=[task],
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeYoutube(
            _fetched_video(),
            failing_video_urls={task.video_url},
        ),  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    caplog.set_level(logging.DEBUG, logger="service")

    results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["failed"]
    assert "total=1, pending=0, retry=1, forced=0" in caplog.text
    assert (
        "Subtitle download started: Retry Video "
        "[video_id=retry-video, queue=retry]"
    ) in caplog.text
    error_record = next(
        record
        for record in caplog.records
        if record.message.startswith("Subtitle download failed: Retry Video")
    )
    debug_record = next(
        record
        for record in caplog.records
        if record.message.startswith("Subtitle download traceback: Retry Video")
    )
    assert error_record.levelno == logging.ERROR
    assert error_record.exc_info is None
    assert debug_record.levelno == logging.DEBUG
    assert debug_record.exc_info is not None


@pytest.mark.asyncio
async def test_analyze_pending_does_not_access_youtube() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        stored_video=_stored_video(fetched),
        analysis_candidates=[VideoReference("test-video", VIDEO_URL)],
    )
    youtube = FakeYoutube(fetched)
    analysis = FakeAnalysis()
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    results = await service.analyze_pending(max_videos=None, force=False)

    assert [result.status for result in results] == ["analyzed"]
    assert youtube.calls == []
    assert [call[0] for call in analysis.calls] == ["translate", "analyze"]


@pytest.mark.asyncio
async def test_analyze_pending_uses_one_snapshot_and_continues_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = [
        VideoReference("failed", "https://youtube.test/watch?v=failed"),
        VideoReference("successful", "https://youtube.test/watch?v=successful"),
    ]
    repository = FakeRepository(analysis_candidates=references)
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeYoutube(_fetched_video()),  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    analyzed_ids: list[str] = []

    async def analyze_video(
        youtube_video_id: str,
        *,
        force: bool,
    ) -> ProcessResult:
        assert force is False
        analyzed_ids.append(youtube_video_id)
        if youtube_video_id == "failed":
            raise RuntimeError("agent failed")
        return ProcessResult(
            video_url="https://youtube.test/watch?v=successful",
            youtube_video_id=youtube_video_id,
            status="analyzed",
        )

    monkeypatch.setattr(service, "analyze_video", analyze_video)

    results = await service.analyze_pending(max_videos=None, force=False)

    assert analyzed_ids == ["failed", "successful"]
    assert [result.status for result in results] == ["failed", "analyzed"]
    assert [call[0] for call in repository.calls].count("list_analysis_candidates") == 1


@pytest.mark.asyncio
async def test_run_channels_finishes_all_downloads_before_first_agent_call() -> None:
    events: list[str] = []
    fetched = _fetched_video()
    references = [
        VideoReference(f"video-{index}", f"https://youtube.test/watch?v={index}")
        for index in range(6)
    ]
    repository = FakeRepository(
        channels=[_channel_record()],
        analysis_candidates=[VideoReference("test-video", VIDEO_URL)],
    )
    youtube = FakeYoutube(
        fetched,
        references=references,
        event_log=events,
        fetch_delay=0.01,
    )
    service = PipelineService(
        _settings(download_concurrency=3),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(event_log=events),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    first_agent_event = next(
        index for index, event in enumerate(events) if event.startswith("agent:")
    )
    download_end_indexes = [
        index
        for index, event in enumerate(events)
        if event.startswith("download:end:")
    ]
    assert len(download_end_indexes) == len(references)
    assert max(download_end_indexes) < first_agent_event
    assert results[-1].status == "analyzed"


@pytest.mark.asyncio
async def test_analysis_failure_does_not_prevent_channel_backfill_completion() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        channels=[_channel_record()],
        analysis_candidates=[VideoReference("test-video", VIDEO_URL)],
    )
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL)],
    )
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(fail_at="analyze"),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["downloaded", "failed"]
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls


@pytest.mark.asyncio
async def test_failed_download_continues_after_backfill_marker_is_saved() -> None:
    fetched = _fetched_video()
    failed_url = "https://youtube.test/watch?v=failed"
    successful_url = "https://youtube.test/watch?v=successful"
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(
        fetched,
        references=[
            VideoReference("failed", failed_url),
            VideoReference("successful", successful_url),
        ],
        failing_video_urls={failed_url},
        fetch_delay=0.01,
    )
    analysis = FakeAnalysis()
    service = PipelineService(
        _settings(download_concurrency=2),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        analysis,  # type: ignore[arg-type]
    )

    results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["failed", "downloaded"]
    assert sum(call[0] == "fetch_video" for call in youtube.calls) == 2
    assert repository.subtitle_download_errors == {"failed": "video download failed"}
    assert next(
        task.status
        for task in repository.download_tasks
        if task.youtube_video_id == "failed"
    ) is SubtitleDownloadStatus.FAILED
    assert [call[0] for call in repository.calls].count(
        "list_subtitle_download_candidates"
    ) == 1
    assert (
        "mark_subtitle_download_failed",
        ("failed", "video download failed"),
        {},
    ) in repository.calls
    assert analysis.calls == []
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls
    call_names = [call[0] for call in repository.calls]
    assert call_names.index("mark_channel_backfill_completed") < call_names.index(
        "mark_subtitle_download_failed"
    )


@pytest.mark.asyncio
async def test_failed_download_continues_with_next_channel_queue_task() -> None:
    second_channel_id = uuid4()
    second_channel = ChannelRecord(
        id=second_channel_id,
        youtube_channel_id="second-channel",
        title="Second Channel",
        channel_url="https://www.youtube.com/@second-channel",
        is_subscribed=True,
        initial_backfill_completed_at=None,
    )
    failed_task = _download_task("failed-first", SubtitleDownloadStatus.PENDING)
    successful_task = _download_task(
        "successful-second",
        SubtitleDownloadStatus.PENDING,
        channel_id=second_channel_id,
    )
    repository = FakeRepository(
        channels=[_channel_record(), second_channel],
        download_tasks=[failed_task, successful_task],
    )
    youtube = FakeYoutube(
        _fetched_video(),
        failing_video_urls={failed_task.video_url},
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["failed", "downloaded"]
    assert [call[1][0] for call in youtube.calls if call[0] == "fetch_video"] == [
        failed_task.video_url,
        successful_task.video_url,
    ]
    assert (
        "mark_channel_backfill_completed",
        (second_channel_id,),
        {},
    ) in repository.calls
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls


@pytest.mark.asyncio
async def test_failed_download_is_retried_on_next_run_and_clears_error() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL, "Test Video")],
        failing_video_urls={VIDEO_URL},
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    first_results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in first_results] == ["failed"]
    assert repository.download_tasks[0].status is SubtitleDownloadStatus.FAILED
    assert repository.subtitle_download_errors == {
        "test-video": "video download failed"
    }
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls

    youtube.failing_video_urls.clear()
    second_results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in second_results] == ["downloaded"]
    assert repository.download_tasks[0].status is SubtitleDownloadStatus.DOWNLOADED
    assert repository.subtitle_download_errors == {}
    assert sum(call[0] == "fetch_video" for call in youtube.calls) == 2
    assert (
        "mark_channel_backfill_completed",
        (CHANNEL_ID,),
        {},
    ) in repository.calls


@pytest.mark.asyncio
async def test_global_queue_runs_pending_before_failed_and_ignores_discovery_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(
        channels=[_channel_record()],
        download_tasks=[
            _download_task("retry-last", SubtitleDownloadStatus.FAILED),
            _download_task("historical-pending", SubtitleDownloadStatus.PENDING),
        ],
    )
    youtube = FakeYoutube(
        _fetched_video(),
        references=[
            VideoReference(
                "new-in-window",
                "https://youtube.test/watch?v=new-in-window",
            )
        ],
        source_exhausted=False,
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    downloaded_ids: list[str] = []

    async def download_video(
        video_url: str,
        *,
        youtube_video_id: str | None,
        force: bool,
    ) -> ProcessResult:
        assert youtube_video_id is not None
        assert force is False
        downloaded_ids.append(youtube_video_id)
        return ProcessResult(
            video_url=video_url,
            youtube_video_id=youtube_video_id,
            status="downloaded",
        )

    monkeypatch.setattr(service, "download_video", download_video)

    results = await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=False,
    )

    assert [result.status for result in results] == [
        "downloaded",
        "downloaded",
        "downloaded",
    ]
    assert downloaded_ids == [
        "historical-pending",
        "new-in-window",
        "retry-last",
    ]
    assert youtube.calls == [
        (
            "discover_channel_videos",
            (
                "test-channel",
                1,
                frozenset({"historical-pending", "retry-last"}),
                False,
            ),
        )
    ]
    assert all(
        call[0] != "mark_channel_backfill_completed" for call in repository.calls
    )


@pytest.mark.asyncio
async def test_download_cancellation_is_not_recorded_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(
        channels=[_channel_record()],
        download_tasks=[
            _download_task("cancelled", SubtitleDownloadStatus.PENDING),
        ],
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        FakeYoutube(_fetched_video()),  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    async def cancel_download(*args: Any, **kwargs: Any) -> ProcessResult:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "download_video", cancel_download)

    with pytest.raises(asyncio.CancelledError):
        await service.download_channels(
            None,
            max_videos_per_channel=None,
            force=False,
        )

    assert repository.download_tasks[0].status is SubtitleDownloadStatus.PENDING
    assert all(
        call[0] != "mark_subtitle_download_failed" for call in repository.calls
    )


@pytest.mark.asyncio
async def test_force_redownload_only_enqueues_discovered_completed_videos(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        stored_video=_stored_video(fetched),
        channels=[_channel_record()],
        download_tasks=[
            _download_task("test-video", SubtitleDownloadStatus.DOWNLOADED),
            _download_task("outside-window", SubtitleDownloadStatus.DOWNLOADED),
        ],
    )
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL, "Test Video")],
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO, logger="service")

    results = await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=True,
    )

    assert [result.youtube_video_id for result in results] == ["test-video"]
    assert youtube.calls == [
        (
            "discover_channel_videos",
            ("test-channel", 1, frozenset(), False),
        ),
        ("fetch_video", (VIDEO_URL,)),
    ]
    assert (
        "list_subtitle_download_candidates",
        ([CHANNEL_ID], ["test-video"]),
        {},
    ) in repository.calls
    assert all(call[0] != "list_known_video_ids" for call in repository.calls)
    assert "total=1, pending=0, retry=0, forced=1" in caplog.text
    assert (
        "Subtitle download started: Test Video "
        "[video_id=test-video, queue=forced]"
    ) in caplog.text


@pytest.mark.asyncio
async def test_force_failure_retries_despite_stored_terminal_subtitle_state() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(
        stored_video=_stored_video(fetched, subtitle_status="fetched"),
        channels=[_channel_record()],
        download_tasks=[
            _download_task("test-video", SubtitleDownloadStatus.DOWNLOADED),
        ],
    )
    youtube = FakeYoutube(
        fetched,
        references=[VideoReference("test-video", VIDEO_URL, "Test Video")],
        failing_video_urls={VIDEO_URL},
    )
    service = PipelineService(
        _settings(download_concurrency=1),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    forced_results = await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=True,
    )

    assert [result.status for result in forced_results] == ["failed"]
    assert repository.stored_video is not None
    assert repository.stored_video.subtitle_status == "fetched"
    assert (
        repository.stored_video.subtitle_download_status
        is SubtitleDownloadStatus.FAILED
    )

    youtube.failing_video_urls.clear()
    retry_results = await service.download_channels(
        None,
        max_videos_per_channel=1,
        force=False,
    )

    assert [result.status for result in retry_results] == ["downloaded"]
    assert sum(call[0] == "fetch_video" for call in youtube.calls) == 2
    assert repository.download_tasks[0].status is SubtitleDownloadStatus.DOWNLOADED


@pytest.mark.asyncio
async def test_large_channel_downloads_use_configured_bounded_concurrency() -> None:
    fetched = _fetched_video()
    references = [
        VideoReference(f"video-{index}", f"https://youtube.test/watch?v={index}")
        for index in range(10)
    ]
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(fetched, references=references, fetch_delay=0.01)
    service = PipelineService(
        _settings(download_concurrency=3),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.download_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert len(results) == len(references)
    assert youtube.peak_fetches > 1
    assert youtube.peak_fetches <= 3


@pytest.mark.asyncio
async def test_process_channel_preserves_caller_supplied_limiter() -> None:
    fetched = _fetched_video()
    references = [
        VideoReference(f"video-{index}", f"https://youtube.test/watch?v={index}")
        for index in range(3)
    ]
    repository = FakeRepository()
    youtube = FakeYoutube(fetched, references=references, fetch_delay=0.01)
    service = PipelineService(
        _settings(download_concurrency=3),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.process_channel(
        _channel_record(),
        max_videos=1,
        force=False,
        limiter=asyncio.Semaphore(1),
    )

    assert len(results) == len(references)
    assert youtube.peak_fetches == 1


@pytest.mark.asyncio
async def test_failed_channel_does_not_stop_later_active_database_channels() -> None:
    fetched = _fetched_video()
    first_channel = _channel_record()
    second_channel = ChannelRecord(
        id=uuid4(),
        youtube_channel_id="second-channel",
        title="Second Channel",
        channel_url="https://www.youtube.com/@second-channel",
        is_subscribed=True,
        initial_backfill_completed_at=None,
    )
    repository = FakeRepository(channels=[first_channel, second_channel])
    youtube = FakeYoutube(
        fetched,
        failing_channel_ids={first_channel.youtube_channel_id},
    )
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        None,
        max_videos_per_channel=None,
        force=False,
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].video_url == first_channel.channel_url
    assert youtube.calls == [
        (
            "discover_channel_videos",
            (first_channel.youtube_channel_id, None, frozenset(), False),
        ),
        (
            "discover_channel_videos",
            (second_channel.youtube_channel_id, None, frozenset(), False),
        ),
    ]
    assert (
        "mark_channel_backfill_completed",
        (second_channel.id,),
        {},
    ) in repository.calls
    assert (
        "mark_channel_checked",
        (second_channel.id,),
        {},
    ) in repository.calls


def test_run_parser_accepts_repeated_channels_and_zero_limit() -> None:
    args = _parser().parse_args(
        [
            "run",
            "--channel",
            "https://www.youtube.com/@first",
            "--channel",
            "https://www.youtube.com/@second",
            "--max-videos-per-channel",
            "0",
        ]
    )

    assert args.channel == [
        "https://www.youtube.com/@first",
        "https://www.youtube.com/@second",
    ]
    assert args.max_videos_per_channel == 0


def test_run_parser_defaults_to_database_channels() -> None:
    args = _parser().parse_args(["run"])

    assert args.channel == []
