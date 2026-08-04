from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

from config import DatabaseSettings
from database import PipelineRepository
from models import (
    AgentInvocation,
    AnalysisOutcome,
    AnalysisProjection,
    ChannelMetadata,
    DownloadedSubtitle,
    TranslationResult,
    VideoMetadata,
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

    initial_metadata = {
        "prompt_sha256": "prompt-sha",
        "translation_schema_sha256": "translation-schema-sha",
        "schema_sha256": "schema-sha",
        "source_sha256": "source-sha",
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
    analysis_id = await repository.complete_analysis_run(
        run_id,
        video_id,
        subtitle_id,
        outcome,
        profile_name="research-v1",
        schema_version="2026-08-04",
        run_metadata=run_metadata,
    )

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
               analysis_metadata::text AS metadata
        FROM video_analyses WHERE id = $1
        """,
        analysis_id,
    )
    assert analysis_row is not None
    assert json.loads(analysis_row["payload"]) == payload
    assert json.loads(analysis_row["key_points"]) == outcome.projection.key_points
    assert json.loads(analysis_row["metadata"]) == analysis_metadata

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
        source_sha256="source-sha",
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
        source_sha256="source-sha",
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
