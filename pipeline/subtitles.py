from __future__ import annotations

import html
import io
import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import webvtt

from models import SubtitleCandidate


class SubtitleSelectionError(ValueError):
    """Raised when subtitle metadata is malformed."""


class SubtitleParseError(ValueError):
    """Raised when a downloaded subtitle cannot be normalized."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(value))
    text = "".join(parser.parts)
    return re.sub(r"\s+", " ", text).strip()


def _deduplicate_lines(lines: Iterable[str]) -> str:
    result: list[str] = []
    for line in lines:
        cleaned = _clean_text(line)
        if cleaned and (not result or result[-1] != cleaned):
            result.append(cleaned)
    if not result:
        raise SubtitleParseError("Subtitle did not contain any text")
    return "\n".join(result)


def _normalize_webvtt(raw_text: str) -> str:
    try:
        captions = webvtt.from_string(raw_text).captions
    except Exception as exc:
        raise SubtitleParseError("Invalid WebVTT subtitle") from exc
    return _deduplicate_lines(caption.text for caption in captions)


def _normalize_buffer_format(raw_text: str, source_format: str) -> str:
    try:
        captions = webvtt.from_buffer(io.StringIO(raw_text), format=source_format).captions
    except Exception as exc:
        raise SubtitleParseError(f"Invalid {source_format} subtitle") from exc
    return _deduplicate_lines(caption.text for caption in captions)


def _normalize_json3(raw_text: str) -> str:
    try:
        payload = json.loads(raw_text)
        events = payload["events"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SubtitleParseError("Invalid JSON3 subtitle") from exc

    lines: list[str] = []
    for event in events:
        segments = event.get("segs") if isinstance(event, dict) else None
        if not isinstance(segments, list):
            continue
        line = "".join(
            segment.get("utf8", "") for segment in segments if isinstance(segment, dict)
        )
        lines.append(line)
    return _deduplicate_lines(lines)


def _normalize_xml(raw_text: str) -> str:
    try:
        root = ElementTree.fromstring(raw_text)
    except ElementTree.ParseError as exc:
        raise SubtitleParseError("Invalid XML subtitle") from exc

    lines: list[str] = []
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"p", "text"}:
            lines.append("".join(element.itertext()))
    return _deduplicate_lines(lines)


def normalize_subtitle(raw_text: str, source_format: str) -> str:
    normalized_format = source_format.casefold()
    if normalized_format == "vtt":
        return _normalize_webvtt(raw_text)
    if normalized_format in {"srt", "sbv"}:
        return _normalize_buffer_format(raw_text, normalized_format)
    if normalized_format == "json3":
        return _normalize_json3(raw_text)
    if normalized_format in {"ttml", "srv1", "srv2", "srv3"}:
        return _normalize_xml(raw_text)
    raise SubtitleParseError(f"Unsupported subtitle format: {source_format}")


def is_chinese_language(language_code: str) -> bool:
    normalized = language_code.casefold()
    return normalized == "zh" or normalized.startswith("zh-")


def _is_auto_translated_url(url: str | None) -> bool:
    if not url:
        return False
    return "tlang" in parse_qs(urlparse(url).query, keep_blank_values=True)


class SubtitleSelector:
    def __init__(
        self,
        language_priority: Sequence[Sequence[str]],
        format_priority: Sequence[str],
        *,
        allow_automatic_captions: bool,
        exclude_auto_translated_captions: bool,
    ) -> None:
        self._language_priority = tuple(
            tuple(language.casefold() for language in group) for group in language_priority
        )
        self._format_priority = tuple(extension.casefold() for extension in format_priority)
        self._allow_automatic_captions = allow_automatic_captions
        self._exclude_auto_translated_captions = exclude_auto_translated_captions

    def select(self, info: Mapping[str, object]) -> SubtitleCandidate | None:
        candidates: list[tuple[tuple[int, int, int, int, int], SubtitleCandidate]] = []
        sources: list[tuple[object, bool]] = [(info.get("subtitles"), False)]
        if self._allow_automatic_captions:
            sources.append((info.get("automatic_captions"), True))

        for source, is_auto_generated in sources:
            if not isinstance(source, Mapping):
                continue
            for language_code, formats in source.items():
                if not isinstance(language_code, str) or not isinstance(formats, list):
                    continue
                language_rank = self._language_rank(language_code)
                if language_rank is None:
                    continue
                for format_index, subtitle_format in enumerate(formats):
                    if not isinstance(subtitle_format, Mapping):
                        continue
                    extension = subtitle_format.get("ext")
                    url = subtitle_format.get("url")
                    inline_data = subtitle_format.get("data")
                    if not isinstance(extension, str):
                        continue
                    if extension.casefold() not in self._format_priority:
                        continue
                    if not isinstance(url, str) and not isinstance(inline_data, str):
                        continue
                    if (
                        is_auto_generated
                        and self._exclude_auto_translated_captions
                        and _is_auto_translated_url(url if isinstance(url, str) else None)
                    ):
                        continue

                    headers = subtitle_format.get("http_headers")
                    candidate = SubtitleCandidate(
                        language_code=language_code,
                        language_name=(
                            subtitle_format.get("name")
                            if isinstance(subtitle_format.get("name"), str)
                            else None
                        ),
                        source_format=extension,
                        is_auto_generated=is_auto_generated,
                        url=url if isinstance(url, str) else None,
                        inline_data=inline_data if isinstance(inline_data, str) else None,
                        http_headers=(
                            {str(key): str(value) for key, value in headers.items()}
                            if isinstance(headers, Mapping)
                            else {}
                        ),
                    )
                    source_rank = 1 if is_auto_generated else 0
                    format_rank = self._format_rank(extension, format_index, len(formats))
                    rank = (*language_rank, source_rank, *format_rank)
                    candidates.append((rank, candidate))

        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    def _language_rank(self, language_code: str) -> tuple[int, int] | None:
        normalized = language_code.casefold()
        for group_index, group in enumerate(self._language_priority):
            for language_index, configured in enumerate(group):
                if normalized == configured:
                    return group_index, language_index * 2

        for group_index, group in enumerate(self._language_priority):
            for language_index, configured in enumerate(group):
                if "-" not in configured and normalized.startswith(f"{configured}-"):
                    return group_index, language_index * 2 + 1
        return None

    def _format_rank(
        self,
        extension: str,
        format_index: int,
        format_count: int,
    ) -> tuple[int, int]:
        normalized = extension.casefold()
        try:
            return self._format_priority.index(normalized), 0
        except ValueError:
            return len(self._format_priority), format_count - format_index
