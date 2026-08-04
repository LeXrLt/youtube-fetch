from __future__ import annotations

import json

import pytest

from subtitles import (
    SubtitleParseError,
    SubtitleSelector,
    is_chinese_language,
    normalize_subtitle,
)


def _selector() -> SubtitleSelector:
    return SubtitleSelector(
        [
            ["zh-Hans", "zh-CN", "zh"],
            ["zh-Hant", "zh-TW"],
            ["en", "en-US"],
        ],
        ["vtt", "json3"],
        allow_automatic_captions=True,
        exclude_auto_translated_captions=True,
    )


def test_selector_excludes_youtube_auto_translations() -> None:
    selected = _selector().select(
        {
            "subtitles": {
                "en": [{"ext": "vtt", "url": "https://captions.test/en.vtt"}],
            },
            "automatic_captions": {
                "zh-Hans": [
                    {
                        "ext": "vtt",
                        "url": "https://captions.test/zh.vtt?lang=en&tlang=zh-Hans",
                    }
                ]
            },
        }
    )

    assert selected is not None
    assert selected.language_code == "en"
    assert selected.is_auto_generated is False


def test_selector_uses_language_priority_before_caption_source() -> None:
    selected = _selector().select(
        {
            "subtitles": {
                "en": [{"ext": "vtt", "url": "https://captions.test/en.vtt"}],
            },
            "automatic_captions": {
                "zh-Hans": [
                    {"ext": "json3", "url": "https://captions.test/zh.json3?lang=zh-Hans"}
                ]
            },
        }
    )

    assert selected is not None
    assert selected.language_code == "zh-Hans"
    assert selected.is_auto_generated is True


def test_selector_prefers_traditional_chinese_over_english() -> None:
    selected = _selector().select(
        {
            "subtitles": {
                "en-US": [{"ext": "vtt", "url": "https://captions.test/en.vtt"}],
                "zh-TW": [{"ext": "vtt", "url": "https://captions.test/zh.vtt"}],
            }
        }
    )

    assert selected is not None
    assert selected.language_code == "zh-TW"


def test_selector_ignores_unconfigured_formats() -> None:
    selected = _selector().select(
        {
            "subtitles": {
                "zh-Hans": [{"ext": "ass", "url": "https://captions.test/zh.ass"}],
                "en": [{"ext": "vtt", "url": "https://captions.test/en.vtt"}],
            }
        }
    )

    assert selected is not None
    assert selected.language_code == "en"
    assert selected.source_format == "vtt"


def test_normalize_webvtt_removes_tags_and_consecutive_duplicates() -> None:
    raw = """WEBVTT

00:00:00.000 --> 00:00:01.000
<c>First line</c>

00:00:01.000 --> 00:00:02.000
First line

00:00:02.000 --> 00:00:03.000
Second&nbsp;line
"""

    assert normalize_subtitle(raw, "vtt") == "First line\nSecond line"


def test_normalize_json3() -> None:
    raw = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                {"segs": [{"utf8": "Next line"}]},
            ]
        }
    )

    assert normalize_subtitle(raw, "json3") == "Hello world\nNext line"


def test_normalize_ttml() -> None:
    raw = """<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
      <p begin="0s">First <span>line</span></p>
      <p begin="1s">Second line</p>
    </div></body></tt>"""

    assert normalize_subtitle(raw, "ttml") == "First line\nSecond line"


def test_normalize_rejects_empty_subtitle() -> None:
    with pytest.raises(SubtitleParseError, match="any text"):
        normalize_subtitle("WEBVTT\n", "vtt")


@pytest.mark.parametrize("language", ["zh", "zh-Hans", "ZH-tw"])
def test_is_chinese_language(language: str) -> None:
    assert is_chinese_language(language)
