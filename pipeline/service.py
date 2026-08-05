from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from analysis import AnalysisEngine
from config import RuntimeSettings
from database import PipelineRepository
from models import (
    AgentInvocation,
    ChannelRecord,
    FetchedVideo,
    ProcessResult,
    StoredVideo,
    TranslationResult,
    VideoReference,
)
from youtube import YoutubeClient

LOGGER = logging.getLogger(__name__)


class PipelineService:
    def __init__(
        self,
        settings: RuntimeSettings,
        repository: PipelineRepository,
        youtube: YoutubeClient,
        analysis: AnalysisEngine,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._youtube = youtube
        self._analysis = analysis

    async def add_channel(self, channel_url: str, researcher_name: str | None) -> UUID:
        channel = await self._youtube.inspect_channel(channel_url)
        return await self._repository.register_channel(channel, researcher_name)

    async def process_video(
        self,
        video_url: str,
        *,
        youtube_video_id: str | None = None,
        force: bool = False,
    ) -> ProcessResult:
        stored = None
        if youtube_video_id is not None and not force:
            stored = await self._repository.get_stored_video(youtube_video_id)

        if stored is None or stored.subtitle_status == "pending":
            fetched = await self._youtube.fetch_video(video_url)
            video_id, subtitle_id = await self._repository.save_fetched_video(
                fetched.metadata,
                fetched.subtitle,
            )
            stored_translation = None
        else:
            fetched = stored.fetched
            video_id = stored.video_id
            subtitle_id = stored.subtitle_track_id
            stored_translation = _stored_translation(stored, self._settings)

        return await self._analyze_video(
            video_url,
            fetched,
            video_id,
            subtitle_id,
            stored_translation=stored_translation,
            force=force,
        )

    async def _analyze_video(
        self,
        video_url: str,
        fetched: FetchedVideo,
        video_id: UUID,
        subtitle_id: UUID | None,
        *,
        stored_translation: TranslationResult | None,
        force: bool,
    ) -> ProcessResult:
        if fetched.subtitle is None or subtitle_id is None:
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=fetched.metadata.youtube_video_id,
                status="no_subtitle",
                detail="No preferred original subtitle was available",
            )
        if fetched.subtitle.normalized_text is None:
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=fetched.metadata.youtube_video_id,
                status="invalid_subtitle",
                detail="The original subtitle was saved but could not be normalized",
            )

        source_sha256 = hashlib.sha256(
            fetched.subtitle.normalized_text.encode("utf-8")
        ).hexdigest()
        if not force and await self._repository.has_matching_analysis(
            video_id,
            subtitle_id,
            profile_name=self._settings.agent.profile_name,
            schema_version=self._settings.agent.schema_version,
            prompt_version=self._settings.prompt_version,
            prompt_sha256=self._settings.prompt_sha256,
            translation_schema_sha256=self._settings.translation_schema_sha256,
            schema_sha256=self._settings.analysis_schema_sha256,
            source_sha256=source_sha256,
        ):
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=fetched.metadata.youtube_video_id,
                status="skipped",
                detail="Matching analysis already exists",
            )

        initial_metadata = {
            "profile_name": self._settings.agent.profile_name,
            "schema_version": self._settings.agent.schema_version,
            "prompt_version": self._settings.prompt_version,
            "prompt_sha256": self._settings.prompt_sha256,
            "translation_schema_sha256": self._settings.translation_schema_sha256,
            "schema_sha256": self._settings.analysis_schema_sha256,
            "source_sha256": source_sha256,
            "source_language": fetched.subtitle.language_code,
        }
        run_id = await self._repository.start_analysis_run(
            video_id,
            subtitle_id,
            agent_model=self._settings.agent.model or None,
            prompt_version=self._settings.prompt_version,
            metadata=initial_metadata,
        )

        failure_metadata: dict[str, Any] = {}

        async def save_invocation(invocation: AgentInvocation) -> None:
            await self._repository.save_agent_invocation(run_id, invocation)

        try:
            translation = stored_translation
            if translation is None or force:
                translation = await self._analysis.translate(
                    fetched.subtitle.language_code,
                    fetched.subtitle.normalized_text,
                    invocation_sink=save_invocation,
                )
                await self._repository.save_translation(subtitle_id, translation)
            failure_metadata["translation"] = translation.metadata

            outcome = await self._analysis.analyze(
                fetched.metadata,
                fetched.subtitle.language_code,
                translation.translated_text,
                invocation_sink=save_invocation,
            )
            run_metadata = {
                "translation": translation.metadata,
                "analysis": outcome.metadata,
            }
            await self._repository.complete_analysis_run(
                run_id,
                video_id,
                subtitle_id,
                outcome,
                profile_name=self._settings.agent.profile_name,
                schema_version=self._settings.agent.schema_version,
                run_metadata=run_metadata,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._repository.cancel_analysis_run(
                    run_id,
                    "Pipeline task cancelled",
                    failure_metadata,
                )
            )
            raise
        except Exception as exc:
            await self._repository.fail_analysis_run(run_id, str(exc), failure_metadata)
            raise

        return ProcessResult(
            video_url=video_url,
            youtube_video_id=fetched.metadata.youtube_video_id,
            status="analyzed",
        )

    async def process_channel(
        self,
        channel: ChannelRecord,
        *,
        max_videos: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        try:
            references = await self._youtube.discover_channel_videos(
                channel.channel_url,
                max_videos,
            )
            results = await self._process_references(references, force=force)
            if max_videos is None and not any(
                result.status == "failed" for result in results
            ):
                await self._repository.mark_channel_backfill_completed(channel.id)
            return results
        finally:
            await self._repository.mark_channel_checked(channel.id)

    async def run_channels(
        self,
        channel_urls: Sequence[str] | None,
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        if not channel_urls:
            return await self.run_all_channels(
                max_videos_per_channel=max_videos_per_channel,
                force=force,
            )

        channel_ids = [
            await self.add_channel(channel_url, researcher_name=None)
            for channel_url in dict.fromkeys(channel_urls)
        ]
        channels = await self._repository.get_channels(_unique(channel_ids))

        return await self._process_channels(
            channels,
            max_videos_per_channel=max_videos_per_channel,
            force=force,
        )

    async def run_all_channels(
        self,
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        return await self._process_channels(
            await self._repository.list_active_channels(),
            max_videos_per_channel=max_videos_per_channel,
            force=force,
        )

    async def _process_channels(
        self,
        channels: Sequence[ChannelRecord],
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for channel in channels:
            LOGGER.info("Processing channel %s", channel.title)
            try:
                results.extend(
                    await self.process_channel(
                        channel,
                        max_videos=max_videos_per_channel,
                        force=force,
                    )
                )
            except Exception as exc:
                LOGGER.exception("Failed to process channel %s", channel.channel_url)
                results.append(
                    ProcessResult(
                        video_url=channel.channel_url,
                        youtube_video_id=None,
                        status="failed",
                        detail=str(exc),
                    )
                )
        return results

    async def _process_references(
        self,
        references: Sequence[VideoReference],
        *,
        force: bool,
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for reference in references:
            try:
                results.append(
                    await self.process_video(
                        reference.video_url,
                        youtube_video_id=reference.youtube_video_id,
                        force=force,
                    )
                )
            except Exception as exc:
                LOGGER.exception("Failed to process %s", reference.video_url)
                results.append(
                    ProcessResult(
                        video_url=reference.video_url,
                        youtube_video_id=reference.youtube_video_id,
                        status="failed",
                        detail=str(exc),
                    )
                )
        return results


def _stored_translation(
    stored: StoredVideo,
    settings: RuntimeSettings,
) -> TranslationResult | None:
    if stored.translated_text is None or stored.translated_language_code is None:
        return None
    metadata = stored.translation_metadata
    if metadata.get("mode") != "copied_chinese_source" and (
        metadata.get("mode") != "codex_translation"
        or metadata.get("prompt_version") != settings.prompts.translation.version
        or metadata.get("translation_schema_sha256")
        != settings.translation_schema_sha256
    ):
        return None
    return TranslationResult(
        translated_text=stored.translated_text,
        translated_language_code=stored.translated_language_code,
        metadata=metadata,
    )


def _unique(values: Sequence[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))
