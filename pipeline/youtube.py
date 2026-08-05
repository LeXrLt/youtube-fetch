from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking.common import Request

from config import YoutubeSettings
from models import (
    ChannelMetadata,
    DownloadedSubtitle,
    FetchedVideo,
    SubtitleCandidate,
    VideoMetadata,
    VideoReference,
)
from subtitles import (
    SubtitleParseError,
    SubtitleSelector,
    normalize_subtitle,
)

LOGGER = logging.getLogger(__name__)


class YoutubeMetadataError(ValueError):
    """Raised when yt-dlp does not return required YouTube metadata."""


class YoutubeChannelReferenceError(YoutubeMetadataError):
    """Raised when a channel reference is not an allowed YouTube channel form."""


_YOUTUBE_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_CHANNEL_ROUTE_NAMES = frozenset({"channel", "c", "user"})
_CHANNEL_TAB_NAMES = frozenset(
    {"about", "community", "featured", "live", "playlists", "shorts", "streams", "videos"}
)
_MAX_CHANNEL_REFERENCE_LENGTH = 512
_MAX_CHANNEL_NAME_LENGTH = 100


def normalize_channel_reference(reference: str) -> str:
    """Return a canonical, allowlisted YouTube channel URL for a user reference."""

    if not isinstance(reference, str):
        raise YoutubeChannelReferenceError("Channel reference must be text")
    value = reference.strip()
    if (
        not value
        or len(value) > _MAX_CHANNEL_REFERENCE_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise YoutubeChannelReferenceError("Invalid YouTube channel reference")

    if value.startswith("@"):
        return _handle_url(value[1:])
    if _YOUTUBE_CHANNEL_ID.fullmatch(value):
        return f"https://www.youtube.com/channel/{value}"

    lowered = value.casefold()
    if "://" not in value and any(lowered.startswith(f"{host}/") for host in _YOUTUBE_HOSTS):
        value = f"https://{value}"
    elif "://" not in value:
        return _handle_url(value)

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise YoutubeChannelReferenceError("Invalid YouTube channel URL") from exc

    hostname = (parsed.hostname or "").rstrip(".").casefold()
    default_port = (parsed.scheme.casefold() == "https" and port in (None, 443)) or (
        parsed.scheme.casefold() == "http" and port in (None, 80)
    )
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or hostname not in _YOUTUBE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or not default_port
    ):
        raise YoutubeChannelReferenceError("Only YouTube channel URLs are supported")

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if not segments:
        raise YoutubeChannelReferenceError("YouTube channel URL is missing a channel")

    first = segments[0]
    if first.startswith("@"):
        base_segments = (first,)
        canonical = _handle_url(first[1:])
    elif first in _CHANNEL_ROUTE_NAMES and len(segments) >= 2:
        identifier = _channel_name(segments[1])
        if first == "channel" and not _YOUTUBE_CHANNEL_ID.fullmatch(identifier):
            raise YoutubeChannelReferenceError("Invalid YouTube channel ID")
        base_segments = (first, segments[1])
        canonical = f"https://www.youtube.com/{first}/{quote(identifier, safe='-._~')}"
    else:
        raise YoutubeChannelReferenceError("URL does not identify a YouTube channel")

    remainder = segments[len(base_segments) :]
    if len(remainder) > 1 or (remainder and remainder[0] not in _CHANNEL_TAB_NAMES):
        raise YoutubeChannelReferenceError("URL does not identify a YouTube channel")
    return canonical


def _handle_url(handle: str) -> str:
    normalized = _channel_name(handle)
    return f"https://www.youtube.com/@{quote(normalized, safe='-._~')}"


def _channel_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_CHANNEL_NAME_LENGTH
        or "@" in normalized
        or any(character.isspace() or character in "/\\?#" for character in normalized)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise YoutubeChannelReferenceError("Invalid YouTube channel name")
    return normalized


class _YtDlpLogger:
    def debug(self, message: str) -> None:
        if not message.startswith("[debug] "):
            LOGGER.info(message)

    def info(self, message: str) -> None:
        LOGGER.info(message)

    def warning(self, message: str) -> None:
        LOGGER.warning(message)

    def error(self, message: str) -> None:
        LOGGER.error(message)


class YoutubeClient:
    def __init__(self, settings: YoutubeSettings) -> None:
        self._settings = settings
        self._selector = SubtitleSelector(
            settings.language_priority,
            settings.format_priority,
            allow_automatic_captions=settings.allow_automatic_captions,
            exclude_auto_translated_captions=settings.exclude_auto_translated_captions,
        )

    async def fetch_video(self, video_url: str) -> FetchedVideo:
        return await asyncio.to_thread(self._fetch_video_sync, video_url)

    async def discover_channel_videos(
        self,
        channel_url: str,
        limit: int | None,
    ) -> list[VideoReference]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        normalized_url = normalize_channel_reference(channel_url)
        return await asyncio.to_thread(self._discover_channel_videos_sync, normalized_url, limit)

    async def discover_subscribed_channels(self) -> list[ChannelMetadata]:
        return await asyncio.to_thread(self._discover_subscribed_channels_sync)

    async def inspect_channel(self, channel_url: str) -> ChannelMetadata:
        normalized_url = normalize_channel_reference(channel_url)
        channel = await asyncio.to_thread(self._inspect_channel_sync, normalized_url)
        if not _YOUTUBE_CHANNEL_ID.fullmatch(channel.youtube_channel_id):
            raise YoutubeMetadataError("YouTube returned an invalid channel identifier")
        return channel

    def _base_options(self) -> dict[str, object]:
        return {
            "quiet": True,
            "no_warnings": False,
            "logger": _YtDlpLogger(),
            "socket_timeout": self._settings.socket_timeout_seconds,
            "skip_download": True,
            "cookiefile": str(self._settings.cookie_file),
        }

    def _fetch_video_sync(self, video_url: str) -> FetchedVideo:
        options = {**self._base_options(), "noplaylist": True}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not isinstance(info, Mapping):
                raise YoutubeMetadataError("yt-dlp returned invalid video metadata")
            metadata = _video_metadata(info)
            candidate = self._selector.select(info)
            if candidate is None:
                return FetchedVideo(metadata=metadata, subtitle=None)
            raw_text = _download_subtitle(ydl, info, candidate)
            try:
                normalized_text = normalize_subtitle(raw_text, candidate.source_format)
            except SubtitleParseError as exc:
                LOGGER.warning(
                    "Preserving an unparseable %s subtitle for video %s: %s",
                    candidate.source_format,
                    metadata.youtube_video_id,
                    exc,
                )
                normalized_text = None
            subtitle = DownloadedSubtitle(
                language_code=candidate.language_code,
                language_name=candidate.language_name,
                source_format=candidate.source_format,
                is_auto_generated=candidate.is_auto_generated,
                raw_text=raw_text,
                normalized_text=normalized_text,
            )
            return FetchedVideo(metadata=metadata, subtitle=subtitle)

    def _discover_channel_videos_sync(
        self,
        channel_url: str,
        limit: int | None,
    ) -> list[VideoReference]:
        options = {
            **self._base_options(),
            "extract_flat": "in_playlist",
        }
        if limit is not None:
            options["playlistend"] = limit
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        if not isinstance(info, Mapping):
            raise YoutubeMetadataError("yt-dlp returned invalid channel metadata")
        entries = info.get("entries")
        if not isinstance(entries, Iterable) or isinstance(entries, str | bytes | Mapping):
            raise YoutubeMetadataError("The URL did not resolve to a channel video list")

        references: list[VideoReference] = []
        seen_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            webpage_url = entry.get("url") or entry.get("webpage_url")
            video_id = entry.get("id")
            if not isinstance(video_id, str) or not video_id or video_id in seen_ids:
                continue
            if not isinstance(webpage_url, str) or not webpage_url.startswith(
                ("http://", "https://")
            ):
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            references.append(VideoReference(youtube_video_id=video_id, video_url=webpage_url))
            seen_ids.add(video_id)
        return references

    def _discover_subscribed_channels_sync(self) -> list[ChannelMetadata]:
        options = {**self._base_options(), "extract_flat": "in_playlist"}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(self._settings.subscription_feed_url, download=False)
        if not isinstance(info, Mapping):
            raise YoutubeMetadataError("yt-dlp returned invalid subscription metadata")
        entries = info.get("entries")
        if not isinstance(entries, Iterable) or isinstance(entries, str | bytes | Mapping):
            raise YoutubeMetadataError("The authenticated subscription feed is not a channel list")

        channels: list[ChannelMetadata] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise YoutubeMetadataError(
                    f"Subscription feed entry {index} is not a channel object"
                )
            try:
                channel = _channel_metadata(entry)
            except YoutubeMetadataError as exc:
                raise YoutubeMetadataError(
                    f"Subscription feed entry {index} has incomplete channel metadata"
                ) from exc
            if channel.youtube_channel_id in seen_ids:
                continue
            channels.append(channel)
            seen_ids.add(channel.youtube_channel_id)
        return channels

    def _inspect_channel_sync(self, channel_url: str) -> ChannelMetadata:
        options = {
            **self._base_options(),
            "extract_flat": "in_playlist",
            "playlistend": 1,
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(channel_url, download=False)
        if not isinstance(info, Mapping):
            raise YoutubeMetadataError("yt-dlp returned invalid channel metadata")
        return _channel_metadata(info, fallback_url=channel_url, channel_page=True)


def _required_string(info: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise YoutubeMetadataError(f"Missing required metadata: {' or '.join(keys)}")


def _optional_string(info: Mapping[str, object], *keys: str) -> str | None:
    try:
        return _required_string(info, *keys)
    except YoutubeMetadataError:
        return None


def _channel_metadata(
    info: Mapping[str, object],
    *,
    fallback_url: str | None = None,
    channel_page: bool = False,
) -> ChannelMetadata:
    channel_id = _required_string(info, "channel_id", "uploader_id")
    title = _required_string(info, "channel", "uploader", "title")
    channel_url = _optional_string(info, "channel_url", "uploader_url") or fallback_url
    if not channel_url:
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
    uploader_id = _optional_string(info, "uploader_id")
    handle = uploader_id if uploader_id and uploader_id.startswith("@") else None
    description = _optional_string(info, "channel_description")
    if channel_page:
        description = description or _optional_string(info, "description")
    avatar_url = _channel_avatar_url(info, channel_page=channel_page)
    return ChannelMetadata(
        youtube_channel_id=channel_id,
        title=title,
        channel_url=channel_url,
        handle=handle,
        description=description,
        avatar_url=avatar_url,
    )


def _channel_avatar_url(
    info: Mapping[str, object],
    *,
    channel_page: bool = False,
) -> str | None:
    thumbnails = info.get("channel_thumbnails")
    if isinstance(thumbnails, list):
        urls = [
            thumbnail.get("url")
            for thumbnail in thumbnails
            if isinstance(thumbnail, Mapping)
            and isinstance(thumbnail.get("url"), str)
            and thumbnail.get("url", "").strip()
        ]
        if urls:
            return urls[-1].strip()

    if not channel_page:
        return None
    page_thumbnails = info.get("thumbnails")
    if not isinstance(page_thumbnails, list):
        return None

    valid_thumbnails = [
        thumbnail
        for thumbnail in page_thumbnails
        if isinstance(thumbnail, Mapping)
        and isinstance(thumbnail.get("url"), str)
        and thumbnail.get("url", "").strip()
    ]
    avatar_thumbnails = [
        thumbnail
        for thumbnail in valid_thumbnails
        if isinstance(thumbnail.get("id"), str) and thumbnail.get("id", "").startswith("avatar")
    ]
    if avatar_thumbnails:
        return str(avatar_thumbnails[-1]["url"]).strip()

    square_thumbnails = [
        thumbnail
        for thumbnail in valid_thumbnails
        if isinstance(thumbnail.get("width"), int | float)
        and isinstance(thumbnail.get("height"), int | float)
        and thumbnail["width"] > 0
        and thumbnail["height"] > 0
        and abs(thumbnail["width"] - thumbnail["height"])
        <= max(thumbnail["width"], thumbnail["height"]) * 0.1
    ]
    if not square_thumbnails:
        return None
    largest_square = max(
        square_thumbnails,
        key=lambda thumbnail: thumbnail["width"] * thumbnail["height"],
    )
    return str(largest_square["url"]).strip()


def _video_metadata(info: Mapping[str, object]) -> VideoMetadata:
    duration = info.get("duration")
    duration_seconds = (
        int(duration) if isinstance(duration, int | float) and duration >= 0 else None
    )
    published_at = _published_at(info)
    return VideoMetadata(
        youtube_video_id=_required_string(info, "id"),
        channel=_channel_metadata(info),
        title=_required_string(info, "title"),
        video_url=_required_string(info, "webpage_url", "original_url"),
        description=_optional_string(info, "description"),
        duration_seconds=duration_seconds,
        published_at=published_at,
    )


def _published_at(info: Mapping[str, object]) -> datetime | None:
    timestamp = info.get("timestamp")
    if isinstance(timestamp, int | float):
        return datetime.fromtimestamp(timestamp, tz=UTC)
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str):
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _download_subtitle(
    ydl: YoutubeDL,
    info: Mapping[str, Any],
    candidate: SubtitleCandidate,
) -> str:
    if candidate.inline_data is not None:
        return candidate.inline_data
    if candidate.url is None:
        raise YoutubeMetadataError("Selected subtitle has no URL or inline data")

    info_headers = info.get("http_headers")
    headers = (
        {str(key): str(value) for key, value in info_headers.items()}
        if isinstance(info_headers, Mapping)
        else {}
    )
    headers.update(candidate.http_headers)
    request = Request(candidate.url, headers=headers)
    with ydl.urlopen(request) as response:
        payload = response.read()
    if not isinstance(payload, bytes):
        raise YoutubeMetadataError("Subtitle download returned non-byte content")
    return payload.decode("utf-8-sig", errors="replace")
