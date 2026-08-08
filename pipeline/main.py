from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from yt_dlp.utils import DownloadError

from agent import CodexStructuredAgent
from analysis import AnalysisEngine
from config import DEFAULT_CONFIG_PATH, RuntimeSettings, load_settings
from database import PipelineAlreadyRunningError, PipelineRepository
from service import PipelineService
from youtube import YoutubeClient, YoutubeMetadataError

LOGGER = logging.getLogger(__name__)


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _add_channel_arguments(
    parser: argparse.ArgumentParser,
    *,
    force_help: str,
) -> None:
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="URL",
        help="YouTube channel URL; may be repeated",
    )
    parser.add_argument("--max-videos-per-channel", type=_non_negative_int)
    parser.add_argument("--force", action="store_true", help=force_help)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch and analyze YouTube subtitles")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Pipeline TOML configuration file",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="Create the database and apply migrations")
    subparsers.add_parser("config-check", help="Validate configuration without side effects")

    channel_add = subparsers.add_parser("channel-add", help="Register a YouTube channel")
    channel_add.add_argument("channel_url", help="YouTube channel URL, handle, or channel ID")
    channel_add.add_argument("--researcher", help="Researcher display name")

    channel_inspect = subparsers.add_parser(
        "channel-inspect",
        help="Validate a YouTube channel and print its metadata without writing",
    )
    channel_inspect.add_argument(
        "channel_reference",
        help="YouTube channel URL, handle, or channel ID",
    )

    video = subparsers.add_parser("video", help="Fetch and analyze one video")
    video.add_argument("video_url")
    video.add_argument("--force", action="store_true", help="Create a new analysis revision")

    download = subparsers.add_parser(
        "download",
        help="Fetch subtitles for specified channels or active database channels",
    )
    _add_channel_arguments(download, force_help="Refetch video metadata and subtitles")

    analyze = subparsers.add_parser(
        "analyze",
        help="Analyze subtitles already stored in the database",
    )
    analyze.add_argument(
        "--limit",
        type=_non_negative_int,
        default=0,
        help="Maximum videos to analyze; 0 means unlimited",
    )
    analyze.add_argument("--force", action="store_true", help="Create new analysis revisions")

    run = subparsers.add_parser(
        "run",
        help="Process specified channels or active database channels",
    )
    _add_channel_arguments(run, force_help="Refetch subtitles and create new analyses")
    return parser


async def _migrate(settings: RuntimeSettings) -> None:
    process = await asyncio.create_subprocess_exec(
        str(settings.project_root / "db" / "migrate.sh"),
        cwd=settings.project_root,
        env=os.environ.copy(),
    )
    return_code = await process.wait()
    if return_code != 0:
        raise RuntimeError(f"Database migration failed with exit code {return_code}")


def _service(settings: RuntimeSettings, repository: PipelineRepository) -> PipelineService:
    agent = CodexStructuredAgent(settings.agent)
    return PipelineService(
        settings,
        repository,
        YoutubeClient(settings.youtube),
        AnalysisEngine(settings, agent),
    )


def _max_videos_per_channel(
    args: argparse.Namespace,
    settings: RuntimeSettings,
) -> int | None:
    value = (
        args.max_videos_per_channel
        if args.max_videos_per_channel is not None
        else settings.youtube.max_videos_per_channel
    )
    if value < 0:
        raise ValueError("max-videos-per-channel must not be negative")
    return value or None


def _results_exit_code(results: list[object]) -> int:
    return 1 if any(getattr(result, "status", None) == "failed" for result in results) else 0


async def _run(args: argparse.Namespace) -> int:
    settings = await load_settings(config_path=args.config)
    if args.command == "config-check":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "profile": settings.agent.profile_name,
                    "schema_version": settings.agent.schema_version,
                    "schema_sha256": settings.analysis_schema_sha256,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "channel-inspect":
        try:
            channel = await YoutubeClient(settings.youtube).inspect_channel(args.channel_reference)
        except (DownloadError, YoutubeMetadataError):
            print(json.dumps({"error": "channel_not_found"}, ensure_ascii=False))
            return 2
        print(json.dumps(asdict(channel), ensure_ascii=False))
        return 0

    await _migrate(settings)
    if args.command == "migrate":
        return 0

    repository = PipelineRepository(settings.database)
    try:
        await repository.connect()
        service = _service(settings, repository)
        if args.command == "channel-add":
            channel_id = await service.add_channel(args.channel_url, args.researcher)
            print(json.dumps({"channel_id": str(channel_id)}, ensure_ascii=False))
            return 0
        if args.command == "download":
            async with repository.process_lock("download"):
                results = await service.download_channels(
                    args.channel,
                    max_videos_per_channel=_max_videos_per_channel(args, settings),
                    force=args.force,
                )
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
            return _results_exit_code(results)
        if args.command == "analyze":
            if args.limit < 0:
                raise ValueError("limit must not be negative")
            async with repository.process_lock("analysis"):
                results = await service.analyze_pending(
                    max_videos=args.limit or None,
                    force=args.force,
                )
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
            return _results_exit_code(results)
        if args.command == "video":
            stage_results = []
            async with repository.process_lock("download"):
                download_result = await service.download_video(
                    args.video_url,
                    force=args.force,
                )
            stage_results.append(download_result)
            result = download_result
            if download_result.youtube_video_id is not None:
                async with repository.process_lock("analysis"):
                    result = await service.analyze_video(
                        download_result.youtube_video_id,
                        force=args.force,
                    )
                stage_results.append(result)
            print(json.dumps(asdict(result), ensure_ascii=False))
            return _results_exit_code(stage_results)
        if args.command == "run":
            results = []
            async with repository.process_lock("download"):
                results.extend(
                    await service.download_channels(
                        args.channel,
                        max_videos_per_channel=_max_videos_per_channel(args, settings),
                        force=args.force,
                    )
                )
            async with repository.process_lock("analysis"):
                results.extend(
                    await service.analyze_pending(max_videos=None, force=args.force)
                )
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
            return _results_exit_code(results)
    finally:
        await repository.close()

    raise RuntimeError(f"Unsupported command: {args.command}")


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except PipelineAlreadyRunningError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from None
    except Exception:
        LOGGER.exception("Pipeline command failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
