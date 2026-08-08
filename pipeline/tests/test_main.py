from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import main as main_module
from models import ProcessResult


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
) -> FakeService:
    settings = SimpleNamespace(
        database=object(),
        youtube=SimpleNamespace(max_videos_per_channel=default_max_videos),
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

    monkeypatch.setattr(main_module, "load_settings", load_settings)
    monkeypatch.setattr(main_module, "_migrate", migrate)
    monkeypatch.setattr(main_module, "PipelineRepository", lambda database: repository)
    monkeypatch.setattr(main_module, "_service", create_service)
    return service


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


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--max-videos-per-channel", "-1"],
        ["download", "--max-videos-per-channel", "-1"],
        ["analyze", "--limit", "-1"],
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
