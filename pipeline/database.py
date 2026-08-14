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
    PublicationStep,
    PublicationStepInput,
    StoredVideo,
    SubtitleDownloadStatus,
    SubtitleDownloadTask,
    TranslationResult,
    VideoMetadata,
    VideoReference,
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
    async def process_lock(
        self,
        lock_name: str = "pipeline_process",
    ) -> AsyncIterator[None]:
        if not isinstance(lock_name, str) or not lock_name.strip():
            raise ValueError("lock_name must be a non-empty string")

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
                    hashtextextended(current_database() || ':' || $1::text, 0)
                )
                """,
                lock_name,
            )
            if acquired is not True:
                raise PipelineAlreadyRunningError(
                    f"Another Pipeline process is already running for {lock_name!r}"
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

    async def list_known_video_ids(
        self,
        channel_ids: Sequence[UUID],
    ) -> dict[UUID, set[str]]:
        if not channel_ids:
            return {}
        if any(not isinstance(channel_id, UUID) for channel_id in channel_ids):
            raise TypeError("channel_ids must contain only UUID values")

        normalized_channel_ids = list(dict.fromkeys(channel_ids))
        rows = await self._require_pool().fetch(
            """
            SELECT channel_id, youtube_video_id
            FROM videos
            WHERE channel_id = ANY($1::uuid[])
            """,
            normalized_channel_ids,
        )
        known_video_ids = {
            channel_id: set() for channel_id in normalized_channel_ids
        }
        for row in rows:
            known_video_ids[row["channel_id"]].add(row["youtube_video_id"])
        return known_video_ids

    async def enqueue_subtitle_downloads(
        self,
        channel_id: UUID,
        references: Sequence[VideoReference],
    ) -> dict[str, SubtitleDownloadStatus]:
        if not isinstance(channel_id, UUID):
            raise TypeError("channel_id must be a UUID")
        if not references:
            return {}

        unique_references: dict[str, tuple[str, str]] = {}
        for reference in references:
            youtube_video_id = reference.youtube_video_id.strip()
            video_url = reference.video_url.strip()
            if not youtube_video_id:
                raise ValueError("youtube_video_id must be a non-empty string")
            if not video_url:
                raise ValueError("video_url must be a non-empty string")
            title = (reference.title or "").strip() or youtube_video_id
            unique_references[youtube_video_id] = (video_url, title)

        youtube_video_ids = list(unique_references)
        video_urls = [unique_references[video_id][0] for video_id in youtube_video_ids]
        titles = [unique_references[video_id][1] for video_id in youtube_video_ids]
        rows = await self._require_pool().fetch(
            """
            INSERT INTO videos(
                channel_id,
                youtube_video_id,
                title,
                video_url,
                subtitle_download_status,
                subtitle_download_error
            )
            SELECT $1, input.youtube_video_id, input.title, input.video_url, 0, NULL
            FROM unnest($2::text[], $3::text[], $4::text[])
                AS input(youtube_video_id, video_url, title)
            ON CONFLICT (youtube_video_id) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                title = CASE
                    WHEN EXCLUDED.title = EXCLUDED.youtube_video_id
                    THEN videos.title
                    ELSE EXCLUDED.title
                END,
                video_url = EXCLUDED.video_url,
                updated_at = now()
            RETURNING youtube_video_id, subtitle_download_status
            """,
            channel_id,
            youtube_video_ids,
            video_urls,
            titles,
        )
        return {
            row["youtube_video_id"]: SubtitleDownloadStatus(
                row["subtitle_download_status"]
            )
            for row in rows
        }

    async def list_subtitle_download_candidates(
        self,
        channel_ids: Sequence[UUID],
        force_video_ids: Sequence[str],
    ) -> list[SubtitleDownloadTask]:
        if not channel_ids:
            return []
        if any(not isinstance(channel_id, UUID) for channel_id in channel_ids):
            raise TypeError("channel_ids must contain only UUID values")
        normalized_force_ids: list[str] = []
        seen_force_ids: set[str] = set()
        for video_id in force_video_ids:
            normalized_video_id = video_id.strip()
            if not normalized_video_id:
                raise ValueError("force_video_ids must contain non-empty strings")
            if normalized_video_id not in seen_force_ids:
                normalized_force_ids.append(normalized_video_id)
                seen_force_ids.add(normalized_video_id)

        normalized_channel_ids = list(dict.fromkeys(channel_ids))
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE videos AS video
                SET subtitle_status = 'pending',
                    subtitle_checked_at = NULL,
                    subtitle_download_status = 0,
                    updated_at = now()
                WHERE video.channel_id = ANY($1::uuid[])
                  AND video.subtitle_download_status = 1
                  AND video.subtitle_status IN ('fetched', 'invalid')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM subtitle_tracks AS subtitle
                      WHERE subtitle.video_id = video.id
                  )
                """,
                normalized_channel_ids,
            )
            rows = await connection.fetch(
                """
                SELECT channel_id, youtube_video_id, video_url, title,
                       subtitle_download_status
                FROM videos
                WHERE channel_id = ANY($1::uuid[])
                  AND (
                      subtitle_download_status IN (0, 2)
                      OR (
                          subtitle_download_status = 1
                          AND youtube_video_id = ANY($2::text[])
                      )
                  )
                ORDER BY subtitle_download_status ASC,
                         subtitle_checked_at ASC NULLS FIRST,
                         created_at ASC,
                         id ASC
                """,
                normalized_channel_ids,
                normalized_force_ids,
            )
        return [
            SubtitleDownloadTask(
                channel_id=row["channel_id"],
                youtube_video_id=row["youtube_video_id"],
                video_url=row["video_url"],
                title=(row["title"] or "").strip() or row["youtube_video_id"],
                status=SubtitleDownloadStatus(row["subtitle_download_status"]),
            )
            for row in rows
        ]

    async def mark_subtitle_download_failed(
        self,
        youtube_video_id: str,
        error_message: str,
    ) -> None:
        youtube_video_id = youtube_video_id.strip()
        error_message = error_message.strip()
        if not youtube_video_id:
            raise ValueError("youtube_video_id must be a non-empty string")
        if not error_message:
            raise ValueError("error_message must be a non-empty string")
        updated_video_id = await self._require_pool().fetchval(
            """
            UPDATE videos
            SET subtitle_download_status = 2,
                subtitle_download_error = $2,
                subtitle_checked_at = now(),
                updated_at = now()
            WHERE youtube_video_id = $1
            RETURNING id
            """,
            youtube_video_id,
            error_message[:8000],
        )
        if updated_video_id is None:
            raise LookupError(f"Unknown YouTube video ID: {youtube_video_id}")

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
                video.subtitle_download_status,
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
        subtitle_download_status = SubtitleDownloadStatus(
            row["subtitle_download_status"]
        )
        if row["subtitle_track_id"] is None and subtitle_status in {"fetched", "invalid"}:
            subtitle_status = "pending"
            subtitle_download_status = SubtitleDownloadStatus.PENDING
        return StoredVideo(
            video_id=row["video_id"],
            subtitle_track_id=row["subtitle_track_id"],
            fetched=FetchedVideo(metadata=metadata, subtitle=subtitle),
            subtitle_status=subtitle_status,
            subtitle_download_status=subtitle_download_status,
            translated_text=row["translated_text"],
            translated_language_code=row["translated_language_code"],
            translation_metadata=_json_object(row["translation_metadata"]),
        )

    async def save_fetched_video(
        self,
        video: VideoMetadata,
        subtitle: DownloadedSubtitle | None,
        translation: TranslationResult | None = None,
    ) -> tuple[UUID, UUID | None]:
        if subtitle is None and translation is not None:
            raise ValueError("translation requires a subtitle")

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
                    subtitle_checked_at,
                    subtitle_download_status,
                    subtitle_download_error
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    CASE WHEN $8 IN ('fetched', 'invalid') THEN now() ELSE NULL END,
                    $8,
                    now(),
                    1,
                    NULL
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
                    subtitle_download_status = 1,
                    subtitle_download_error = NULL,
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
                    normalized_text,
                    translated_text,
                    translated_language_code,
                    translation_metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
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
                    translated_text = COALESCE(
                        subtitle_tracks.translated_text,
                        EXCLUDED.translated_text
                    ),
                    translated_language_code = COALESCE(
                        subtitle_tracks.translated_language_code,
                        EXCLUDED.translated_language_code
                    ),
                    translation_metadata = CASE
                        WHEN subtitle_tracks.translated_text IS NULL
                             AND EXCLUDED.translated_text IS NOT NULL
                        THEN EXCLUDED.translation_metadata
                        ELSE subtitle_tracks.translation_metadata
                    END,
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
                translation.translated_text if translation is not None else None,
                (
                    translation.translated_language_code
                    if translation is not None
                    else None
                ),
                _json(translation.metadata if translation is not None else {}),
            )
            return video_id, subtitle_id

    async def list_analysis_candidates(
        self,
        *,
        profile_name: str,
        schema_version: str,
        prompt_version: str,
        prompt_sha256: str,
        translation_schema_sha256: str,
        schema_sha256: str,
        force: bool,
        limit: int | None,
        publication_target_key: str | None = None,
    ) -> list[VideoReference]:
        rows = await self._require_pool().fetch(
            """
            SELECT video.youtube_video_id, video.video_url
            FROM videos AS video
            LEFT JOIN LATERAL (
                SELECT subtitle.id,
                       subtitle.normalized_text,
                       subtitle.translated_text,
                       subtitle.translated_language_code
                FROM subtitle_tracks AS subtitle
                WHERE subtitle.video_id = video.id
                ORDER BY subtitle.fetched_at DESC,
                         subtitle.created_at DESC,
                         subtitle.id DESC
                LIMIT 1
            ) AS latest_subtitle ON true
            LEFT JOIN LATERAL (
                SELECT analysis.id
                FROM video_analyses AS analysis
                WHERE $8::text IS NOT NULL
                  AND analysis.video_id = video.id
                  AND EXISTS (
                      SELECT 1
                      FROM bbs_publication_steps AS publication
                      WHERE publication.video_analysis_id = analysis.id
                        AND publication.target_key = $8
                        AND publication.status IN (
                            'pending',
                            'claimed',
                            'in_progress',
                            'created',
                            'failed'
                        )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM bbs_publication_steps AS blocked_publication
                      WHERE blocked_publication.video_analysis_id = analysis.id
                        AND blocked_publication.target_key = $8
                        AND blocked_publication.status = 'uncertain'
                  )
                ORDER BY analysis.analyzed_at ASC,
                         analysis.created_at ASC,
                         analysis.id ASC
                LIMIT 1
            ) AS pending_publication ON true
            WHERE (
               pending_publication.id IS NOT NULL
               OR (
                  latest_subtitle.normalized_text IS NOT NULL
                  AND (
                    $7::boolean
                    OR NOT EXISTS (
                      SELECT 1
                      FROM video_analyses AS analysis
                      JOIN analysis_runs AS run
                        ON run.id = analysis.analysis_run_id
                      WHERE analysis.video_id = video.id
                        AND analysis.subtitle_track_id = latest_subtitle.id
                        AND analysis.profile_name = $1
                        AND analysis.output_schema_version = $2
                        AND run.status = 'succeeded'
                        AND run.prompt_version = $3
                        AND run.metadata ->> 'prompt_sha256' = $4
                        AND run.metadata ->> 'translation_schema_sha256' = $5
                        AND run.metadata ->> 'schema_sha256' = $6
                        AND run.metadata ->> 'source_sha256' = encode(
                            digest(latest_subtitle.normalized_text, 'sha256'),
                            'hex'
                        )
                        AND latest_subtitle.translated_text IS NOT NULL
                        AND latest_subtitle.translated_language_code IS NOT NULL
                    )
                  )
               )
            )
              AND NOT EXISTS (
                  SELECT 1
                  FROM video_analyses AS blocked_analysis
                  JOIN bbs_publication_steps AS blocked_publication
                    ON blocked_publication.video_analysis_id = blocked_analysis.id
                  WHERE blocked_analysis.video_id = video.id
                    AND blocked_publication.target_key = $8
                    AND blocked_publication.status = 'uncertain'
              )
            ORDER BY (pending_publication.id IS NULL) ASC,
                     video.created_at ASC,
                     video.id ASC
            LIMIT $9::integer
            """,
            profile_name,
            schema_version,
            prompt_version,
            prompt_sha256,
            translation_schema_sha256,
            schema_sha256,
            force,
            publication_target_key,
            limit,
        )
        return [
            VideoReference(
                youtube_video_id=row["youtube_video_id"],
                video_url=row["video_url"],
            )
            for row in rows
        ]

    async def get_pending_publication_analysis_id(
        self,
        video_id: UUID,
        target_key: str,
    ) -> UUID | None:
        return await self._require_pool().fetchval(
            """
            SELECT analysis.id
            FROM video_analyses AS analysis
            WHERE analysis.video_id = $1
              AND EXISTS (
                  SELECT 1
                  FROM bbs_publication_steps AS publication
                  WHERE publication.video_analysis_id = analysis.id
                    AND publication.target_key = $2
                    AND publication.status IN (
                        'pending',
                        'claimed',
                        'in_progress',
                        'created',
                        'failed',
                        'uncertain'
                    )
              )
            ORDER BY CASE WHEN EXISTS (
                         SELECT 1
                         FROM bbs_publication_steps AS blocked_publication
                         WHERE blocked_publication.video_analysis_id = analysis.id
                           AND blocked_publication.target_key = $2
                           AND blocked_publication.status = 'uncertain'
                     ) THEN 0 ELSE 1 END,
                     analysis.analyzed_at ASC,
                     analysis.created_at ASC,
                     analysis.id ASC
            LIMIT 1
            """,
            video_id,
            target_key,
        )

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
        return (
            await self.get_matching_analysis_id(
                video_id,
                subtitle_track_id,
                profile_name=profile_name,
                schema_version=schema_version,
                prompt_version=prompt_version,
                prompt_sha256=prompt_sha256,
                translation_schema_sha256=translation_schema_sha256,
                schema_sha256=schema_sha256,
                source_sha256=source_sha256,
            )
            is not None
        )

    async def get_matching_analysis_id(
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
        publication_target_key: str | None = None,
    ) -> UUID | None:
        return await self._require_pool().fetchval(
            """
            SELECT analysis.id
            FROM video_analyses AS analysis
            JOIN analysis_runs AS run ON run.id = analysis.analysis_run_id
            JOIN subtitle_tracks AS subtitle
              ON subtitle.id = analysis.subtitle_track_id
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
              AND subtitle.translated_text IS NOT NULL
              AND subtitle.translated_language_code IS NOT NULL
            ORDER BY CASE
                       WHEN $10::text IS NOT NULL AND EXISTS (
                           SELECT 1
                           FROM bbs_publication_steps AS publication
                           WHERE publication.video_analysis_id = analysis.id
                             AND publication.target_key = $10
                             AND publication.status NOT IN ('succeeded', 'skipped')
                       ) THEN 0
                       ELSE 1
                     END,
                     analysis.analyzed_at DESC,
                     analysis.created_at DESC,
                     analysis.id DESC
            LIMIT 1
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
            publication_target_key,
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
        publication_steps: Sequence[PublicationStepInput] = (),
    ) -> UUID:
        normalized_publication_steps = _validate_publication_steps(publication_steps)
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
            if normalized_publication_steps:
                await connection.executemany(
                    """
                    INSERT INTO bbs_publication_steps(
                        video_analysis_id,
                        target_key,
                        step,
                        topic_title,
                        markdown_snapshot,
                        status,
                        completed_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        CASE WHEN $6 THEN 'skipped' ELSE 'pending' END,
                        CASE WHEN $6 THEN now() ELSE NULL END
                    )
                    ON CONFLICT (video_analysis_id, target_key, step) DO NOTHING
                    """,
                    [
                        (
                            analysis_id,
                            step.target_key,
                            step.step,
                            step.topic_title,
                            step.markdown_snapshot,
                            step.skipped,
                        )
                        for step in normalized_publication_steps
                    ],
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

    async def list_publication_steps(
        self,
        video_analysis_id: UUID,
        target_key: str,
    ) -> list[PublicationStep]:
        rows = await self._require_pool().fetch(
            """
            SELECT video_analysis_id, target_key, step, topic_title,
                   markdown_snapshot, content_sha256, status,
                   remote_topic_id, remote_comment_id, remote_status,
                   attempt_count, error_message, request_metadata,
                   response_metadata, started_at, completed_at
            FROM bbs_publication_steps
            WHERE video_analysis_id = $1 AND target_key = $2
            ORDER BY CASE step
                WHEN 'topic' THEN 1
                WHEN 'translation' THEN 2
                WHEN 'source' THEN 3
                ELSE 4
            END
            """,
            video_analysis_id,
            target_key,
        )
        return [
            PublicationStep(
                video_analysis_id=row["video_analysis_id"],
                target_key=row["target_key"],
                step=row["step"],
                topic_title=row["topic_title"],
                markdown_snapshot=row["markdown_snapshot"],
                content_sha256=row["content_sha256"],
                status=row["status"],
                remote_topic_id=row["remote_topic_id"],
                remote_comment_id=row["remote_comment_id"],
                remote_status=row["remote_status"],
                attempt_count=row["attempt_count"],
                error_message=row["error_message"],
                request_metadata=_json_object(row["request_metadata"]),
                response_metadata=_json_object(row["response_metadata"]),
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]

    async def claim_publication_step(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> bool:
        normalized_metadata = _validate_publication_request_metadata(request_metadata)
        claimed = await self._require_pool().fetchval(
            """
            UPDATE bbs_publication_steps
            SET status = 'claimed',
                attempt_count = CASE
                    WHEN status = 'claimed' THEN attempt_count
                    ELSE attempt_count + 1
                END,
                error_message = NULL,
                remote_topic_id = NULL,
                remote_comment_id = NULL,
                remote_status = NULL,
                request_metadata = request_metadata || $4::jsonb,
                started_at = NULL,
                completed_at = NULL,
                updated_at = now()
            WHERE video_analysis_id = $1
              AND target_key = $2
              AND step = $3
              AND status IN ('pending', 'failed', 'claimed')
              AND (
                  NOT (request_metadata ? 'portal_target')
                  OR request_metadata -> 'portal_target'
                     = ($4::jsonb) -> 'portal_target'
              )
            RETURNING true
            """,
            video_analysis_id,
            target_key,
            step,
            _json(normalized_metadata),
        )
        return bool(claimed)

    async def mark_publication_step_in_progress(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
    ) -> bool:
        started = await self._require_pool().fetchval(
            """
            UPDATE bbs_publication_steps
            SET status = 'in_progress',
                started_at = now(),
                updated_at = now()
            WHERE video_analysis_id = $1
              AND target_key = $2
              AND step = $3
              AND status = 'claimed'
            RETURNING true
            """,
            video_analysis_id,
            target_key,
            step,
        )
        return bool(started)

    async def mark_publication_step_created(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        *,
        remote_topic_id: str | None,
        remote_comment_id: int | None,
        remote_status: int | None,
        response_metadata: dict[str, Any],
    ) -> None:
        if step == "topic":
            if not remote_topic_id or remote_comment_id is not None:
                raise ValueError("topic creation requires only a remote topic ID")
        elif step in {"translation", "source"}:
            if remote_topic_id is not None or not isinstance(remote_comment_id, int):
                raise ValueError("comment creation requires only a remote comment ID")
        else:
            raise ValueError(f"Unsupported publication step: {step}")
        updated = await self._require_pool().fetchval(
            """
            UPDATE bbs_publication_steps
            SET status = 'created',
                remote_topic_id = $4,
                remote_comment_id = $5,
                remote_status = $6,
                response_metadata = response_metadata || $7::jsonb,
                updated_at = now()
            WHERE video_analysis_id = $1
              AND target_key = $2
              AND step = $3
              AND status = 'in_progress'
            RETURNING true
            """,
            video_analysis_id,
            target_key,
            step,
            remote_topic_id,
            remote_comment_id,
            remote_status,
            _json(response_metadata),
        )
        if not updated:
            raise RuntimeError(f"Publication step is not in progress: {step}")

    async def mark_publication_step_succeeded(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        *,
        remote_status: int | None,
        response_metadata: dict[str, Any],
    ) -> None:
        updated = await self._require_pool().fetchval(
            """
            UPDATE bbs_publication_steps
            SET status = 'succeeded',
                remote_status = COALESCE($4, remote_status),
                error_message = NULL,
                response_metadata = response_metadata || $5::jsonb,
                completed_at = now(),
                updated_at = now()
            WHERE video_analysis_id = $1
              AND target_key = $2
              AND step = $3
              AND status = 'created'
            RETURNING true
            """,
            video_analysis_id,
            target_key,
            step,
            remote_status,
            _json(response_metadata),
        )
        if not updated:
            raise RuntimeError(f"Publication step has not recorded a remote object: {step}")

    async def mark_publication_step_failed(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        error_message: str,
        *,
        uncertain: bool,
    ) -> None:
        message = error_message.strip()
        if not message:
            raise ValueError("Publication failure requires a non-empty error message")
        updated = await self._require_pool().fetchval(
            """
            UPDATE bbs_publication_steps
            SET status = CASE WHEN $5 THEN 'uncertain' ELSE 'failed' END,
                error_message = $4,
                completed_at = now(),
                updated_at = now()
            WHERE video_analysis_id = $1
              AND target_key = $2
              AND step = $3
              AND status = 'in_progress'
            RETURNING true
            """,
            video_analysis_id,
            target_key,
            step,
            message[:8000],
            uncertain,
        )
        if not updated:
            raise RuntimeError(f"Publication step is not in progress: {step}")

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


def _validate_publication_steps(
    steps: Sequence[PublicationStepInput],
) -> tuple[PublicationStepInput, ...]:
    normalized = tuple(steps)
    if not normalized:
        return ()
    if any(not isinstance(step, PublicationStepInput) for step in normalized):
        raise TypeError("publication_steps must contain PublicationStepInput values")
    if {step.step for step in normalized} != {"topic", "translation", "source"}:
        raise ValueError("publication_steps must contain topic, translation, and source")
    if len(normalized) != 3:
        raise ValueError("publication_steps must contain each step exactly once")

    target_keys = {step.target_key for step in normalized}
    if len(target_keys) != 1 or any(
        not step.target_key.strip() or step.target_key != step.target_key.strip()
        for step in normalized
    ):
        raise ValueError("publication_steps must use one non-empty target_key")

    for step in normalized:
        if step.step == "topic":
            if (
                step.topic_title is None
                or not step.topic_title.strip()
                or len(step.topic_title) > 128
            ):
                raise ValueError("topic publication requires a title of at most 128 characters")
        elif step.topic_title is not None:
            raise ValueError("comment publication steps must not contain a topic title")

        if step.skipped:
            if step.step != "source" or step.markdown_snapshot is not None:
                raise ValueError("only an empty source publication step may be skipped")
        elif (
            step.markdown_snapshot is None
            or not step.markdown_snapshot.strip()
        ):
            raise ValueError("non-skipped publication steps require Markdown content")
    return normalized


def _validate_publication_request_metadata(
    request_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(request_metadata, dict):
        raise ValueError("publication claims require request metadata")
    portal_target = request_metadata.get("portal_target")
    if not isinstance(portal_target, dict) or set(portal_target) != {
        "origin",
        "user_id",
        "category_id",
        "category_name",
        "username",
    }:
        raise ValueError("publication claims require a complete portal_target")

    for field in ("origin", "user_id", "category_name", "username"):
        value = portal_target[field]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"portal_target.{field} must be a non-empty trimmed string")
    category_id = portal_target["category_id"]
    if isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0:
        raise ValueError("portal_target.category_id must be a positive integer")
    return request_metadata


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
