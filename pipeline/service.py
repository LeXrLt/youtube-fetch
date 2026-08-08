from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
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
    SubtitleDownloadStatus,
    SubtitleDownloadTask,
    TranslationResult,
    VideoReference,
)
from youtube import YoutubeClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PreparedVideo:
    fetched: FetchedVideo
    video_id: UUID
    subtitle_id: UUID | None
    stored_translation: TranslationResult | None
    downloaded: bool


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
        prepared = await self._prepare_video(
            video_url,
            youtube_video_id=youtube_video_id,
            force=force,
        )
        return await self._analyze_video(
            video_url,
            prepared.fetched,
            prepared.video_id,
            prepared.subtitle_id,
            stored_translation=prepared.stored_translation,
            force=force,
        )

    async def download_video(
        self,
        video_url: str,
        *,
        youtube_video_id: str | None = None,
        force: bool = False,
    ) -> ProcessResult:
        prepared = await self._prepare_video(
            video_url,
            youtube_video_id=youtube_video_id,
            force=force,
        )
        video_id = prepared.fetched.metadata.youtube_video_id
        if not prepared.downloaded:
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=video_id,
                status="skipped",
                detail="Subtitle download state already exists",
            )
        if prepared.fetched.subtitle is None:
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=video_id,
                status="no_subtitle",
                detail="No preferred original subtitle was available",
            )
        if prepared.fetched.subtitle.normalized_text is None:
            return ProcessResult(
                video_url=video_url,
                youtube_video_id=video_id,
                status="invalid_subtitle",
                detail="The original subtitle was saved but could not be normalized",
            )
        return ProcessResult(
            video_url=video_url,
            youtube_video_id=video_id,
            status="downloaded",
            detail=_subtitle_download_detail(prepared.fetched),
        )

    async def analyze_video(
        self,
        youtube_video_id: str,
        *,
        force: bool = False,
    ) -> ProcessResult:
        stored = await self._repository.get_stored_video(youtube_video_id)
        if stored is None:
            return ProcessResult(
                video_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
                youtube_video_id=youtube_video_id,
                status="failed",
                detail="Video has not been downloaded",
            )
        return await self._analyze_video(
            stored.fetched.metadata.video_url,
            stored.fetched,
            stored.video_id,
            stored.subtitle_track_id,
            stored_translation=_stored_translation(stored, self._settings),
            force=force,
        )

    async def _prepare_video(
        self,
        video_url: str,
        *,
        youtube_video_id: str | None,
        force: bool,
    ) -> _PreparedVideo:
        stored = None
        if youtube_video_id is not None and not force:
            stored = await self._repository.get_stored_video(youtube_video_id)

        if (
            stored is None
            or stored.subtitle_download_status is not SubtitleDownloadStatus.DOWNLOADED
        ):
            fetched = await self._youtube.fetch_video(video_url)
            stored_translation = _copied_chinese_translation(
                self._analysis,
                fetched,
            )
            video_id, subtitle_id = await self._repository.save_fetched_video(
                fetched.metadata,
                fetched.subtitle,
                stored_translation,
            )
            downloaded = True
        else:
            fetched = stored.fetched
            video_id = stored.video_id
            subtitle_id = stored.subtitle_track_id
            stored_translation = _stored_translation(stored, self._settings)
            downloaded = False

        return _PreparedVideo(
            fetched,
            video_id,
            subtitle_id,
            stored_translation,
            downloaded,
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

        if stored_translation is None:
            stored_translation = self._analysis.copy_chinese_source(
                fetched.subtitle.language_code,
                fetched.subtitle.normalized_text,
            )
            if stored_translation is not None:
                await self._repository.save_translation(
                    subtitle_id,
                    stored_translation,
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
        limiter: asyncio.Semaphore | None = None,
    ) -> list[ProcessResult]:
        return await self._process_channels(
            [channel],
            max_videos_per_channel=max_videos,
            force=force,
            limiter=limiter,
        )

    async def download_channels(
        self,
        channel_urls: Sequence[str] | None,
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        if not channel_urls:
            channels = await self._repository.list_active_channels()
        else:
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

    async def analyze_pending(
        self,
        *,
        max_videos: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        references = await self._repository.list_analysis_candidates(
            profile_name=self._settings.agent.profile_name,
            schema_version=self._settings.agent.schema_version,
            prompt_version=self._settings.prompt_version,
            prompt_sha256=self._settings.prompt_sha256,
            translation_schema_sha256=self._settings.translation_schema_sha256,
            schema_sha256=self._settings.analysis_schema_sha256,
            force=force,
            limit=max_videos,
        )
        results: list[ProcessResult] = []
        for reference in references:
            try:
                results.append(
                    await self.analyze_video(
                        reference.youtube_video_id,
                        force=force,
                    )
                )
            except Exception as exc:
                LOGGER.exception("Failed to analyze %s", reference.video_url)
                results.append(
                    ProcessResult(
                        video_url=reference.video_url,
                        youtube_video_id=reference.youtube_video_id,
                        status="failed",
                        detail=str(exc),
                    )
                )
        return results

    async def run_channels(
        self,
        channel_urls: Sequence[str] | None,
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        downloads = await self.download_channels(
            channel_urls,
            max_videos_per_channel=max_videos_per_channel,
            force=force,
        )
        analyses = await self.analyze_pending(max_videos=None, force=force)
        return [*downloads, *analyses]

    async def run_all_channels(
        self,
        *,
        max_videos_per_channel: int | None,
        force: bool,
    ) -> list[ProcessResult]:
        return await self.run_channels(
            None,
            max_videos_per_channel=max_videos_per_channel,
            force=force,
        )

    async def _process_channels(
        self,
        channels: Sequence[ChannelRecord],
        *,
        max_videos_per_channel: int | None,
        force: bool,
        limiter: asyncio.Semaphore | None = None,
    ) -> list[ProcessResult]:
        if not channels:
            return []

        limiter = limiter or asyncio.Semaphore(
            self._settings.pipeline.download_concurrency
        )
        channel_ids = _unique([channel.id for channel in channels])
        known_video_ids_by_channel = (
            {}
            if force
            else await self._repository.list_known_video_ids(channel_ids)
        )

        async def discover(
            channel: ChannelRecord,
        ) -> tuple[list[VideoReference], ProcessResult | None]:
            known_video_ids = known_video_ids_by_channel.get(channel.id, set())
            initial_backfill = channel.initial_backfill_completed_at is None
            mode = (
                "forced refresh"
                if force
                else "initial backfill"
                if initial_backfill
                else "incremental"
            )
            stop_at_known = not force and not initial_backfill
            LOGGER.info(
                "Channel discovery started: %s [channel_id=%s, mode=%s, "
                "known_videos=%d, limit=%s]",
                channel.title,
                channel.youtube_channel_id,
                mode,
                len(known_video_ids),
                max_videos_per_channel
                if max_videos_per_channel is not None
                else "unlimited",
            )
            try:
                async with limiter:
                    discovery = await self._youtube.discover_channel_videos(
                        channel.youtube_channel_id,
                        max_videos_per_channel,
                        known_video_ids=known_video_ids,
                        stop_at_known=stop_at_known,
                    )
                references = discovery.references
                await self._repository.enqueue_subtitle_downloads(
                    channel.id,
                    references,
                )
                if discovery.source_exhausted and initial_backfill:
                    await self._repository.mark_channel_backfill_completed(channel.id)
                LOGGER.info(
                    "Channel discovery completed: %s [channel_id=%s, mode=%s, "
                    "discovered_videos=%d, stop_reason=%s]",
                    channel.title,
                    channel.youtube_channel_id,
                    mode,
                    len(references),
                    _discovery_stop_reason(
                        source_exhausted=discovery.source_exhausted,
                        stopped_at_known=discovery.stopped_at_known,
                    ),
                )
                return references, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = _exception_detail(exc)
                LOGGER.error(
                    "Channel discovery failed: %s [channel_id=%s]: %s",
                    channel.title,
                    channel.youtube_channel_id,
                    detail,
                )
                LOGGER.debug(
                    "Channel discovery traceback: %s [channel_id=%s]",
                    channel.title,
                    channel.youtube_channel_id,
                    exc_info=True,
                )
                return [], ProcessResult(
                    video_url=channel.channel_url,
                    youtube_video_id=None,
                    status="failed",
                    detail=detail,
                )
            finally:
                await self._repository.mark_channel_checked(channel.id)

        discoveries = await asyncio.gather(*(discover(channel) for channel in channels))
        results = [failure for _, failure in discoveries if failure is not None]
        force_video_ids = (
            [
                reference.youtube_video_id
                for references, failure in discoveries
                if failure is None
                for reference in references
            ]
            if force
            else []
        )

        tasks = await self._repository.list_subtitle_download_candidates(
            channel_ids,
            force_video_ids,
        )
        forced_video_ids = set(force_video_ids)
        pending_count = sum(
            task.status is SubtitleDownloadStatus.PENDING
            and task.youtube_video_id not in forced_video_ids
            for task in tasks
        )
        retry_count = sum(
            task.status is SubtitleDownloadStatus.FAILED
            and task.youtube_video_id not in forced_video_ids
            for task in tasks
        )
        forced_count = sum(
            task.youtube_video_id in forced_video_ids for task in tasks
        )
        if tasks:
            LOGGER.info(
                "Subtitle download queue ready: total=%d, pending=%d, "
                "retry=%d, forced=%d",
                len(tasks),
                pending_count,
                retry_count,
                forced_count,
            )
        else:
            LOGGER.info(
                "Subtitle download queue is empty: pending=0, retry=0, forced=0"
            )
        download_results = await self._download_tasks(
            tasks,
            force=force,
            limiter=limiter,
            forced_video_ids=forced_video_ids,
        )
        results.extend(download_results)
        return results

    async def _download_tasks(
        self,
        tasks: Sequence[SubtitleDownloadTask],
        *,
        force: bool,
        limiter: asyncio.Semaphore,
        forced_video_ids: set[str],
    ) -> list[ProcessResult]:
        results: list[ProcessResult | None] = [None] * len(tasks)
        indexed_tasks = iter(enumerate(tasks))

        async def worker() -> None:
            for index, task in indexed_tasks:
                queue_kind = _subtitle_queue_kind(task, forced_video_ids)
                LOGGER.info(
                    "Subtitle download started: %s [video_id=%s, queue=%s]",
                    task.title,
                    task.youtube_video_id,
                    queue_kind,
                )
                try:
                    async with limiter:
                        result = await self.download_video(
                            task.video_url,
                            youtube_video_id=task.youtube_video_id,
                            force=force,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = _exception_detail(exc)
                    LOGGER.error(
                        "Subtitle download failed: %s [video_id=%s, queue=%s]: %s",
                        task.title,
                        task.youtube_video_id,
                        queue_kind,
                        detail,
                    )
                    LOGGER.debug(
                        "Subtitle download traceback: %s [video_id=%s]",
                        task.title,
                        task.youtube_video_id,
                        exc_info=True,
                    )
                    try:
                        await self._repository.mark_subtitle_download_failed(
                            task.youtube_video_id,
                            detail,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as persistence_exc:
                        persistence_detail = _exception_detail(persistence_exc)
                        LOGGER.error(
                            "Could not save subtitle failure state: %s "
                            "[video_id=%s]: %s",
                            task.title,
                            task.youtube_video_id,
                            persistence_detail,
                        )
                        LOGGER.debug(
                            "Subtitle failure persistence traceback: %s "
                            "[video_id=%s]",
                            task.title,
                            task.youtube_video_id,
                            exc_info=True,
                        )
                        detail = (
                            f"{detail}; could not persist failure state: "
                            f"{persistence_detail}"
                        )
                    result = ProcessResult(
                        video_url=task.video_url,
                        youtube_video_id=task.youtube_video_id,
                        status="failed",
                        detail=detail,
                    )
                else:
                    _log_subtitle_download_result(task, result, queue_kind)
                results[index] = result

        worker_count = min(
            len(tasks),
            self._settings.pipeline.download_concurrency,
        )
        await asyncio.gather(*(worker() for _ in range(worker_count)))
        return [result for result in results if result is not None]


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


def _copied_chinese_translation(
    analysis: AnalysisEngine,
    fetched: FetchedVideo,
) -> TranslationResult | None:
    subtitle = fetched.subtitle
    if subtitle is None or subtitle.normalized_text is None:
        return None
    return analysis.copy_chinese_source(
        subtitle.language_code,
        subtitle.normalized_text,
    )


def _unique(values: Sequence[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))


def _subtitle_download_detail(fetched: FetchedVideo) -> str | None:
    subtitle = fetched.subtitle
    if subtitle is None:
        return None
    source = "automatic" if subtitle.is_auto_generated else "manual"
    return (
        f"language={subtitle.language_code}, source={source}, "
        f"format={subtitle.source_format}"
    )


def _subtitle_queue_kind(
    task: SubtitleDownloadTask,
    forced_video_ids: set[str],
) -> str:
    if task.youtube_video_id in forced_video_ids:
        return "forced"
    if task.status is SubtitleDownloadStatus.FAILED:
        return "retry"
    return "pending"


def _log_subtitle_download_result(
    task: SubtitleDownloadTask,
    result: ProcessResult,
    queue_kind: str,
) -> None:
    context = (
        f"{task.title} [video_id={task.youtube_video_id}, queue={queue_kind}]"
    )
    if result.status == "downloaded":
        LOGGER.info(
            "Subtitle downloaded: %s: %s",
            context,
            result.detail or "subtitle metadata unavailable",
        )
    elif result.status == "no_subtitle":
        LOGGER.info(
            "No matching subtitle: %s: subtitle check completed; no subtitle "
            "matched the configured language and format priorities",
            context,
        )
    elif result.status == "invalid_subtitle":
        LOGGER.warning(
            "Subtitle downloaded but could not be normalized: %s",
            context,
        )
    elif result.status == "skipped":
        LOGGER.info("Subtitle download skipped: %s: %s", context, result.detail)
    elif result.status == "failed":
        LOGGER.error("Subtitle download failed: %s: %s", context, result.detail)
    else:
        LOGGER.info(
            "Subtitle download finished: %s: status=%s",
            context,
            result.status,
        )


def _discovery_stop_reason(*, source_exhausted: bool, stopped_at_known: bool) -> str:
    if stopped_at_known:
        return "known video boundary reached"
    if source_exhausted:
        return "channel source exhausted"
    return "batch limit reached"


def _exception_detail(exc: Exception) -> str:
    return str(exc).strip() or type(exc).__name__
