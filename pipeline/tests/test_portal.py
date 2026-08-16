from __future__ import annotations

import asyncio
import json
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from models import AnalysisOutcome, AnalysisProjection, ChannelMetadata, VideoMetadata
from portal import (
    CommandResult,
    CreatedComment,
    PortalBusinessError,
    PortalClient,
    PortalConfigurationError,
    PortalPreparationError,
    PortalTopicPendingReviewError,
    PortalTransportError,
    PortalVerificationError,
    PortalWriteUncertainError,
    build_publication_content,
)
from subtitles import is_simplified_chinese_language

TOKEN = "test-token_123.~"
TOPIC_ID = "opaque-topic-id"
TITLE = "A portal topic"
LONG_TRANSCRIPT = "private transcript " * 200


def _response(data: object, *, success: bool = True) -> str:
    return json.dumps(
        {"success": success, "errorCode": 0, "message": "", "data": data},
        ensure_ascii=False,
    )


def _failure(code: int = 1001, message: str = "operation rejected") -> str:
    return json.dumps({"success": False, "errorCode": code, "message": message, "data": None})


def _current_user(**overrides: object) -> str:
    data: dict[str, object] = {
        "id": "encoded-user-id",
        "username": "publisher",
        "emailVerified": True,
        "status": 0,
        "forbidden": False,
        "createTime": 1_700_000_000_000,
    }
    data.update(overrides)
    return _response(data)


def _config(**overrides: object) -> str:
    data: dict[str, object] = {
        "installed": True,
        "loginRequired": True,
        "modules": {"topic": True, "tweet": True, "qa": True},
        "topicCaptcha": False,
        "createTopicEmailVerified": True,
        "createCommentEmailVerified": True,
        "userObserveSeconds": 0,
    }
    data.update(overrides)
    return _response(data)


def _categories(*items: dict[str, object]) -> str:
    values = list(items) or [{"id": 7, "name": "Youtube", "type": "normal", "children": []}]
    return _response(values)


def _created_topic(*, id_value: object = TOPIC_ID, status: int = 0, title: str = TITLE) -> str:
    return _response(
        {
            "id": id_value,
            "type": 0,
            "title": title,
            "status": status,
            "category": {"id": 7, "name": "Youtube", "type": "normal"},
            "tags": [{"name": "AI"}],
            "summary": "safe summary",
        }
    )


def _topic_detail(*, status: int = 0, title: str = TITLE, topic_id: str = TOPIC_ID) -> str:
    return _response(
        {
            "id": topic_id,
            "title": title,
            "status": status,
            "category": {"id": 7, "name": "Youtube", "type": "normal"},
            "content": "<p>rendered topic</p>",
        }
    )


def _created_comment(*, id_value: object = 41, content: str = "<p>rendered</p>") -> str:
    return _response(
        {
            "id": id_value,
            "entityType": "topic",
            "contentType": "markdown",
            "content": content,
            "imageList": [],
            "status": 0,
        }
    )


def _comments(*comments: dict[str, object], cursor: str = "", has_more: bool = False) -> str:
    return _response({"results": list(comments), "cursor": cursor, "hasMore": has_more})


@dataclass(frozen=True)
class FakeResponse:
    body: str
    returncode: int = 0


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


class FakeRunner:
    def __init__(self, responses: list[str | FakeResponse]) -> None:
        self.responses = [
            response if isinstance(response, FakeResponse) else FakeResponse(response)
            for response in responses
        ]
        self.calls: list[tuple[str, ...]] = []
        self.curl_calls: list[tuple[str, ...]] = []
        self.header_path: Path | None = None
        self.header_mode: int | None = None
        self.header_is_expected = False
        self.output_modes: list[int] = []
        self.output_paths: list[Path] = []
        self.topic_requests: list[dict[str, Any]] = []
        self.comment_request_bodies: list[str] = []

    async def __call__(self, argv: tuple[str, ...], stdin: bytes | None) -> CommandResult:
        self.calls.append(argv)
        if argv[0] == "jq":
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate(stdin)
            return CommandResult(process.returncode, stdout, stderr)

        assert argv[0] == "curl"
        assert argv[1] == "-q"
        assert "--no-location" in argv
        self.curl_calls.append(argv)
        assert self.responses, f"Unexpected curl call: {argv}"
        header_argument = argv[argv.index("-H") + 1]
        assert header_argument.startswith("@")
        self.header_path = Path(header_argument[1:])
        self.header_mode = await asyncio.to_thread(_file_mode, self.header_path)
        self.header_is_expected = (
            await asyncio.to_thread(_read_text, self.header_path) == f"X-User-Token: {TOKEN}\n"
        )

        output_path = Path(argv[argv.index("-o") + 1])
        self.output_paths.append(output_path)
        self.output_modes.append(await asyncio.to_thread(_file_mode, output_path))
        fake_response = self.responses.pop(0)
        await asyncio.to_thread(_write_text, output_path, fake_response.body)

        if "--data-binary" in argv:
            request_argument = argv[argv.index("--data-binary") + 1]
            assert request_argument.startswith("@")
            request_path = Path(request_argument[1:])
            assert await asyncio.to_thread(_file_mode, request_path) == 0o600
            self.topic_requests.append(
                json.loads(await asyncio.to_thread(_read_text, request_path))
            )
        for index, argument in enumerate(argv):
            if argument == "--data-urlencode" and index + 1 < len(argv):
                value = argv[index + 1]
                if value.startswith("content@"):
                    content_path = Path(value.removeprefix("content@"))
                    assert await asyncio.to_thread(_file_mode, content_path) == 0o600
                    self.comment_request_bodies.append(
                        await asyncio.to_thread(_read_text, content_path)
                    )
        return CommandResult(fake_response.returncode)


def _env_file(
    tmp_path: Path,
    *,
    base_url: str = "https://portal.test",
    token: str = TOKEN,
    mode: int = 0o600,
    extra: str = "",
) -> Path:
    path = tmp_path / "portal.env"
    path.write_text(f"BBS_BASE_URL={base_url}\nBBS_TOKEN={token}\n{extra}", encoding="utf-8")
    path.chmod(mode)
    return path


def _prepared_responses(*extra: str | FakeResponse) -> list[str | FakeResponse]:
    return [_current_user(), _config(), _categories(), *extra]


async def _prepare(
    tmp_path: Path, responses: list[str | FakeResponse]
) -> tuple[PortalClient, FakeRunner]:
    runner = FakeRunner(responses)
    client = PortalClient(_env_file(tmp_path), runner=runner)
    await client.prepare()
    return client, runner


@pytest.mark.parametrize(
    ("mode", "base_url", "token", "extra"),
    [
        (0o644, "https://portal.test", TOKEN, ""),
        (0o600, "http://portal.test", TOKEN, ""),
        (0o600, "ftp://portal.test", TOKEN, ""),
        (0o600, "https://portal.test", "bad token", ""),
        (0o600, "https://portal.test", TOKEN, "OTHER=value\n"),
        (0o600, "https://portal.test", TOKEN, "BBS_TOKEN=duplicate\n"),
        (0o600, "https://portal.test/path", TOKEN, ""),
        (0o600, "https://portal .test", TOKEN, ""),
    ],
)
async def test_invalid_env_is_rejected_before_any_command(
    tmp_path: Path, mode: int, base_url: str, token: str, extra: str
) -> None:
    env_path = _env_file(tmp_path, mode=mode, base_url=base_url, token=token, extra=extra)
    runner = FakeRunner([])
    client = PortalClient(env_path, runner=runner)

    with pytest.raises(PortalConfigurationError) as caught:
        await client.prepare()

    assert TOKEN not in str(caught.value)
    assert runner.calls == []


async def test_loopback_http_uses_noproxy_and_cleans_private_header(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(_prepared_responses())
    env_path = _env_file(tmp_path, base_url="http://127.0.0.1:8080")

    async with PortalClient(env_path, runner=runner) as client:
        session = await client.prepare()
        assert session.category_id == 7
        assert session.origin == "http://127.0.0.1:8080"
        assert session.user_id == "encoded-user-id"
        assert all("--noproxy" in call and "*" in call for call in runner.curl_calls)
        assert all(
            call[1] == "-q"
            and "--no-location" in call
            and call[call.index("--proto") + 1] == "=http"
            and call[call.index("--proto-redir") + 1] == "=http"
            and "--connect-timeout" in call
            and call[call.index("--connect-timeout") + 1] == "10"
            and "--max-time" in call
            and call[call.index("--max-time") + 1] == "60"
            and call[call.index("--max-filesize") + 1] == str(8 * 1024 * 1024)
            for call in runner.curl_calls
        )
        assert all(TOKEN not in argument for call in runner.curl_calls for argument in call)
        assert runner.header_mode == 0o600
        assert runner.header_is_expected
        assert runner.output_modes == [0o600, 0o600, 0o600]
        header_path = runner.header_path

    assert header_path is not None
    assert not header_path.exists()


async def test_remote_https_does_not_add_noproxy(tmp_path: Path) -> None:
    client, runner = await _prepare(tmp_path, _prepared_responses())
    try:
        assert all("--noproxy" not in call for call in runner.curl_calls)
    finally:
        await client.close()


async def test_configured_origin_is_available_before_authenticated_requests(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([])

    async with PortalClient(
        _env_file(tmp_path, base_url="HTTPS://Portal.TEST:443/"),
        runner=runner,
    ) as client:
        assert client.configured_origin == "https://portal.test"
        assert runner.calls == []


async def test_origin_is_canonical_and_current_user_id_is_required(tmp_path: Path) -> None:
    runner = FakeRunner(_prepared_responses())
    client = PortalClient(_env_file(tmp_path, base_url="HTTPS://Portal.TEST:443/"), runner=runner)
    try:
        session = await client.prepare()
        assert session.origin == "https://portal.test"
        assert session.user_id == "encoded-user-id"
        assert all(
            any(argument.startswith("https://portal.test/") for argument in call)
            for call in runner.curl_calls
        )
        assert all(call[call.index("--proto") + 1] == "=https" for call in runner.curl_calls)
    finally:
        await client.close()

    for invalid_id in (None, "", 7):
        invalid_client = PortalClient(
            _env_file(tmp_path), runner=FakeRunner([_current_user(id=invalid_id)])
        )
        try:
            with pytest.raises(PortalPreparationError, match="token is invalid"):
                await invalid_client.prepare()
        finally:
            await invalid_client.close()


async def test_first_q_option_prevents_default_curlrc_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    curl_home = tmp_path / "curl-home"
    curl_home.mkdir()
    (curl_home / ".curlrc").write_text(
        'header = "X-Loaded-From-Curlrc: yes"\nlocation\n', encoding="utf-8"
    )
    monkeypatch.setenv("CURL_HOME", str(curl_home))

    requests: list[bytes] = []
    bodies = {
        "/api/user/current": _current_user(),
        "/api/config/configs": _config(),
        "/api/topic/categories": _categories(),
    }

    async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            request_target = request.split(b" ", 2)[1].decode("ascii")
            path = request_target.partition("?")[0]
            body = bodies[path].encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_request, "127.0.0.1", 0)
    try:
        socket = server.sockets[0]
        port = int(socket.getsockname()[1])
        async with server:
            async with PortalClient(
                _env_file(tmp_path, base_url=f"http://127.0.0.1:{port}")
            ) as client:
                session = await client.prepare()
                assert session.user_id == "encoded-user-id"
    finally:
        server.close()
        await server.wait_closed()

    assert len(requests) == 3
    assert all(b"X-Loaded-From-Curlrc" not in request for request in requests)


async def test_chunked_response_is_stopped_before_it_can_exceed_disk_limit(
    tmp_path: Path,
) -> None:
    total_chunks = 2048
    sent_chunks = 0
    handler_done = asyncio.Event()

    async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal sent_chunks
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            for _index in range(total_chunks):
                chunk = b"x" * 4096
                writer.write(b"1000\r\n" + chunk + b"\r\n")
                await writer.drain()
                sent_chunks += 1
                await asyncio.sleep(0.001)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handler_done.set()

    server = await asyncio.start_server(handle_request, "127.0.0.1", 0)
    client: PortalClient | None = None
    try:
        socket = server.sockets[0]
        port = int(socket.getsockname()[1])
        async with server:
            client = PortalClient(
                _env_file(tmp_path, base_url=f"http://127.0.0.1:{port}"),
                get_attempts=1,
                max_response_bytes=512,
            )
            with pytest.raises(PortalTransportError, match="read attempts"):
                await client.prepare()
            assert client._session_dir is not None
            response_paths = list(client._session_dir.glob("get-response-*"))
            assert response_paths == []
            await asyncio.wait_for(handler_done.wait(), timeout=2)
    finally:
        if client is not None:
            await client.close()
        server.close()
        await server.wait_closed()

    assert sent_chunks < total_chunks


async def test_symlinked_env_is_rejected(tmp_path: Path) -> None:
    target = _env_file(tmp_path)
    symlink = tmp_path / "linked.env"
    symlink.symlink_to(target)
    client = PortalClient(symlink, runner=FakeRunner([]))

    with pytest.raises(PortalConfigurationError, match="regular file"):
        await client.prepare()


@pytest.mark.parametrize(
    "categories",
    [
        _categories({"id": 1, "name": "Other", "type": "normal"}),
        _categories(
            {"id": 1, "name": "Youtube", "type": "normal"},
            {"id": 2, "name": "Youtube", "type": "normal"},
        ),
    ],
)
async def test_prepare_requires_one_exact_normal_youtube_category(
    tmp_path: Path, categories: str
) -> None:
    runner = FakeRunner([_current_user(), _config(), categories])
    client = PortalClient(_env_file(tmp_path), runner=runner)
    try:
        with pytest.raises(PortalPreparationError, match="missing or not unique"):
            await client.prepare()
    finally:
        await client.close()


async def test_current_user_must_contain_a_username(tmp_path: Path) -> None:
    runner = FakeRunner([_response(None)])
    client = PortalClient(_env_file(tmp_path), runner=runner)
    try:
        with pytest.raises(PortalPreparationError, match="token is invalid"):
            await client.prepare()
    finally:
        await client.close()


async def test_category_match_is_case_sensitive_and_normal_only(tmp_path: Path) -> None:
    response = _categories(
        {"id": 1, "name": "youtube", "type": "normal"},
        {"id": 2, "name": "Youtube", "type": "system"},
        {
            "id": 3,
            "name": "Parent",
            "type": "normal",
            "children": [{"id": 9, "name": "Youtube", "type": "normal"}],
        },
    )
    client, _runner = await _prepare(tmp_path, [_current_user(), _config(), response])
    try:
        assert client.session is not None
        assert client.session.category_id == 9
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("user", "config", "message"),
    [
        (_current_user(), _config(topicCaptcha=True), "captcha"),
        (
            _current_user(emailVerified=False),
            _config(createTopicEmailVerified=True),
            "verified email",
        ),
        (
            _current_user(),
            _config(modules={"topic": False}),
            "module is disabled",
        ),
        (
            _current_user(createTime=9_999_999_999_999),
            _config(userObserveSeconds=60),
            "observation period",
        ),
    ],
)
async def test_prepare_enforces_determinable_write_restrictions(
    tmp_path: Path, user: str, config: str, message: str
) -> None:
    runner = FakeRunner([user, config])
    client = PortalClient(_env_file(tmp_path), runner=runner)
    try:
        with pytest.raises(PortalPreparationError, match=message):
            await client.prepare()
        assert len(runner.curl_calls) == 2
    finally:
        await client.close()


async def test_http_200_business_failure_is_not_transport_uncertainty(
    tmp_path: Path,
) -> None:
    client, runner = await _prepare(
        tmp_path, _prepared_responses(_failure(1004, "email is unverified"))
    )
    try:
        with pytest.raises(PortalBusinessError) as caught:
            await client.create_topic(TITLE, "body")
        assert caught.value.error_code == 1004
        assert "email is unverified" in str(caught.value)
        assert len([call for call in runner.curl_calls if "-X" in call and "POST" in call]) == 1
    finally:
        await client.close()


async def test_business_error_redacts_token_from_server_message(tmp_path: Path) -> None:
    client, _runner = await _prepare(
        tmp_path,
        _prepared_responses(_failure(1001, f"invalid credential {TOKEN}")),
    )
    try:
        with pytest.raises(PortalBusinessError) as caught:
            await client.create_topic(TITLE, "body")
        assert TOKEN not in str(caught.value)
        assert "[redacted]" in str(caught.value)
    finally:
        await client.close()


async def test_get_retries_invalid_response_but_post_is_never_retried(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            "not-json",
            "still-not-json",
            _current_user(),
            _config(),
            _categories(),
            FakeResponse("gateway timeout", returncode=28),
        ]
    )
    client = PortalClient(_env_file(tmp_path), runner=runner, get_attempts=3)
    try:
        await client.prepare()
        with pytest.raises(PortalWriteUncertainError, match="do not retry") as caught:
            await client.create_topic(TITLE, LONG_TRANSCRIPT)
        current_calls = [
            call for call in runner.curl_calls if call[-3].endswith("/api/user/current")
        ]
        post_calls = [call for call in runner.curl_calls if "POST" in call]
        assert len(current_calls) == 3
        assert len(post_calls) == 1
        assert TOKEN not in str(caught.value)
        assert LONG_TRANSCRIPT not in str(caught.value)
    finally:
        await client.close()


async def test_get_and_post_responses_are_rejected_above_byte_limit(
    tmp_path: Path,
) -> None:
    oversized_user = _current_user(padding="x" * 2048)
    get_runner = FakeRunner([oversized_user, oversized_user, oversized_user])
    get_client = PortalClient(
        _env_file(tmp_path),
        runner=get_runner,
        max_response_bytes=1024,
    )
    try:
        with pytest.raises(PortalTransportError, match="read attempts"):
            await get_client.prepare()
        assert len(get_runner.curl_calls) == 3
        assert all(not path.exists() for path in get_runner.output_paths)
        assert all(
            call[call.index("--max-filesize") + 1] == "1024" for call in get_runner.curl_calls
        )
    finally:
        await get_client.close()

    topic_data = json.loads(_created_topic())["data"]
    topic_data["padding"] = "x" * 2048
    post_runner = FakeRunner(_prepared_responses(_response(topic_data)))
    post_client = PortalClient(
        _env_file(tmp_path),
        runner=post_runner,
        max_response_bytes=1024,
    )
    try:
        await post_client.prepare()
        with pytest.raises(PortalWriteUncertainError, match="do not retry"):
            await post_client.create_topic(TITLE, "body")
        assert post_runner.output_paths[-1].stat().st_size == 0
        assert len([call for call in post_runner.curl_calls if "POST" in call]) == 1
    finally:
        await post_client.close()


async def test_nonzero_post_with_business_shaped_body_is_still_uncertain(
    tmp_path: Path,
) -> None:
    client, runner = await _prepare(
        tmp_path,
        _prepared_responses(FakeResponse(_failure(1001, "server rejected"), returncode=22)),
    )
    try:
        with pytest.raises(PortalWriteUncertainError):
            await client.create_topic(TITLE, "body")
        assert len([call for call in runner.curl_calls if "POST" in call]) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("id_value", [1, 1.5, None, ""])
async def test_topic_success_requires_nonempty_string_id(tmp_path: Path, id_value: object) -> None:
    client, _runner = await _prepare(
        tmp_path, _prepared_responses(_created_topic(id_value=id_value))
    )
    try:
        with pytest.raises(PortalWriteUncertainError):
            await client.create_topic(TITLE, "body")
    finally:
        await client.close()


async def test_topic_request_uses_prepared_category_and_hides_content_from_curl_argv(
    tmp_path: Path,
) -> None:
    client, runner = await _prepare(tmp_path, _prepared_responses(_created_topic()))
    try:
        created = await client.create_topic(TITLE, LONG_TRANSCRIPT)
        assert created.topic_id == TOPIC_ID
        assert created.status == 0
        assert created.tags == ("AI",)
        request = runner.topic_requests[0]
        assert request == {
            "type": 0,
            "categoryId": 7,
            "title": TITLE,
            "contentType": "markdown",
            "content": LONG_TRANSCRIPT,
            "tags": [],
        }
        post_call = runner.curl_calls[-1]
        assert TOKEN not in "\0".join(post_call)
        assert LONG_TRANSCRIPT not in "\0".join(post_call)
    finally:
        await client.close()


async def test_topic_before_write_runs_after_local_preparation_and_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runner = await _prepare(tmp_path, _prepared_responses(_created_topic()))
    callback_events: list[str] = []

    async def before_write() -> None:
        assert not any("POST" in call for call in runner.curl_calls)
        assert runner.topic_requests == []
        assert client._session_dir is not None
        assert list(client._session_dir.glob("post-response-*"))
        callback_events.append("before-write")

    try:
        await client.create_topic(TITLE, "body", before_write=before_write)
        assert callback_events == ["before-write"]
        assert len([call for call in runner.curl_calls if "POST" in call]) == 1

        async def reject_before_write() -> None:
            raise RuntimeError("state transition failed")

        with pytest.raises(RuntimeError, match="state transition failed"):
            await client.create_topic(TITLE, "body", before_write=reject_before_write)
        assert len([call for call in runner.curl_calls if "POST" in call]) == 1

        async def fail_local_write(_value: str, _label: str) -> Path:
            raise PortalConfigurationError("local preparation failed")

        monkeypatch.setattr(client, "_write_private_text", fail_local_write)
        with pytest.raises(PortalConfigurationError, match="local preparation failed"):
            await client.create_topic(TITLE, "body", before_write=before_write)
        assert callback_events == ["before-write"]
    finally:
        await client.close()


async def test_verify_topic_rejects_pending_review_and_title_mismatch(
    tmp_path: Path,
) -> None:
    client, _runner = await _prepare(
        tmp_path,
        _prepared_responses(_topic_detail(status=2), _topic_detail(title="other")),
    )
    try:
        with pytest.raises(PortalTopicPendingReviewError, match="status=2"):
            await client.verify_topic(TOPIC_ID, TITLE)
        with pytest.raises(PortalVerificationError, match="does not match"):
            await client.verify_topic(TOPIC_ID, TITLE)
    finally:
        await client.close()


async def test_comment_create_and_recovery_readback_use_safe_persisted_fields(
    tmp_path: Path,
) -> None:
    rendered = "<p>rendered translation</p>"
    listed = {
        "id": 41,
        "contentType": "markdown",
        "content": rendered,
        "imageList": [],
        "status": 0,
    }
    client, runner = await _prepare(
        tmp_path,
        _prepared_responses(
            _topic_detail(),
            _created_comment(content=rendered),
            _comments(listed),
            _comments(listed),
        ),
    )
    try:
        await client.verify_topic(TOPIC_ID, TITLE)
        created = await client.create_comment(TOPIC_ID, LONG_TRANSCRIPT)
        assert created == CreatedComment(
            comment_id=41,
            content_type="markdown",
            rendered_content=rendered,
            image_list=(),
            status=0,
        )
        verified = await client.verify_comment(TOPIC_ID, created)
        recovered = await client.verify_comment(
            TOPIC_ID,
            41,
            rendered,
            "markdown",
            (),
        )
        assert verified.comment_id == recovered.comment_id == 41
        assert runner.comment_request_bodies == [LONG_TRANSCRIPT]
        comment_post = [
            call
            for call in runner.curl_calls
            if any(value.endswith("/api/comment/create") for value in call)
        ][0]
        joined = "\0".join(comment_post)
        assert LONG_TRANSCRIPT not in joined
        assert TOKEN not in joined
        assert "entityType=topic" in comment_post
        assert f"entityId={TOPIC_ID}" in comment_post
        assert "contentType=markdown" in comment_post
        assert "imageList=[]" in comment_post
        verification_calls = [
            call
            for call in runner.curl_calls
            if any(value.endswith("/api/comment/comments") for value in call)
        ]
        assert len(verification_calls) == 2
        assert all("cursor=42" in call for call in verification_calls)
    finally:
        await client.close()


async def test_comment_before_write_runs_after_local_preparation_and_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runner = await _prepare(
        tmp_path,
        _prepared_responses(_topic_detail(), _created_comment()),
    )
    callback_events: list[str] = []

    async def before_write() -> None:
        assert not any(
            "POST" in call and any(value.endswith("/api/comment/create") for value in call)
            for call in runner.curl_calls
        )
        assert runner.comment_request_bodies == []
        assert client._session_dir is not None
        assert list(client._session_dir.glob("post-response-*"))
        callback_events.append("before-write")

    try:
        await client.verify_topic(TOPIC_ID, TITLE)
        await client.create_comment(TOPIC_ID, "body", before_write=before_write)
        assert callback_events == ["before-write"]

        async def reject_before_write() -> None:
            raise RuntimeError("state transition failed")

        with pytest.raises(RuntimeError, match="state transition failed"):
            await client.create_comment(TOPIC_ID, "body", before_write=reject_before_write)
        assert (
            len(
                [
                    call
                    for call in runner.curl_calls
                    if "POST" in call
                    and any(value.endswith("/api/comment/create") for value in call)
                ]
            )
            == 1
        )

        async def fail_local_write(_value: str, _label: str) -> Path:
            raise PortalConfigurationError("local preparation failed")

        monkeypatch.setattr(client, "_write_private_text", fail_local_write)
        with pytest.raises(PortalConfigurationError, match="local preparation failed"):
            await client.create_comment(TOPIC_ID, "body", before_write=before_write)
        assert callback_events == ["before-write"]
    finally:
        await client.close()


async def test_comment_recovery_readback_does_not_require_process_local_topic_state(
    tmp_path: Path,
) -> None:
    rendered = "<p>persisted rendering</p>"
    client, _runner = await _prepare(
        tmp_path,
        _prepared_responses(
            _comments(
                {
                    "id": 99,
                    "contentType": "markdown",
                    "content": rendered,
                    "imageList": [],
                    "status": 0,
                }
            )
        ),
    )
    try:
        verified = await client.verify_comment(TOPIC_ID, 99, rendered)
        assert verified.comment_id == 99
    finally:
        await client.close()


async def test_comment_readback_uses_exact_cursor_then_first_page_for_accepted_answer(
    tmp_path: Path,
) -> None:
    rendered = "<p>accepted answer</p>"
    accepted = {
        "id": 41,
        "contentType": "markdown",
        "content": rendered,
        "imageList": [],
        "status": 0,
    }
    client, runner = await _prepare(
        tmp_path,
        _prepared_responses(_comments(), _comments(accepted, cursor="41")),
    )
    try:
        verified = await client.verify_comment(TOPIC_ID, 41, rendered)
        assert verified.comment_id == 41
        calls = [
            call
            for call in runner.curl_calls
            if any(value.endswith("/api/comment/comments") for value in call)
        ]
        assert "cursor=42" in calls[0]
        assert not any(value.startswith("cursor=") for value in calls[1])
    finally:
        await client.close()


async def test_comment_readback_follows_bbs_cursor_without_reusing_it(
    tmp_path: Path,
) -> None:
    rendered = "<p>older persisted comment</p>"
    target = {
        "id": 41,
        "contentType": "markdown",
        "content": rendered,
        "imageList": [],
        "status": 0,
    }
    client, runner = await _prepare(
        tmp_path,
        _prepared_responses(
            _comments(),
            _comments(
                {"id": 99, "contentType": "markdown", "content": "other"},
                cursor="80",
                has_more=True,
            ),
            _comments(target, cursor="41"),
        ),
    )
    try:
        verified = await client.verify_comment(TOPIC_ID, 41, rendered)
        assert verified.comment_id == 41
        calls = [
            call
            for call in runner.curl_calls
            if any(value.endswith("/api/comment/comments") for value in call)
        ]
        assert "cursor=42" in calls[0]
        assert not any(value.startswith("cursor=") for value in calls[1])
        assert "cursor=80" in calls[2]
        comment_response_paths = [
            path
            for call, path in zip(runner.curl_calls, runner.output_paths, strict=True)
            if any(value.endswith("/api/comment/comments") for value in call)
        ]
        assert comment_response_paths
        assert all(not path.exists() for path in comment_response_paths)
    finally:
        await client.close()


async def test_comment_readback_enforces_cumulative_response_budget(
    tmp_path: Path,
) -> None:
    exact_page = _comments()
    first_page = _comments(
        {"id": 99, "contentType": "markdown", "content": "other"},
        cursor="80",
        has_more=True,
    )
    runner = FakeRunner(
        _prepared_responses(
            exact_page,
            first_page,
            _comments(cursor="70", has_more=True),
        )
    )
    budget = len(exact_page.encode("utf-8")) + len(first_page.encode("utf-8")) - 1
    client = PortalClient(
        _env_file(tmp_path),
        runner=runner,
        max_comment_verification_bytes=budget,
    )
    try:
        await client.prepare()
        with pytest.raises(PortalVerificationError, match="cumulative response byte limit"):
            await client.verify_comment(TOPIC_ID, 41, "<p>missing</p>")

        comment_calls_and_paths = [
            (call, path)
            for call, path in zip(runner.curl_calls, runner.output_paths, strict=True)
            if any(value.endswith("/api/comment/comments") for value in call)
        ]
        assert len(comment_calls_and_paths) == 2
        assert all(not path.exists() for _call, path in comment_calls_and_paths)
    finally:
        await client.close()


@pytest.mark.parametrize("id_value", ["41", 41.5, None, True])
async def test_comment_success_requires_positive_integer_id(
    tmp_path: Path, id_value: object
) -> None:
    client, _runner = await _prepare(
        tmp_path,
        _prepared_responses(_topic_detail(), _created_comment(id_value=id_value)),
    )
    try:
        await client.verify_topic(TOPIC_ID, TITLE)
        with pytest.raises(PortalWriteUncertainError):
            await client.create_comment(TOPIC_ID, "comment")
    finally:
        await client.close()


async def test_comment_readback_compares_rendered_content_type_and_images(
    tmp_path: Path,
) -> None:
    client, _runner = await _prepare(
        tmp_path,
        _prepared_responses(
            _topic_detail(),
            _comments(
                {
                    "id": 41,
                    "contentType": "markdown",
                    "content": "different rendering",
                    "imageList": [],
                }
            ),
        ),
    )
    try:
        await client.verify_topic(TOPIC_ID, TITLE)
        with pytest.raises(PortalVerificationError, match="does not match"):
            await client.verify_comment(TOPIC_ID, 41, "<p>expected</p>")
    finally:
        await client.close()


def _video(title: str = "Research video") -> VideoMetadata:
    return VideoMetadata(
        youtube_video_id="video-id",
        channel=ChannelMetadata(
            youtube_channel_id="channel-id",
            title="Research Channel",
            channel_url="https://youtube.test/@research",
        ),
        title=title,
        video_url="https://youtube.test/watch?v=video-id",
        published_at=datetime(2026, 8, 14, tzinfo=UTC),
    )


def _analysis() -> AnalysisOutcome:
    return AnalysisOutcome(
        payload={
            "is_relevant": True,
            "filter_reason": "相关",
            "guests": [
                {
                    "name_original": "Sam Altman",
                    "title": "OpenAI 联合创始人兼 CEO",
                }
            ],
            "sources": [
                {
                    "title": "Primary source",
                    "url": "https://example.test/source",
                    "note": "Background",
                }
            ],
        },
        projection=AnalysisProjection(
            is_relevant=True,
            relevance_score=92.0,
            quality_score=88.0,
            summary="中文摘要",
            translated_summary="中文摘要",
            background_notes="背景说明",
            key_points=["第一点", {"title": "第二点"}],
            tags=[{"name": "AI"}, {"name": "Research"}, {"name": "AI"}],
        ),
        metadata={},
    )


@pytest.mark.parametrize("language", ["zh", "zh-Hans", "ZH-cn", "zh_SG"])
def test_simplified_chinese_language_uses_only_default_first_group(
    language: str,
) -> None:
    assert is_simplified_chinese_language(language)


@pytest.mark.parametrize("language", ["zh-Hant", "zh-TW", "zh-HK", "zh-MO", "en"])
def test_traditional_chinese_and_other_languages_are_not_simplified(
    language: str,
) -> None:
    assert not is_simplified_chinese_language(language)


def test_publication_builder_structures_analysis_and_uses_dynamic_fence() -> None:
    title = "题" * 140
    translated = "第一行\n```\n第二行 with ````` inside"
    source = "source\n``````\nend"

    publication = build_publication_content(_video(title), _analysis(), translated, "en", source)

    assert publication.title == (
        "Sam Altman（OpenAI 联合创始人兼 CEO）｜Research Channel｜2026-08-14"
    )
    assert publication.topic_markdown.startswith("## 摘要\n\n中文摘要\n")
    assert "## 视频信息" in publication.topic_markdown
    assert "## AI 分析" in publication.topic_markdown
    assert "### 关键要点" in publication.topic_markdown
    assert "- 第一点" in publication.topic_markdown
    assert "- 第二点" in publication.topic_markdown
    assert "Primary source" in publication.topic_markdown
    assert "AI、Research" in publication.topic_markdown
    assert "- 是否相关：是" in publication.topic_markdown
    assert "- 过滤理由：相关" in publication.topic_markdown
    assert publication.tags == ()
    assert "``````text\n" in publication.translated_comment_markdown
    assert translated in publication.translated_comment_markdown
    assert publication.source_comment_markdown is not None
    assert "```````text\n" in publication.source_comment_markdown
    assert source in publication.source_comment_markdown


def test_publication_title_keeps_original_guest_names_and_orders_multiple_guests() -> None:
    analysis = {
        "summary": "摘要",
        "guests": [
            {"name_original": "Andrew Ng", "title": "DeepLearning.AI 创始人"},
            {"name_original": "李飞飞", "title": "斯坦福大学教授"},
        ],
    }

    publication = build_publication_content(
        _video(),
        analysis,
        "译文",
        "en",
        "source",
    )

    assert publication.title == (
        "Andrew Ng（DeepLearning.AI 创始人）、李飞飞（斯坦福大学教授）"
        "｜Research Channel｜2026-08-14"
    )


@pytest.mark.parametrize(
    "analysis",
    [{"summary": "摘要"}, {"summary": "摘要", "guests": []}],
)
def test_publication_title_falls_back_to_video_title_without_guest_data(
    analysis: dict[str, object],
) -> None:
    publication = build_publication_content(
        _video(),
        analysis,
        "译文",
        "en",
        "source",
    )

    assert publication.title == "Research video｜Research Channel｜2026-08-14"


def test_publication_title_preserves_channel_and_date_when_truncated() -> None:
    channel = "C" * 80
    video = {
        "title": "Fallback",
        "channel": {"title": channel},
        "published_at": datetime(2026, 8, 14, tzinfo=UTC),
    }
    analysis = {
        "summary": "摘要",
        "guests": [
            {
                "name_original": "Guest " * 30,
                "title": "Very long title " * 20,
            }
        ],
    }

    publication = build_publication_content(video, analysis, "译文", "en", "source")

    assert len(publication.title) == 128
    assert publication.title.endswith(f"｜{'C' * 31}…｜2026-08-14")


def test_publication_title_marks_missing_publish_date_without_substituting_run_time() -> None:
    video = {
        "title": "Fallback",
        "channel": {"title": "Channel"},
        "published_at": None,
    }

    publication = build_publication_content(
        video,
        {
            "summary": "摘要",
            "guests": [{"name_original": "Guest", "title": "Founder"}],
        },
        "译文",
        "en",
        "source",
    )

    assert publication.title == "Guest（Founder）｜Channel｜日期未知"


def test_publication_title_marks_unverified_guest_title_without_guessing() -> None:
    publication = build_publication_content(
        _video(),
        {
            "summary": "摘要",
            "guests": [{"name_original": "Guest", "title": None}],
        },
        "译文",
        "en",
        "source",
    )

    assert publication.title == "Guest（身份未核实）｜Research Channel｜2026-08-14"


def test_publication_title_rejects_malformed_structured_guest_data() -> None:
    with pytest.raises(ValueError, match="name_original and title"):
        build_publication_content(
            _video(),
            {
                "summary": "摘要",
                "guests": [{"name_original": "Guest", "title": "  "}],
            },
            "译文",
            "en",
            "source",
        )


def test_publication_builder_rejects_missing_summary() -> None:
    with pytest.raises(ValueError, match="summary must be a non-empty string"):
        build_publication_content(
            _video(),
            {"guests": []},
            "译文",
            "en",
            "source",
        )


def test_publication_builder_omits_only_simplified_source_comment() -> None:
    simplified = build_publication_content(
        _video(), _analysis().projection, "中文字幕", "zh-CN", "中文字幕"
    )
    traditional = build_publication_content(
        _video(), _analysis().projection, "简体翻译", "zh-Hant", "繁體字幕"
    )

    assert simplified.source_comment_markdown is None
    assert traditional.source_comment_markdown is not None
    assert "繁體字幕" in traditional.source_comment_markdown


def test_publication_builder_renders_model_text_as_plain_text_and_rejects_active_urls() -> None:
    video = {
        "title": "Unsafe [video] <img>",
        "video_url": "javascript:alert(1)",
        "channel": {
            "title": "Channel <svg/onload=alert(1)>",
            "channel_url": "data:text/html,<script>alert(1)</script>",
        },
    }
    analysis = {
        "is_relevant": False,
        "filter_reason": '<iframe src="javascript:evil"></iframe>',
        "summary": "[click](javascript:alert(1))\n<script>alert(1)</script>",
        "background_notes": "![image](https://attacker.test/pixel)",
        "key_points": ["<details open>secret</details>"],
        "tags": [{"name": "`tag` [link](javascript:evil)"}],
        "sources": [
            {
                "title": "Unsafe [source]",
                "url": "javascript:alert(1)",
                "note": "<iframe>note</iframe>",
            },
            {
                "title": "Safe source",
                "url": "https://safe.test/a)b?q=<x>",
                "note": "[plain](javascript:still-text)",
            },
        ],
    }

    publication = build_publication_content(video, analysis, "译文", "en", "source")
    markdown = publication.topic_markdown

    assert re.search(r"(?<!\\)<(?:iframe|script|details)", markdown) is None
    assert "](javascript:" not in markdown
    assert "](data:" not in markdown
    assert r"\[click\]\(javascript\:alert\(1\)\)" in markdown
    assert r"\<iframe src\=\"javascript\:evil\"\>\<\/iframe\>" in markdown
    assert "https://safe.test/a%29b?q=%3Cx%3E" in markdown
    assert "javascript:alert(1)" not in markdown
    assert "## 中文翻译\n\n```text\n译文\n```" in publication.translated_comment_markdown
