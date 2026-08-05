from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg

from config import DatabaseSettings
from models import (
    AgentInvocation,
    AnalysisOutcome,
    ChannelMetadata,
    ChannelRecord,
    DownloadedSubtitle,
    FetchedVideo,
    StoredVideo,
    TranslationResult,
    VideoMetadata,
)


class DatabaseNotStartedError(RuntimeError):
    """Raised when a repository method is used before connecting."""


class PipelineAlreadyRunningError(RuntimeError):
    """Raised when another resource-intensive Pipeline command holds the lock."""


class PipelineRepository:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            host=self._settings.host,
            port=self._settings.port,
            user=self._settings.user,
            password=self._settings.password.get_secret_value(),
            database=self._settings.database,
            min_size=self._settings.min_pool_size,
            max_size=self._settings.max_pool_size,
            command_timeout=self._settings.command_timeout_seconds,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def process_lock(self) -> AsyncIterator[None]:
        # A dedicated session keeps the lock independent from pool size and resets.
        connection = await asyncpg.connect(
            host=self._settings.host,
            port=self._settings.port,
            user=self._settings.user,
            password=self._settings.password.get_secret_value(),
            database=self._settings.database,
            command_timeout=self._settings.command_timeout_seconds,
        )
        try:
            acquired = await connection.fetchval(
                """
                SELECT pg_try_advisory_lock(
                    hashtextextended(current_database() || ':pipeline_process', 0)
                )
                """
            )
            if acquired is not True:
                raise PipelineAlreadyRunningError(
                    "Another resource-intensive Pipeline process is already running"
                )
            yield
        finally:
            try:
                await connection.close()
            finally:
                if not connection.is_closed():
                    connection.terminate()

    async def list_active_channels(self) -> list[ChannelRecord]:
        rows = await self._require_pool().fetch(
            """
            SELECT id, youtube_channel_id, title, channel_url,
                   is_subscribed, initial_backfill_completed_at
            FROM youtube_channels
            WHERE is_active = true
            ORDER BY title, youtube_channel_id
            """
        )
        return [
            ChannelRecord(
                id=row["id"],
                youtube_channel_id=row["youtube_channel_id"],
                title=row["title"],
                channel_url=row["channel_url"],
                is_subscribed=row["is_subscribed"],
                initial_backfill_completed_at=row["initial_backfill_completed_at"],
            )
            for row in rows
        ]

    async def register_channel(
        self,
        channel: ChannelMetadata,
        researcher_name: str | None,
    ) -> UUID:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            researcher_id = None
            if researcher_name:
                researcher_id = await connection.fetchval(
                    """
                    INSERT INTO researchers(display_name)
                    VALUES ($1)
                    ON CONFLICT (display_name) DO UPDATE
                    SET updated_at = now()
                    RETURNING id
                    """,
                    researcher_name,
                )
            return await self._upsert_channel(
                connection,
                channel,
                researcher_id,
                subscribed=None,
            )

    async def sync_subscribed_channels(
        self,
        channels: Sequence[ChannelMetadata],
    ) -> list[ChannelRecord]:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE youtube_channels
                SET is_subscribed = false, updated_at = now()
                WHERE is_subscribed = true
                """
            )
            channel_ids = [
                await self._upsert_channel(
                    connection,
                    channel,
                    None,
                    subscribed=True,
                )
                for channel in channels
            ]
            if not channel_ids:
                return []
            rows = await connection.fetch(
                """
                SELECT id, youtube_channel_id, title, channel_url,
                       is_subscribed, initial_backfill_completed_at
                FROM youtube_channels
                WHERE id = ANY($1::uuid[]) AND is_active = true
                ORDER BY title, youtube_channel_id
                """,
                channel_ids,
            )
            return [_channel_record(row) for row in rows]

    async def get_channels(self, channel_ids: Sequence[UUID]) -> list[ChannelRecord]:
        if not channel_ids:
            return []
        rows = await self._require_pool().fetch(
            """
            SELECT id, youtube_channel_id, title, channel_url,
                   is_subscribed, initial_backfill_completed_at
            FROM youtube_channels
            WHERE id = ANY($1::uuid[]) AND is_active = true
            ORDER BY title, youtube_channel_id
            """,
            list(channel_ids),
        )
        return [_channel_record(row) for row in rows]

    async def get_stored_video(self, youtube_video_id: str) -> StoredVideo | None:
        row = await self._require_pool().fetchrow(
            """
            SELECT
                video.id AS video_id,
                video.youtube_video_id,
                video.title AS video_title,
                video.description,
                video.video_url,
                video.duration_seconds,
                video.published_at,
                video.subtitle_status,
                channel.youtube_channel_id,
                channel.handle,
                channel.title AS channel_title,
                channel.channel_url,
                subtitle.id AS subtitle_track_id,
                subtitle.language_code,
                subtitle.language_name,
                subtitle.source_format,
                subtitle.is_auto_generated,
                subtitle.raw_text,
                subtitle.normalized_text,
                subtitle.translated_text,
                subtitle.translated_language_code,
                subtitle.translation_metadata
            FROM videos AS video
            JOIN youtube_channels AS channel ON channel.id = video.channel_id
            LEFT JOIN LATERAL (
                SELECT *
                FROM subtitle_tracks
                WHERE video_id = video.id
                ORDER BY fetched_at DESC, created_at DESC, id DESC
                LIMIT 1
            ) AS subtitle ON true
            WHERE video.youtube_video_id = $1
            """,
            youtube_video_id,
        )
        if row is None:
            return None

        subtitle = None
        if row["subtitle_track_id"] is not None:
            subtitle = DownloadedSubtitle(
                language_code=row["language_code"],
                language_name=row["language_name"],
                source_format=row["source_format"] or "",
                is_auto_generated=row["is_auto_generated"],
                raw_text=row["raw_text"],
                normalized_text=row["normalized_text"],
            )
        metadata = VideoMetadata(
            youtube_video_id=row["youtube_video_id"],
            channel=ChannelMetadata(
                youtube_channel_id=row["youtube_channel_id"],
                title=row["channel_title"],
                channel_url=row["channel_url"],
                handle=row["handle"],
            ),
            title=row["video_title"],
            video_url=row["video_url"],
            description=row["description"],
            duration_seconds=row["duration_seconds"],
            published_at=row["published_at"],
        )
        subtitle_status = row["subtitle_status"]
        if row["subtitle_track_id"] is None and subtitle_status in {"fetched", "invalid"}:
            subtitle_status = "pending"
        return StoredVideo(
            video_id=row["video_id"],
            subtitle_track_id=row["subtitle_track_id"],
            fetched=FetchedVideo(metadata=metadata, subtitle=subtitle),
            subtitle_status=subtitle_status,
            translated_text=row["translated_text"],
            translated_language_code=row["translated_language_code"],
            translation_metadata=_json_object(row["translation_metadata"]),
        )

    async def save_fetched_video(
        self,
        video: VideoMetadata,
        subtitle: DownloadedSubtitle | None,
    ) -> tuple[UUID, UUID | None]:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            channel_id = await self._upsert_channel(
                connection,
                video.channel,
                None,
                subscribed=None,
            )
            subtitle_status = (
                "unavailable"
                if subtitle is None
                else "invalid"
                if subtitle.normalized_text is None
                else "fetched"
            )
            video_id = await connection.fetchval(
                """
                INSERT INTO videos(
                    channel_id,
                    youtube_video_id,
                    title,
                    description,
                    video_url,
                    duration_seconds,
                    published_at,
                    downloaded_at,
                    subtitle_status,
                    subtitle_checked_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    CASE WHEN $8 IN ('fetched', 'invalid') THEN now() ELSE NULL END,
                    $8,
                    now()
                )
                ON CONFLICT (youtube_video_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    video_url = EXCLUDED.video_url,
                    duration_seconds = EXCLUDED.duration_seconds,
                    published_at = EXCLUDED.published_at,
                    downloaded_at = COALESCE(EXCLUDED.downloaded_at, videos.downloaded_at),
                    subtitle_status = CASE
                        WHEN EXCLUDED.subtitle_status = 'unavailable'
                             AND videos.subtitle_status IN ('fetched', 'invalid')
                        THEN videos.subtitle_status
                        ELSE EXCLUDED.subtitle_status
                    END,
                    subtitle_checked_at = EXCLUDED.subtitle_checked_at,
                    updated_at = now()
                RETURNING id
                """,
                channel_id,
                video.youtube_video_id,
                video.title,
                video.description,
                video.video_url,
                video.duration_seconds,
                video.published_at,
                subtitle_status,
            )
            if subtitle is None:
                return video_id, None
            subtitle_id = await connection.fetchval(
                """
                INSERT INTO subtitle_tracks(
                    video_id,
                    language_code,
                    language_name,
                    source_format,
                    is_auto_generated,
                    raw_text,
                    normalized_text
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (
                    video_id,
                    language_code,
                    is_auto_generated,
                    raw_sha256
                ) DO UPDATE
                SET language_name = EXCLUDED.language_name,
                    normalized_text = COALESCE(
                        subtitle_tracks.normalized_text,
                        EXCLUDED.normalized_text
                    ),
                    fetched_at = now(),
                    updated_at = now()
                RETURNING id
                """,
                video_id,
                subtitle.language_code,
                subtitle.language_name,
                subtitle.source_format,
                subtitle.is_auto_generated,
                subtitle.raw_text,
                subtitle.normalized_text,
            )
            return video_id, subtitle_id

    async def has_matching_analysis(
        self,
        video_id: UUID,
        subtitle_track_id: UUID,
        *,
        profile_name: str,
        schema_version: str,
        prompt_version: str,
        prompt_sha256: str,
        translation_schema_sha256: str,
        schema_sha256: str,
        source_sha256: str,
    ) -> bool:
        return bool(
            await self._require_pool().fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM video_analyses AS analysis
                    JOIN analysis_runs AS run ON run.id = analysis.analysis_run_id
                    WHERE analysis.video_id = $1
                      AND analysis.subtitle_track_id = $2
                      AND analysis.profile_name = $3
                      AND analysis.output_schema_version = $4
                      AND run.status = 'succeeded'
                      AND run.prompt_version = $5
                      AND run.metadata ->> 'prompt_sha256' = $6
                      AND run.metadata ->> 'translation_schema_sha256' = $7
                      AND run.metadata ->> 'schema_sha256' = $8
                      AND run.metadata ->> 'source_sha256' = $9
                )
                """,
                video_id,
                subtitle_track_id,
                profile_name,
                schema_version,
                prompt_version,
                prompt_sha256,
                translation_schema_sha256,
                schema_sha256,
                source_sha256,
            )
        )

    async def start_analysis_run(
        self,
        video_id: UUID,
        subtitle_track_id: UUID,
        *,
        agent_model: str | None,
        prompt_version: str,
        metadata: dict[str, Any],
    ) -> UUID:
        return await self._require_pool().fetchval(
            """
            INSERT INTO analysis_runs(
                agent_name,
                agent_model,
                prompt_version,
                status,
                video_id,
                subtitle_track_id,
                metadata
            )
            VALUES ('codex-sdk-py', $1, $2, 'running', $3, $4, $5::jsonb)
            RETURNING id
            """,
            agent_model,
            prompt_version,
            video_id,
            subtitle_track_id,
            _json(metadata),
        )

    async def save_translation(
        self,
        subtitle_track_id: UUID,
        translation: TranslationResult,
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE subtitle_tracks
            SET translated_text = $2,
                translated_language_code = $3,
                translation_metadata = $4::jsonb,
                updated_at = now()
            WHERE id = $1
            """,
            subtitle_track_id,
            translation.translated_text,
            translation.translated_language_code,
            _json(translation.metadata),
        )

    async def save_agent_invocation(
        self,
        run_id: UUID,
        invocation: AgentInvocation,
    ) -> None:
        await self._require_pool().execute(
            """
            INSERT INTO agent_invocations(
                analysis_run_id,
                stage,
                sequence_number,
                status,
                thread_id,
                agent_input,
                full_prompt,
                intermediate_events,
                final_response,
                agent_output,
                usage,
                error_message,
                started_at,
                finished_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb,
                $9, $10::jsonb, $11::jsonb, $12, $13, $14
            )
            ON CONFLICT (analysis_run_id, stage, sequence_number) DO UPDATE
            SET status = EXCLUDED.status,
                thread_id = EXCLUDED.thread_id,
                agent_input = EXCLUDED.agent_input,
                full_prompt = EXCLUDED.full_prompt,
                intermediate_events = EXCLUDED.intermediate_events,
                final_response = EXCLUDED.final_response,
                agent_output = EXCLUDED.agent_output,
                usage = EXCLUDED.usage,
                error_message = EXCLUDED.error_message,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at
            """,
            run_id,
            invocation.stage,
            invocation.sequence_number,
            invocation.status,
            invocation.thread_id,
            _json(invocation.agent_input),
            invocation.full_prompt,
            _json(invocation.intermediate_events),
            invocation.final_response,
            _json_or_none(invocation.output_payload),
            _json_or_none(invocation.usage),
            invocation.error_message,
            invocation.started_at,
            invocation.finished_at,
        )

    async def complete_analysis_run(
        self,
        run_id: UUID,
        video_id: UUID,
        subtitle_track_id: UUID,
        outcome: AnalysisOutcome,
        *,
        profile_name: str,
        schema_version: str,
        run_metadata: dict[str, Any],
    ) -> UUID:
        pool = self._require_pool()
        projection = outcome.projection
        async with pool.acquire() as connection, connection.transaction():
            analysis_id = await connection.fetchval(
                """
                INSERT INTO video_analyses(
                    video_id,
                    subtitle_track_id,
                    analysis_run_id,
                    relevance_score,
                    quality_score,
                    is_relevant,
                    summary,
                    translated_summary,
                    background_notes,
                    key_points,
                    raw_agent_output,
                    profile_name,
                    output_schema_version,
                    analysis_metadata
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10::jsonb, $11::jsonb, $12, $13, $14::jsonb
                )
                RETURNING id
                """,
                video_id,
                subtitle_track_id,
                run_id,
                projection.relevance_score,
                projection.quality_score,
                projection.is_relevant,
                projection.summary,
                projection.translated_summary,
                projection.background_notes,
                _json(projection.key_points),
                _json(outcome.payload),
                profile_name,
                schema_version,
                _json(outcome.metadata),
            )
            for tag in projection.tags:
                tag_id = await connection.fetchval(
                    """
                    INSERT INTO tags(name, category, description)
                    VALUES ($1, NULLIF($2, ''), NULLIF($3, ''))
                    ON CONFLICT (name) DO UPDATE
                    SET category = COALESCE(EXCLUDED.category, tags.category),
                        description = COALESCE(EXCLUDED.description, tags.description)
                    RETURNING id
                    """,
                    tag["name"],
                    tag["category"],
                    tag["description"],
                )
                await connection.execute(
                    """
                    INSERT INTO video_analysis_tags(video_analysis_id, tag_id, confidence)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (video_analysis_id, tag_id) DO UPDATE
                    SET confidence = EXCLUDED.confidence
                    """,
                    analysis_id,
                    tag_id,
                    tag["confidence"],
                )
            await connection.execute(
                """
                UPDATE analysis_runs
                SET status = 'succeeded',
                    finished_at = now(),
                    metadata = metadata || $2::jsonb
                WHERE id = $1
                """,
                run_id,
                _json(run_metadata),
            )
            return analysis_id

    async def fail_analysis_run(
        self,
        run_id: UUID,
        error_message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE analysis_runs
            SET status = 'failed',
                finished_at = now(),
                error_message = $2,
                metadata = metadata || $3::jsonb
            WHERE id = $1
            """,
            run_id,
            error_message[:8000],
            _json(metadata or {}),
        )

    async def cancel_analysis_run(
        self,
        run_id: UUID,
        error_message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE analysis_runs
            SET status = 'cancelled',
                finished_at = now(),
                error_message = $2,
                metadata = metadata || $3::jsonb
            WHERE id = $1 AND status = 'running'
            """,
            run_id,
            error_message[:8000],
            _json(metadata or {}),
        )

    async def mark_channel_checked(self, channel_id: UUID) -> None:
        await self._require_pool().execute(
            """
            UPDATE youtube_channels
            SET last_checked_at = now(), updated_at = now()
            WHERE id = $1
            """,
            channel_id,
        )

    async def mark_channel_backfill_completed(self, channel_id: UUID) -> None:
        await self._require_pool().execute(
            """
            UPDATE youtube_channels
            SET initial_backfill_completed_at = COALESCE(
                    initial_backfill_completed_at,
                    now()
                ),
                updated_at = now()
            WHERE id = $1
            """,
            channel_id,
        )

    async def _upsert_channel(
        self,
        connection: asyncpg.Connection,
        channel: ChannelMetadata,
        researcher_id: UUID | None,
        *,
        subscribed: bool | None,
    ) -> UUID:
        return await connection.fetchval(
            """
            INSERT INTO youtube_channels(
                researcher_id,
                youtube_channel_id,
                handle,
                title,
                channel_url,
                is_subscribed,
                subscription_last_seen_at
                , description
                , avatar_url
            )
            VALUES (
                $1, $2, $3, $4, $5, COALESCE($6, false), CASE WHEN $6 THEN now() END,
                $7, $8
            )
            ON CONFLICT (youtube_channel_id) DO UPDATE
            SET researcher_id = COALESCE(EXCLUDED.researcher_id, youtube_channels.researcher_id),
                handle = EXCLUDED.handle,
                title = EXCLUDED.title,
                channel_url = EXCLUDED.channel_url,
                description = COALESCE(EXCLUDED.description, youtube_channels.description),
                avatar_url = COALESCE(EXCLUDED.avatar_url, youtube_channels.avatar_url),
                is_subscribed = CASE
                    WHEN $6 IS NULL THEN youtube_channels.is_subscribed
                    ELSE $6
                END,
                subscription_last_seen_at = CASE
                    WHEN $6 THEN now()
                    ELSE youtube_channels.subscription_last_seen_at
                END,
                updated_at = now()
            RETURNING id
            """,
            researcher_id,
            channel.youtube_channel_id,
            channel.handle,
            channel.title,
            channel.channel_url,
            subscribed,
            channel.description,
            channel.avatar_url,
        )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise DatabaseNotStartedError("Database pool is not connected")
        return self._pool


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_or_none(value: object | None) -> str | None:
    return None if value is None else _json(value)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _channel_record(row: asyncpg.Record) -> ChannelRecord:
    return ChannelRecord(
        id=row["id"],
        youtube_channel_id=row["youtube_channel_id"],
        title=row["title"],
        channel_url=row["channel_url"],
        is_subscribed=row["is_subscribed"],
        initial_backfill_completed_at=row["initial_backfill_completed_at"],
    )
