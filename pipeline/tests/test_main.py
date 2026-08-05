from __future__ import annotations

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
    async def process_lock(self) -> AsyncIterator[None]:
        self.events.append("lock_enter")
        try:
            yield
        finally:
            self.events.append("lock_exit")

    async def connect(self) -> None:
        self.events.append("connect")

    async def close(self) -> None:
        self.events.append("close")


class FakeService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def add_channel(self, channel_url: str, researcher: str | None) -> UUID:
        del channel_url, researcher
        self.events.append("channel-add")
        return UUID(int=1)

    async def process_video(self, video_url: str, *, force: bool) -> ProcessResult:
        del force
        self.events.append("video")
        return ProcessResult(
            video_url=video_url,
            youtube_video_id="video-id",
            status="skipped",
        )

    async def run_channels(
        self,
        channel_urls: list[str],
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        del channel_urls, max_videos_per_channel, force
        self.events.append("run")
        return []


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    settings = SimpleNamespace(
        database=object(),
        youtube=SimpleNamespace(max_videos_per_channel=0),
    )
    repository = FakeRepository(events)

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
        return FakeService(events)

    monkeypatch.setattr(main_module, "load_settings", load_settings)
    monkeypatch.setattr(main_module, "_migrate", migrate)
    monkeypatch.setattr(main_module, "PipelineRepository", lambda database: repository)
    monkeypatch.setattr(main_module, "_service", create_service)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "operation"),
    [
        (["run"], "run"),
        (["video", "https://www.youtube.com/watch?v=video-id"], "video"),
    ],
)
async def test_resource_commands_run_inside_process_lock(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    operation: str,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    exit_code = await main_module._run(main_module._parser().parse_args(arguments))

    assert exit_code == 0
    assert events == [
        "migrate",
        "lock_enter",
        "connect",
        "service",
        operation,
        "close",
        "lock_exit",
    ]


@pytest.mark.asyncio
async def test_channel_add_does_not_take_resource_process_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _patch_runtime(monkeypatch, events)

    exit_code = await main_module._run(
        main_module._parser().parse_args(["channel-add", "@example"])
    )

    assert exit_code == 0
    assert events == ["migrate", "connect", "service", "channel-add", "close"]
