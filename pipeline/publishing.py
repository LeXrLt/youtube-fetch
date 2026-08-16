from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from uuid import UUID

from database import PipelineRepository
from models import (
    PublicationResult,
    PublicationSource,
    PublicationStep,
    PublicationStepInput,
)
from portal import (
    CreatedComment,
    CreatedTopic,
    PortalBusinessError,
    PortalClient,
    PortalConfigurationError,
    PortalSession,
    PortalWriteUncertainError,
    VerifiedComment,
    VerifiedTopic,
    build_publication_content,
)

CATEGORY_NAME = "Youtube"
TARGET_KEY = "portal-push:Youtube"
LOGGER = logging.getLogger(__name__)


class PublicationStateError(RuntimeError):
    """Raised when a persisted publication cannot be advanced automatically."""


class PublicationUncertainError(PublicationStateError):
    """Raised when replaying a non-idempotent write could create a duplicate."""


PortalClientFactory = Callable[[], AbstractAsyncContextManager[PortalClient]]


class BbsPublicationBuilder:
    target_key = TARGET_KEY

    def build_steps(
        self,
        source: PublicationSource,
    ) -> Sequence[PublicationStepInput]:
        fetched = source.fetched
        subtitle = fetched.subtitle
        if subtitle is None or subtitle.normalized_text is None:
            raise ValueError("A normalized subtitle is required for BBS publication")
        content = build_publication_content(
            fetched.metadata,
            source.outcome,
            source.translated_text,
            subtitle.language_code,
            subtitle.normalized_text,
        )
        return (
            PublicationStepInput(
                target_key=self.target_key,
                step="topic",
                topic_title=content.title,
                markdown_snapshot=content.topic_markdown,
            ),
            PublicationStepInput(
                target_key=self.target_key,
                step="translation",
                topic_title=None,
                markdown_snapshot=content.translated_comment_markdown,
            ),
            PublicationStepInput(
                target_key=self.target_key,
                step="source",
                topic_title=None,
                markdown_snapshot=content.source_comment_markdown,
                skipped=content.source_comment_markdown is None,
            ),
        )


class BbsPushConsumer:
    def __init__(
        self,
        repository: PipelineRepository,
        publisher: BbsPublisher,
        builder: BbsPublicationBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._builder = builder or BbsPublicationBuilder()

    async def consume(self, *, limit: int | None) -> list[PublicationResult]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        limit = limit or None
        results: list[PublicationResult] = []
        blocked_video_ids: set[str] = set()
        attempted_candidates = 0
        candidates = await self._repository.list_publication_candidates(
            self._publisher.target_key,
        )
        for candidate in candidates:
            if candidate.youtube_video_id in blocked_video_ids:
                continue
            diagnostic_only = candidate.requires_reconciliation
            if not diagnostic_only and limit is not None and attempted_candidates >= limit:
                continue
            if not diagnostic_only:
                attempted_candidates += 1
            try:
                steps = await self._repository.list_publication_steps(
                    candidate.video_analysis_id,
                    self._publisher.target_key,
                )
                if diagnostic_only:
                    reconciliation_step = next(
                        (
                            step
                            for step in steps
                            if step.status in {"in_progress", "uncertain"}
                        ),
                        None,
                    )
                    if reconciliation_step is None:
                        raise PublicationStateError(
                            "The publication candidate no longer matches its "
                            "reconciliation snapshot"
                        )
                    if reconciliation_step.status == "uncertain":
                        raise PublicationUncertainError(
                            _uncertain_message(
                                candidate.video_analysis_id,
                                reconciliation_step.step,
                            )
                        )
                if not steps:
                    source = await self._repository.get_publication_source(
                        candidate.video_analysis_id
                    )
                    if source is None:
                        raise PublicationStateError(
                            "The persisted analysis cannot be reconstructed for publication"
                        )
                    await self._repository.ensure_publication_steps(
                        candidate.video_analysis_id,
                        self._builder.build_steps(source),
                    )
                published = await self._publisher.publish(candidate.video_analysis_id)
                if diagnostic_only:
                    raise PublicationStateError(
                        "A reconciliation-only publication candidate advanced unexpectedly"
                    )
                results.append(
                    PublicationResult(
                        video_analysis_id=candidate.video_analysis_id,
                        youtube_video_id=candidate.youtube_video_id,
                        video_url=candidate.video_url,
                        status="published" if published else "skipped",
                        detail=(
                            "BBS publication completed"
                            if published
                            else "BBS publication was already complete"
                        ),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                blocked_video_ids.add(candidate.youtube_video_id)
                LOGGER.exception(
                    "Failed to publish analysis %s for video %s",
                    candidate.video_analysis_id,
                    candidate.youtube_video_id,
                )
                results.append(
                    PublicationResult(
                        video_analysis_id=candidate.video_analysis_id,
                        youtube_video_id=candidate.youtube_video_id,
                        video_url=candidate.video_url,
                        status="failed",
                        detail=str(exc),
                    )
                )
        return results


class BbsPublisher:
    target_key = TARGET_KEY

    def __init__(
        self,
        repository: PipelineRepository,
        *,
        env_path: Path | None = None,
        client_factory: PortalClientFactory | None = None,
    ) -> None:
        self._repository = repository
        resolved_env_path = env_path or default_portal_env_path()
        self._client_factory = client_factory or (lambda: PortalClient(resolved_env_path))

    async def publish(self, video_analysis_id: UUID) -> bool:
        steps = await self._repository.list_publication_steps(
            video_analysis_id,
            self.target_key,
        )
        if not steps:
            return False
        steps_by_name = _validate_steps(steps)
        if all(step.status in {"succeeded", "skipped"} for step in steps_by_name.values()):
            return False
        await self._reject_uncertain_or_interrupted_steps(steps)

        async with self._client_factory() as client:
            _validate_portal_origin(steps, client.configured_origin)
            session = await client.prepare(CATEGORY_NAME)
            _validate_portal_target(steps, session)
            topic = await self._publish_topic(
                client,
                session,
                steps_by_name["topic"],
            )
            await self._publish_comment(
                client,
                topic.topic_id,
                steps_by_name["translation"],
            )
            await self._publish_comment(
                client,
                topic.topic_id,
                steps_by_name["source"],
            )
        return True

    async def _reject_uncertain_or_interrupted_steps(
        self,
        steps: Sequence[PublicationStep],
    ) -> None:
        for step in steps:
            if step.status == "uncertain":
                raise PublicationUncertainError(
                    _uncertain_message(step.video_analysis_id, step.step)
                )
            if step.status != "in_progress":
                continue
            message = (
                "A previous process stopped while the non-idempotent portal write was "
                "in progress; remote reconciliation is required"
            )
            await _finish_despite_cancellation(
                self._repository.mark_publication_step_failed(
                    step.video_analysis_id,
                    step.target_key,
                    step.step,
                    message,
                    uncertain=True,
                )
            )
            raise PublicationUncertainError(_uncertain_message(step.video_analysis_id, step.step))

    async def _publish_topic(
        self,
        client: PortalClient,
        session: PortalSession,
        step: PublicationStep,
    ) -> VerifiedTopic:
        title = step.topic_title
        markdown = step.markdown_snapshot
        if title is None or markdown is None:
            raise PublicationStateError("The topic publication snapshot is incomplete")

        if step.status in {"pending", "claimed", "failed"}:
            await self._claim_step(
                step,
                session,
                request_metadata={
                    "content_type": "markdown",
                    "tags": [],
                },
            )
            created = await self._create_topic(client, step, title, markdown)
            return await self._verify_created_topic(client, step, title, created)

        if step.status in {"created", "succeeded"}:
            if step.remote_topic_id is None:
                raise PublicationStateError("The persisted topic ID is missing")
            verified = await client.verify_topic(step.remote_topic_id, title)
            if step.status == "created":
                await self._record_topic_succeeded(step, verified)
            return verified

        raise PublicationStateError(f"The topic step cannot run from status={step.status}")

    async def _create_topic(
        self,
        client: PortalClient,
        step: PublicationStep,
        title: str,
        markdown: str,
    ) -> CreatedTopic:
        write_started = False

        async def start_write() -> None:
            nonlocal write_started
            await self._start_write(step)
            write_started = True

        try:
            created = await client.create_topic(
                title,
                markdown,
                tags=(),
                before_write=start_write,
            )
        except asyncio.CancelledError:
            if write_started:
                await self._record_write_failure(
                    step,
                    "Portal topic creation was cancelled with an unknown remote result",
                    uncertain=True,
                )
            raise
        except PortalWriteUncertainError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=True)
            raise
        except PortalBusinessError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=False)
            raise
        except PortalConfigurationError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=True)
            raise
        except Exception as exc:
            if write_started:
                await self._record_write_failure(
                    step,
                    f"Unexpected portal topic creation failure: {type(exc).__name__}",
                    uncertain=True,
                )
            raise

        await _finish_despite_cancellation(
            self._repository.mark_publication_step_created(
                step.video_analysis_id,
                step.target_key,
                step.step,
                remote_topic_id=created.topic_id,
                remote_comment_id=None,
                remote_status=created.status,
                response_metadata=_created_topic_metadata(created),
            )
        )
        return created

    async def _verify_created_topic(
        self,
        client: PortalClient,
        step: PublicationStep,
        title: str,
        created: CreatedTopic,
    ) -> VerifiedTopic:
        verified = await client.verify_topic(created.topic_id, title)
        await self._record_topic_succeeded(step, verified)
        return verified

    async def _record_topic_succeeded(
        self,
        step: PublicationStep,
        verified: VerifiedTopic,
    ) -> None:
        await _finish_despite_cancellation(
            self._repository.mark_publication_step_succeeded(
                step.video_analysis_id,
                step.target_key,
                step.step,
                remote_status=verified.status,
                response_metadata=_verified_topic_metadata(verified),
            )
        )

    async def _publish_comment(
        self,
        client: PortalClient,
        topic_id: str,
        step: PublicationStep,
    ) -> VerifiedComment | None:
        if step.status == "skipped":
            if step.step != "source":
                raise PublicationStateError("Only the source comment may be skipped")
            return None
        markdown = step.markdown_snapshot
        if markdown is None:
            raise PublicationStateError(f"The {step.step} comment snapshot is incomplete")

        if step.status in {"pending", "claimed", "failed"}:
            session = client.session
            if session is None:
                raise PublicationStateError("The portal session is not prepared")
            await self._claim_step(
                step,
                session,
                request_metadata={"content_type": "markdown", "image_list": []},
            )
            created = await self._create_comment(client, topic_id, step, markdown)
            return await self._verify_created_comment(client, topic_id, step, created)

        if step.status in {"created", "succeeded"}:
            if step.remote_comment_id is None:
                raise PublicationStateError("The persisted comment ID is missing")
            rendered_content = step.response_metadata.get("rendered_content")
            image_list = step.response_metadata.get("image_list", [])
            content_type = step.response_metadata.get("content_type", "markdown")
            if not isinstance(rendered_content, str) or not isinstance(image_list, list):
                raise PublicationStateError(
                    f"The persisted {step.step} comment verification data is incomplete"
                )
            if not isinstance(content_type, str):
                raise PublicationStateError("The persisted comment content type is invalid")
            verified = await client.verify_comment(
                topic_id,
                step.remote_comment_id,
                expected_rendered_content=rendered_content,
                expected_content_type=content_type,
                expected_image_list=image_list,
            )
            if step.status == "created":
                await self._record_comment_succeeded(step, verified)
            return verified

        raise PublicationStateError(f"The {step.step} comment cannot run from status={step.status}")

    async def _create_comment(
        self,
        client: PortalClient,
        topic_id: str,
        step: PublicationStep,
        markdown: str,
    ) -> CreatedComment:
        write_started = False

        async def start_write() -> None:
            nonlocal write_started
            await self._start_write(step)
            write_started = True

        try:
            created = await client.create_comment(
                topic_id,
                markdown,
                content_type="markdown",
                before_write=start_write,
            )
        except asyncio.CancelledError:
            if write_started:
                await self._record_write_failure(
                    step,
                    f"Portal {step.step} comment creation was cancelled with an unknown result",
                    uncertain=True,
                )
            raise
        except PortalWriteUncertainError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=True)
            raise
        except PortalBusinessError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=False)
            raise
        except PortalConfigurationError as exc:
            if write_started:
                await self._record_write_failure(step, str(exc), uncertain=True)
            raise
        except Exception as exc:
            if write_started:
                await self._record_write_failure(
                    step,
                    f"Unexpected portal comment creation failure: {type(exc).__name__}",
                    uncertain=True,
                )
            raise

        await _finish_despite_cancellation(
            self._repository.mark_publication_step_created(
                step.video_analysis_id,
                step.target_key,
                step.step,
                remote_topic_id=None,
                remote_comment_id=created.comment_id,
                remote_status=created.status,
                response_metadata=_created_comment_metadata(created),
            )
        )
        return created

    async def _claim_step(
        self,
        step: PublicationStep,
        session: PortalSession,
        *,
        request_metadata: dict[str, object],
    ) -> None:
        claimed = await self._repository.claim_publication_step(
            step.video_analysis_id,
            step.target_key,
            step.step,
            {
                **request_metadata,
                "portal_target": _portal_target(session),
            },
        )
        if not claimed:
            raise PublicationStateError(f"The {step.step} publication step could not be claimed")

    async def _start_write(self, step: PublicationStep) -> None:
        started = await self._repository.mark_publication_step_in_progress(
            step.video_analysis_id,
            step.target_key,
            step.step,
        )
        if not started:
            raise PublicationStateError(f"The {step.step} publication write could not be started")

    async def _verify_created_comment(
        self,
        client: PortalClient,
        topic_id: str,
        step: PublicationStep,
        created: CreatedComment,
    ) -> VerifiedComment:
        verified = await client.verify_comment(topic_id, created)
        await self._record_comment_succeeded(step, verified)
        return verified

    async def _record_comment_succeeded(
        self,
        step: PublicationStep,
        verified: VerifiedComment,
    ) -> None:
        await _finish_despite_cancellation(
            self._repository.mark_publication_step_succeeded(
                step.video_analysis_id,
                step.target_key,
                step.step,
                remote_status=verified.status,
                response_metadata={
                    "verified": True,
                    "content_type": verified.content_type,
                    "image_list": list(verified.image_list),
                },
            )
        )

    async def _record_write_failure(
        self,
        step: PublicationStep,
        message: str,
        *,
        uncertain: bool,
    ) -> None:
        await _finish_despite_cancellation(
            self._repository.mark_publication_step_failed(
                step.video_analysis_id,
                step.target_key,
                step.step,
                message,
                uncertain=uncertain,
            )
        )


def default_portal_env_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return codex_home / "skills" / "portal-push" / ".env"


def _validate_steps(steps: Sequence[PublicationStep]) -> dict[str, PublicationStep]:
    steps_by_name = {step.step: step for step in steps}
    if len(steps) != 3 or set(steps_by_name) != {"topic", "translation", "source"}:
        raise PublicationStateError(
            "A BBS publication must contain exactly topic, translation, and source steps"
        )
    if any(step.target_key != TARGET_KEY for step in steps):
        raise PublicationStateError("BBS publication steps have an unexpected target")
    analysis_ids = {step.video_analysis_id for step in steps}
    if len(analysis_ids) != 1:
        raise PublicationStateError("BBS publication steps span multiple analyses")
    return steps_by_name


def _portal_target(session: PortalSession) -> dict[str, object]:
    return {
        "origin": session.origin,
        "user_id": session.user_id,
        "username": session.username,
        "category_id": session.category_id,
        "category_name": session.category_name,
    }


def _validate_portal_target(
    steps: Sequence[PublicationStep],
    session: PortalSession,
) -> None:
    expected = _portal_target(session)
    for step in steps:
        if step.status in {"pending", "skipped"}:
            continue
        persisted = step.request_metadata.get("portal_target")
        if persisted != expected:
            raise PublicationStateError(
                "The configured portal target does not match the target bound to "
                f"analysis={step.video_analysis_id} step={step.step}"
            )


def _validate_portal_origin(
    steps: Sequence[PublicationStep],
    configured_origin: str,
) -> None:
    for step in steps:
        if step.status in {"pending", "skipped"}:
            continue
        persisted = step.request_metadata.get("portal_target")
        if not isinstance(persisted, dict) or persisted.get("origin") != configured_origin:
            raise PublicationStateError(
                "The configured portal origin does not match the target bound to "
                f"analysis={step.video_analysis_id} step={step.step}"
            )


async def _finish_despite_cancellation(awaitable: Awaitable[None]) -> None:
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


def _created_topic_metadata(topic: CreatedTopic) -> dict[str, object]:
    return {
        "title": topic.title,
        "category_id": topic.category_id,
        "category_name": topic.category_name,
        "tags": list(topic.tags),
    }


def _verified_topic_metadata(topic: VerifiedTopic) -> dict[str, object]:
    return {
        "verified": True,
        "title": topic.title,
        "category_id": topic.category_id,
        "category_name": topic.category_name,
    }


def _created_comment_metadata(comment: CreatedComment) -> dict[str, object]:
    return {
        "content_type": comment.content_type,
        "rendered_content": comment.rendered_content,
        "image_list": list(comment.image_list),
    }


def _uncertain_message(video_analysis_id: UUID, step: str) -> str:
    return (
        f"BBS publication {video_analysis_id} step={step} has an uncertain remote result; "
        "reconcile it on the portal before retrying"
    )
