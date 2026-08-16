from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import main as main_module
from models import ProcessResult, PublicationResult


class FakeRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def process_lock(self, name: str) -> AsyncIterator[None]:
        self.events.append(f"lock:{name}:enter")
        try:
            yield
        finally:
            self.events.append(f"lock:{name}:exit")

    async def connect(self) -> None:
        self.events.append("connect")

    async def close(self) -> None:
        self.events.append("close")


class FakeService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def add_channel(self, channel_url: str, researcher: str | None) -> UUID:
        self.events.append(f"channel-add:{channel_url}:{researcher}")
        return UUID(int=1)

    async def download_video(
        self,
        video_url: str,
        *,
        youtube_video_id: str | None = None,
        force: bool,
    ) -> ProcessResult:
        self.events.append(f"download_video:{video_url}:{youtube_video_id}:{force}")
        return ProcessResult(
            video_url=video_url,
            youtube_video_id="video-id",
            status="downloaded",
        )

    async def analyze_video(
        self,
        youtube_video_id: str,
        *,
        force: bool,
    ) -> ProcessResult:
        self.events.append(f"analyze_video:{youtube_video_id}:{force}")
        return ProcessResult(
            video_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
            youtube_video_id=youtube_video_id,
            status="analyzed",
        )

    async def download_channels(
        self,
        channel_urls: list[str],
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        self.events.append(
            f"download_channels:start:{','.join(channel_urls)}:"
            f"{max_videos_per_channel}:{force}"
        )
        results = []
        for index, channel_url in enumerate(channel_urls or ["database-channel"], start=1):
            self.events.append(f"download:{channel_url}")
            results.append(
                ProcessResult(
                    video_url=f"https://www.youtube.com/watch?v=download-{index}",
                    youtube_video_id=f"download-{index}",
                    status="downloaded",
                )
            )
        self.events.append("download_channels:end")
        return results

    async def analyze_pending(
        self,
        *,
        max_videos: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        self.events.append(f"analyze_pending:{max_videos}:{force}")
        return [
            ProcessResult(
                video_url="https://www.youtube.com/watch?v=analysis-1",
                youtube_video_id="analysis-1",
                status="analyzed",
            )
        ]


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    default_max_videos: int = 0,
    cleanup_enabled: bool = True,
) -> FakeService:
    settings = SimpleNamespace(
        database=object(),
        youtube=SimpleNamespace(max_videos_per_channel=default_max_videos),
        agent=SimpleNamespace(
            cleanup_historical_sessions=cleanup_enabled,
            codex_path="/usr/bin/codex-test",
            session_cleanup_timeout_seconds=123,
        ),
    )
    repository = FakeRepository(events)
    service = FakeService(events)

    async def load_settings(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        return settings

    async def migrate(runtime_settings: SimpleNamespace) -> None:
        assert runtime_settings is settings
        events.append("migrate")

    def create_service(
        runtime_settings: SimpleNamespace,
        runtime_repository: FakeRepository,
    ) -> FakeService:
        assert runtime_settings is settings
        assert runtime_repository is repository
        events.append("service")
        return service

    async def cleanup_sessions(
        *,
        codex_path: str,
        timeout_seconds: float,
    ) -> SimpleNamespace:
        assert codex_path == "/usr/bin/codex-test"
        assert timeout_seconds == 123
        events.append("cleanup")
        return SimpleNamespace(
            scanned_threads=5,
            matched_threads=2,
            deleted_threads=1,
            already_missing_threads=0,
            skipped_live_threads=1,
            skipped_loaded_threads=1,
            skipped_descendant_threads=0,
            unverified_threads=1,
            failed_thread_ids=(),
        )

    monkeypatch.setattr(main_module, "load_settings", load_settings)
    monkeypatch.setattr(main_module, "_migrate", migrate)
    monkeypatch.setattr(main_module, "PipelineRepository", lambda database: repository)
    monkeypatch.setattr(main_module, "_service", create_service)
    monkeypatch.setattr(
        main_module,
        "cleanup_historical_agent_sessions",
        cleanup_sessions,
    )
    return service


def _patch_push_consumer(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    results: list[PublicationResult],
) -> None:
    publisher = object()

    def create_publisher(repository: FakeRepository) -> object:
        assert isinstance(repository, FakeRepository)
        events.append("publisher")
        return publisher

    class FakePushConsumer:
        def __init__(
            self,
            repository: FakeRepository,
            actual_publisher: object,
        ) -> None:
            assert isinstance(repository, FakeRepository)
            assert actual_publisher is publisher
            events.append("consumer")

        async def consume(self, *, limit: int | None) -> list[PublicationResult]:
            events.append(f"consume:{limit}")
            return results

    monkeypatch.setattr(main_module, "BbsPublisher", create_publisher)
    monkeypatch.setattr(main_module, "BbsPushConsumer", FakePushConsumer)


def test_service_factory_does_not_construct_bbs_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(agent=object(), youtube=object())
    repository = object()
    agent = object()
    youtube = object()
    analysis = object()

    monkeypatch.setattr(main_module, "CodexStructuredAgent", lambda actual: agent)
    monkeypatch.setattr(main_module, "YoutubeClient", lambda actual: youtube)
    monkeypatch.setattr(
        main_module,
        "AnalysisEngine",
        lambda actual_settings, actual_agent: analysis,
    )

    def reject_publisher(actual_repository: object) -> None:
        del actual_repository
        pytest.fail("The analysis service must not construct a BBS publisher")

    monkeypatch.setattr(main_module, "BbsPublisher", reject_publisher)

    service = main_module._service(settings, repository)  # type: ignore[arg-type]

    assert isinstance(service, main_module.PipelineService)


@pytest.mark.asyncio
async def test_run_finishes_all_downloads_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)
    args = main_module._parser().parse_args(
        [
            "run",
            "--channel",
            "first",
            "--channel",
            "second",
            "--max-videos-per-channel",
            "2",
            "--force",
        ]
    )

    exit_code = await main_module._run(args)

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:download:enter",
        "download_channels:start:first,second:2:True",
        "download:first",
        "download:second",
        "download_channels:end",
        "lock:download:exit",
        "lock:analysis:enter",
        "cleanup",
        "analyze_pending:None:True",
        "lock:analysis:exit",
        "close",
    ]
    assert [item["status"] for item in json.loads(capsys.readouterr().out)] == [
        "downloaded",
        "downloaded",
        "analyzed",
    ]


@pytest.mark.asyncio
async def test_download_uses_only_download_lock_and_configured_default_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events, default_max_videos=7)

    exit_code = await main_module._run(
        main_module._parser().parse_args(["download"])
    )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:download:enter",
        "download_channels:start::7:False",
        "download:database-channel",
        "download_channels:end",
        "lock:download:exit",
        "close",
    ]
    assert json.loads(capsys.readouterr().out) == [
        {
            "video_url": "https://www.youtube.com/watch?v=download-1",
            "youtube_video_id": "download-1",
            "status": "downloaded",
            "detail": None,
        }
    ]


@pytest.mark.asyncio
async def test_analyze_uses_only_analysis_lock_and_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    exit_code = await main_module._run(
        main_module._parser().parse_args(["analyze", "--limit", "3", "--force"])
    )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:analysis:enter",
        "cleanup",
        "analyze_pending:3:True",
        "lock:analysis:exit",
        "close",
    ]
    assert json.loads(capsys.readouterr().out) == [
        {
            "video_url": "https://www.youtube.com/watch?v=analysis-1",
            "youtube_video_id": "analysis-1",
            "status": "analyzed",
            "detail": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_limit"),
    [
        (["push"], None),
        (["push", "--limit", "20"], 20),
    ],
)
async def test_push_uses_publication_lock_without_constructing_analysis_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected_limit: int | None,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)
    result = PublicationResult(
        video_analysis_id=UUID(int=2),
        youtube_video_id="published-video",
        video_url="https://www.youtube.com/watch?v=published-video",
        status="published",
    )
    _patch_push_consumer(monkeypatch, events, [result])

    exit_code = await main_module._run(main_module._parser().parse_args(arguments))

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "publisher",
        "consumer",
        "lock:publication:enter",
        f"consume:{expected_limit}",
        "lock:publication:exit",
        "close",
    ]
    assert json.loads(capsys.readouterr().out) == [
        {
            "video_analysis_id": str(UUID(int=2)),
            "youtube_video_id": "published-video",
            "video_url": "https://www.youtube.com/watch?v=published-video",
            "status": "published",
            "detail": None,
        }
    ]


@pytest.mark.asyncio
async def test_push_uncertain_reconciliation_failure_sets_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)
    _patch_push_consumer(
        monkeypatch,
        events,
        [
            PublicationResult(
                video_analysis_id=UUID(int=3),
                youtube_video_id="failed-video",
                video_url="https://www.youtube.com/watch?v=failed-video",
                status="failed",
                detail="remote result is uncertain; reconcile it before retrying",
            )
        ],
    )

    exit_code = await main_module._run(main_module._parser().parse_args(["push"]))

    assert exit_code == 1


@pytest.mark.asyncio
async def test_video_uses_separate_stage_locks_and_keeps_object_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)
    video_url = "https://www.youtube.com/watch?v=video-id"

    exit_code = await main_module._run(
        main_module._parser().parse_args(["video", video_url, "--force"])
    )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:download:enter",
        f"download_video:{video_url}:None:True",
        "lock:download:exit",
        "lock:analysis:enter",
        "cleanup",
        "analyze_video:video-id:True",
        "lock:analysis:exit",
        "close",
    ]
    assert json.loads(capsys.readouterr().out) == {
        "video_url": video_url,
        "youtube_video_id": "video-id",
        "status": "analyzed",
        "detail": None,
    }


@pytest.mark.asyncio
async def test_channel_add_does_not_take_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    exit_code = await main_module._run(
        main_module._parser().parse_args(
            ["channel-add", "@example", "--researcher", "Example"]
        )
    )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "channel-add:@example:Example",
        "close",
    ]


@pytest.mark.asyncio
async def test_disabled_session_cleanup_does_not_call_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events, cleanup_enabled=False)

    exit_code = await main_module._run(
        main_module._parser().parse_args(["analyze"])
    )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:analysis:enter",
        "analyze_pending:None:False",
        "lock:analysis:exit",
        "close",
    ]


@pytest.mark.asyncio
async def test_session_cleanup_failure_warns_and_continues_analysis(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    async def fail_cleanup(**kwargs: Any) -> None:
        del kwargs
        events.append("cleanup-failed")
        raise main_module.CodexSessionCleanupError("cleanup unavailable")

    monkeypatch.setattr(main_module, "cleanup_historical_agent_sessions", fail_cleanup)

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        exit_code = await main_module._run(
            main_module._parser().parse_args(["analyze"])
        )

    assert exit_code == 0
    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:analysis:enter",
        "cleanup-failed",
        "analyze_pending:None:False",
        "lock:analysis:exit",
        "close",
    ]
    assert "cleanup failed; continuing analysis" in caplog.text
    assert "cleanup unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_session_cleanup_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    async def cancel_cleanup(**kwargs: Any) -> None:
        del kwargs
        events.append("cleanup-cancelled")
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module, "cleanup_historical_agent_sessions", cancel_cleanup)

    with pytest.raises(asyncio.CancelledError):
        await main_module._run(main_module._parser().parse_args(["analyze"]))

    assert events == [
        "migrate",
        "connect",
        "service",
        "lock:analysis:enter",
        "cleanup-cancelled",
        "lock:analysis:exit",
        "close",
    ]


@pytest.mark.asyncio
async def test_session_cleanup_success_log_contains_counts_but_not_thread_ids(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = SimpleNamespace(
        agent=SimpleNamespace(
            cleanup_historical_sessions=True,
            codex_path="codex",
            session_cleanup_timeout_seconds=300,
        )
    )

    async def cleanup_sessions(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            scanned_threads=8,
            matched_threads=3,
            deleted_threads=1,
            already_missing_threads=1,
            skipped_live_threads=1,
            skipped_loaded_threads=1,
            skipped_descendant_threads=1,
            unverified_threads=1,
            failed_thread_ids=("sensitive-thread-id",),
        )

    monkeypatch.setattr(
        main_module,
        "cleanup_historical_agent_sessions",
        cleanup_sessions,
    )

    with caplog.at_level(logging.INFO, logger=main_module.__name__):
        await main_module._cleanup_historical_sessions(settings)

    assert "scanned=8 matched=3 deleted=1" in caplog.text
    assert "skipped_descendant=1" in caplog.text
    assert "failed=1" in caplog.text
    assert "sensitive-thread-id" not in caplog.text
    assert caplog.records[-1].levelno == logging.WARNING


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--max-videos-per-channel", "-1"],
        ["download", "--max-videos-per-channel", "-1"],
        ["analyze", "--limit", "-1"],
        ["push", "--limit", "-1"],
    ],
)
def test_parser_rejects_negative_limits(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main_module._parser().parse_args(arguments)

    assert error.value.code == 2


@pytest.mark.asyncio
async def test_failed_stage_result_sets_nonzero_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    service = _patch_runtime(monkeypatch, events)

    async def analyze_pending(*, max_videos: int | None, force: bool) -> list[ProcessResult]:
        del max_videos, force
        return [
            ProcessResult(
                video_url="https://www.youtube.com/watch?v=failed",
                youtube_video_id="failed",
                status="failed",
            )
        ]

    monkeypatch.setattr(service, "analyze_pending", analyze_pending)

    exit_code = await main_module._run(
        main_module._parser().parse_args(["analyze"])
    )

    assert exit_code == 1
