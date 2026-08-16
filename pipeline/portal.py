from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import string
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from models import AnalysisOutcome, AnalysisProjection, VideoMetadata
from subtitles import is_simplified_chinese_language


class PortalError(RuntimeError):
    """Base class for safe-to-report portal failures."""


class PortalConfigurationError(PortalError):
    """Raised when local credentials or required executables are invalid."""


class PortalPreparationError(PortalError):
    """Raised when the authenticated account or portal cannot publish safely."""


class PortalBusinessError(PortalError):
    """Raised for an explicit ``success=false`` portal response."""

    def __init__(self, operation: str, error_code: int, message: str) -> None:
        self.operation = operation
        self.error_code = error_code
        self.portal_message = _safe_message(message)
        super().__init__(
            f"Portal rejected {operation} (errorCode={error_code}): "
            f"{self.portal_message or 'no message'}"
        )


class PortalTransportError(PortalError):
    """Raised when a read-only request has no trustworthy portal response."""


class PortalInvalidResponseError(PortalError):
    """Raised when a successful read response violates the documented schema."""


class PortalWriteUncertainError(PortalError):
    """Raised when a non-idempotent write may have reached the portal."""


class PortalVerificationError(PortalError):
    """Raised when a read-back does not match the corresponding write."""


class PortalTopicPendingReviewError(PortalVerificationError):
    """Raised when a created topic is waiting for moderation."""


class PortalTopicStatusError(PortalVerificationError):
    """Raised when a topic exists but is not in the normal published state."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


CommandRunner = Callable[[tuple[str, ...], bytes | None], Awaitable[CommandResult]]


@dataclass(frozen=True, slots=True)
class PortalSession:
    origin: str
    user_id: str
    username: str
    category_id: int
    category_name: str


@dataclass(frozen=True, slots=True)
class CreatedTopic:
    topic_id: str
    status: int
    title: str | None
    category_id: int | None
    category_name: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedTopic:
    topic_id: str
    title: str
    status: int
    category_id: int | None
    category_name: str | None


@dataclass(frozen=True, slots=True)
class CreatedComment:
    comment_id: int
    content_type: str
    rendered_content: str
    image_list: tuple[object, ...]
    status: int | None


@dataclass(frozen=True, slots=True)
class VerifiedComment:
    comment_id: int
    content_type: str
    rendered_content: str
    image_list: tuple[object, ...]
    status: int | None


@dataclass(frozen=True, slots=True)
class PublicationContent:
    title: str
    topic_markdown: str
    translated_comment_markdown: str
    source_comment_markdown: str | None
    tags: tuple[str, ...]


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]+\Z")
_MAX_SAFE_MESSAGE_CHARS = 256
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_COMMENT_VERIFICATION_BYTES = 64 * 1024 * 1024
_MAX_COMMENT_VERIFICATION_PAGES = 100
_MAX_BBS_COMMENT_ID = 2**63 - 1


async def _default_command_runner(argv: tuple[str, ...], stdin: bytes | None) -> CommandResult:
    response_path: Path | None = None
    response_limit: int | None = None
    executed_argv = argv
    if argv and argv[0] == "curl":
        try:
            output_index = argv.index("-o")
            limit_index = argv.index("--max-filesize")
            response_path = Path(argv[output_index + 1])
            response_limit = int(argv[limit_index + 1])
        except (ValueError, IndexError) as exc:
            raise PortalConfigurationError(
                "curl command is missing its bounded response output"
            ) from exc
        executed_argv = (*argv[:output_index], *argv[output_index + 2 :])

    try:
        process = await asyncio.create_subprocess_exec(
            *executed_argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PortalConfigurationError(f"Required executable is unavailable: {argv[0]}") from exc

    if response_path is not None and response_limit is not None:
        return await _communicate_bounded_curl(
            process,
            stdin=stdin,
            response_path=response_path,
            response_limit=response_limit,
        )

    try:
        stdout, stderr = await process.communicate(stdin)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    return CommandResult(process.returncode, stdout, stderr)


async def _communicate_bounded_curl(
    process: asyncio.subprocess.Process,
    *,
    stdin: bytes | None,
    response_path: Path,
    response_limit: int,
) -> CommandResult:
    if process.stdout is None or process.stderr is None:
        await _terminate_process(process)
        raise PortalConfigurationError("curl subprocess pipes are unavailable")

    if stdin is not None:
        if process.stdin is None:
            await _terminate_process(process)
            raise PortalConfigurationError("curl subprocess stdin is unavailable")
        process.stdin.write(stdin)
        await process.stdin.drain()
        process.stdin.close()

    stderr_task = asyncio.create_task(process.stderr.read())
    output = bytearray()
    exceeded = False
    try:
        while True:
            chunk = await process.stdout.read(min(64 * 1024, response_limit + 1 - len(output)))
            if not chunk:
                break
            if len(output) + len(chunk) > response_limit:
                exceeded = True
                break
            output.extend(chunk)
        if exceeded:
            await _terminate_process(process)
        else:
            await process.wait()
        stderr = await stderr_task
    except asyncio.CancelledError:
        await _terminate_process(process)
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
        raise

    if exceeded:
        return CommandResult(
            returncode=63,
            stderr=b"Portal response exceeded the configured byte limit",
        )
    try:
        await asyncio.to_thread(response_path.write_bytes, bytes(output))
    except OSError:
        return CommandResult(returncode=23, stderr=b"Portal response could not be stored")
    return CommandResult(process.returncode or 0, stderr=stderr)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    await asyncio.shield(process.wait())


async def _remove_private_file(path: Path) -> None:
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except OSError:
        pass


class PortalClient:
    """Asynchronous bbs-go client that delegates every HTTP request to curl."""

    def __init__(
        self,
        env_path: str | Path,
        *,
        runner: CommandRunner | None = None,
        get_attempts: int = 3,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_comment_verification_bytes: int = _DEFAULT_MAX_COMMENT_VERIFICATION_BYTES,
    ) -> None:
        if get_attempts < 1:
            raise ValueError("get_attempts must be positive")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be positive")
        if (
            isinstance(max_comment_verification_bytes, bool)
            or not isinstance(max_comment_verification_bytes, int)
            or max_comment_verification_bytes < 1
        ):
            raise ValueError("max_comment_verification_bytes must be positive")
        self._env_path = Path(env_path)
        self._runner = runner or _default_command_runner
        self._get_attempts = get_attempts
        self._max_response_bytes = max_response_bytes
        self._max_comment_verification_bytes = max_comment_verification_bytes
        self._base_url: str | None = None
        self._network_args: tuple[str, ...] = ()
        self._session_dir: Path | None = None
        self._auth_header_path: Path | None = None
        self._session: PortalSession | None = None
        self._verified_topic_ids: set[str] = set()
        self._closed = False

    async def __aenter__(self) -> PortalClient:
        await self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    @property
    def session(self) -> PortalSession | None:
        return self._session

    @property
    def configured_origin(self) -> str:
        if self._base_url is None:
            raise PortalConfigurationError("Portal session is not initialized")
        return self._base_url

    async def prepare(self, category_name: str = "Youtube") -> PortalSession:
        """Validate credentials, write restrictions, and one exact normal category."""

        if not category_name:
            raise ValueError("category_name must not be empty")
        await self._ensure_open()

        current_path = await self._get("/api/user/current", operation="current user")
        try:
            current = await self._jq_object(
                current_path,
                """
                .data as $user |
                if ($user | type) != "object" or
                   ($user.id | type) != "string" or
                   ($user.id | length) == 0 or
                   ($user.username | type) != "string" or
                   ($user.username | length) == 0 or
                   (($user.emailVerified != null) and
                    (($user.emailVerified | type) != "boolean")) or
                   (($user.status != null) and
                    (($user.status | type) != "number" or
                     ($user.status | floor) != $user.status)) or
                   (($user.forbidden != null) and
                    (($user.forbidden | type) != "boolean")) or
                   (($user.createTime != null) and
                    (($user.createTime | type) != "number" or
                     ($user.createTime | floor) != $user.createTime))
                then error("invalid current user")
                else {
                  id: $user.id,
                  username: $user.username,
                  emailVerified: ($user.emailVerified // null),
                  status: ($user.status // null),
                  forbidden: ($user.forbidden // null),
                  createTime: ($user.createTime // null)
                }
                end
                """,
                operation="current user",
            )
        except PortalInvalidResponseError as exc:
            raise PortalPreparationError(
                "Portal token is invalid or the current user response is incomplete"
            ) from exc
        self._validate_user_state(current)

        config_path = await self._get("/api/config/configs", operation="portal config")
        config = await self._jq_object(
            config_path,
            """
            .data as $config |
            if ($config | type) != "object" or
               ($config.installed | type) != "boolean" or
               ($config.loginRequired | type) != "boolean" or
               ($config.modules | type) != "object" or
               ($config.modules.topic | type) != "boolean" or
               ($config.topicCaptcha | type) != "boolean" or
               ($config.createTopicEmailVerified | type) != "boolean" or
               ($config.createCommentEmailVerified | type) != "boolean" or
               ($config.userObserveSeconds | type) != "number" or
               ($config.userObserveSeconds | floor) != $config.userObserveSeconds or
               $config.userObserveSeconds < 0
            then error("invalid portal config")
            else {
              installed: $config.installed,
              loginRequired: $config.loginRequired,
              topicEnabled: $config.modules.topic,
              topicCaptcha: $config.topicCaptcha,
              createTopicEmailVerified: $config.createTopicEmailVerified,
              createCommentEmailVerified: $config.createCommentEmailVerified,
              userObserveSeconds: $config.userObserveSeconds
            }
            end
            """,
            operation="portal config",
        )
        self._validate_write_restrictions(current, config)

        categories_path = await self._get(
            "/api/topic/categories",
            operation="topic categories",
            query=("type=0",),
        )
        categories = await self._jq_object(
            categories_path,
            """
            [.data | .. | objects |
             select(.name? == $name and .type? == "normal") | .id] as $matches |
            [.data | .. | objects |
             select(.id? and .name? and .type? == "normal") |
             {id, name}] as $available |
            if any($matches[];
                   (type != "number") or (floor != .) or (. <= 0))
            then error("invalid category id")
            else {matches: $matches, available: $available}
            end
            """,
            operation="topic categories",
            jq_args=("--arg", "name", category_name),
        )
        matches = categories["matches"]
        if not isinstance(matches, list) or len(matches) != 1:
            available = _format_categories(categories.get("available"))
            raise PortalPreparationError(
                f"Portal category {category_name!r} is missing or not unique; "
                f"available normal categories: {available}"
            )
        category_id = matches[0]
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            raise PortalInvalidResponseError("Invalid topic category response")

        session = PortalSession(
            origin=self._base_url or "",
            user_id=str(current["id"]),
            username=str(current["username"]),
            category_id=category_id,
            category_name=category_name,
        )
        self._session = session
        return session

    async def create_topic(
        self,
        title: str,
        markdown: str,
        *,
        tags: Sequence[str] = (),
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> CreatedTopic:
        """Create one Markdown topic exactly once; ambiguous results are not retried."""

        session = self._require_prepared()
        normalized_title = title.strip()
        if not normalized_title or len(normalized_title) > 128:
            raise ValueError("title must contain between 1 and 128 Unicode characters")
        if not markdown.strip():
            raise ValueError("markdown must not be empty")
        if tags:
            raise ValueError(
                "Portal topic tags must remain empty; analysis tags belong in the body"
            )

        content_path = await self._write_private_text(markdown, "topic-content")
        tags_result = await self._run(
            ("jq", "-cn", "[]"),
            None,
        )
        if tags_result.returncode != 0:
            raise PortalConfigurationError("jq could not construct the topic tags")
        tags_path = await self._write_private_bytes(tags_result.stdout, "topic-tags")

        payload_result = await self._run(
            (
                "jq",
                "-cn",
                "--rawfile",
                "content",
                str(content_path),
                "--arg",
                "title",
                normalized_title,
                "--argjson",
                "categoryId",
                str(session.category_id),
                "--slurpfile",
                "tags",
                str(tags_path),
                "{type: 0, categoryId: $categoryId, title: $title, "
                'contentType: "markdown", content: $content, tags: $tags[0]}',
            ),
            None,
        )
        if payload_result.returncode != 0:
            raise PortalConfigurationError("jq could not construct the topic request")
        payload_path = await self._write_private_bytes(payload_result.stdout, "topic-request")

        response_path = await self._post(
            "/api/topic/create",
            operation="create topic",
            before_write=before_write,
            extra=(
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                f"@{payload_path}",
            ),
        )
        topic = await self._jq_object(
            response_path,
            """
            .data as $topic |
            if ($topic | type) != "object" or
               ($topic.id | type) != "string" or
               ($topic.id | length) == 0 or
               ($topic.status | type) != "number" or
               ($topic.status | floor) != $topic.status or
               (($topic.title != null) and (($topic.title | type) != "string"))
            then error("invalid created topic")
            else {
              id: $topic.id,
              status: $topic.status,
              title: ($topic.title // null),
              categoryId: (if ($topic.category | type) == "object"
                           then ($topic.category.id // null) else null end),
              categoryName: (if ($topic.category | type) == "object"
                             then ($topic.category.name // null) else null end),
              tags: [($topic.tags // [])[] |
                     if type == "string" then .
                     elif type == "object" and (.name | type) == "string"
                     then .name else empty end]
            }
            end
            """,
            operation="create topic",
            write=True,
        )
        topic_id = topic["id"]
        status_value = topic["status"]
        if (
            not isinstance(topic_id, str)
            or isinstance(status_value, bool)
            or not isinstance(status_value, int)
        ):
            raise PortalWriteUncertainError(
                "Portal topic creation returned an unusable success response"
            )
        return CreatedTopic(
            topic_id=topic_id,
            status=status_value,
            title=topic.get("title") if isinstance(topic.get("title"), str) else None,
            category_id=_optional_int(topic.get("categoryId")),
            category_name=(
                topic.get("categoryName") if isinstance(topic.get("categoryName"), str) else None
            ),
            tags=tuple(tag for tag in topic.get("tags", []) if isinstance(tag, str)),
        )

    async def verify_topic(self, topic_id: str, expected_title: str) -> VerifiedTopic:
        """Read a topic back and allow continuation only for status zero."""

        self._require_prepared()
        _validate_topic_id(topic_id)
        if not expected_title:
            raise ValueError("expected_title must not be empty")
        response_path = await self._get(
            f"/api/topic/{quote(topic_id, safe='')}", operation="verify topic"
        )
        try:
            topic = await self._jq_object(
                response_path,
                """
                .data as $topic |
                if ($topic | type) != "object" or
                   ($topic.id | type) != "string" or
                   ($topic.title | type) != "string" or
                   ($topic.status | type) != "number" or
                   ($topic.status | floor) != $topic.status
                then error("invalid topic detail")
                else {
                  id: $topic.id,
                  title: $topic.title,
                  status: $topic.status,
                  categoryId: (if ($topic.category | type) == "object"
                               then ($topic.category.id // null) else null end),
                  categoryName: (if ($topic.category | type) == "object"
                                 then ($topic.category.name // null) else null end)
                }
                end
                """,
                operation="verify topic",
            )
        except PortalInvalidResponseError as exc:
            raise PortalVerificationError("Topic detail response is invalid") from exc

        if topic.get("id") != topic_id or topic.get("title") != expected_title:
            raise PortalVerificationError("Topic read-back does not match its create request")
        status_value = topic.get("status")
        if isinstance(status_value, bool) or not isinstance(status_value, int):
            raise PortalVerificationError("Topic read-back has an invalid status")
        if status_value == 2:
            raise PortalTopicPendingReviewError(
                "Topic was created but is pending moderation (status=2)"
            )
        if status_value != 0:
            raise PortalTopicStatusError(f"Topic cannot be continued because status={status_value}")

        self._verified_topic_ids.add(topic_id)
        return VerifiedTopic(
            topic_id=topic_id,
            title=expected_title,
            status=status_value,
            category_id=_optional_int(topic.get("categoryId")),
            category_name=(
                topic.get("categoryName") if isinstance(topic.get("categoryName"), str) else None
            ),
        )

    async def create_comment(
        self,
        topic_id: str,
        content: str,
        *,
        content_type: str = "markdown",
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> CreatedComment:
        """Create one top-level topic comment with its body read from a private file."""

        self._require_prepared()
        _validate_topic_id(topic_id)
        if topic_id not in self._verified_topic_ids:
            raise PortalVerificationError(
                "Topic must pass verify_topic before comments can be created"
            )
        if not content.strip():
            raise ValueError("comment content must not be empty")
        if content_type not in {"text", "markdown"}:
            raise ValueError("content_type must be 'text' or 'markdown'")

        content_path = await self._write_private_text(content, "comment-content")
        response_path = await self._post(
            "/api/comment/create",
            operation="create comment",
            before_write=before_write,
            extra=(
                "--data-urlencode",
                "entityType=topic",
                "--data-urlencode",
                f"entityId={topic_id}",
                "--data-urlencode",
                f"contentType={content_type}",
                "--data-urlencode",
                f"content@{content_path}",
                "--data-urlencode",
                "imageList=[]",
            ),
        )
        comment = await self._jq_object(
            response_path,
            """
            .data as $comment |
            if ($comment | type) != "object" or
               ($comment.id | type) != "number" or
               ($comment.id | floor) != $comment.id or
               $comment.id <= 0 or
               $comment.contentType != $type or
               ($comment.content | type) != "string" or
               ($comment.content | length) == 0 or
               (($comment.imageList != null) and
                (($comment.imageList | type) != "array")) or
               (($comment.status != null) and
                (($comment.status | type) != "number" or
                 ($comment.status | floor) != $comment.status))
            then error("invalid created comment")
            else {
              id: $comment.id,
              contentType: $comment.contentType,
              content: $comment.content,
              imageList: ($comment.imageList // []),
              status: ($comment.status // null)
            }
            end
            """,
            operation="create comment",
            jq_args=("--arg", "type", content_type),
            write=True,
        )
        comment_id = comment.get("id")
        rendered_content = comment.get("content")
        if (
            isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or not isinstance(rendered_content, str)
        ):
            raise PortalWriteUncertainError(
                "Portal comment creation returned an unusable success response"
            )
        image_list = comment.get("imageList")
        if not isinstance(image_list, list):
            raise PortalWriteUncertainError(
                "Portal comment creation returned an invalid image list"
            )
        return CreatedComment(
            comment_id=comment_id,
            content_type=content_type,
            rendered_content=rendered_content,
            image_list=tuple(image_list),
            status=_optional_int(comment.get("status")),
        )

    async def verify_comment(
        self,
        topic_id: str,
        comment: CreatedComment | int,
        expected_rendered_content: str | None = None,
        expected_content_type: str = "markdown",
        expected_image_list: Sequence[object] = (),
    ) -> VerifiedComment:
        """Verify a comment using either its result object or persisted safe fields."""

        self._require_prepared()
        _validate_topic_id(topic_id)
        if isinstance(comment, CreatedComment):
            if expected_rendered_content is not None or expected_image_list:
                raise ValueError("Explicit expected fields cannot be mixed with CreatedComment")
            comment_id = comment.comment_id
            rendered_content = comment.rendered_content
            content_type = comment.content_type
            image_list = comment.image_list
        else:
            comment_id = comment
            if expected_rendered_content is None:
                raise ValueError(
                    "expected_rendered_content is required with an explicit comment ID"
                )
            rendered_content = expected_rendered_content
            content_type = expected_content_type
            image_list = tuple(expected_image_list)
        if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
            raise ValueError("comment_id must be a positive integer")
        if not isinstance(rendered_content, str) or not rendered_content:
            raise ValueError("expected rendered content must be a non-empty string")
        if content_type not in {"text", "markdown"}:
            raise ValueError("expected_content_type must be 'text' or 'markdown'")

        expected_content_path = await self._write_private_text(
            rendered_content, "expected-comment-content"
        )
        try:
            serialized_images = json.dumps(
                list(image_list), ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_image_list must be JSON-compatible") from exc
        expected_images_source = await self._write_private_text(
            serialized_images, "expected-comment-images-source"
        )
        expected_images_result = await self._run(
            (
                "jq",
                "-ce",
                'if type == "array" then . else error("invalid image list") end',
                str(expected_images_source),
            ),
            None,
        )
        if expected_images_result.returncode != 0:
            raise ValueError("expected_image_list must be JSON-compatible")
        expected_images_path = await self._write_private_bytes(
            expected_images_result.stdout, "expected-comment-images"
        )

        base_query = ("entityType=topic", f"entityId={topic_id}")
        verification_bytes = 0

        def account_response(response_bytes: int) -> None:
            nonlocal verification_bytes
            verification_bytes += response_bytes
            if verification_bytes > self._max_comment_verification_bytes:
                raise PortalVerificationError(
                    "Comment read-back exceeded the cumulative response byte limit"
                )

        if comment_id < _MAX_BBS_COMMENT_ID:
            verified, _cursor, _has_more, response_bytes = await self._read_comment_page(
                query=(*base_query, f"cursor={comment_id + 1}"),
                comment_id=comment_id,
                content_type=content_type,
                expected_content_path=expected_content_path,
                expected_images_path=expected_images_path,
            )
            account_response(response_bytes)
            if verified is not None:
                return verified

        # A bbs-go accepted answer is pinned on the first page and excluded from
        # cursor-filtered results. Starting at page one also provides a bounded
        # compatibility fallback if the server changes its cursor implementation.
        next_cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page_number in range(_MAX_COMMENT_VERIFICATION_PAGES):
            query = base_query
            if next_cursor is not None:
                query = (*query, f"cursor={next_cursor}")
            verified, cursor, has_more, response_bytes = await self._read_comment_page(
                query=query,
                comment_id=comment_id,
                content_type=content_type,
                expected_content_path=expected_content_path,
                expected_images_path=expected_images_path,
            )
            account_response(response_bytes)
            if verified is not None:
                return verified
            if not has_more:
                break
            if not cursor or cursor in seen_cursors:
                raise PortalVerificationError(
                    "Comment read-back returned an invalid pagination cursor"
                )
            seen_cursors.add(cursor)
            next_cursor = cursor
        else:
            raise PortalVerificationError("Comment read-back exceeded the verification page limit")

        raise PortalVerificationError("Comment read-back does not match its create response")

    async def _read_comment_page(
        self,
        *,
        query: tuple[str, ...],
        comment_id: int,
        content_type: str,
        expected_content_path: Path,
        expected_images_path: Path,
    ) -> tuple[VerifiedComment | None, str, bool, int]:
        response_path = await self._get(
            "/api/comment/comments",
            operation="verify comment",
            query=query,
        )
        try:
            try:
                response_bytes = (await asyncio.to_thread(response_path.stat)).st_size
                page = await self._jq_object(
                    response_path,
                    """
                .data as $page |
                ($page.results // []) as $results |
                if ($page | type) != "object" or
                   (($page.results != null) and
                    (($page.results | type) != "array")) or
                   ($page.cursor | type) != "string" or
                   ($page.hasMore | type) != "boolean"
                then error("invalid comments page")
                else [$results[] | select(.id == $id)] as $targets |
                     [$targets[] |
                      select(.contentType == $type and
                             .content == $expectedContent and
                             (.imageList // []) == $expectedImages[0] and
                             ((.status == null) or
                              ((.status | type) == "number" and
                               (.status | floor) == .status)))] as $matches |
                     {
                       targetCount: ($targets | length),
                       matchCount: ($matches | length),
                       match: (if ($matches | length) == 0 then null else {
                         id: $matches[0].id,
                         contentType: $matches[0].contentType,
                         content: $matches[0].content,
                         imageList: ($matches[0].imageList // []),
                         status: ($matches[0].status // null)
                       } end),
                       cursor: $page.cursor,
                       hasMore: $page.hasMore
                     }
                end
                    """,
                    operation="verify comment",
                    jq_args=(
                        "--argjson",
                        "id",
                        str(comment_id),
                        "--arg",
                        "type",
                        content_type,
                        "--rawfile",
                        "expectedContent",
                        str(expected_content_path),
                        "--slurpfile",
                        "expectedImages",
                        str(expected_images_path),
                    ),
                )
            except OSError as exc:
                raise PortalVerificationError(
                    "Comment read-back response could not be measured"
                ) from exc
            except PortalInvalidResponseError as exc:
                raise PortalVerificationError("Comment read-back response is invalid") from exc
        finally:
            await _remove_private_file(response_path)

        target_count = page.get("targetCount")
        match_count = page.get("matchCount")
        if target_count != 0:
            if target_count != 1 or match_count != 1:
                raise PortalVerificationError(
                    "Comment read-back does not match its create response"
                )
            match = page.get("match")
            if not isinstance(match, Mapping):
                raise PortalVerificationError("Comment read-back has invalid fields")
            returned_id = match.get("id")
            returned_images = match.get("imageList")
            returned_content = match.get("content")
            if (
                isinstance(returned_id, bool)
                or returned_id != comment_id
                or not isinstance(returned_images, list)
                or not isinstance(returned_content, str)
            ):
                raise PortalVerificationError("Comment read-back has invalid fields")
            return (
                VerifiedComment(
                    comment_id=returned_id,
                    content_type=content_type,
                    rendered_content=returned_content,
                    image_list=tuple(returned_images),
                    status=_optional_int(match.get("status")),
                ),
                str(page["cursor"]),
                bool(page["hasMore"]),
                response_bytes,
            )

        cursor = page.get("cursor")
        has_more = page.get("hasMore")
        if not isinstance(cursor, str) or not isinstance(has_more, bool):
            raise PortalVerificationError("Comment read-back has invalid pagination fields")
        return None, cursor, has_more, response_bytes

    async def close(self) -> None:
        session_dir = self._session_dir
        self._base_url = None
        self._network_args = ()
        self._auth_header_path = None
        self._session_dir = None
        self._session = None
        self._verified_topic_ids.clear()
        self._closed = True
        if session_dir is not None:
            await asyncio.to_thread(shutil.rmtree, session_dir, True)

    async def _ensure_open(self) -> None:
        if self._closed:
            raise PortalConfigurationError("PortalClient is already closed")
        if self._session_dir is not None:
            return
        base_url, token, is_loopback = await asyncio.to_thread(_load_portal_env, self._env_path)
        session_dir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="portal-"))
        await asyncio.to_thread(os.chmod, session_dir, 0o700)
        self._session_dir = session_dir
        try:
            auth_path = await self._write_private_text(f"X-User-Token: {token}\n", "auth-header")
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, session_dir, True)
            self._session_dir = None
            raise
        finally:
            token = ""
        self._base_url = base_url
        self._auth_header_path = auth_path
        self._network_args = ("--noproxy", "*") if is_loopback else ()

    def _require_prepared(self) -> PortalSession:
        if self._session is None:
            raise PortalPreparationError("PortalClient.prepare must succeed first")
        return self._session

    async def _get(
        self,
        path: str,
        *,
        operation: str,
        query: tuple[str, ...] = (),
    ) -> Path:
        last_reason = "transport failure"
        for _attempt in range(self._get_attempts):
            response_path = await self._private_path("get-response")
            args: list[str] = []
            if query:
                args.append("--get")
                for value in query:
                    args.extend(("--data-urlencode", value))
            result = await self._curl("GET", path, response_path=response_path, extra=tuple(args))
            try:
                envelope = await self._read_envelope(response_path, operation=operation)
            except PortalInvalidResponseError:
                envelope = None
                last_reason = "invalid JSON response"
            if envelope is not None and envelope["success"] is False:
                error = await self._business_error(operation, envelope)
                await _remove_private_file(response_path)
                raise error
            if result.returncode == 0 and envelope is not None:
                return response_path
            await _remove_private_file(response_path)
            last_reason = "curl did not complete successfully"
        raise PortalTransportError(
            f"Portal {operation} failed after {self._get_attempts} read attempts: {last_reason}"
        )

    async def _post(
        self,
        path: str,
        *,
        operation: str,
        extra: tuple[str, ...],
        before_write: Callable[[], Awaitable[None]] | None = None,
    ) -> Path:
        response_path = await self._private_path("post-response")
        if before_write is not None:
            await before_write()
        result = await self._curl("POST", path, response_path=response_path, extra=extra)
        if result.returncode != 0:
            raise PortalWriteUncertainError(
                f"Portal {operation} had an ambiguous transport result; do not retry"
            )
        try:
            envelope = await self._read_envelope(response_path, operation=operation)
        except PortalInvalidResponseError as exc:
            raise PortalWriteUncertainError(
                f"Portal {operation} returned no trustworthy response; do not retry"
            ) from exc
        if envelope["success"] is False:
            raise await self._business_error(operation, envelope)
        return response_path

    async def _business_error(
        self, operation: str, envelope: Mapping[str, Any]
    ) -> PortalBusinessError:
        message = str(envelope["message"])
        auth_path = self._auth_header_path
        if auth_path is not None:
            try:
                header = await asyncio.to_thread(auth_path.read_text, encoding="utf-8")
            except OSError:
                header = ""
            prefix = "X-User-Token: "
            if header.startswith(prefix):
                token = header.removeprefix(prefix).rstrip("\r\n")
                if token:
                    message = message.replace(token, "[redacted]")
        return PortalBusinessError(operation, int(envelope["errorCode"]), message)

    async def _curl(
        self,
        method: str,
        path: str,
        *,
        response_path: Path,
        extra: tuple[str, ...],
    ) -> CommandResult:
        await self._ensure_open()
        if self._base_url is None or self._auth_header_path is None:
            raise PortalConfigurationError("Portal session is not initialized")
        protocol = urlsplit(self._base_url).scheme
        argv = (
            "curl",
            "-q",
            "-sS",
            "--fail-with-body",
            "--no-location",
            "--proto",
            f"={protocol}",
            "--proto-redir",
            f"={protocol}",
            "--connect-timeout",
            "10",
            "--max-time",
            "60",
            "--max-filesize",
            str(self._max_response_bytes),
            *self._network_args,
            "-X",
            method,
            "-H",
            f"@{self._auth_header_path}",
            *extra,
            f"{self._base_url}{path}",
            "-o",
            str(response_path),
        )
        result = await self._run(argv, None)
        try:
            response_size = await asyncio.to_thread(response_path.stat)
        except OSError:
            response_size = None
        if response_size is not None and response_size.st_size > self._max_response_bytes:
            await asyncio.to_thread(response_path.write_bytes, b"")
            return CommandResult(
                returncode=result.returncode or 63,
                stdout=result.stdout,
                stderr=b"Portal response exceeded the configured byte limit",
            )
        return result

    async def _read_envelope(self, response_path: Path, *, operation: str) -> dict[str, Any]:
        return await self._jq_object(
            response_path,
            """
            if type != "object" or
               (.success | type) != "boolean" or
               (.errorCode | type) != "number" or
               (.errorCode | floor) != .errorCode or
               (.message | type) != "string"
            then error("invalid response envelope")
            else {success, errorCode, message}
            end
            """,
            operation=operation,
        )

    async def _jq_object(
        self,
        source_path: Path,
        expression: str,
        *,
        operation: str,
        jq_args: tuple[str, ...] = (),
        write: bool = False,
    ) -> dict[str, Any]:
        result = await self._run(("jq", "-ce", *jq_args, expression, str(source_path)), None)
        if result.returncode != 0:
            error_type = PortalWriteUncertainError if write else PortalInvalidResponseError
            raise error_type(f"Portal {operation} response has an invalid structure")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            error_type = PortalWriteUncertainError if write else PortalInvalidResponseError
            raise error_type(f"Portal {operation} response could not be normalized") from exc
        if not isinstance(value, dict):
            error_type = PortalWriteUncertainError if write else PortalInvalidResponseError
            raise error_type(f"Portal {operation} response is not an object")
        return value

    async def _run(self, argv: tuple[str, ...], stdin: bytes | None) -> CommandResult:
        return await self._runner(argv, stdin)

    async def _private_path(self, label: str) -> Path:
        if self._session_dir is None:
            raise PortalConfigurationError("Portal session is not initialized")

        def create() -> Path:
            descriptor, raw_path = tempfile.mkstemp(prefix=f"{label}-", dir=self._session_dir)
            os.close(descriptor)
            os.chmod(raw_path, 0o600)
            return Path(raw_path)

        return await asyncio.to_thread(create)

    async def _write_private_bytes(self, value: bytes, label: str) -> Path:
        path = await self._private_path(label)
        await asyncio.to_thread(path.write_bytes, value)
        await asyncio.to_thread(os.chmod, path, 0o600)
        return path

    async def _write_private_text(self, value: str, label: str) -> Path:
        return await self._write_private_bytes(value.encode("utf-8"), label)

    @staticmethod
    def _validate_user_state(user: Mapping[str, object]) -> None:
        status_value = user.get("status")
        if status_value is not None and status_value != 0:
            raise PortalPreparationError("Portal account is not active")
        if user.get("forbidden") is True:
            raise PortalPreparationError("Portal account is forbidden from posting")

    @staticmethod
    def _validate_write_restrictions(
        user: Mapping[str, object], config: Mapping[str, object]
    ) -> None:
        if config.get("installed") is not True:
            raise PortalPreparationError("Portal is not installed")
        if config.get("topicEnabled") is not True:
            raise PortalPreparationError("Portal topic module is disabled")
        if config.get("topicCaptcha") is True:
            raise PortalPreparationError(
                "Portal topic captcha requires an interactive publish flow"
            )
        email_verified = user.get("emailVerified")
        if (
            config.get("createTopicEmailVerified") is True
            or config.get("createCommentEmailVerified") is True
        ) and email_verified is not True:
            raise PortalPreparationError(
                "Portal requires a verified email for topic or comment creation"
            )

        observe_seconds = config.get("userObserveSeconds")
        if isinstance(observe_seconds, bool) or not isinstance(observe_seconds, int):
            raise PortalInvalidResponseError("Portal observation config is invalid")
        if observe_seconds <= 0:
            return
        create_time = user.get("createTime")
        if isinstance(create_time, bool) or not isinstance(create_time, int):
            raise PortalPreparationError(
                "Portal account age cannot be verified for the configured observation period"
            )
        if create_time + observe_seconds * 1000 > int(time.time() * 1000):
            raise PortalPreparationError(
                "Portal account is still in the configured observation period"
            )


def build_publication_content(
    video: VideoMetadata | Mapping[str, object],
    analysis: AnalysisOutcome | AnalysisProjection | Mapping[str, object],
    translated_text: str,
    source_language: str,
    source_text: str,
) -> PublicationContent:
    """Build the topic and transcript comments without performing any I/O."""

    title = _video_value(video, "title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("video title must be a non-empty string")
    if not translated_text.strip():
        raise ValueError("translated_text must not be empty")
    if not source_text.strip():
        raise ValueError("source_text must not be empty")
    if not source_language.strip():
        raise ValueError("source_language must not be empty")

    projection, payload = _analysis_parts(analysis)
    video_url = _video_value(video, "video_url")
    channel = _video_value(video, "channel")
    channel_title = _object_value(channel, "title")
    channel_url = _object_value(channel, "channel_url")
    published_at = _video_value(video, "published_at")
    publication_title = _build_publication_title(
        title,
        channel_title,
        published_at,
        payload,
    )

    summary = _object_value(projection, "translated_summary") or _object_value(
        projection, "summary"
    )
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("analysis summary must be a non-empty string")
    lines = ["## 摘要", "", _escape_markdown_text(summary.strip()), ""]

    lines.extend(["## 视频信息", ""])
    safe_video_url = _safe_http_url(video_url)
    escaped_title = _escape_markdown_text(title.strip())
    if safe_video_url is not None:
        lines.append(f"- 标题：[{escaped_title}]({safe_video_url})")
    else:
        lines.append(f"- 标题：{escaped_title}")
    if isinstance(channel_title, str) and channel_title:
        escaped_channel_title = _escape_markdown_text(channel_title)
        safe_channel_url = _safe_http_url(channel_url)
        if safe_channel_url is not None:
            lines.append(f"- 频道：[{escaped_channel_title}]({safe_channel_url})")
        else:
            lines.append(f"- 频道：{escaped_channel_title}")
    if isinstance(published_at, datetime):
        lines.append(f"- 发布时间：{published_at.isoformat()}")
    escaped_source_language = _escape_markdown_text(source_language.strip())
    lines.append(f"- 字幕语言：{escaped_source_language}")

    lines.extend(["", "## AI 分析", ""])
    background = _object_value(projection, "background_notes")
    if isinstance(background, str) and background.strip():
        lines.extend(["### 背景补充", "", _escape_markdown_text(background.strip()), ""])

    key_points = _object_value(projection, "key_points")
    if isinstance(key_points, Sequence) and not isinstance(key_points, str | bytes):
        rendered_points = [
            _escape_markdown_text(_render_analysis_value(point)) for point in key_points
        ]
        rendered_points = [point for point in rendered_points if point]
        if rendered_points:
            lines.extend(["### 关键要点", ""])
            lines.extend(f"- {point}" for point in rendered_points)
            lines.append("")

    score_lines: list[str] = []
    is_relevant = _object_value(projection, "is_relevant")
    relevance_score = _object_value(projection, "relevance_score")
    quality_score = _object_value(projection, "quality_score")
    if isinstance(is_relevant, bool):
        score_lines.append(f"- 是否相关：{'是' if is_relevant else '否'}")
    if isinstance(relevance_score, int | float) and not isinstance(relevance_score, bool):
        score_lines.append(f"- 相关度：{relevance_score:g}")
    if isinstance(quality_score, int | float) and not isinstance(quality_score, bool):
        score_lines.append(f"- 内容质量：{quality_score:g}")
    filter_reason = payload.get("filter_reason") if isinstance(payload, Mapping) else None
    if isinstance(filter_reason, str) and filter_reason.strip():
        score_lines.append(f"- 过滤理由：{_escape_markdown_text(filter_reason.strip())}")
    if score_lines:
        lines.extend(["### 评估", "", *score_lines, ""])

    tags = _publication_tags(projection)
    if tags:
        lines.extend(
            [
                "### 标签",
                "",
                "、".join(_escape_markdown_text(tag) for tag in tags),
                "",
            ]
        )

    sources = payload.get("sources") if isinstance(payload, Mapping) else None
    if isinstance(sources, Sequence) and not isinstance(sources, str | bytes):
        rendered_sources = [_render_source(source) for source in sources]
        rendered_sources = [source for source in rendered_sources if source]
        if rendered_sources:
            lines.extend(["### 参考资料", "", *rendered_sources, ""])

    topic_markdown = "\n".join(lines).rstrip() + "\n"
    translated_comment = "## 中文翻译\n\n" + _fenced_transcript(translated_text) + "\n"
    source_comment = None
    if not is_simplified_chinese_language(source_language):
        source_comment = (
            f"## 原文字幕（{escaped_source_language}）\n\n" + _fenced_transcript(source_text) + "\n"
        )
    return PublicationContent(
        title=publication_title,
        topic_markdown=topic_markdown,
        translated_comment_markdown=translated_comment,
        source_comment_markdown=source_comment,
        tags=(),
    )


def _build_publication_title(
    video_title: str,
    channel_title: object,
    published_at: object,
    payload: Mapping[str, object],
) -> str:
    subject = _publication_guest_subject(payload, video_title)
    channel = _normalized_title_component(channel_title) or "频道未知"
    published_date = (
        published_at.date().isoformat() if isinstance(published_at, datetime) else "日期未知"
    )
    separator = "｜"
    rendered = separator.join((subject, channel, published_date))
    if len(rendered) <= 128:
        return rendered

    channel = _truncate_title_component(channel, 32)
    subject_limit = 128 - len(channel) - len(published_date) - 2 * len(separator)
    subject = _truncate_title_component(subject, max(subject_limit, 1))
    return separator.join((subject, channel, published_date))[:128]


def _publication_guest_subject(
    payload: Mapping[str, object],
    video_title: str,
) -> str:
    fallback = _normalized_title_component(video_title)
    raw_guests = payload.get("guests")
    if raw_guests is None:
        return fallback
    if not isinstance(raw_guests, Sequence) or isinstance(raw_guests, str | bytes):
        raise ValueError("analysis guests must be an array")

    rendered: list[str] = []
    for guest in raw_guests:
        if not isinstance(guest, Mapping):
            raise ValueError("each analysis guest must be an object")
        name = _normalized_title_component(guest.get("name_original"))
        if "title" not in guest:
            raise ValueError("analysis guest title is required")
        raw_guest_title = guest.get("title")
        guest_title = (
            "身份未核实"
            if raw_guest_title is None
            else _normalized_title_component(raw_guest_title)
        )
        if not name or not guest_title:
            raise ValueError("analysis guest name_original and title must not be empty")
        rendered.append(f"{name}（{guest_title}）")
    return "、".join(rendered) if rendered else fallback


def _normalized_title_component(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _truncate_title_component(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


def _load_portal_env(env_path: Path) -> tuple[str, str, bool]:
    try:
        metadata = env_path.lstat()
    except FileNotFoundError as exc:
        raise PortalConfigurationError("Portal credential file does not exist") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PortalConfigurationError("Portal credential path must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PortalConfigurationError("Portal credential file permissions must be 0600")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(env_path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
            opened_metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or stat.S_IMODE(opened_metadata.st_mode) != 0o600
                or opened_metadata.st_dev != metadata.st_dev
                or opened_metadata.st_ino != metadata.st_ino
            ):
                raise PortalConfigurationError(
                    "Portal credential file changed while it was being opened"
                )
            lines = stream.read().splitlines()
    except UnicodeError as exc:
        raise PortalConfigurationError("Portal credential file must be UTF-8") from exc

    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PortalConfigurationError("Portal credential file contains invalid content")
        key, value = line.split("=", 1)
        if key not in {"BBS_BASE_URL", "BBS_TOKEN"} or key in values:
            raise PortalConfigurationError(
                "Portal credential file must contain only the two supported keys"
            )
        values[key] = value
    if set(values) != {"BBS_BASE_URL", "BBS_TOKEN"}:
        raise PortalConfigurationError(
            "Portal credential file must define BBS_BASE_URL and BBS_TOKEN exactly once"
        )

    configured_base_url = values["BBS_BASE_URL"].rstrip("/")
    token = values["BBS_TOKEN"]
    if not token or _TOKEN_PATTERN.fullmatch(token) is None:
        raise PortalConfigurationError("BBS_TOKEN is empty or contains invalid characters")
    try:
        parsed = urlsplit(configured_base_url)
        port = parsed.port
    except ValueError as exc:
        raise PortalConfigurationError("BBS_BASE_URL is invalid") from exc
    if (
        any(character.isspace() for character in configured_base_url)
        or any(ord(character) < 32 or ord(character) == 127 for character in configured_base_url)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise PortalConfigurationError("BBS_BASE_URL must be an HTTP(S) origin")
    is_loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not is_loopback:
        raise PortalConfigurationError("Remote portal credentials require HTTPS")
    if parsed.scheme not in {"http", "https"}:
        raise PortalConfigurationError("BBS_BASE_URL must use HTTP or HTTPS")
    hostname = parsed.hostname.casefold()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme == "http" else 443
    rendered_port = "" if port is None or port == default_port else f":{port}"
    origin = f"{parsed.scheme}://{rendered_host}{rendered_port}"
    return origin, token, is_loopback


def _safe_message(message: str) -> str:
    compact = " ".join(message.split())
    if len(compact) <= _MAX_SAFE_MESSAGE_CHARS:
        return compact
    return compact[:_MAX_SAFE_MESSAGE_CHARS] + "..."


def _validate_topic_id(topic_id: str) -> None:
    if not isinstance(topic_id, str) or not topic_id:
        raise ValueError("topic_id must be a non-empty opaque string")
    if any(ord(character) < 32 or ord(character) == 127 for character in topic_id):
        raise ValueError("topic_id contains control characters")


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _format_categories(value: object) -> str:
    if not isinstance(value, list):
        return "none"
    rendered: list[str] = []
    for item in value[:20]:
        if not isinstance(item, Mapping):
            continue
        category_id = item.get("id")
        name = item.get("name")
        if isinstance(category_id, int) and isinstance(name, str):
            rendered.append(f"{category_id}:{_safe_message(name)}")
    return ", ".join(rendered) or "none"


def _video_value(video: object, name: str) -> object:
    return _object_value(video, name)


def _object_value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _analysis_parts(
    analysis: AnalysisOutcome | AnalysisProjection | Mapping[str, object],
) -> tuple[object, Mapping[str, object]]:
    if isinstance(analysis, AnalysisOutcome):
        return analysis.projection, analysis.payload
    if isinstance(analysis, AnalysisProjection):
        return analysis, {}
    if isinstance(analysis, Mapping):
        projection = analysis.get("projection")
        payload = analysis.get("payload")
        if projection is not None:
            return projection, payload if isinstance(payload, Mapping) else analysis
        return analysis, analysis
    raise TypeError("analysis must be an AnalysisOutcome, projection, or mapping")


def _publication_tags(projection: object) -> tuple[str, ...]:
    values = _object_value(projection, "tags")
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return ()
    tags: list[str] = []
    for value in values:
        name = _object_value(value, "name")
        if isinstance(name, str) and name.strip() and name.strip() not in tags:
            tags.append(name.strip())
    return tuple(tags[:12])


def _render_analysis_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("title", "point", "content", "summary", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _render_source(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    title = value.get("title")
    url = value.get("url")
    note = value.get("note")
    if not isinstance(title, str) or not title.strip():
        return ""
    label = _escape_markdown_text(title.strip())
    safe_url = _safe_http_url(url)
    rendered = f"- [{label}]({safe_url})" if safe_url is not None else f"- {label}"
    if isinstance(note, str) and note.strip():
        rendered += f"：{_escape_markdown_text(note.strip())}"
    return rendered


def _escape_markdown_text(value: str) -> str:
    """Render untrusted values as text in a Markdown document."""

    return "".join(
        f"\\{character}" if character in string.punctuation else character for character in value
    )


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in candidate
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        _port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    normalized = parsed._replace(scheme=parsed.scheme.casefold()).geturl()
    return quote(normalized, safe=":/?#[]@!$&'+,;=%~._-")


def _fenced_transcript(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    separator = "" if text.endswith("\n") else "\n"
    return f"{fence}text\n{text}{separator}{fence}"
