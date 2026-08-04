from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from string import Template
from typing import Literal

from dotenv import dotenv_values
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.12 is required by this project.
    import tomli as tomllib


PIPELINE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_ROOT.parent
DEFAULT_CONFIG_PATH = PIPELINE_ROOT / "config" / "pipeline.toml"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str
    password: SecretStr
    database: str
    min_pool_size: int = Field(default=1, ge=1)
    max_pool_size: int = Field(default=5, ge=1)
    command_timeout_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def validate_pool_sizes(self) -> DatabaseSettings:
        if self.max_pool_size < self.min_pool_size:
            raise ValueError("max_pool_size must be greater than or equal to min_pool_size")
        return self


class YoutubeSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    cookie_file: Path
    subscription_feed_url: str = "https://www.youtube.com/feed/channels"
    max_videos_per_channel: int = Field(default=0, ge=0)
    socket_timeout_seconds: float = Field(default=30, gt=0)
    allow_automatic_captions: bool = True
    exclude_auto_translated_captions: bool = True
    format_priority: list[str]
    language_priority: list[list[str]]

    @model_validator(mode="after")
    def validate_priorities(self) -> YoutubeSettings:
        languages = [language.casefold() for group in self.language_priority for language in group]
        if not languages or len(languages) != len(set(languages)):
            raise ValueError("language_priority must contain unique language codes")
        if not self.format_priority:
            raise ValueError("format_priority must not be empty")
        return self


class AgentSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    codex_path: str = ""
    model: str = ""
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"
    analysis_web_search: Literal["disabled", "cached", "live"] = "live"
    turn_timeout_seconds: float = Field(default=600, gt=0)
    translation_chunk_chars: int = Field(default=12000, ge=1000)
    analysis_input_max_chars: int = Field(default=180000, ge=10000)
    prompt_file: Path
    translation_schema_file: Path
    analysis_schema_file: Path
    profile_name: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)


class ProjectionSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    translated_text: str
    is_relevant: str
    relevance_score: str
    quality_score: str
    summary: str
    translated_summary: str
    background_notes: str
    key_points: str
    tags: str
    tag_name: str
    tag_category: str
    tag_description: str
    tag_confidence: str


class PromptSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1)
    template: str = Field(min_length=1)

    def render(self, **values: object) -> str:
        rendered = Template(self.template).substitute(
            {key: "" if value is None else str(value) for key, value in values.items()}
        )
        return rendered.strip()


class PromptCatalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    translation: PromptSpec
    analysis: PromptSpec


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    pipeline_root: Path
    database: DatabaseSettings
    youtube: YoutubeSettings
    agent: AgentSettings
    projection: ProjectionSettings
    prompts: PromptCatalog
    translation_schema: dict[str, object]
    analysis_schema: dict[str, object]
    prompt_sha256: str
    translation_schema_sha256: str
    analysis_schema_sha256: str

    @property
    def prompt_version(self) -> str:
        return (
            f"translation:{self.prompts.translation.version};"
            f"analysis:{self.prompts.analysis.version}"
        )


def _resolve_path(pipeline_root: Path, configured_path: str | Path) -> Path:
    path = Path(configured_path)
    return path if path.is_absolute() else pipeline_root / path


def _load_json_schema(path: Path) -> tuple[dict[str, object], str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON schema must be an object: {path}")
    Draft202012Validator.check_schema(payload)
    return payload, hashlib.sha256(raw).hexdigest()


def _load_settings(config_path: Path, env_path: Path) -> RuntimeSettings:
    config_path = config_path.resolve()
    env_path = env_path.resolve()
    with config_path.open("rb") as config_file:
        config_data = tomllib.load(config_file)

    dotenv_data = {
        key: value for key, value in dotenv_values(env_path).items() if value is not None
    }
    environment = {**dotenv_data, **os.environ}
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise ValueError(f"Missing database environment variables: {', '.join(missing)}")

    database_data = config_data.get("database", {})
    database = DatabaseSettings(
        host=environment.get("POSTGRES_HOST", "localhost"),
        port=environment.get("POSTGRES_PORT", "5432"),
        user=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        database=environment["POSTGRES_DB"],
        **database_data,
    )
    youtube_data = dict(config_data.get("youtube", {}))
    if environment.get("YOUTUBE_COOKIE_FILE"):
        youtube_data["cookie_file"] = environment["YOUTUBE_COOKIE_FILE"]
    youtube_data["cookie_file"] = _resolve_path(PIPELINE_ROOT, youtube_data["cookie_file"])
    youtube = YoutubeSettings.model_validate(youtube_data)
    if not youtube.cookie_file.is_file():
        raise ValueError(f"YouTube cookie file does not exist: {youtube.cookie_file}")
    if youtube.cookie_file.stat().st_mode & 0o077:
        raise ValueError("YouTube cookie file must not be accessible by group or other users")
    agent_data = dict(config_data.get("agent", {}))
    if environment.get("CODEX_MODEL"):
        agent_data["model"] = environment["CODEX_MODEL"]
    if environment.get("CODEX_PATH"):
        agent_data["codex_path"] = environment["CODEX_PATH"]
    for key in ("prompt_file", "translation_schema_file", "analysis_schema_file"):
        agent_data[key] = _resolve_path(PIPELINE_ROOT, agent_data[key])
    agent = AgentSettings.model_validate(agent_data)
    projection = ProjectionSettings.model_validate(config_data.get("projection", {}))

    with agent.prompt_file.open("rb") as prompt_file:
        prompts = PromptCatalog.model_validate(tomllib.load(prompt_file))
    prompt_sha256 = hashlib.sha256(agent.prompt_file.read_bytes()).hexdigest()
    translation_schema, translation_schema_sha256 = _load_json_schema(
        agent.translation_schema_file
    )
    analysis_schema, analysis_schema_sha256 = _load_json_schema(agent.analysis_schema_file)

    return RuntimeSettings(
        project_root=PROJECT_ROOT,
        pipeline_root=PIPELINE_ROOT,
        database=database,
        youtube=youtube,
        agent=agent,
        projection=projection,
        prompts=prompts,
        translation_schema=translation_schema,
        analysis_schema=analysis_schema,
        prompt_sha256=prompt_sha256,
        translation_schema_sha256=translation_schema_sha256,
        analysis_schema_sha256=analysis_schema_sha256,
    )


async def load_settings(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = PROJECT_ROOT / ".env",
) -> RuntimeSettings:
    return await asyncio.to_thread(_load_settings, config_path, env_path)
