from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from agent import CodexStructuredAgent
from analysis import AnalysisEngine
from config import DEFAULT_CONFIG_PATH, RuntimeSettings, load_settings
from database import PipelineRepository
from service import PipelineService
from youtube import YoutubeClient

LOGGER = logging.getLogger(__name__)


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
    channel_add.add_argument("channel_url")
    channel_add.add_argument("--researcher", help="Researcher display name")

    video = subparsers.add_parser("video", help="Fetch and analyze one video")
    video.add_argument("video_url")
    video.add_argument("--force", action="store_true", help="Create a new analysis revision")

    run = subparsers.add_parser(
        "run",
        help="Process specified channels or the authenticated subscriptions",
    )
    run.add_argument(
        "--channel",
        action="append",
        default=[],
        metavar="URL",
        help="YouTube channel URL; may be repeated",
    )
    run.add_argument("--max-videos-per-channel", type=int)
    run.add_argument("--force", action="store_true", help="Create new analysis revisions")
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

    await _migrate(settings)
    if args.command == "migrate":
        return 0

    repository = PipelineRepository(settings.database)
    await repository.connect()
    try:
        service = _service(settings, repository)
        if args.command == "channel-add":
            channel_id = await service.add_channel(args.channel_url, args.researcher)
            print(json.dumps({"channel_id": str(channel_id)}, ensure_ascii=False))
            return 0
        if args.command == "video":
            result = await service.process_video(args.video_url, force=args.force)
            print(json.dumps(asdict(result), ensure_ascii=False))
            return 0
        if args.command == "run":
            max_videos = (
                args.max_videos_per_channel
                if args.max_videos_per_channel is not None
                else settings.youtube.max_videos_per_channel
            )
            if max_videos < 0:
                raise ValueError("max-videos-per-channel must not be negative")
            results = await service.run_channels(
                args.channel,
                max_videos_per_channel=max_videos or None,
                force=args.force,
            )
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
            return 1 if any(result.status == "failed" for result in results) else 0
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
    except Exception:
        LOGGER.exception("Pipeline command failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
