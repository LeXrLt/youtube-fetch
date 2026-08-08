from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import PIPELINE_ROOT, PipelineSettings, load_settings


def test_pipeline_settings_defaults_download_concurrency_to_four() -> None:
    assert PipelineSettings().download_concurrency == 4


@pytest.mark.asyncio
async def test_load_settings_prefers_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_HOST=from-file\n"
        "POSTGRES_USER=file-user\n"
        "POSTGRES_PASSWORD=file-password\n"
        "POSTGRES_DB=file-db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("POSTGRES_HOST", "from-environment")
    monkeypatch.setenv("POSTGRES_USER", "environment-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "environment-password")
    monkeypatch.setenv("POSTGRES_DB", "environment-db")

    settings = await load_settings(env_path=env_file)

    assert settings.database.host == "from-environment"
    assert settings.database.user == "environment-user"
    assert settings.database.database == "environment-db"
    assert settings.database.password.get_secret_value() == "environment-password"
    assert settings.pipeline.download_concurrency == 4


@pytest.mark.parametrize("value", [0, 17])
def test_pipeline_settings_rejects_invalid_download_concurrency(value: int) -> None:
    with pytest.raises(ValidationError, match="download_concurrency"):
        PipelineSettings(download_concurrency=value)


@pytest.mark.parametrize("value", [1, 16])
def test_pipeline_settings_accepts_download_concurrency_bounds(value: int) -> None:
    settings = PipelineSettings(download_concurrency=value)

    assert settings.download_concurrency == value


@pytest.mark.asyncio
async def test_load_settings_rejects_invalid_json_schema(tmp_path: Path) -> None:
    config_text = (PIPELINE_ROOT / "config" / "pipeline.toml").read_text(encoding="utf-8")
    invalid_schema = tmp_path / "invalid.schema.json"
    invalid_schema.write_text(json.dumps({"type": "unknown"}), encoding="utf-8")
    config_file = tmp_path / "pipeline.toml"
    config_file.write_text(
        config_text.replace(
            'analysis_schema_file = "config/analysis.schema.json"',
            f'analysis_schema_file = "{invalid_schema}"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="unknown"):
        await load_settings(config_path=config_file)


@pytest.mark.asyncio
async def test_load_settings_requires_private_cookie_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_file = tmp_path / "youtube.cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="ascii")
    cookie_file.chmod(0o644)
    monkeypatch.setenv("YOUTUBE_COOKIE_FILE", str(cookie_file))

    with pytest.raises(ValueError, match="must not be accessible"):
        await load_settings()

    cookie_file.chmod(0o600)
    settings = await load_settings()
    assert settings.youtube.cookie_file == cookie_file
