from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import youtube as youtube_module
from youtube import (
    YoutubeChannelReferenceError,
    YoutubeClient,
    YoutubeMetadataError,
    normalize_channel_reference,
)

CHANNEL_ID = "UCaaaaaaaaaaaaaaaaaaaaaa"


class FakeYoutubeDL:
    payload: dict[str, Any] = {}
    calls: list[tuple[dict[str, object], str, bool, bool]] = []
    active = False

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self) -> FakeYoutubeDL:
        type(self).active = True
        return self

    def __exit__(self, *args: object) -> None:
        del args
        type(self).active = False

    def extract_info(
        self,
        url: str,
        *,
        download: bool,
        process: bool = True,
    ) -> dict[str, Any]:
        type(self).calls.append((self.options, url, download, process))
        return type(self).payload


def _settings(
    cookie_file: Path | None,
    *,
    cookie_source: str = "file",
    chrome_profile: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        cookie_source=cookie_source,
        cookie_file=cookie_file,
        chrome_profile=chrome_profile,
        subscription_feed_url="https://www.youtube.com/feed/channels",
        socket_timeout_seconds=30,
        allow_automatic_captions=True,
        exclude_auto_translated_captions=True,
        format_priority=["vtt"],
        language_priority=[["zh-Hans"], ["zh-Hant"], ["en"]],
    )


@pytest.fixture(autouse=True)
def fake_youtube_dl(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeYoutubeDL.payload = {}
    FakeYoutubeDL.calls = []
    FakeYoutubeDL.active = False
    monkeypatch.setattr(youtube_module, "YoutubeDL", FakeYoutubeDL)


@pytest.mark.asyncio
async def test_blocking_call_preserves_normal_and_error_results() -> None:
    expected_error = RuntimeError("yt-dlp failed")

    assert await youtube_module._run_blocking_call(lambda: "completed") == "completed"

    def fail() -> None:
        raise expected_error

    with pytest.raises(RuntimeError) as raised:
        await youtube_module._run_blocking_call(fail)

    assert raised.value is expected_error


@pytest.mark.asyncio
async def test_cancelled_blocking_call_waits_for_worker_to_finish() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def block() -> None:
        started.set()
        release.wait()
        finished.set()

    task = asyncio.create_task(youtube_module._run_blocking_call(block))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)

    try:
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_worker_error_does_not_replace_pending_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()

    def fail_after_release() -> None:
        started.set()
        release.wait()
        raise RuntimeError("late yt-dlp failure")

    task = asyncio.create_task(youtube_module._run_blocking_call(fail_after_release))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_discovers_authenticated_subscriptions_with_cookie_file(
    tmp_path: Path,
) -> None:
    cookie_file = tmp_path / "youtube.cookies.txt"
    FakeYoutubeDL.payload = {
        "entries": [
            {
                "channel_id": "channel-one",
                "channel": "Channel One",
                "channel_url": "https://www.youtube.com/channel/channel-one",
            },
            {
                "channel_id": "channel-one",
                "channel": "Duplicate Channel One",
                "channel_url": "https://www.youtube.com/channel/channel-one",
            },
            {
                "channel_id": "channel-two",
                "channel": "Channel Two",
                "channel_url": "https://www.youtube.com/channel/channel-two",
            },
        ]
    }

    channels = await YoutubeClient(_settings(cookie_file)).discover_subscribed_channels()

    assert [channel.youtube_channel_id for channel in channels] == [
        "channel-one",
        "channel-two",
    ]
    assert len(FakeYoutubeDL.calls) == 1
    options, url, download, process = FakeYoutubeDL.calls[0]
    assert url == "https://www.youtube.com/feed/channels"
    assert download is False
    assert process is True
    assert options["cookiefile"] == str(cookie_file)
    assert "cookiesfrombrowser" not in options
    assert options["extract_flat"] == "in_playlist"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "expected_specification"),
    [
        (None, ("chrome",)),
        ("Profile 2", ("chrome", "Profile 2", None, None)),
    ],
)
async def test_uses_chrome_browser_cookies_without_cookie_file(
    profile: str | None,
    expected_specification: tuple[object, ...],
) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": CHANNEL_ID,
        "channel": "Channel One",
        "channel_url": "https://www.youtube.com/@channel-one",
        "uploader_id": "@channel-one",
    }

    await YoutubeClient(
        _settings(
            None,
            cookie_source="chrome",
            chrome_profile=profile,
        )
    ).inspect_channel("@channel-one")

    options, _, _, _ = FakeYoutubeDL.calls[0]
    assert options["cookiesfrombrowser"] == expected_specification
    assert "cookiefile" not in options


@pytest.mark.asyncio
async def test_inspects_channel_profile_metadata(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": CHANNEL_ID,
        "channel": "Channel One",
        "channel_url": "https://www.youtube.com/@channel-one",
        "uploader_id": "@channel-one",
        "channel_description": "Original channel description",
        "channel_thumbnails": [
            {"url": "https://images.test/avatar-small.jpg", "width": 88},
            {"url": "https://images.test/avatar-large.jpg", "width": 176},
        ],
    }

    channel = await YoutubeClient(_settings(tmp_path / "cookies")).inspect_channel(
        "https://www.youtube.com/@channel-one"
    )

    assert channel.description == "Original channel description"
    assert channel.avatar_url == "https://images.test/avatar-large.jpg"


@pytest.mark.asyncio
async def test_inspects_channel_profile_from_playlist_metadata(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": CHANNEL_ID,
        "channel": "Channel One",
        "channel_url": "https://www.youtube.com/channel/channel-one",
        "uploader_id": "@channel-one",
        "description": "Playlist channel description",
        "thumbnails": [
            {
                "id": "banner_uncropped",
                "url": "https://images.test/banner.jpg",
                "width": 2560,
                "height": 424,
            },
            {
                "id": "7",
                "url": "https://images.test/avatar-square.jpg",
                "width": 900,
                "height": 900,
            },
            {
                "id": "avatar_uncropped",
                "url": "https://images.test/avatar-original.jpg",
            },
        ],
    }

    channel = await YoutubeClient(_settings(tmp_path / "cookies")).inspect_channel(
        "https://www.youtube.com/@channel-one"
    )

    assert channel.description == "Playlist channel description"
    assert channel.avatar_url == "https://images.test/avatar-original.jpg"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("@OpenAI", "https://www.youtube.com/@OpenAI"),
        ("OpenAI", "https://www.youtube.com/@OpenAI"),
        (CHANNEL_ID, f"https://www.youtube.com/channel/{CHANNEL_ID}"),
        ("youtube.com/@OpenAI/videos", "https://www.youtube.com/@OpenAI"),
        ("https://m.youtube.com/@OpenAI?view=0", "https://www.youtube.com/@OpenAI"),
        (
            f"http://www.youtube.com/channel/{CHANNEL_ID}/shorts",
            f"https://www.youtube.com/channel/{CHANNEL_ID}",
        ),
        ("https://www.youtube.com/user/legacy-name", "https://www.youtube.com/user/legacy-name"),
        ("https://youtube.com/c/custom.name", "https://www.youtube.com/c/custom.name"),
        ("研究员", "https://www.youtube.com/@%E7%A0%94%E7%A9%B6%E5%91%98"),
    ],
)
def test_normalizes_supported_channel_references(reference: str, expected: str) -> None:
    assert normalize_channel_reference(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "https://example.com/@OpenAI",
        "https://www.youtube.com/watch?v=video",
        "https://www.youtube.com/playlist?list=items",
        "https://www.youtube.com/@OpenAI/videos/extra",
        "https://user:password@www.youtube.com/@OpenAI",
        "https://www.youtube.com:8443/@OpenAI",
        "bad handle",
        "name/with/slash",
        "name\x00suffix",
    ],
)
def test_rejects_unsupported_channel_references(reference: str) -> None:
    with pytest.raises(YoutubeChannelReferenceError):
        normalize_channel_reference(reference)


@pytest.mark.asyncio
async def test_inspection_normalizes_reference_before_extracting(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": CHANNEL_ID,
        "channel": "Channel One",
        "channel_url": "https://www.youtube.com/@channel-one",
    }

    await YoutubeClient(_settings(tmp_path / "cookies")).inspect_channel("@channel-one")

    _, url, download, process = FakeYoutubeDL.calls[0]
    assert url == "https://www.youtube.com/@channel-one"
    assert download is False
    assert process is True


@pytest.mark.asyncio
async def test_inspection_rejects_non_channel_metadata(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": "video-uploader",
        "channel": "Not a channel page",
        "channel_url": "https://www.youtube.com/@channel-one",
    }

    with pytest.raises(YoutubeMetadataError, match="channel identifier"):
        await YoutubeClient(_settings(tmp_path / "cookies")).inspect_channel("@channel-one")


@pytest.mark.asyncio
async def test_video_metadata_does_not_use_video_profile_fields(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "id": "video-one",
        "title": "Video One",
        "webpage_url": "https://www.youtube.com/watch?v=video-one",
        "channel_id": "channel-one",
        "channel": "Channel One",
        "channel_url": "https://www.youtube.com/channel/channel-one",
        "description": "Video description",
        "thumbnails": [
            {
                "id": "0",
                "url": "https://images.test/video-thumbnail.jpg",
                "width": 1280,
                "height": 720,
            }
        ],
    }

    fetched = await YoutubeClient(_settings(tmp_path / "cookies")).fetch_video(
        "https://www.youtube.com/watch?v=video-one"
    )

    assert fetched.metadata.description == "Video description"
    assert fetched.metadata.channel.description is None
    assert fetched.metadata.channel.avatar_url is None


@pytest.mark.asyncio
async def test_rejects_partial_subscription_snapshot(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "entries": [
            {
                "channel_id": "channel-one",
                "channel": "Channel One",
                "channel_url": "https://www.youtube.com/channel/channel-one",
            },
            {"channel": "Missing stable channel identifier"},
        ]
    }

    with pytest.raises(YoutubeMetadataError, match="entry 1"):
        await YoutubeClient(_settings(tmp_path / "cookies")).discover_subscribed_channels()


@pytest.mark.asyncio
async def test_unlimited_channel_discovery_deduplicates_video_references(
    tmp_path: Path,
) -> None:
    FakeYoutubeDL.payload = {
        "entries": [
            {
                "id": "video-one",
                "url": "https://www.youtube.com/watch?v=video-one",
                "title": "Video One",
                "timestamp": 1_754_006_400,
            },
            {
                "id": "video-one",
                "url": "https://www.youtube.com/watch?v=video-one",
                "title": "Ignored Duplicate",
            },
            {"id": "video-two", "url": "video-two"},
        ]
    }

    result = await YoutubeClient(_settings(tmp_path / "cookies")).discover_channel_videos(
        CHANNEL_ID,
        None,
        known_video_ids=set(),
        stop_at_known=False,
    )

    assert [
        (reference.youtube_video_id, reference.video_url)
        for reference in result.references
    ] == [
        ("video-one", "https://www.youtube.com/watch?v=video-one"),
        ("video-two", "https://www.youtube.com/watch?v=video-two"),
    ]
    assert [reference.title for reference in result.references] == ["Video One", None]
    assert [reference.published_at for reference in result.references] == [
        datetime(2025, 8, 1, tzinfo=UTC),
        None,
    ]
    assert result.source_exhausted is True
    assert result.stopped_at_known is False
    options, url, download, process = FakeYoutubeDL.calls[0]
    assert url == f"https://www.youtube.com/playlist?list=UU{CHANNEL_ID[2:]}"
    assert download is False
    assert process is False
    assert "playlistend" not in options
    assert options["extract_flat"] == "in_playlist"


@pytest.mark.asyncio
async def test_incremental_discovery_ignores_limit_until_known_boundary(
    tmp_path: Path,
) -> None:
    consumed: list[str] = []

    def lazy_entries() -> Any:
        for video_id in ("new-one", "new-two", "known", "must-not-be-consumed"):
            assert FakeYoutubeDL.active
            consumed.append(video_id)
            if video_id == "must-not-be-consumed":
                raise AssertionError("discovery consumed entries after the known boundary")
            yield {"id": video_id, "url": video_id, "title": video_id.title()}

    FakeYoutubeDL.payload = {"entries": lazy_entries()}

    result = await YoutubeClient(_settings(tmp_path / "cookies")).discover_channel_videos(
        CHANNEL_ID,
        1,
        known_video_ids={"known"},
        stop_at_known=True,
    )

    assert [reference.youtube_video_id for reference in result.references] == [
        "new-one",
        "new-two",
    ]
    assert consumed == ["new-one", "new-two", "known"]
    assert result.source_exhausted is False
    assert result.stopped_at_known is True
    assert FakeYoutubeDL.active is False


@pytest.mark.asyncio
async def test_backfill_limit_counts_only_unknown_videos(tmp_path: Path) -> None:
    consumed: list[str] = []

    def lazy_entries() -> Any:
        for video_id in ("known-one", "new-one", "known-two", "new-two", "later"):
            assert FakeYoutubeDL.active
            consumed.append(video_id)
            yield {"id": video_id, "url": video_id}

    FakeYoutubeDL.payload = {"entries": lazy_entries()}

    result = await YoutubeClient(_settings(tmp_path / "cookies")).discover_channel_videos(
        CHANNEL_ID,
        2,
        known_video_ids={"known-one", "known-two"},
        stop_at_known=False,
    )

    assert [reference.youtube_video_id for reference in result.references] == [
        "new-one",
        "new-two",
    ]
    assert consumed == ["known-one", "new-one", "known-two", "new-two", "later"]
    assert result.source_exhausted is False
    assert result.stopped_at_known is False


@pytest.mark.asyncio
async def test_backfill_exact_limit_at_source_end_is_exhausted(tmp_path: Path) -> None:
    consumed: list[str] = []

    def lazy_entries() -> Any:
        for video_id in ("known-one", "new-one", "known-two", "new-two"):
            assert FakeYoutubeDL.active
            consumed.append(video_id)
            yield {"id": video_id, "url": video_id}

    FakeYoutubeDL.payload = {"entries": lazy_entries()}

    result = await YoutubeClient(_settings(tmp_path / "cookies")).discover_channel_videos(
        CHANNEL_ID,
        2,
        known_video_ids={"known-one", "known-two"},
        stop_at_known=False,
    )

    assert [reference.youtube_video_id for reference in result.references] == [
        "new-one",
        "new-two",
    ]
    assert consumed == ["known-one", "new-one", "known-two", "new-two"]
    assert result.source_exhausted is True
    assert result.stopped_at_known is False


@pytest.mark.asyncio
async def test_channel_discovery_validates_identifier_and_playlist_shape(
    tmp_path: Path,
) -> None:
    client = YoutubeClient(_settings(tmp_path / "cookies"))

    with pytest.raises(YoutubeMetadataError, match="channel identifier"):
        await client.discover_channel_videos(
            "not-a-ucid",
            None,
            known_video_ids=set(),
            stop_at_known=False,
        )

    FakeYoutubeDL.payload = {"entries": {"not": "an iterable video list"}}
    with pytest.raises(YoutubeMetadataError, match="uploads playlist"):
        await client.discover_channel_videos(
            CHANNEL_ID,
            None,
            known_video_ids=set(),
            stop_at_known=False,
        )


def test_yt_dlp_progress_messages_are_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger=youtube_module.__name__)
    logger = youtube_module._YtDlpLogger()

    logger.debug("video: Downloading webpage")
    logger.info("video: Downloading player API JSON")
    logger.warning("warning message")
    logger.error("error message")

    levels = {record.message: record.levelname for record in caplog.records}
    assert levels == {
        "video: Downloading webpage": "DEBUG",
        "video: Downloading player API JSON": "DEBUG",
        "warning message": "WARNING",
        "error message": "ERROR",
    }

    caplog.clear()
    caplog.set_level("INFO", logger=youtube_module.__name__)
    logger.debug("video: Downloading webpage")
    logger.info("video: Downloading player API JSON")
    logger.warning("visible warning")

    assert [record.message for record in caplog.records] == ["visible warning"]
