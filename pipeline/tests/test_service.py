from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
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
    StoredVideo,
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


def _settings() -> SimpleNamespace:
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
    )


def _fetched_video(
    *,
    with_subtitle: bool = True,
    normalized_text: str | None = "Original subtitle",
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
            is_auto_generated=False,
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
    translation: TranslationResult | None = None,
) -> StoredVideo:
    return StoredVideo(
        video_id=VIDEO_ID,
        subtitle_track_id=SUBTITLE_ID if fetched.subtitle is not None else None,
        fetched=fetched,
        subtitle_status=subtitle_status,
        translated_text=translation.translated_text if translation is not None else None,
        translated_language_code=(
            translation.translated_language_code if translation is not None else None
        ),
        translation_metadata=translation.metadata if translation is not None else {},
    )


def _channel_record() -> ChannelRecord:
    return ChannelRecord(
        id=CHANNEL_ID,
        youtube_channel_id="test-channel",
        title="Test Channel",
        channel_url=CHANNEL_URL,
        is_subscribed=True,
        initial_backfill_completed_at=None,
    )


@dataclass
class FakeYoutube:
    fetched: FetchedVideo
    references: list[VideoReference] = field(default_factory=list)
    failing_channel_urls: set[str] = field(default_factory=set)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetch_video(self, video_url: str) -> FetchedVideo:
        self.calls.append(("fetch_video", (video_url,)))
        assert video_url == VIDEO_URL
        return self.fetched

    async def inspect_channel(self, channel_url: str) -> ChannelMetadata:
        self.calls.append(("inspect_channel", (channel_url,)))
        return self.fetched.metadata.channel

    async def discover_channel_videos(
        self,
        channel_url: str,
        limit: int | None,
    ) -> list[VideoReference]:
        self.calls.append(("discover_channel_videos", (channel_url, limit)))
        if channel_url in self.failing_channel_urls:
            raise RuntimeError("channel discovery failed")
        return self.references


class FakeRepository:
    def __init__(
        self,
        *,
        matching_analysis: bool = False,
        stored_video: StoredVideo | None = None,
        channels: list[ChannelRecord] | None = None,
    ) -> None:
        self.matching_analysis = matching_analysis
        self.stored_video = stored_video
        self.channels = channels or []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def get_stored_video(self, youtube_video_id: str) -> StoredVideo | None:
        self.calls.append(("get_stored_video", (youtube_video_id,), {}))
        return self.stored_video

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

    async def mark_channel_backfill_completed(self, channel_id: UUID) -> None:
        self.calls.append(("mark_channel_backfill_completed", (channel_id,), {}))

    async def mark_channel_checked(self, channel_id: UUID) -> None:
        self.calls.append(("mark_channel_checked", (channel_id,), {}))

    async def save_fetched_video(
        self,
        video: VideoMetadata,
        subtitle: DownloadedSubtitle | None,
    ) -> tuple[UUID, UUID | None]:
        self.calls.append(("save_fetched_video", (video, subtitle), {}))
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
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def translate(self, *args: Any, **kwargs: Any) -> TranslationResult:
        self.calls.append(("translate", args))
        await kwargs["invocation_sink"](TRANSLATION_INVOCATION)
        if self.fail_at == "cancel":
            raise asyncio.CancelledError
        if self.fail_at == "translate":
            raise RuntimeError("translation agent failed")
        return _translation()

    async def analyze(self, *args: Any, **kwargs: Any) -> AnalysisOutcome:
        self.calls.append(("analyze", args))
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

    assert [result.status for result in results] == ["skipped"]
    assert youtube.calls == [
        ("discover_channel_videos", (CHANNEL_URL, None)),
    ]
    assert [call[0] for call in repository.calls] == [
        "list_active_channels",
        "get_stored_video",
        "has_matching_analysis",
        "mark_channel_backfill_completed",
        "mark_channel_checked",
    ]


@pytest.mark.asyncio
async def test_run_without_channels_returns_empty_when_database_has_no_active_channels() -> None:
    fetched = _fetched_video()
    repository = FakeRepository()
    youtube = FakeYoutube(fetched)
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
    assert repository.calls == [("list_active_channels", (), {})]


@pytest.mark.asyncio
async def test_explicit_channel_cap_does_not_mark_backfill_completed() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(fetched)
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
        ("discover_channel_videos", (CHANNEL_URL, 3)),
    ]
    assert [call[0] for call in repository.calls] == [
        "register_channel",
        "get_channels",
        "mark_channel_checked",
    ]


@pytest.mark.asyncio
async def test_failed_video_does_not_stop_channel_or_complete_backfill() -> None:
    fetched = _fetched_video()
    repository = FakeRepository(channels=[_channel_record()])
    youtube = FakeYoutube(
        fetched,
        references=[
            VideoReference("test-video", VIDEO_URL),
            VideoReference("test-video-2", VIDEO_URL),
        ],
    )
    service = PipelineService(
        _settings(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        youtube,  # type: ignore[arg-type]
        FakeAnalysis(fail_at="analyze"),  # type: ignore[arg-type]
    )

    results = await service.run_channels(
        [CHANNEL_URL],
        max_videos_per_channel=None,
        force=False,
    )

    assert [result.status for result in results] == ["failed", "failed"]
    assert sum(call[0] == "fetch_video" for call in youtube.calls) == 2
    assert all(
        call[0] != "mark_channel_backfill_completed" for call in repository.calls
    )
    assert repository.calls[-1] == ("mark_channel_checked", (CHANNEL_ID,), {})


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
        failing_channel_urls={first_channel.channel_url},
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
        ("discover_channel_videos", (first_channel.channel_url, None)),
        ("discover_channel_videos", (second_channel.channel_url, None)),
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
