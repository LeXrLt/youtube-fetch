from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

from config import DatabaseSettings
from database import PipelineAlreadyRunningError, PipelineRepository
from models import (
    AgentInvocation,
    AnalysisOutcome,
    AnalysisProjection,
    ChannelMetadata,
    DownloadedSubtitle,
    PublicationCandidate,
    PublicationStepInput,
    SubtitleDownloadStatus,
    TranslationResult,
    VideoMetadata,
    VideoReference,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _run_migrations(environment: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        str(PROJECT_ROOT / "db" / "migrate.sh"),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (
        f"db/migrate.sh exited {process.returncode}\n"
        f"stdout:\n{stdout.decode()}\nstderr:\n{stderr.decode()}"
    )


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[PipelineRepository]:
    dotenv = {
        key: value
        for key, value in dotenv_values(PROJECT_ROOT / ".env").items()
        if value is not None
    }
    source_database = dotenv["POSTGRES_DB"]
    database_name = f"youtube_fetch_pytest_{secrets.token_hex(12)}"
    assert database_name != source_database
    connection_options = {
        "host": dotenv.get("POSTGRES_HOST", "localhost"),
        "port": int(dotenv.get("POSTGRES_PORT", "5432")),
        "user": dotenv["POSTGRES_USER"],
        "password": dotenv["POSTGRES_PASSWORD"],
    }
    admin = await asyncpg.connect(database="postgres", **connection_options)
    repo: PipelineRepository | None = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        created = True
        migration_environment = {
            **os.environ,
            "ENV_FILE": os.devnull,
            "POSTGRES_HOST": str(connection_options["host"]),
            "POSTGRES_PORT": str(connection_options["port"]),
            "POSTGRES_USER": str(connection_options["user"]),
            "POSTGRES_PASSWORD": str(connection_options["password"]),
            "POSTGRES_DB": database_name,
        }
        await _run_migrations(migration_environment)
        repo = PipelineRepository(
            DatabaseSettings(
                **connection_options,
                database=database_name,
                min_pool_size=1,
                max_pool_size=2,
            )
        )
        await repo.connect()
        yield repo
    finally:
        try:
            if repo is not None:
                await repo.close()
        finally:
            try:
                if created:
                    try:
                        await admin.execute(
                            """
                            SELECT pg_terminate_backend(pid)
                            FROM pg_stat_activity
                            WHERE datname = $1 AND pid <> pg_backend_pid()
                            """,
                            database_name,
                        )
                    finally:
                        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
            finally:
                await admin.close()


@pytest.mark.asyncio
async def test_repository_process_lock_is_exclusive_and_released(
    repository: PipelineRepository,
) -> None:
    single_connection_repository = PipelineRepository(
        repository._settings.model_copy(
            update={"min_pool_size": 1, "max_pool_size": 1}
        )
    )
    await single_connection_repository.connect()
    try:
        async with single_connection_repository.process_lock("download"):
            assert (
                await single_connection_repository._require_pool().fetchval("SELECT 1")
                == 1
            )
            with pytest.raises(PipelineAlreadyRunningError, match="already running"):
                async with repository.process_lock("download"):
                    pytest.fail("A concurrent process lock must not be acquired")
            async with repository.process_lock("analysis"):
                assert await repository._require_pool().fetchval("SELECT 1") == 1

        with pytest.raises(RuntimeError, match="lock body failed"):
            async with single_connection_repository.process_lock("download"):
                raise RuntimeError("lock body failed")

        async with single_connection_repository.process_lock("download"):
            assert (
                await single_connection_repository._require_pool().fetchval("SELECT 1")
                == 1
            )
        with pytest.raises(ValueError, match="non-empty"):
            async with single_connection_repository.process_lock("  "):
                pytest.fail("A blank process lock name must be rejected")
    finally:
        await single_connection_repository.close()


@pytest.mark.asyncio
async def test_repository_process_lock_is_released_on_cancellation(
    repository: PipelineRepository,
) -> None:
    acquired = asyncio.Event()
    blocker = asyncio.Event()

    async def hold_lock() -> None:
        async with repository.process_lock("analysis"):
            acquired.set()
            await blocker.wait()

    holder = asyncio.create_task(hold_lock())
    try:
        await acquired.wait()
        with pytest.raises(PipelineAlreadyRunningError, match="already running"):
            async with repository.process_lock("analysis"):
                pytest.fail("A concurrent process lock must not be acquired")
    finally:
        holder.cancel()
        with suppress(asyncio.CancelledError):
            await holder

    async with repository.process_lock("analysis"):
        assert await repository._require_pool().fetchval("SELECT 1") == 1


@pytest.mark.asyncio
async def test_subtitle_download_status_migration_maps_legacy_results(
    repository: PipelineRepository,
) -> None:
    pool = repository._require_pool()
    await pool.execute(
        """
        DROP INDEX idx_videos_subtitle_download_queue;
        ALTER TABLE videos
          DROP COLUMN subtitle_download_status,
          DROP COLUMN subtitle_download_error;

        INSERT INTO youtube_channels(
            youtube_channel_id, title, channel_url
        ) VALUES (
            'migration-status-channel',
            'Migration Status Channel',
            'https://www.youtube.com/@migration-status-channel'
        );

        INSERT INTO videos(
            channel_id, youtube_video_id, title, video_url, subtitle_status
        )
        SELECT channel.id, status.value, status.value,
               'https://www.youtube.com/watch?v=' || status.value,
               status.value
        FROM youtube_channels AS channel
        CROSS JOIN (
            VALUES ('pending'), ('fetched'), ('unavailable'), ('invalid')
        ) AS status(value)
        WHERE channel.youtube_channel_id = 'migration-status-channel';
        """
    )
    migration_sql = (
        PROJECT_ROOT / "db" / "migrations" / "011_subtitle_download_status.sql"
    ).read_text(encoding="utf-8")
    await pool.execute(migration_sql)

    rows = await pool.fetch(
        """
        SELECT subtitle_status, subtitle_download_status, subtitle_download_error
        FROM videos
        ORDER BY subtitle_status
        """
    )
    assert {
        row["subtitle_status"]: (
            row["subtitle_download_status"],
            row["subtitle_download_error"],
        )
        for row in rows
    } == {
        "fetched": (1, None),
        "invalid": (1, None),
        "pending": (0, None),
        "unavailable": (1, None),
    }

    video_id = await pool.fetchval("SELECT id FROM videos LIMIT 1")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "UPDATE videos SET subtitle_download_status = 3 WHERE id = $1",
            video_id,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            """
            UPDATE videos
            SET subtitle_download_status = 2, subtitle_download_error = NULL
            WHERE id = $1
            """,
            video_id,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "UPDATE videos SET subtitle_download_error = 'stale' WHERE id = $1",
            video_id,
        )


@pytest.mark.asyncio
async def test_repository_persists_and_orders_subtitle_download_queue(
    repository: PipelineRepository,
) -> None:
    first_channel = ChannelMetadata(
        youtube_channel_id="download-queue-one",
        title="Download Queue One",
        channel_url="https://www.youtube.com/@download-queue-one",
    )
    second_channel = ChannelMetadata(
        youtube_channel_id="download-queue-two",
        title="Download Queue Two",
        channel_url="https://www.youtube.com/@download-queue-two",
    )
    first_channel_id = await repository.register_channel(first_channel, None)
    second_channel_id = await repository.register_channel(second_channel, None)
    references = [
        VideoReference(
            "queue-failed-old",
            "https://www.youtube.com/watch?v=queue-failed-old",
            "Old failure title",
            datetime(2026, 8, 1, tzinfo=UTC),
        ),
        VideoReference(
            "queue-failed-new",
            "https://www.youtube.com/watch?v=queue-failed-new",
            "New failure title",
            datetime(2026, 8, 4, tzinfo=UTC),
        ),
        VideoReference(
            "queue-complete",
            "https://www.youtube.com/watch?v=queue-complete",
            "Complete title",
            datetime(2026, 8, 2, tzinfo=UTC),
        ),
    ]
    assert await repository.enqueue_subtitle_downloads(
        first_channel_id, references
    ) == {
        "queue-failed-old": SubtitleDownloadStatus.PENDING,
        "queue-failed-new": SubtitleDownloadStatus.PENDING,
        "queue-complete": SubtitleDownloadStatus.PENDING,
    }
    assert await repository.enqueue_subtitle_downloads(
        second_channel_id,
        [
            VideoReference(
                "queue-pending",
                "https://www.youtube.com/watch?v=queue-pending",
                "Pending title",
                datetime(2026, 8, 3, tzinfo=UTC),
            )
        ],
    ) == {"queue-pending": SubtitleDownloadStatus.PENDING}

    complete_video = VideoMetadata(
        youtube_video_id="queue-complete",
        channel=first_channel,
        title="Fetched complete title",
        video_url="https://www.youtube.com/watch?v=queue-complete",
    )
    await repository.save_fetched_video(complete_video, None)
    await repository.mark_subtitle_download_failed("queue-failed-old", "old failure")
    await repository.mark_subtitle_download_failed("queue-failed-new", "new failure")
    pool = repository._require_pool()
    await pool.execute(
        """
        UPDATE videos
        SET subtitle_checked_at = CASE youtube_video_id
            WHEN 'queue-failed-old' THEN '2026-08-01 00:00:00+00'::timestamptz
            WHEN 'queue-failed-new' THEN '2026-08-02 00:00:00+00'::timestamptz
        END
        WHERE youtube_video_id IN ('queue-failed-old', 'queue-failed-new')
        """
    )

    assert await repository.list_known_video_ids(
        [second_channel_id, first_channel_id, first_channel_id]
    ) == {
        second_channel_id: {"queue-pending"},
        first_channel_id: {
            "queue-failed-old",
            "queue-failed-new",
            "queue-complete",
        },
    }
    assert await repository.list_known_video_ids([]) == {}
    with pytest.raises(TypeError, match="UUID"):
        await repository.list_known_video_ids(["not-a-uuid"])  # type: ignore[list-item]

    repeated = await repository.enqueue_subtitle_downloads(
        first_channel_id,
        [
            VideoReference(
                "queue-failed-old",
                "https://www.youtube.com/watch?v=queue-failed-old",
            ),
            VideoReference(
                "queue-complete",
                "https://www.youtube.com/watch?v=queue-complete",
                "Updated flat title",
            ),
        ],
    )
    assert repeated == {
        "queue-failed-old": SubtitleDownloadStatus.FAILED,
        "queue-complete": SubtitleDownloadStatus.DOWNLOADED,
    }
    persisted = await pool.fetchrow(
        """
        SELECT title, subtitle_status, subtitle_download_status,
               subtitle_download_error
        FROM videos WHERE youtube_video_id = 'queue-failed-old'
        """
    )
    assert persisted is not None
    assert persisted["title"] == "Old failure title"
    assert persisted["subtitle_status"] == "pending"
    assert persisted["subtitle_download_status"] == 2
    assert persisted["subtitle_download_error"] == "old failure"

    candidates = await repository.list_subtitle_download_candidates(
        [second_channel_id, first_channel_id],
        ["queue-complete"],
    )
    assert [task.youtube_video_id for task in candidates] == [
        "queue-failed-new",
        "queue-pending",
        "queue-complete",
        "queue-failed-old",
    ]
    assert candidates[0].status is SubtitleDownloadStatus.FAILED
    assert candidates[-1].status is SubtitleDownloadStatus.FAILED
    assert {task.youtube_video_id: task.title for task in candidates} == {
        "queue-pending": "Pending title",
        "queue-complete": "Updated flat title",
        "queue-failed-old": "Old failure title",
        "queue-failed-new": "New failure title",
    }
    assert await repository.list_subtitle_download_candidates([], []) == []

    await repository.save_fetched_video(
        VideoMetadata(
            youtube_video_id="queue-failed-old",
            channel=first_channel,
            title="Recovered title",
            video_url="https://www.youtube.com/watch?v=queue-failed-old",
        ),
        None,
    )
    recovered = await pool.fetchrow(
        """
        SELECT subtitle_status, subtitle_download_status, subtitle_download_error
        FROM videos WHERE youtube_video_id = 'queue-failed-old'
        """
    )
    assert recovered is not None
    assert recovered["subtitle_status"] == "unavailable"
    assert recovered["subtitle_download_status"] == 1
    assert recovered["subtitle_download_error"] is None

    with pytest.raises(ValueError, match="error_message"):
        await repository.mark_subtitle_download_failed("queue-pending", "  ")
    with pytest.raises(LookupError, match="Unknown"):
        await repository.mark_subtitle_download_failed("missing-video", "failed")


@pytest.mark.asyncio
async def test_repository_lists_stable_analysis_candidate_snapshot(
    repository: PipelineRepository,
) -> None:
    identity = {
        "profile_name": "research-v1",
        "schema_version": "2026-08-04",
        "prompt_version": "translation:t1;analysis:a1",
        "prompt_sha256": "prompt-sha",
        "translation_schema_sha256": "translation-schema-sha",
        "schema_sha256": "schema-sha",
    }
    channel = ChannelMetadata(
        youtube_channel_id="candidate-channel",
        title="Candidate Channel",
        channel_url="https://www.youtube.com/@candidate-channel",
    )

    async def save_video(
        youtube_video_id: str,
        normalized_text: str | None,
        translation: TranslationResult | None = None,
    ) -> tuple[UUID, UUID]:
        video_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        video = VideoMetadata(
            youtube_video_id=youtube_video_id,
            channel=channel,
            title=youtube_video_id,
            video_url=video_url,
        )
        subtitle = DownloadedSubtitle(
            language_code="en",
            language_name="English",
            source_format="vtt",
            is_auto_generated=False,
            raw_text=f"WEBVTT\n\n{youtube_video_id}:{normalized_text}",
            normalized_text=normalized_text,
        )
        video_id, subtitle_id = await repository.save_fetched_video(
            video,
            subtitle,
            translation,
        )
        assert subtitle_id is not None
        return video_id, subtitle_id

    pending_ids = await save_video("candidate-pending", "待分析字幕")
    matched_ids = await save_video(
        "candidate-matched",
        "Already analyzed",
        TranslationResult(
            translated_text="已分析",
            translated_language_code="zh-Hans",
            metadata={"mode": "test"},
        ),
    )
    recoverable_ids = await save_video(
        "candidate-missing-translation",
        "Analysis succeeded before translation was persisted",
    )
    failed_ids = await save_video("candidate-failed", "Failed analysis")
    running_ids = await save_video("candidate-running", "Running analysis")
    cancelled_ids = await save_video("candidate-cancelled", "Cancelled analysis")
    invalid_ids = await save_video("candidate-invalid", "Older valid subtitle")
    _, latest_invalid_subtitle_id = await save_video("candidate-invalid", None)

    pool = repository._require_pool()
    await pool.execute(
        """
        UPDATE subtitle_tracks
        SET fetched_at = CASE
                WHEN id = $1 THEN '2026-08-01 00:00:00+00'::timestamptz
                WHEN id = $2 THEN '2026-08-02 00:00:00+00'::timestamptz
                ELSE fetched_at
            END,
            created_at = CASE
                WHEN id = $1 THEN '2026-08-01 00:00:00+00'::timestamptz
                WHEN id = $2 THEN '2026-08-02 00:00:00+00'::timestamptz
                ELSE created_at
            END
        WHERE id = ANY($3::uuid[])
        """,
        invalid_ids[1],
        latest_invalid_subtitle_id,
        [invalid_ids[1], latest_invalid_subtitle_id],
    )

    async def add_analysis(
        video_and_subtitle_ids: tuple[UUID, UUID],
        status: str,
    ) -> None:
        video_id, subtitle_id = video_and_subtitle_ids
        normalized_text = await pool.fetchval(
            "SELECT normalized_text FROM subtitle_tracks WHERE id = $1",
            subtitle_id,
        )
        metadata = {
            "prompt_sha256": identity["prompt_sha256"],
            "translation_schema_sha256": identity["translation_schema_sha256"],
            "schema_sha256": identity["schema_sha256"],
            "source_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
        }
        run_id = await repository.start_analysis_run(
            video_id,
            subtitle_id,
            agent_model=None,
            prompt_version=identity["prompt_version"],
            metadata=metadata,
        )
        await pool.execute(
            """
            INSERT INTO video_analyses(
                video_id,
                subtitle_track_id,
                analysis_run_id,
                profile_name,
                output_schema_version,
                translated_subtitle_snapshot
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            video_id,
            subtitle_id,
            run_id,
            identity["profile_name"],
            identity["schema_version"],
            normalized_text,
        )
        if status != "running":
            await pool.execute(
                "UPDATE analysis_runs SET status = $2 WHERE id = $1",
                run_id,
                status,
            )

    await add_analysis(matched_ids, "succeeded")
    await add_analysis(recoverable_ids, "succeeded")
    await add_analysis(failed_ids, "failed")
    await add_analysis(running_ids, "running")
    await add_analysis(cancelled_ids, "cancelled")

    all_video_ids = [
        pending_ids[0],
        matched_ids[0],
        recoverable_ids[0],
        failed_ids[0],
        running_ids[0],
        cancelled_ids[0],
        invalid_ids[0],
    ]
    await pool.execute(
        """
        UPDATE videos
        SET created_at = '2026-08-03 00:00:00+00'::timestamptz,
            published_at = CASE id
                WHEN $1 THEN '2026-08-01 00:00:00+00'::timestamptz
                WHEN $2 THEN '2026-08-05 00:00:00+00'::timestamptz
                WHEN $3 THEN '2026-08-03 00:00:00+00'::timestamptz
                WHEN $4 THEN '2026-08-02 00:00:00+00'::timestamptz
                WHEN $5 THEN '2026-08-04 00:00:00+00'::timestamptz
                ELSE NULL
            END
        WHERE id = ANY($6::uuid[])
        """,
        pending_ids[0],
        recoverable_ids[0],
        failed_ids[0],
        running_ids[0],
        cancelled_ids[0],
        all_video_ids,
    )
    expected_ids = [
        "candidate-missing-translation",
        "candidate-cancelled",
        "candidate-failed",
        "candidate-running",
        "candidate-pending",
    ]

    candidates = await repository.list_analysis_candidates(
        **identity,
        force=False,
        limit=None,
    )
    assert [candidate.youtube_video_id for candidate in candidates] == expected_ids
    assert "candidate-missing-translation" in expected_ids
    assert "candidate-matched" not in expected_ids
    assert candidates == [
        VideoReference(
            youtube_video_id=youtube_video_id,
            video_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
        )
        for youtube_video_id in expected_ids
    ]

    limited = await repository.list_analysis_candidates(
        **identity,
        force=False,
        limit=2,
    )
    assert limited == candidates[:2]

    for changed_identity in (
        identity | {"profile_name": "research-v2"},
        identity | {"schema_version": "2026-08-05"},
        identity | {"prompt_version": "translation:t1;analysis:a2"},
        identity | {"prompt_sha256": "different-prompt-sha"},
        identity | {"translation_schema_sha256": "different-translation-schema-sha"},
        identity | {"schema_sha256": "different-schema-sha"},
    ):
        changed_candidates = await repository.list_analysis_candidates(
            **changed_identity,
            force=False,
            limit=None,
        )
        assert "candidate-matched" in {
            candidate.youtube_video_id for candidate in changed_candidates
        }
        assert "candidate-invalid" not in {
            candidate.youtube_video_id for candidate in changed_candidates
        }

    matched_run_id = await pool.fetchval(
        "SELECT analysis_run_id FROM video_analyses WHERE video_id = $1",
        matched_ids[0],
    )
    await pool.execute(
        """
        UPDATE analysis_runs
        SET metadata = jsonb_set(metadata, '{source_sha256}', '"stale-source-sha"')
        WHERE id = $1
        """,
        matched_run_id,
    )
    source_changed_candidates = await repository.list_analysis_candidates(
        **identity,
        force=False,
        limit=None,
    )
    assert "candidate-matched" in {
        candidate.youtube_video_id for candidate in source_changed_candidates
    }

    forced_candidates = await repository.list_analysis_candidates(
        **identity,
        force=True,
        limit=None,
    )
    assert "candidate-matched" in {
        candidate.youtube_video_id for candidate in forced_candidates
    }
    assert "candidate-invalid" not in {
        candidate.youtube_video_id for candidate in forced_candidates
    }


@pytest.mark.asyncio
async def test_save_fetched_video_only_fills_missing_translation(
    repository: PipelineRepository,
) -> None:
    video = VideoMetadata(
        youtube_video_id="atomic-translation-video",
        channel=ChannelMetadata(
            youtube_channel_id="atomic-translation-channel",
            title="Atomic Translation Channel",
            channel_url="https://www.youtube.com/@atomic-translation-channel",
        ),
        title="Atomic Translation Video",
        video_url="https://www.youtube.com/watch?v=atomic-translation-video",
    )
    subtitle = DownloadedSubtitle(
        language_code="zh-Hant",
        language_name="Chinese (Traditional)",
        source_format="vtt",
        is_auto_generated=False,
        raw_text="WEBVTT\n\n中文字幕原文",
        normalized_text="中文字幕原文",
    )
    first_translation = TranslationResult(
        translated_text=subtitle.normalized_text,
        translated_language_code=subtitle.language_code,
        metadata={"mode": "copied_chinese_source", "sequence": 1},
    )
    replacement_translation = TranslationResult(
        translated_text="不应覆盖的翻译",
        translated_language_code="zh-Hant",
        metadata={"mode": "replacement", "sequence": 2},
    )

    video_id, translated_subtitle_id = await repository.save_fetched_video(
        video,
        subtitle,
        first_translation,
    )
    assert translated_subtitle_id is not None
    assert await repository.save_fetched_video(
        video,
        subtitle,
        None,
    ) == (video_id, translated_subtitle_id)
    assert await repository.save_fetched_video(
        video,
        subtitle,
        replacement_translation,
    ) == (video_id, translated_subtitle_id)

    pool = repository._require_pool()
    persisted_first = await pool.fetchrow(
        """
        SELECT translated_text, translated_language_code,
               translation_metadata::text AS translation_metadata
        FROM subtitle_tracks
        WHERE id = $1
        """,
        translated_subtitle_id,
    )
    assert persisted_first is not None
    assert persisted_first["translated_text"] == first_translation.translated_text
    assert persisted_first["translated_language_code"] == subtitle.language_code
    assert json.loads(persisted_first["translation_metadata"]) == (
        first_translation.metadata
    )

    await pool.execute(
        """
        UPDATE subtitle_tracks
        SET translated_text = NULL,
            translated_language_code = NULL,
            translation_metadata = '{}'::jsonb
        WHERE id = $1
        """,
        translated_subtitle_id,
    )
    assert await repository.save_fetched_video(
        video,
        subtitle,
        first_translation,
    ) == (video_id, translated_subtitle_id)
    persisted_fill = await pool.fetchrow(
        """
        SELECT translated_text, translated_language_code,
               translation_metadata::text AS translation_metadata
        FROM subtitle_tracks
        WHERE id = $1
        """,
        translated_subtitle_id,
    )
    assert persisted_fill is not None
    assert persisted_fill["translated_text"] == first_translation.translated_text
    assert persisted_fill["translated_language_code"] == subtitle.language_code
    assert json.loads(persisted_fill["translation_metadata"]) == first_translation.metadata

    missing_video = replace(
        video,
        youtube_video_id="translation-without-subtitle",
        video_url="https://www.youtube.com/watch?v=translation-without-subtitle",
    )
    with pytest.raises(ValueError, match="requires a subtitle"):
        await repository.save_fetched_video(
            missing_video,
            None,
            first_translation,
        )
    assert await repository.get_stored_video(missing_video.youtube_video_id) is None


@pytest.mark.asyncio
async def test_repository_publication_queue_is_fifo_per_video_and_requeues_uncertain(
    repository: PipelineRepository,
) -> None:
    channel = ChannelMetadata(
        youtube_channel_id="publication-queue-channel",
        title="Publication Queue Channel",
        channel_url="https://www.youtube.com/@publication-queue-channel",
    )
    subtitle = DownloadedSubtitle(
        language_code="en",
        language_name="English",
        source_format="vtt",
        is_auto_generated=False,
        raw_text="WEBVTT\n\nPublication queue source",
        normalized_text="Publication queue source",
    )
    first_video = VideoMetadata(
        youtube_video_id="publication-queue-first",
        channel=channel,
        title="First publication queue video",
        video_url="https://www.youtube.com/watch?v=publication-queue-first",
    )
    second_video = replace(
        first_video,
        youtube_video_id="publication-queue-second",
        title="Second publication queue video",
        video_url="https://www.youtube.com/watch?v=publication-queue-second",
    )
    first_video_id, first_subtitle_id = await repository.save_fetched_video(
        first_video,
        subtitle,
    )
    second_video_id, second_subtitle_id = await repository.save_fetched_video(
        second_video,
        subtitle,
    )
    assert first_subtitle_id is not None
    assert second_subtitle_id is not None
    pool = repository._require_pool()
    await pool.execute(
        """
        UPDATE videos
        SET published_at = CASE id
            WHEN $1 THEN '2026-08-01 00:00:00+00'::timestamptz
            WHEN $2 THEN '2026-08-02 00:00:00+00'::timestamptz
        END
        WHERE id = ANY($3::uuid[])
        """,
        first_video_id,
        second_video_id,
        [first_video_id, second_video_id],
    )

    async def add_succeeded_revision(
        video_id: UUID,
        subtitle_id: UUID,
        snapshot: str,
        timestamp: datetime,
    ) -> UUID:
        run_id = await repository.start_analysis_run(
            video_id,
            subtitle_id,
            agent_model=None,
            prompt_version="publication-queue",
            metadata={},
        )
        analysis_id = await pool.fetchval(
            """
            INSERT INTO video_analyses(
                video_id,
                subtitle_track_id,
                analysis_run_id,
                translated_subtitle_snapshot,
                raw_agent_output,
                summary,
                analyzed_at,
                created_at
            )
            VALUES (
                $1, $2, $3, $4, '{"guests": []}'::jsonb,
                'Publication queue summary', $5, $5
            )
            RETURNING id
            """,
            video_id,
            subtitle_id,
            run_id,
            snapshot,
            timestamp,
        )
        await pool.execute(
            "UPDATE analysis_runs SET status = 'succeeded' WHERE id = $1",
            run_id,
        )
        return analysis_id

    first_oldest_id = await add_succeeded_revision(
        first_video_id,
        first_subtitle_id,
        "First translated revision",
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    first_newer_id = await add_succeeded_revision(
        first_video_id,
        first_subtitle_id,
        "Second translated revision",
        datetime(2026, 8, 2, tzinfo=UTC),
    )
    second_id = await add_succeeded_revision(
        second_video_id,
        second_subtitle_id,
        "Other video translated revision",
        datetime(2026, 8, 3, tzinfo=UTC),
    )
    target_key = "portal-push:Youtube"
    expected_oldest = PublicationCandidate(
        first_oldest_id,
        first_video.youtube_video_id,
        first_video.video_url,
    )
    expected_newer = PublicationCandidate(
        first_newer_id,
        first_video.youtube_video_id,
        first_video.video_url,
    )
    expected_second = PublicationCandidate(
        second_id,
        second_video.youtube_video_id,
        second_video.video_url,
    )
    assert await repository.list_publication_candidates(target_key) == [
        expected_second,
        expected_oldest,
        expected_newer,
    ]
    await pool.execute(
        "UPDATE video_analyses SET raw_agent_output = '{}'::jsonb WHERE id = $1",
        second_id,
    )
    assert await repository.list_publication_candidates("portal-push:Strict") == [
        expected_oldest,
        expected_newer,
    ]
    await pool.execute(
        """
        UPDATE video_analyses
        SET raw_agent_output = '{"guests": []}'::jsonb
        WHERE id = $1
        """,
        second_id,
    )
    await pool.execute(
        "UPDATE video_analyses SET summary = NULL WHERE id = $1",
        second_id,
    )
    assert await repository.list_publication_candidates("portal-push:Summary") == [
        expected_oldest,
        expected_newer,
    ]
    await pool.execute(
        "UPDATE video_analyses SET summary = 'Publication queue summary' WHERE id = $1",
        second_id,
    )

    def publication_steps(title: str) -> list[PublicationStepInput]:
        return [
            PublicationStepInput(target_key, "topic", title, "Analysis markdown"),
            PublicationStepInput(
                target_key,
                "translation",
                None,
                "Translation markdown",
            ),
            PublicationStepInput(target_key, "source", None, None, skipped=True),
        ]

    portal_target = {
        "origin": "https://portal.test",
        "user_id": "publication-queue-user",
        "category_id": 4,
        "category_name": "Youtube",
        "username": "publication-queue-publisher",
    }
    await repository.ensure_publication_steps(
        first_oldest_id,
        publication_steps("Oldest revision"),
    )
    for step, remote_topic_id, remote_comment_id in (
        ("topic", "publication-queue-topic", None),
        ("translation", None, 501),
    ):
        assert await repository.claim_publication_step(
            first_oldest_id,
            target_key,
            step,
            {"portal_target": portal_target},
        )
        assert await repository.mark_publication_step_in_progress(
            first_oldest_id,
            target_key,
            step,
        )
        await repository.mark_publication_step_created(
            first_oldest_id,
            target_key,
            step,
            remote_topic_id=remote_topic_id,
            remote_comment_id=remote_comment_id,
            remote_status=0,
            response_metadata={},
        )
        await repository.mark_publication_step_succeeded(
            first_oldest_id,
            target_key,
            step,
            remote_status=0,
            response_metadata={},
        )
    assert await repository.list_publication_candidates(target_key) == [
        expected_second,
        expected_newer,
    ]

    await repository.ensure_publication_steps(
        first_newer_id,
        publication_steps("Newer revision"),
    )
    assert await repository.claim_publication_step(
        first_newer_id,
        target_key,
        "topic",
        {"portal_target": portal_target},
    )
    assert await repository.mark_publication_step_in_progress(
        first_newer_id,
        target_key,
        "topic",
    )
    await repository.mark_publication_step_failed(
        first_newer_id,
        target_key,
        "topic",
        "remote result requires reconciliation",
        uncertain=True,
    )
    expected_uncertain = replace(
        expected_newer,
        requires_reconciliation=True,
    )
    assert await repository.list_publication_candidates(target_key) == [
        expected_second,
        expected_uncertain,
    ]

    legacy_video = replace(
        first_video,
        youtube_video_id="publication-queue-legacy",
        title="Legacy publication queue video",
        video_url="https://www.youtube.com/watch?v=publication-queue-legacy",
    )
    legacy_video_id, _ = await repository.save_fetched_video(legacy_video, None)
    await pool.execute(
        """
        ALTER TABLE video_analyses
        DISABLE TRIGGER video_analyses_translation_snapshot_guard
        """
    )
    try:
        legacy_analysis_id = await pool.fetchval(
            """
            INSERT INTO video_analyses(video_id, analyzed_at, created_at)
            VALUES (
                $1,
                '2026-07-01 00:00:00+00'::timestamptz,
                '2026-07-01 00:00:00+00'::timestamptz
            )
            RETURNING id
            """,
            legacy_video_id,
        )
    finally:
        await pool.execute(
            """
            ALTER TABLE video_analyses
            ENABLE TRIGGER video_analyses_translation_snapshot_guard
            """
        )

    legacy_target = "portal-push:Legacy"
    legacy_steps = [
        replace(step, target_key=legacy_target)
        for step in publication_steps("Legacy snapshot")
    ]
    await repository.ensure_publication_steps(legacy_analysis_id, legacy_steps)
    assert await repository.list_publication_candidates(legacy_target) == [
        expected_second,
        expected_oldest,
        expected_newer,
        PublicationCandidate(
            legacy_analysis_id,
            legacy_video.youtube_video_id,
            legacy_video.video_url,
        ),
    ]


@pytest.mark.asyncio
async def test_repository_persists_pipeline_artifacts_and_run_states(
    repository: PipelineRepository,
) -> None:
    subtitle = DownloadedSubtitle(
        language_code="en",
        language_name="English",
        source_format="vtt",
        is_auto_generated=False,
        raw_text="WEBVTT\n\n00:00.000 --> 00:01.000\nOriginal subtitle",
        normalized_text="Original subtitle",
    )
    video = VideoMetadata(
        youtube_video_id="integration-video",
        channel=ChannelMetadata(
            youtube_channel_id="integration-channel",
            title="Integration Channel",
            channel_url="https://www.youtube.com/@integration-channel",
            handle="@integration-channel",
        ),
        title="Integration Video",
        video_url="https://www.youtube.com/watch?v=integration-video",
        description="Repository integration test",
        duration_seconds=123,
        published_at=datetime(2026, 8, 4, 12, 30, tzinfo=UTC),
    )
    video_id, subtitle_id = await repository.save_fetched_video(video, subtitle)
    assert subtitle_id is not None

    translation_metadata = {
        "mode": "agent",
        "thread_ids": ["thread-1", "thread-2"],
        "usage": [{"input_tokens": 23, "cached": False}],
        "nullable": None,
        "locale": "中文",
    }
    translation = TranslationResult(
        translated_text="原始字幕",
        translated_language_code="zh-Hans",
        metadata=translation_metadata,
    )
    await repository.save_translation(subtitle_id, translation)

    source_sha256 = hashlib.sha256(b"Original subtitle").hexdigest()
    initial_metadata = {
        "prompt_sha256": "prompt-sha",
        "translation_schema_sha256": "translation-schema-sha",
        "schema_sha256": "schema-sha",
        "source_sha256": source_sha256,
        "options": {"web_search": True, "temperature": 0},
    }
    run_id = await repository.start_analysis_run(
        video_id,
        subtitle_id,
        agent_model="gpt-integration",
        prompt_version="translation:t1;analysis:a1",
        metadata=initial_metadata,
    )
    translation_invocation = AgentInvocation(
        stage="translation",
        sequence_number=1,
        status="succeeded",
        thread_id="translation-thread-1",
        agent_input={
            "source_language": "en",
            "chunk_index": 1,
            "chunk_count": 1,
            "subtitle_text": "Original subtitle",
        },
        full_prompt="Translate this complete prompt: Original subtitle",
        intermediate_events=[
            {"type": "thread.started", "thread_id": "translation-thread-1"},
            {
                "type": "item.completed",
                "item": {"type": "reasoning", "id": "reasoning-1", "text": "翻译过程"},
            },
        ],
        final_response='{"translated_text":"原始字幕"}',
        output_payload={"translated_text": "原始字幕"},
        usage={"input_tokens": 20, "cached_input_tokens": 2, "output_tokens": 8},
        error_message=None,
        started_at=datetime(2026, 8, 4, 12, 31, tzinfo=UTC),
        finished_at=datetime(2026, 8, 4, 12, 32, tzinfo=UTC),
    )
    failed_analysis_invocation = AgentInvocation(
        stage="analysis",
        sequence_number=1,
        status="failed",
        thread_id="analysis-thread-failed",
        agent_input={"video": {"title": "Integration Video"}, "subtitle_text": "原始字幕"},
        full_prompt="Analyze this complete prompt: 原始字幕",
        intermediate_events=[
            {"type": "thread.started", "thread_id": "analysis-thread-failed"},
            {"type": "turn.failed", "error": {"message": "model failure"}},
        ],
        final_response=None,
        output_payload=None,
        usage=None,
        error_message="model failure",
        started_at=datetime(2026, 8, 4, 12, 33, tzinfo=UTC),
        finished_at=datetime(2026, 8, 4, 12, 34, tzinfo=UTC),
    )
    await repository.save_agent_invocation(run_id, translation_invocation)
    await repository.save_agent_invocation(run_id, failed_analysis_invocation)

    invocation_rows = await repository._require_pool().fetch(
        """
        SELECT stage, sequence_number, status, thread_id,
               agent_input::text AS agent_input,
               full_prompt,
               intermediate_events::text AS intermediate_events,
               final_response,
               agent_output::text AS agent_output,
               usage::text AS usage,
               error_message,
               started_at,
               finished_at
        FROM agent_invocations
        WHERE analysis_run_id = $1
        ORDER BY stage
        """,
        run_id,
    )
    assert len(invocation_rows) == 2
    rows_by_stage = {row["stage"]: row for row in invocation_rows}
    saved_translation = rows_by_stage["translation"]
    assert saved_translation["status"] == "succeeded"
    assert saved_translation["thread_id"] == translation_invocation.thread_id
    assert json.loads(saved_translation["agent_input"]) == translation_invocation.agent_input
    assert saved_translation["full_prompt"] == translation_invocation.full_prompt
    assert json.loads(saved_translation["intermediate_events"]) == (
        translation_invocation.intermediate_events
    )
    assert saved_translation["final_response"] == translation_invocation.final_response
    assert json.loads(saved_translation["agent_output"]) == translation_invocation.output_payload
    assert json.loads(saved_translation["usage"]) == translation_invocation.usage
    assert saved_translation["error_message"] is None
    assert saved_translation["started_at"] == translation_invocation.started_at
    assert saved_translation["finished_at"] == translation_invocation.finished_at
    saved_failure = rows_by_stage["analysis"]
    assert saved_failure["status"] == "failed"
    assert json.loads(saved_failure["agent_input"]) == failed_analysis_invocation.agent_input
    assert json.loads(saved_failure["intermediate_events"]) == (
        failed_analysis_invocation.intermediate_events
    )
    assert saved_failure["final_response"] is None
    assert saved_failure["agent_output"] is None
    assert saved_failure["usage"] is None
    assert saved_failure["error_message"] == "model failure"
    payload = {
        "contract_version": 3,
        "guests": [],
        "result": {
            "summary": "A variable payload",
            "evidence": [
                {"timestamp": 1.25, "quote": "原文", "verified": True},
                {"timestamp": None, "quote": "second", "verified": False},
            ],
            "extension": {"arbitrary": [1, "two", None]},
        },
    }
    analysis_metadata = {
        "schema_sha256": "schema-sha",
        "source_sha256": "source-sha",
        "thread_id": "analysis-thread",
    }
    outcome = AnalysisOutcome(
        payload=payload,
        projection=AnalysisProjection(
            is_relevant=True,
            relevance_score=0.95,
            quality_score=0.75,
            summary="A variable payload",
            translated_summary="可变结构",
            background_notes="context",
            key_points=["first", {"nested": [1, 2, 3]}],
            tags=[
                {
                    "name": "postgresql",
                    "category": "technology",
                    "description": "Database topic",
                    "confidence": 0.98,
                },
                {
                    "name": "testing",
                    "category": "practice",
                    "description": "Test topic",
                    "confidence": None,
                },
            ],
        ),
        metadata=analysis_metadata,
    )
    run_metadata = {
        "translation": translation_metadata,
        "analysis": {"thread_id": "analysis-thread", "usage": {"output_tokens": 44}},
    }
    publication_steps = [
        PublicationStepInput(
            target_key="portal-push:Youtube",
            step="topic",
            topic_title="Integration Video",
            markdown_snapshot="# AI analysis",
        ),
        PublicationStepInput(
            target_key="portal-push:Youtube",
            step="translation",
            topic_title=None,
            markdown_snapshot="## Subtitle translation\n\nTranslated text",
        ),
        PublicationStepInput(
            target_key="portal-push:Youtube",
            step="source",
            topic_title=None,
            markdown_snapshot=None,
            skipped=True,
        ),
    ]
    portal_target = {
        "origin": "https://portal.test",
        "user_id": "publisher-user-id",
        "category_id": 4,
        "category_name": "Youtube",
        "username": "publisher",
    }
    analysis_id = await repository.complete_analysis_run(
        run_id,
        video_id,
        subtitle_id,
        outcome,
        profile_name="research-v1",
        schema_version="2026-08-04",
        run_metadata=run_metadata,
        translated_subtitle=translation.translated_text,
    )
    await repository.ensure_publication_steps(analysis_id, publication_steps)
    await repository.ensure_publication_steps(analysis_id, publication_steps)

    pool = repository._require_pool()
    subtitle_row = await pool.fetchrow(
        """
        SELECT raw_text, normalized_text, translated_text,
               translated_language_code, translation_metadata::text AS translation_metadata
        FROM subtitle_tracks WHERE id = $1
        """,
        subtitle_id,
    )
    assert subtitle_row is not None
    assert subtitle_row["raw_text"] == subtitle.raw_text
    assert subtitle_row["normalized_text"] == subtitle.normalized_text
    assert subtitle_row["translated_text"] == "原始字幕"
    assert subtitle_row["translated_language_code"] == "zh-Hans"
    assert json.loads(subtitle_row["translation_metadata"]) == translation_metadata

    analysis_row = await pool.fetchrow(
        """
        SELECT raw_agent_output::text AS payload, key_points::text AS key_points,
               analysis_metadata::text AS metadata,
               translated_subtitle_snapshot
        FROM video_analyses WHERE id = $1
        """,
        analysis_id,
    )
    assert analysis_row is not None
    assert json.loads(analysis_row["payload"]) == payload
    assert json.loads(analysis_row["key_points"]) == outcome.projection.key_points
    assert json.loads(analysis_row["metadata"]) == analysis_metadata
    assert analysis_row["translated_subtitle_snapshot"] == translation.translated_text

    with pytest.raises(asyncpg.RaiseError, match="snapshot is immutable"):
        await pool.execute(
            """
            UPDATE video_analyses
            SET translated_subtitle_snapshot = 'mutated translation'
            WHERE id = $1
            """,
            analysis_id,
        )
    with pytest.raises(RuntimeError, match="different immutable snapshot"):
        await repository.ensure_publication_steps(
            analysis_id,
            [
                replace(publication_steps[0], topic_title="Changed title"),
                *publication_steps[1:],
            ],
        )

    saved_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert [step.step for step in saved_steps] == ["topic", "translation", "source"]
    assert [step.status for step in saved_steps] == ["pending", "pending", "skipped"]
    assert saved_steps[0].content_sha256 == hashlib.sha256(
        b"# AI analysis"
    ).hexdigest()
    assert saved_steps[2].markdown_snapshot is None
    assert saved_steps[2].content_sha256 is None

    candidate_identity = {
        "profile_name": "research-v1",
        "schema_version": "2026-08-04",
        "prompt_version": "translation:t1;analysis:a1",
        "prompt_sha256": "prompt-sha",
        "translation_schema_sha256": "translation-schema-sha",
        "schema_sha256": "schema-sha",
        "force": False,
        "limit": None,
    }
    assert await repository.list_analysis_candidates(**candidate_identity) == []
    expected_publication_candidate = PublicationCandidate(
        video_analysis_id=analysis_id,
        youtube_video_id=video.youtube_video_id,
        video_url=video.video_url,
    )
    assert await repository.list_publication_candidates(
        "portal-push:Youtube",
    ) == [expected_publication_candidate]
    with pytest.raises(ValueError, match="target_key"):
        await repository.list_publication_candidates(" ")

    invalid_latest_subtitle_id = await pool.fetchval(
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
        VALUES ($1, 'en', 'English', 'vtt', false, $2, NULL)
        RETURNING id
        """,
        video_id,
        "WEBVTT\n\ninvalid newer subtitle",
    )
    assert await repository.list_publication_candidates(
        "portal-push:Youtube",
    ) == [expected_publication_candidate]
    publication_source = await repository.get_publication_source(analysis_id)
    assert publication_source is not None
    assert publication_source.video_analysis_id == analysis_id
    assert publication_source.fetched.metadata == video
    assert publication_source.fetched.subtitle == subtitle
    assert publication_source.translated_text == translation.translated_text
    assert publication_source.outcome == outcome
    assert await repository.get_publication_source(UUID(int=0)) is None
    await pool.execute(
        "DELETE FROM subtitle_tracks WHERE id = $1",
        invalid_latest_subtitle_id,
    )

    assert await repository.claim_publication_step(
        analysis_id,
        "portal-push:Youtube",
        "topic",
        {"portal_target": portal_target, "content_type": "markdown"},
    )
    claimed_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert claimed_steps[0].status == "claimed"
    assert claimed_steps[0].attempt_count == 1
    assert claimed_steps[0].started_at is None
    assert claimed_steps[0].request_metadata["portal_target"] == portal_target
    assert await repository.list_publication_candidates(
        "portal-push:Youtube",
    ) == [expected_publication_candidate]

    assert await repository.claim_publication_step(
        analysis_id,
        "portal-push:Youtube",
        "topic",
        {"portal_target": portal_target, "tags": []},
    )
    reclaimed_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert reclaimed_steps[0].status == "claimed"
    assert reclaimed_steps[0].attempt_count == 1
    assert reclaimed_steps[0].request_metadata["tags"] == []

    drifted_target = {**portal_target, "username": "different-publisher"}
    assert not await repository.claim_publication_step(
        analysis_id,
        "portal-push:Youtube",
        "topic",
        {"portal_target": drifted_target},
    )
    with pytest.raises(ValueError, match="complete portal_target"):
        await repository.claim_publication_step(
            analysis_id,
            "portal-push:Youtube",
            "topic",
            {"content_type": "markdown"},
        )
    with pytest.raises(asyncpg.RaiseError, match="portal target is immutable"):
        await pool.execute(
            """
            UPDATE bbs_publication_steps
            SET request_metadata = request_metadata || $4::jsonb
            WHERE video_analysis_id = $1 AND target_key = $2 AND step = $3
            """,
            analysis_id,
            "portal-push:Youtube",
            "topic",
            json.dumps({"portal_target": drifted_target}),
        )
    with pytest.raises(asyncpg.RaiseError, match="portal target is immutable"):
        await pool.execute(
            """
            UPDATE bbs_publication_steps
            SET request_metadata = request_metadata - 'portal_target'
            WHERE video_analysis_id = $1 AND target_key = $2 AND step = $3
            """,
            analysis_id,
            "portal-push:Youtube",
            "topic",
        )
    target_bound_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert target_bound_steps[0].request_metadata["portal_target"] == portal_target

    transition_results = await asyncio.gather(
        repository.mark_publication_step_in_progress(
            analysis_id,
            "portal-push:Youtube",
            "topic",
        ),
        repository.mark_publication_step_in_progress(
            analysis_id,
            "portal-push:Youtube",
            "topic",
        ),
    )
    assert sorted(transition_results) == [False, True]
    in_progress_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert in_progress_steps[0].status == "in_progress"
    assert in_progress_steps[0].started_at is not None
    assert await repository.list_publication_candidates(
        "portal-push:Youtube",
    ) == [replace(expected_publication_candidate, requires_reconciliation=True)]

    await repository.mark_publication_step_created(
        analysis_id,
        "portal-push:Youtube",
        "topic",
        remote_topic_id="opaque-topic-id",
        remote_comment_id=None,
        remote_status=0,
        response_metadata={"title": "Integration Video"},
    )
    assert await repository.list_publication_candidates(
        "portal-push:Youtube",
    ) == [expected_publication_candidate]
    await repository.mark_publication_step_succeeded(
        analysis_id,
        "portal-push:Youtube",
        "topic",
        remote_status=0,
        response_metadata={"verified": True},
    )

    assert await repository.claim_publication_step(
        analysis_id,
        "portal-push:Youtube",
        "translation",
        {"portal_target": portal_target, "content_type": "markdown"},
    )
    assert await repository.mark_publication_step_in_progress(
        analysis_id,
        "portal-push:Youtube",
        "translation",
    )
    await repository.mark_publication_step_failed(
        analysis_id,
        "portal-push:Youtube",
        "translation",
        "definitive business failure",
        uncertain=False,
    )
    assert await repository.claim_publication_step(
        analysis_id,
        "portal-push:Youtube",
        "translation",
        {"portal_target": portal_target, "content_type": "markdown"},
    )
    assert await repository.mark_publication_step_in_progress(
        analysis_id,
        "portal-push:Youtube",
        "translation",
    )
    await repository.mark_publication_step_created(
        analysis_id,
        "portal-push:Youtube",
        "translation",
        remote_topic_id=None,
        remote_comment_id=123,
        remote_status=0,
        response_metadata={"content_type": "markdown"},
    )
    await repository.mark_publication_step_succeeded(
        analysis_id,
        "portal-push:Youtube",
        "translation",
        remote_status=0,
        response_metadata={"verified": True},
    )

    completed_steps = await repository.list_publication_steps(
        analysis_id,
        "portal-push:Youtube",
    )
    assert [step.status for step in completed_steps] == [
        "succeeded",
        "succeeded",
        "skipped",
    ]
    assert completed_steps[0].remote_topic_id == "opaque-topic-id"
    assert completed_steps[1].remote_comment_id == 123
    assert completed_steps[1].attempt_count == 2
    assert (
        await repository.list_publication_candidates("portal-push:Youtube")
        == []
    )

    uncertain_target_key = "portal-push:Uncertain"
    await pool.executemany(
        """
        INSERT INTO bbs_publication_steps(
            video_analysis_id,
            target_key,
            step,
            topic_title,
            markdown_snapshot,
            status,
            attempt_count,
            error_message,
            request_metadata,
            started_at,
            completed_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            $9::jsonb, $10, $11
        )
        """,
        [
            (
                analysis_id,
                uncertain_target_key,
                "topic",
                "Uncertain topic",
                "# Uncertain topic",
                "uncertain",
                1,
                "remote result requires reconciliation",
                json.dumps({"portal_target": portal_target}),
                datetime.now(UTC),
                datetime.now(UTC),
            ),
            (
                analysis_id,
                uncertain_target_key,
                "translation",
                None,
                "Pending translation",
                "pending",
                0,
                None,
                "{}",
                None,
                None,
            ),
            (
                analysis_id,
                uncertain_target_key,
                "source",
                None,
                None,
                "skipped",
                0,
                None,
                "{}",
                None,
                datetime.now(UTC),
            ),
        ],
    )
    assert await repository.list_publication_candidates(
        uncertain_target_key,
    ) == [replace(expected_publication_candidate, requires_reconciliation=True)]

    tags = await pool.fetch(
        """
        SELECT tags.name, tags.category, tags.description, video_analysis_tags.confidence
        FROM video_analysis_tags
        JOIN tags ON tags.id = video_analysis_tags.tag_id
        WHERE video_analysis_tags.video_analysis_id = $1
        ORDER BY tags.name
        """,
        analysis_id,
    )
    assert [dict(row) for row in tags] == [
        {
            "name": "postgresql",
            "category": "technology",
            "description": "Database topic",
            "confidence": Decimal("0.98"),
        },
        {
            "name": "testing",
            "category": "practice",
            "description": "Test topic",
            "confidence": None,
        },
    ]

    succeeded_run = await pool.fetchrow(
        """
        SELECT status, finished_at, error_message, metadata::text AS metadata
        FROM analysis_runs WHERE id = $1
        """,
        run_id,
    )
    assert succeeded_run is not None
    assert succeeded_run["status"] == "succeeded"
    assert succeeded_run["finished_at"] is not None
    assert succeeded_run["error_message"] is None
    assert json.loads(succeeded_run["metadata"]) == initial_metadata | run_metadata
    assert await repository.has_matching_analysis(
        video_id,
        subtitle_id,
        profile_name="research-v1",
        schema_version="2026-08-04",
        prompt_version="translation:t1;analysis:a1",
        prompt_sha256="prompt-sha",
        translation_schema_sha256="translation-schema-sha",
        schema_sha256="schema-sha",
        source_sha256=source_sha256,
    )
    assert not await repository.has_matching_analysis(
        video_id,
        subtitle_id,
        profile_name="research-v1",
        schema_version="2026-08-04",
        prompt_version="translation:t1;analysis:a1",
        prompt_sha256="different-prompt-sha",
        translation_schema_sha256="translation-schema-sha",
        schema_sha256="schema-sha",
        source_sha256=source_sha256,
    )

    await pool.execute(
        """
        UPDATE subtitle_tracks
        SET translated_text = NULL,
            translated_language_code = NULL,
            translation_metadata = '{}'::jsonb
        WHERE id = $1
        """,
        subtitle_id,
    )
    assert not await repository.has_matching_analysis(
        video_id,
        subtitle_id,
        profile_name="research-v1",
        schema_version="2026-08-04",
        prompt_version="translation:t1;analysis:a1",
        prompt_sha256="prompt-sha",
        translation_schema_sha256="translation-schema-sha",
        schema_sha256="schema-sha",
        source_sha256=source_sha256,
    )
    assert await repository.save_fetched_video(video, subtitle, translation) == (
        video_id,
        subtitle_id,
    )
    assert await repository.has_matching_analysis(
        video_id,
        subtitle_id,
        profile_name="research-v1",
        schema_version="2026-08-04",
        prompt_version="translation:t1;analysis:a1",
        prompt_sha256="prompt-sha",
        translation_schema_sha256="translation-schema-sha",
        schema_sha256="schema-sha",
        source_sha256=source_sha256,
    )

    repeated_results = await asyncio.gather(
        repository.save_fetched_video(video, subtitle),
        repository.save_fetched_video(video, subtitle),
    )
    assert repeated_results == [
        (video_id, subtitle_id),
        (video_id, subtitle_id),
    ]

    revised_subtitle = replace(
        subtitle,
        raw_text="WEBVTT\n\n00:00.000 --> 00:01.000\nRevised raw subtitle",
        normalized_text="Revised raw subtitle",
    )
    revised_video_id, revised_subtitle_id = await repository.save_fetched_video(
        video,
        revised_subtitle,
    )
    assert revised_video_id == video_id
    assert revised_subtitle_id is not None
    assert revised_subtitle_id != subtitle_id

    subtitle_versions = await pool.fetch(
        """
        SELECT id, raw_text, normalized_text, translated_text,
               translated_language_code, translation_metadata::text AS translation_metadata
        FROM subtitle_tracks
        WHERE video_id = $1 AND language_code = $2 AND is_auto_generated = $3
        ORDER BY created_at, id
        """,
        video_id,
        subtitle.language_code,
        subtitle.is_auto_generated,
    )
    assert len(subtitle_versions) == 2
    versions_by_id = {row["id"]: row for row in subtitle_versions}
    original_version = versions_by_id[subtitle_id]
    assert original_version["raw_text"] == subtitle.raw_text
    assert original_version["normalized_text"] == subtitle.normalized_text
    assert original_version["translated_text"] == translation.translated_text
    assert original_version["translated_language_code"] == "zh-Hans"
    assert json.loads(original_version["translation_metadata"]) == translation_metadata
    revised_version = versions_by_id[revised_subtitle_id]
    assert revised_version["raw_text"] == revised_subtitle.raw_text
    assert revised_version["normalized_text"] == revised_subtitle.normalized_text
    assert revised_version["translated_text"] is None
    assert revised_version["translated_language_code"] is None
    assert json.loads(revised_version["translation_metadata"]) == {}

    historical_analysis = await pool.fetchrow(
        """
        SELECT subtitle_track_id, raw_agent_output::text AS payload
        FROM video_analyses WHERE id = $1
        """,
        analysis_id,
    )
    assert historical_analysis is not None
    assert historical_analysis["subtitle_track_id"] == subtitle_id
    assert json.loads(historical_analysis["payload"]) == payload

    failed_run_id = await repository.start_analysis_run(
        video_id,
        subtitle_id,
        agent_model=None,
        prompt_version="translation:t1;analysis:a1",
        metadata={"attempt": 2},
    )
    failure_metadata = {"translation": {"thread_ids": ["translation-thread"]}}
    await repository.fail_analysis_run(
        failed_run_id,
        "agent process exited with status 1",
        failure_metadata,
    )
    failed_run = await pool.fetchrow(
        """
        SELECT status, finished_at, error_message, metadata::text AS metadata
        FROM analysis_runs WHERE id = $1
        """,
        failed_run_id,
    )
    assert failed_run is not None
    assert failed_run["status"] == "failed"
    assert failed_run["finished_at"] is not None
    assert failed_run["error_message"] == "agent process exited with status 1"
    assert json.loads(failed_run["metadata"]) == {"attempt": 2} | failure_metadata

    cancelled_run_id = await repository.start_analysis_run(
        video_id,
        subtitle_id,
        agent_model=None,
        prompt_version="translation:t1;analysis:a1",
        metadata={"attempt": 3},
    )
    cancelled_invocation = replace(
        failed_analysis_invocation,
        status="cancelled",
        error_message="Codex invocation cancelled",
    )
    await repository.save_agent_invocation(cancelled_run_id, cancelled_invocation)
    await repository.cancel_analysis_run(
        cancelled_run_id,
        "Pipeline task cancelled",
        {"translation": translation_metadata},
    )
    cancelled_run = await pool.fetchrow(
        """
        SELECT status, finished_at, error_message, metadata::text AS metadata
        FROM analysis_runs WHERE id = $1
        """,
        cancelled_run_id,
    )
    assert cancelled_run is not None
    assert cancelled_run["status"] == "cancelled"
    assert cancelled_run["finished_at"] is not None
    assert cancelled_run["error_message"] == "Pipeline task cancelled"
    assert json.loads(cancelled_run["metadata"]) == {
        "attempt": 3,
        "translation": translation_metadata,
    }

    with pytest.raises(asyncpg.CheckViolationError):
        await repository.save_agent_invocation(
            cancelled_run_id,
            replace(
                cancelled_invocation,
                sequence_number=2,
                status="succeeded",
                error_message="success cannot have an error",
            ),
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await repository.save_agent_invocation(
            cancelled_run_id,
            replace(
                cancelled_invocation,
                sequence_number=3,
                started_at=datetime(2026, 8, 4, 12, 36, tzinfo=UTC),
                finished_at=datetime(2026, 8, 4, 12, 35, tzinfo=UTC),
            ),
        )

    other_video = replace(
        video,
        youtube_video_id="integration-video-other",
        video_url="https://www.youtube.com/watch?v=integration-video-other",
        title="Other Integration Video",
    )
    other_subtitle = replace(
        subtitle,
        raw_text="WEBVTT\n\nOther video subtitle",
        normalized_text="Other video subtitle",
    )
    other_video_id, other_subtitle_id = await repository.save_fetched_video(
        other_video,
        other_subtitle,
    )
    assert other_subtitle_id is not None
    cross_video_run_id = await repository.start_analysis_run(
        video_id,
        subtitle_id,
        agent_model="gpt-integration",
        prompt_version="translation:t1;analysis:a1",
        metadata=initial_metadata,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await repository.complete_analysis_run(
            cross_video_run_id,
            other_video_id,
            other_subtitle_id,
            outcome,
            profile_name="research-v1",
            schema_version="2026-08-04",
            run_metadata=run_metadata,
            translated_subtitle="Other translated subtitle",
        )

    assert (
        await pool.fetchval(
            "SELECT count(*) FROM video_analyses WHERE analysis_run_id = $1",
            cross_video_run_id,
        )
        == 0
    )
    assert (
        await pool.fetchval(
            "SELECT status FROM analysis_runs WHERE id = $1",
            cross_video_run_id,
        )
        == "running"
    )


@pytest.mark.asyncio
async def test_repository_syncs_subscriptions_and_marks_backfill_once(
    repository: PipelineRepository,
) -> None:
    manual = ChannelMetadata(
        youtube_channel_id="manual-channel",
        title="Manual Channel",
        channel_url="https://www.youtube.com/@manual-channel",
    )
    manual_id = await repository.register_channel(manual, None)
    first_subscription = ChannelMetadata(
        youtube_channel_id="subscription-one",
        title="Subscription One",
        channel_url="https://www.youtube.com/@subscription-one",
    )
    retained_subscription = ChannelMetadata(
        youtube_channel_id="subscription-two",
        title="Subscription Two",
        channel_url="https://www.youtube.com/@subscription-two",
    )

    first_sync = await repository.sync_subscribed_channels(
        [first_subscription, retained_subscription]
    )
    assert {channel.youtube_channel_id for channel in first_sync} == {
        "subscription-one",
        "subscription-two",
    }
    assert all(channel.is_subscribed for channel in first_sync)

    second_sync = await repository.sync_subscribed_channels([retained_subscription])
    assert [channel.youtube_channel_id for channel in second_sync] == ["subscription-two"]
    active = {
        channel.youtube_channel_id: channel
        for channel in await repository.list_active_channels()
    }
    assert active["manual-channel"].id == manual_id
    assert active["manual-channel"].is_subscribed is False
    assert active["subscription-one"].is_subscribed is False
    retained = active["subscription-two"]
    assert retained.is_subscribed is True
    assert retained.initial_backfill_completed_at is None

    await repository.mark_channel_backfill_completed(retained.id)
    completed = (await repository.get_channels([retained.id]))[0]
    assert completed.initial_backfill_completed_at is not None
    await repository.mark_channel_backfill_completed(retained.id)
    completed_again = (await repository.get_channels([retained.id]))[0]
    assert completed_again.initial_backfill_completed_at == (
        completed.initial_backfill_completed_at
    )


@pytest.mark.asyncio
async def test_repository_treats_missing_fetched_subtitle_as_pending(
    repository: PipelineRepository,
) -> None:
    video = VideoMetadata(
        youtube_video_id="missing-subtitle-video",
        channel=ChannelMetadata(
            youtube_channel_id="missing-subtitle-channel",
            title="Missing Subtitle Channel",
            channel_url="https://www.youtube.com/@missing-subtitle-channel",
        ),
        title="Missing Subtitle Video",
        video_url="https://www.youtube.com/watch?v=missing-subtitle-video",
    )
    subtitle = DownloadedSubtitle(
        language_code="en",
        language_name="English",
        source_format="vtt",
        is_auto_generated=False,
        raw_text="WEBVTT\n\nSubtitle that will be removed",
        normalized_text="Subtitle that will be removed",
    )
    _, subtitle_id = await repository.save_fetched_video(video, subtitle)
    assert subtitle_id is not None
    await repository._require_pool().execute(
        "DELETE FROM subtitle_tracks WHERE id = $1",
        subtitle_id,
    )

    stored = await repository.get_stored_video(video.youtube_video_id)

    assert stored is not None
    assert stored.subtitle_track_id is None
    assert stored.fetched.subtitle is None
    assert stored.subtitle_status == "pending"
    assert stored.subtitle_download_status is SubtitleDownloadStatus.PENDING

    pool = repository._require_pool()
    channel_id = await pool.fetchval(
        "SELECT channel_id FROM videos WHERE youtube_video_id = $1",
        video.youtube_video_id,
    )
    candidates = await repository.list_subtitle_download_candidates([channel_id], [])

    assert [candidate.youtube_video_id for candidate in candidates] == [
        video.youtube_video_id
    ]
    assert candidates[0].status is SubtitleDownloadStatus.PENDING
    persisted = await pool.fetchrow(
        """
        SELECT subtitle_status, subtitle_checked_at, subtitle_download_status
        FROM videos
        WHERE youtube_video_id = $1
        """,
        video.youtube_video_id,
    )
    assert persisted is not None
    assert persisted["subtitle_status"] == "pending"
    assert persisted["subtitle_checked_at"] is None
    assert persisted["subtitle_download_status"] == 0
