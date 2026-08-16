from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from models import (
    AnalysisOutcome,
    AnalysisProjection,
    ChannelMetadata,
    DownloadedSubtitle,
    FetchedVideo,
    PublicationCandidate,
    PublicationSource,
    PublicationStep,
    TranslationResult,
    VideoMetadata,
)
from portal import (
    CreatedComment,
    CreatedTopic,
    PortalBusinessError,
    PortalConfigurationError,
    PortalSession,
    PortalTopicPendingReviewError,
    PortalVerificationError,
    PortalWriteUncertainError,
    VerifiedComment,
    VerifiedTopic,
)
from publishing import (
    TARGET_KEY,
    BbsPublicationBuilder,
    BbsPublisher,
    BbsPushConsumer,
    PublicationStateError,
    PublicationUncertainError,
)

ANALYSIS_ID = UUID("11111111-1111-1111-1111-111111111111")
TOPIC_ID = "opaque-topic-id"
BOUND_TARGET = {
    "origin": "https://portal.test",
    "user_id": "encoded-user-id",
    "username": "publisher",
    "category_id": 7,
    "category_name": "Youtube",
}


def _step(
    name: str,
    *,
    status: str = "pending",
    remote_topic_id: str | None = None,
    remote_comment_id: int | None = None,
    remote_status: int | None = None,
    request_metadata: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> PublicationStep:
    snapshots = {
        "topic": ("A topic", "# Topic snapshot"),
        "translation": (None, "Translated snapshot"),
        "source": (None, "Source snapshot"),
    }
    title, markdown = snapshots[name]
    return PublicationStep(
        video_analysis_id=ANALYSIS_ID,
        target_key=TARGET_KEY,
        step=name,  # type: ignore[arg-type]
        topic_title=title,
        markdown_snapshot=markdown,
        content_sha256="snapshot-sha256",
        status=status,  # type: ignore[arg-type]
        remote_topic_id=remote_topic_id,
        remote_comment_id=remote_comment_id,
        remote_status=remote_status,
        attempt_count=0,
        error_message=None,
        request_metadata=request_metadata or {},
        response_metadata=response_metadata or {},
        started_at=None,
        completed_at=None,
    )


def _pending_steps() -> list[PublicationStep]:
    return [_step("topic"), _step("translation"), _step("source")]


class FakeRepository:
    def __init__(
        self,
        steps: Sequence[PublicationStep],
        events: list[str] | None = None,
    ) -> None:
        self.steps = {step.step: step for step in steps}
        self.events = events if events is not None else []

    def _replace(self, name: str, **changes: object) -> None:
        self.steps[name] = replace(self.steps[name], **changes)

    async def list_publication_steps(
        self, video_analysis_id: UUID, target_key: str
    ) -> list[PublicationStep]:
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        self.events.append("repo:list")
        return list(self.steps.values())

    async def claim_publication_step(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> bool:
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        current = self.steps[step]
        if current.status not in {"pending", "claimed", "failed"}:
            return False
        self.events.append(f"repo:claim:{step}")
        self._replace(
            step,
            status="claimed",
            attempt_count=(
                current.attempt_count if current.status == "claimed" else current.attempt_count + 1
            ),
            error_message=None,
            request_metadata={**current.request_metadata, **(request_metadata or {})},
        )
        return True

    async def mark_publication_step_in_progress(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
    ) -> bool:
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        if self.steps[step].status != "claimed":
            return False
        self.events.append(f"repo:start:{step}")
        self._replace(step, status="in_progress")
        return True

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
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        assert self.steps[step].status == "in_progress"
        if step == "topic":
            assert isinstance(remote_topic_id, str)
            assert remote_comment_id is None
        else:
            assert remote_topic_id is None
            assert isinstance(remote_comment_id, int)
        self.events.append(f"repo:created:{step}")
        self._replace(
            step,
            status="created",
            remote_topic_id=remote_topic_id,
            remote_comment_id=remote_comment_id,
            remote_status=remote_status,
            response_metadata={**self.steps[step].response_metadata, **response_metadata},
        )

    async def mark_publication_step_succeeded(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        *,
        remote_status: int | None,
        response_metadata: dict[str, Any],
    ) -> None:
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        assert self.steps[step].status == "created"
        self.events.append(f"repo:succeeded:{step}")
        self._replace(
            step,
            status="succeeded",
            remote_status=remote_status,
            response_metadata={**self.steps[step].response_metadata, **response_metadata},
        )

    async def mark_publication_step_failed(
        self,
        video_analysis_id: UUID,
        target_key: str,
        step: str,
        error_message: str,
        *,
        uncertain: bool,
    ) -> None:
        assert video_analysis_id == ANALYSIS_ID
        assert target_key == TARGET_KEY
        assert self.steps[step].status == "in_progress"
        self.events.append(f"repo:{'uncertain' if uncertain else 'failed'}:{step}")
        self._replace(
            step,
            status="uncertain" if uncertain else "failed",
            error_message=error_message,
        )


class FakePortalClient:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        configured_origin: str = "https://portal.test",
    ) -> None:
        self.events = events if events is not None else []
        self.configured_origin = configured_origin
        self.topic_status = 0
        self.create_topic_failures: list[BaseException] = []
        self.create_comment_failures: dict[str, list[BaseException]] = {
            "translation": [],
            "source": [],
        }
        self.prewrite_failures: dict[str, list[BaseException]] = {
            "topic": [],
            "translation": [],
            "source": [],
        }
        self.verify_failures: dict[str, list[BaseException]] = {
            "topic": [],
            "translation": [],
            "source": [],
        }
        self.session: PortalSession | None = None

    async def __aenter__(self) -> FakePortalClient:
        self.events.append("client:enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        self.events.append("client:exit")

    async def prepare(self, category_name: str) -> PortalSession:
        assert category_name == "Youtube"
        self.events.append("client:prepare")
        self.session = PortalSession(
            origin=self.configured_origin,
            user_id="encoded-user-id",
            username="publisher",
            category_id=7,
            category_name=category_name,
        )
        return self.session

    async def create_topic(
        self,
        title: str,
        markdown: str,
        *,
        tags: tuple[()] = (),
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> CreatedTopic:
        assert title == "A topic"
        assert markdown == "# Topic snapshot"
        assert tags == ()
        if self.prewrite_failures["topic"]:
            raise self.prewrite_failures["topic"].pop(0)
        if before_write is not None:
            await before_write()
        self.events.append("client:create:topic")
        if self.create_topic_failures:
            raise self.create_topic_failures.pop(0)
        return CreatedTopic(TOPIC_ID, self.topic_status, title, 7, "Youtube", ())

    async def verify_topic(self, topic_id: str, title: str) -> VerifiedTopic:
        assert topic_id == TOPIC_ID
        assert title == "A topic"
        self.events.append("client:verify:topic")
        self._raise_verify_failure("topic")
        return VerifiedTopic(topic_id, title, 0, 7, "Youtube")

    async def create_comment(
        self,
        topic_id: str,
        markdown: str,
        *,
        content_type: str,
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> CreatedComment:
        assert topic_id == TOPIC_ID
        assert content_type == "markdown"
        name = self._comment_name(markdown)
        if self.prewrite_failures[name]:
            raise self.prewrite_failures[name].pop(0)
        if before_write is not None:
            await before_write()
        self.events.append(f"client:create:{name}")
        if self.create_comment_failures[name]:
            raise self.create_comment_failures[name].pop(0)
        comment_id = 41 if name == "translation" else 42
        return CreatedComment(comment_id, content_type, f"<p>{name}</p>", (), 0)

    async def verify_comment(
        self,
        topic_id: str,
        comment: CreatedComment | int,
        **expected: object,
    ) -> VerifiedComment:
        assert topic_id == TOPIC_ID
        comment_id = comment.comment_id if isinstance(comment, CreatedComment) else comment
        name = "translation" if comment_id == 41 else "source"
        assert comment_id in {41, 42}
        self.events.append(f"client:verify:{name}")
        self._raise_verify_failure(name)
        rendered = f"<p>{name}</p>"
        if expected:
            assert expected == {
                "expected_rendered_content": rendered,
                "expected_content_type": "markdown",
                "expected_image_list": [],
            }
        return VerifiedComment(comment_id, "markdown", rendered, (), 0)

    def _raise_verify_failure(self, name: str) -> None:
        if self.verify_failures[name]:
            raise self.verify_failures[name].pop(0)

    @staticmethod
    def _comment_name(markdown: str) -> str:
        if markdown == "Translated snapshot":
            return "translation"
        assert markdown == "Source snapshot"
        return "source"


def _publisher(repository: FakeRepository, client: FakePortalClient) -> BbsPublisher:
    return BbsPublisher(
        repository,  # type: ignore[arg-type]
        client_factory=lambda: client,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_publish_runs_three_steps_in_strict_order_and_records_typed_ids() -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)

    assert await _publisher(repository, client).publish(ANALYSIS_ID) is True

    assert events == [
        "repo:list",
        "client:enter",
        "client:prepare",
        "repo:claim:topic",
        "repo:start:topic",
        "client:create:topic",
        "repo:created:topic",
        "client:verify:topic",
        "repo:succeeded:topic",
        "repo:claim:translation",
        "repo:start:translation",
        "client:create:translation",
        "repo:created:translation",
        "client:verify:translation",
        "repo:succeeded:translation",
        "repo:claim:source",
        "repo:start:source",
        "client:create:source",
        "repo:created:source",
        "client:verify:source",
        "repo:succeeded:source",
        "client:exit",
    ]
    assert repository.steps["topic"].remote_topic_id == TOPIC_ID
    assert repository.steps["topic"].remote_comment_id is None
    assert repository.steps["translation"].remote_topic_id is None
    assert repository.steps["translation"].remote_comment_id == 41
    assert repository.steps["source"].remote_topic_id is None
    assert repository.steps["source"].remote_comment_id == 42
    assert all(
        step.request_metadata["portal_target"] == BOUND_TARGET for step in repository.steps.values()
    )


@pytest.mark.parametrize("interrupted_step", ["topic", "translation", "source"])
@pytest.mark.asyncio
async def test_created_step_resumes_with_get_without_repeating_post(
    interrupted_step: str,
) -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)
    client.verify_failures[interrupted_step].append(RuntimeError("process interrupted"))
    publisher = _publisher(repository, client)

    with pytest.raises(RuntimeError, match="process interrupted"):
        await publisher.publish(ANALYSIS_ID)

    assert repository.steps[interrupted_step].status == "created"
    posts_before_resume = {
        name: events.count(f"client:create:{name}") for name in ("topic", "translation", "source")
    }

    assert await publisher.publish(ANALYSIS_ID) is True

    assert repository.steps[interrupted_step].status == "succeeded"
    assert (
        events.count(f"client:create:{interrupted_step}") == posts_before_resume[interrupted_step]
    )
    assert events.count(f"client:verify:{interrupted_step}") == 2
    assert all(step.status == "succeeded" for step in repository.steps.values())


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [
            _step("topic", status="succeeded"),
            _step("translation", status="succeeded"),
            _step("source", status="skipped"),
        ],
    ],
)
@pytest.mark.asyncio
async def test_publish_returns_false_without_opening_portal_when_nothing_is_pending(
    steps: list[PublicationStep],
) -> None:
    repository = FakeRepository(steps)

    def forbidden_factory() -> FakePortalClient:
        raise AssertionError("portal must not be opened")

    publisher = BbsPublisher(
        repository,  # type: ignore[arg-type]
        client_factory=forbidden_factory,  # type: ignore[arg-type]
    )

    assert await publisher.publish(ANALYSIS_ID) is False
    assert repository.events == ["repo:list"]


@pytest.mark.parametrize("failed_step", ["topic", "translation"])
@pytest.mark.asyncio
async def test_business_failure_is_failed_and_can_be_claimed_again(
    failed_step: str,
) -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)
    failure = PortalBusinessError(f"create {failed_step}", 1001, "operation rejected")
    if failed_step == "topic":
        client.create_topic_failures.append(failure)
    else:
        client.create_comment_failures[failed_step].append(failure)
    publisher = _publisher(repository, client)

    with pytest.raises(PortalBusinessError, match="operation rejected"):
        await publisher.publish(ANALYSIS_ID)

    assert repository.steps[failed_step].status == "failed"
    assert repository.steps[failed_step].attempt_count == 1
    assert await publisher.publish(ANALYSIS_ID) is True
    assert repository.steps[failed_step].attempt_count == 2
    assert events.count(f"client:create:{failed_step}") == 2


@pytest.mark.parametrize(
    ("failed_step", "failure_kind"),
    [
        ("topic", "uncertain"),
        ("topic", "configuration"),
        ("topic", "cancelled"),
        ("translation", "uncertain"),
        ("translation", "configuration"),
        ("translation", "cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_uncertain_or_cancelled_write_is_never_resent(
    failed_step: str,
    failure_kind: str,
) -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)
    if failure_kind == "uncertain":
        failure: BaseException = PortalWriteUncertainError("ambiguous write; do not retry")
        expected_exception: type[BaseException] = PortalWriteUncertainError
    elif failure_kind == "configuration":
        failure = PortalConfigurationError("local response processing failed")
        expected_exception = PortalConfigurationError
    else:
        failure = asyncio.CancelledError()
        expected_exception = asyncio.CancelledError
    if failed_step == "topic":
        client.create_topic_failures.append(failure)
    else:
        client.create_comment_failures[failed_step].append(failure)
    publisher = _publisher(repository, client)

    with pytest.raises(expected_exception):
        await publisher.publish(ANALYSIS_ID)

    assert repository.steps[failed_step].status == "uncertain"
    assert events.count(f"client:create:{failed_step}") == 1
    with pytest.raises(PublicationUncertainError, match="reconcile it on the portal"):
        await publisher.publish(ANALYSIS_ID)
    assert events.count(f"client:create:{failed_step}") == 1
    assert events.count("client:enter") == 1


@pytest.mark.asyncio
async def test_in_progress_step_found_at_startup_becomes_uncertain_without_writing() -> None:
    events: list[str] = []
    steps = _pending_steps()
    steps[0] = replace(steps[0], status="in_progress")
    repository = FakeRepository(steps, events)
    client = FakePortalClient(events)

    with pytest.raises(PublicationUncertainError, match="step=topic"):
        await _publisher(repository, client).publish(ANALYSIS_ID)

    assert repository.steps["topic"].status == "uncertain"
    assert events == ["repo:list", "repo:uncertain:topic"]


@pytest.mark.asyncio
async def test_claimed_step_found_at_startup_is_safely_resumed() -> None:
    events: list[str] = []
    steps = _pending_steps()
    steps[0] = replace(
        steps[0],
        status="claimed",
        attempt_count=1,
        request_metadata={"portal_target": BOUND_TARGET},
    )
    repository = FakeRepository(steps, events)

    assert await _publisher(repository, FakePortalClient(events)).publish(ANALYSIS_ID) is True

    assert repository.steps["topic"].status == "succeeded"
    assert events.count("repo:claim:topic") == 1
    assert events.count("repo:start:topic") == 1
    assert events.count("client:create:topic") == 1


@pytest.mark.parametrize("failed_step", ["topic", "translation"])
@pytest.mark.asyncio
async def test_local_prewrite_failure_leaves_claimed_step_safe_to_resume(
    failed_step: str,
) -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)
    client.prewrite_failures[failed_step].append(RuntimeError("local preparation failed"))
    publisher = _publisher(repository, client)

    with pytest.raises(RuntimeError, match="local preparation failed"):
        await publisher.publish(ANALYSIS_ID)

    assert repository.steps[failed_step].status == "claimed"
    assert repository.steps[failed_step].attempt_count == 1
    assert events.count(f"repo:start:{failed_step}") == 0
    assert events.count(f"client:create:{failed_step}") == 0

    assert await publisher.publish(ANALYSIS_ID) is True
    assert repository.steps[failed_step].attempt_count == 1
    assert events.count(f"client:create:{failed_step}") == 1


@pytest.mark.asyncio
async def test_portal_target_drift_stops_before_remote_read_or_write() -> None:
    events: list[str] = []
    steps = _pending_steps()
    steps[0] = replace(
        steps[0],
        status="created",
        attempt_count=1,
        remote_topic_id=TOPIC_ID,
        request_metadata={"portal_target": BOUND_TARGET},
    )
    repository = FakeRepository(steps, events)
    client = FakePortalClient(events, configured_origin="https://other-portal.test")

    with pytest.raises(PublicationStateError, match="origin does not match"):
        await _publisher(repository, client).publish(ANALYSIS_ID)

    assert events == ["repo:list", "client:enter", "client:exit"]


@pytest.mark.parametrize(
    ("topic_status", "verification_error"),
    [
        (2, PortalTopicPendingReviewError("topic is pending review")),
        (0, PortalVerificationError("topic read-back failed")),
    ],
)
@pytest.mark.asyncio
async def test_unverified_topic_stays_created_and_comments_are_not_written(
    topic_status: int,
    verification_error: BaseException,
) -> None:
    events: list[str] = []
    repository = FakeRepository(_pending_steps(), events)
    client = FakePortalClient(events)
    client.topic_status = topic_status
    client.verify_failures["topic"].append(verification_error)

    with pytest.raises(type(verification_error)):
        await _publisher(repository, client).publish(ANALYSIS_ID)

    topic = repository.steps["topic"]
    assert topic.status == "created"
    assert topic.remote_topic_id == TOPIC_ID
    assert topic.remote_status == topic_status
    assert repository.steps["translation"].status == "pending"
    assert repository.steps["source"].status == "pending"
    assert not any(event.startswith("client:create:translation") for event in events)
    assert not any(event.startswith("client:create:source") for event in events)


def _fetched(language_code: str) -> FetchedVideo:
    return FetchedVideo(
        metadata=VideoMetadata(
            youtube_video_id="video-id",
            channel=ChannelMetadata(
                youtube_channel_id="channel-id",
                title="Channel",
                channel_url="https://youtube.test/channel",
            ),
            title="  Video title  ",
            video_url="https://youtube.test/watch?v=video-id",
            published_at=datetime(2026, 8, 14, tzinfo=UTC),
        ),
        subtitle=DownloadedSubtitle(
            language_code=language_code,
            language_name=None,
            source_format="vtt",
            is_auto_generated=False,
            raw_text="raw",
            normalized_text="Original transcript",
        ),
    )


def _translation() -> TranslationResult:
    return TranslationResult("Translated transcript", "zh-Hans", {"model": "test"})


def _outcome() -> AnalysisOutcome:
    return AnalysisOutcome(
        payload={
            "guests": [
                {
                    "name_original": "Guest Name",
                    "title": "Example 公司创始人",
                }
            ],
            "sources": [],
        },
        projection=AnalysisProjection(
            is_relevant=True,
            relevance_score=0.9,
            quality_score=0.8,
            summary="Summary",
            translated_summary="中文摘要",
            background_notes=None,
            key_points=["Point"],
            tags=[{"name": "AI"}],
        ),
        metadata={"model": "test"},
    )


def _publication_source(language_code: str) -> PublicationSource:
    return PublicationSource(
        video_analysis_id=ANALYSIS_ID,
        fetched=_fetched(language_code),
        translated_text=_translation().translated_text,
        outcome=_outcome(),
    )


def test_build_steps_creates_three_immutable_publication_snapshots() -> None:
    steps = BbsPublicationBuilder().build_steps(_publication_source("en"))

    assert [step.step for step in steps] == ["topic", "translation", "source"]
    assert all(step.target_key == TARGET_KEY for step in steps)
    assert steps[0].topic_title == ("Guest Name（Example 公司创始人）｜Channel｜2026-08-14")
    assert "## 视频信息" in (steps[0].markdown_snapshot or "")
    assert "中文摘要" in (steps[0].markdown_snapshot or "")
    assert steps[1].topic_title is None
    assert steps[1].markdown_snapshot == ("## 中文翻译\n\n```text\nTranslated transcript\n```\n")
    assert steps[2].topic_title is None
    assert steps[2].markdown_snapshot == (
        "## 原文字幕（en）\n\n```text\nOriginal transcript\n```\n"
    )
    assert not any(step.skipped for step in steps)


def test_build_steps_skips_source_comment_for_simplified_chinese() -> None:
    steps = BbsPublicationBuilder().build_steps(_publication_source("zh_CN"))

    assert steps[2].step == "source"
    assert steps[2].markdown_snapshot is None
    assert steps[2].skipped is True


class FakeConsumerRepository:
    def __init__(
        self,
        candidates: Sequence[PublicationCandidate],
        sources: dict[UUID, PublicationSource | None],
        *,
        analyses_with_steps: set[UUID] | None = None,
    ) -> None:
        self.candidates = list(candidates)
        self.sources = sources
        self.analyses_with_steps = analyses_with_steps or set()
        self.reconciliation_statuses = {
            candidate.video_analysis_id: "uncertain"
            for candidate in candidates
            if candidate.requires_reconciliation
        }
        self.calls: list[tuple[object, ...]] = []

    async def list_publication_candidates(
        self,
        target_key: str,
    ) -> list[PublicationCandidate]:
        self.calls.append(("list_candidates", target_key))
        return list(self.candidates)

    async def list_publication_steps(
        self,
        video_analysis_id: UUID,
        target_key: str,
    ) -> list[PublicationStep]:
        self.calls.append(("list_steps", video_analysis_id, target_key))
        reconciliation_status = self.reconciliation_statuses.get(video_analysis_id)
        if reconciliation_status is not None:
            return [_step("topic", status=reconciliation_status)]
        return [_step("topic")] if video_analysis_id in self.analyses_with_steps else []

    async def get_publication_source(
        self,
        video_analysis_id: UUID,
    ) -> PublicationSource | None:
        self.calls.append(("get_source", video_analysis_id))
        return self.sources.get(video_analysis_id)

    async def ensure_publication_steps(
        self,
        video_analysis_id: UUID,
        steps: Sequence[object],
    ) -> None:
        self.calls.append(("ensure_steps", video_analysis_id, tuple(step.step for step in steps)))


class FakeConsumerPublisher:
    target_key = TARGET_KEY

    def __init__(
        self,
        outcomes: dict[UUID, bool | BaseException],
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[UUID] = []

    async def publish(self, video_analysis_id: UUID) -> bool:
        self.calls.append(video_analysis_id)
        outcome = self.outcomes[video_analysis_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _candidate(video_analysis_id: UUID, suffix: str) -> PublicationCandidate:
    return PublicationCandidate(
        video_analysis_id=video_analysis_id,
        youtube_video_id=f"video-{suffix}",
        video_url=f"https://youtube.test/watch?v=video-{suffix}",
    )


@pytest.mark.asyncio
async def test_consumer_initializes_snapshots_then_publishes_candidate() -> None:
    candidate = _candidate(ANALYSIS_ID, "one")
    source = _publication_source("en")
    repository = FakeConsumerRepository([candidate], {ANALYSIS_ID: source})
    publisher = FakeConsumerPublisher({ANALYSIS_ID: True})

    results = await BbsPushConsumer(  # type: ignore[arg-type]
        repository,
        publisher,
    ).consume(limit=20)

    assert [(result.status, result.video_analysis_id) for result in results] == [
        ("published", ANALYSIS_ID)
    ]
    assert repository.calls == [
        ("list_candidates", TARGET_KEY),
        ("list_steps", ANALYSIS_ID, TARGET_KEY),
        ("get_source", ANALYSIS_ID),
        ("ensure_steps", ANALYSIS_ID, ("topic", "translation", "source")),
    ]
    assert publisher.calls == [ANALYSIS_ID]


@pytest.mark.asyncio
async def test_consumer_reuses_existing_snapshots_without_loading_source() -> None:
    candidate = _candidate(ANALYSIS_ID, "one")
    repository = FakeConsumerRepository(
        [candidate],
        {},
        analyses_with_steps={ANALYSIS_ID},
    )
    publisher = FakeConsumerPublisher({ANALYSIS_ID: False})

    results = await BbsPushConsumer(  # type: ignore[arg-type]
        repository,
        publisher,
    ).consume(limit=None)

    assert [result.status for result in results] == ["skipped"]
    assert repository.calls == [
        ("list_candidates", TARGET_KEY),
        ("list_steps", ANALYSIS_ID, TARGET_KEY),
    ]


@pytest.mark.asyncio
async def test_consumer_records_failure_and_continues_with_snapshot_batch() -> None:
    blocked_newer_id = UUID("22222222-2222-2222-2222-222222222222")
    other_video_id = UUID("33333333-3333-3333-3333-333333333333")
    candidates = [
        _candidate(ANALYSIS_ID, "blocked"),
        _candidate(blocked_newer_id, "blocked"),
        _candidate(other_video_id, "other"),
    ]
    other_source = replace(
        _publication_source("zh-CN"),
        video_analysis_id=other_video_id,
    )
    repository = FakeConsumerRepository(
        candidates,
        {ANALYSIS_ID: None, other_video_id: other_source},
    )
    publisher = FakeConsumerPublisher({other_video_id: True})

    results = await BbsPushConsumer(  # type: ignore[arg-type]
        repository,
        publisher,
    ).consume(limit=2)

    assert [result.status for result in results] == ["failed", "published"]
    assert "cannot be reconstructed" in (results[0].detail or "")
    assert [result.video_analysis_id for result in results] == [
        ANALYSIS_ID,
        other_video_id,
    ]
    assert publisher.calls == [other_video_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 0])
async def test_consumer_without_limit_drains_revisions_for_the_same_video(
    limit: int | None,
) -> None:
    newer_id = UUID("22222222-2222-2222-2222-222222222222")
    candidates = [
        _candidate(ANALYSIS_ID, "shared"),
        _candidate(newer_id, "shared"),
    ]
    repository = FakeConsumerRepository(
        candidates,
        {},
        analyses_with_steps={ANALYSIS_ID, newer_id},
    )
    publisher = FakeConsumerPublisher({ANALYSIS_ID: True, newer_id: True})

    results = await BbsPushConsumer(  # type: ignore[arg-type]
        repository,
        publisher,
    ).consume(limit=limit)

    assert [result.video_analysis_id for result in results] == [ANALYSIS_ID, newer_id]
    assert [result.status for result in results] == ["published", "published"]
    assert publisher.calls == [ANALYSIS_ID, newer_id]


@pytest.mark.asyncio
async def test_consumer_does_not_chase_candidates_added_after_snapshot() -> None:
    late_id = UUID("22222222-2222-2222-2222-222222222222")
    repository = FakeConsumerRepository(
        [_candidate(ANALYSIS_ID, "initial")],
        {},
        analyses_with_steps={ANALYSIS_ID, late_id},
    )

    class AppendingPublisher(FakeConsumerPublisher):
        async def publish(self, video_analysis_id: UUID) -> bool:
            repository.candidates.append(_candidate(late_id, "late"))
            return await super().publish(video_analysis_id)

    publisher = AppendingPublisher({ANALYSIS_ID: True, late_id: True})

    results = await BbsPushConsumer(  # type: ignore[arg-type]
        repository,
        publisher,
    ).consume(limit=None)

    assert [result.video_analysis_id for result in results] == [ANALYSIS_ID]
    assert publisher.calls == [ANALYSIS_ID]
    assert repository.calls.count(("list_candidates", TARGET_KEY)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciliation_status", "expected_reconciliation_publish_call"),
    [("uncertain", False), ("in_progress", True)],
)
async def test_consumer_blocks_later_revisions_after_reconciliation_failure_for_one_run(
    reconciliation_status: str,
    expected_reconciliation_publish_call: bool,
) -> None:
    blocked_newer_id = UUID("22222222-2222-2222-2222-222222222222")
    other_video_id = UUID("33333333-3333-3333-3333-333333333333")
    candidates = [
        replace(
            _candidate(ANALYSIS_ID, "blocked"),
            requires_reconciliation=True,
        ),
        _candidate(blocked_newer_id, "blocked"),
        _candidate(other_video_id, "other"),
    ]
    repository = FakeConsumerRepository(
        candidates,
        {},
        analyses_with_steps={ANALYSIS_ID, blocked_newer_id, other_video_id},
    )
    repository.reconciliation_statuses[ANALYSIS_ID] = reconciliation_status
    publisher = FakeConsumerPublisher(
        {
            ANALYSIS_ID: PublicationUncertainError(
                "remote result is uncertain; reconcile it before retrying"
            ),
            blocked_newer_id: True,
            other_video_id: True,
        }
    )
    consumer = BbsPushConsumer(repository, publisher)  # type: ignore[arg-type]

    first_run = await consumer.consume(limit=1)

    assert [result.video_analysis_id for result in first_run] == [
        ANALYSIS_ID,
        other_video_id,
    ]
    assert [result.status for result in first_run] == ["failed", "published"]
    assert "reconcile" in (first_run[0].detail or "")
    assert publisher.calls == (
        [ANALYSIS_ID, other_video_id]
        if expected_reconciliation_publish_call
        else [other_video_id]
    )


@pytest.mark.asyncio
async def test_consumer_propagates_cancellation() -> None:
    candidate = _candidate(ANALYSIS_ID, "one")
    repository = FakeConsumerRepository(
        [candidate],
        {},
        analyses_with_steps={ANALYSIS_ID},
    )
    publisher = FakeConsumerPublisher({ANALYSIS_ID: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await BbsPushConsumer(  # type: ignore[arg-type]
            repository,
            publisher,
        ).consume(limit=1)


@pytest.mark.asyncio
async def test_consumer_rejects_invalid_internal_limit() -> None:
    repository = FakeConsumerRepository([], {})
    publisher = FakeConsumerPublisher({})

    for limit in (-1, True):
        with pytest.raises(ValueError, match="non-negative integer or None"):
            await BbsPushConsumer(  # type: ignore[arg-type]
                repository,
                publisher,
            ).consume(limit=limit)
