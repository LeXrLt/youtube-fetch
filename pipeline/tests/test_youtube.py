from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import youtube as youtube_module
from youtube import YoutubeClient, YoutubeMetadataError


class FakeYoutubeDL:
    payload: dict[str, Any] = {}
    calls: list[tuple[dict[str, object], str, bool]] = []

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        type(self).calls.append((self.options, url, download))
        return type(self).payload


def _settings(cookie_file: Path) -> SimpleNamespace:
    return SimpleNamespace(
        cookie_file=cookie_file,
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
    monkeypatch.setattr(youtube_module, "YoutubeDL", FakeYoutubeDL)


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
    options, url, download = FakeYoutubeDL.calls[0]
    assert url == "https://www.youtube.com/feed/channels"
    assert download is False
    assert options["cookiefile"] == str(cookie_file)
    assert options["extract_flat"] == "in_playlist"


@pytest.mark.asyncio
async def test_inspects_channel_profile_metadata(tmp_path: Path) -> None:
    FakeYoutubeDL.payload = {
        "channel_id": "channel-one",
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
        "channel_id": "channel-one",
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
            {"id": "video-one", "url": "https://www.youtube.com/watch?v=video-one"},
            {"id": "video-one", "url": "https://www.youtube.com/watch?v=video-one"},
            {"id": "video-two", "url": "video-two"},
        ]
    }

    references = await YoutubeClient(_settings(tmp_path / "cookies")).discover_channel_videos(
        "https://www.youtube.com/@channel",
        None,
    )

    assert [(reference.youtube_video_id, reference.video_url) for reference in references] == [
        ("video-one", "https://www.youtube.com/watch?v=video-one"),
        ("video-two", "https://www.youtube.com/watch?v=video-two"),
    ]
    options, _, _ = FakeYoutubeDL.calls[0]
    assert "playlistend" not in options
    assert options["extract_flat"] == "in_playlist"
