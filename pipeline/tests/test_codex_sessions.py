from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import codex_sessions as sessions_module
from codex_sessions import CodexSessionCleanupError, cleanup_historical_agent_sessions

_SOURCE_KINDS = {
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
}
_THREAD_IDS = (
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b001",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b002",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b003",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b004",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b005",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b006",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b007",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b008",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b009",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b00a",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b00b",
    "01890f3e-9d54-7cc1-98c8-4f6c11e5b00c",
)


class _FakeStdin:
    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self._buffer = bytearray()
        self._closing = False

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def drain(self) -> None:
        while b"\n" in self._buffer:
            raw_message, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            message = json.loads(raw_message)
            assert isinstance(message, dict)
            self._process.server.receive(message, self._process.stdout)

    def close(self) -> None:
        self._closing = True
        self._process.finish(0)

    async def wait_closed(self) -> None:
        await self._process.wait()

    def is_closing(self) -> bool:
        return self._closing


class _FakeProcess:
    def __init__(
        self,
        server: _FakeAppServer,
        *,
        close_stderr: bool = True,
    ) -> None:
        self.server = server
        self._close_stderr = close_stderr
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = _FakeStdin(self)
        self.returncode: int | None = None
        self._finished = asyncio.Event()

    def finish(self, returncode: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.feed_eof()
        if self._close_stderr:
            self.stderr.feed_eof()
        self._finished.set()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.finish(-15)

    def kill(self) -> None:
        self.finish(-9)


PageKey = tuple[bool, str | None]
PageFactory = Callable[[dict[str, object]], dict[str, object]]


class _FakeAppServer:
    def __init__(
        self,
        codex_home: Path,
        *,
        pages: Mapping[PageKey, dict[str, object] | PageFactory],
        delete_errors: Mapping[str, tuple[int, str]] | None = None,
    ) -> None:
        self.codex_home = codex_home
        self.pages = dict(pages)
        self.delete_errors = dict(delete_errors or {})
        self.messages: list[dict[str, Any]] = []
        self.notifications_sent = 0

    def receive(
        self,
        message: dict[str, Any],
        stdout: asyncio.StreamReader,
    ) -> None:
        self.messages.append(message)
        request_id = message.get("id")
        if request_id is None:
            assert message == {"method": "initialized"}
            return

        self.notifications_sent += 1
        self._feed(
            stdout,
            {
                "method": "codex/event/test-notification",
                "params": {"requestId": request_id},
            },
        )
        method = message.get("method")
        params = message.get("params")
        assert isinstance(params, dict)
        if method == "initialize":
            response: dict[str, object] = {
                "id": request_id,
                "result": {"codexHome": str(self.codex_home)},
            }
        elif method == "thread/list":
            archived = params.get("archived")
            cursor = params.get("cursor")
            assert isinstance(archived, bool)
            assert cursor is None or isinstance(cursor, str)
            page = self.pages[(archived, cursor)]
            result = page(params) if callable(page) else page
            response = {"id": request_id, "result": result}
        elif method == "thread/delete":
            thread_id = params.get("threadId")
            assert isinstance(thread_id, str)
            error = self.delete_errors.get(thread_id)
            if error is None:
                response = {"id": request_id, "result": {}}
            else:
                response = {
                    "id": request_id,
                    "error": {"code": error[0], "message": error[1]},
                }
        else:
            raise AssertionError(f"unexpected method: {method!r}")
        self._feed(stdout, response)

    @staticmethod
    def _feed(stdout: asyncio.StreamReader, message: dict[str, object]) -> None:
        stdout.feed_data(json.dumps(message).encode("utf-8") + b"\n")

    @property
    def list_messages(self) -> list[dict[str, Any]]:
        return [item for item in self.messages if item.get("method") == "thread/list"]

    @property
    def deleted_thread_ids(self) -> list[str]:
        return [
            item["params"]["threadId"]
            for item in self.messages
            if item.get("method") == "thread/delete"
        ]


def _install_app_server(
    monkeypatch: pytest.MonkeyPatch,
    server: _FakeAppServer,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    invocations: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def create_subprocess_exec(
        *args: object,
        **kwargs: object,
    ) -> _FakeProcess:
        invocations.append((args, kwargs))
        return _FakeProcess(server)

    monkeypatch.setattr(
        sessions_module.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    return invocations


def _rollout(
    codex_home: Path,
    thread_id: str,
    cwd: Path,
    *,
    payload_changes: Mapping[str, object] | None = None,
    raw_metadata: bytes | None = None,
    outside_home: Path | None = None,
) -> Path:
    root = outside_home if outside_home is not None else codex_home / "sessions"
    path = root / "2026" / "08" / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "id": thread_id,
        "session_id": thread_id,
        "cwd": str(cwd),
        "originator": sessions_module.AGENT_SESSION_ORIGINATOR,
        "source": "exec",
    }
    payload.update(payload_changes or {})
    metadata = {"type": "session_meta", "payload": payload}
    path.write_bytes(
        raw_metadata
        if raw_metadata is not None
        else json.dumps(metadata).encode("utf-8") + b"\n"
    )
    return path


def _thread(
    codex_home: Path,
    thread_id: str,
    cwd: Path,
    **changes: object,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": thread_id,
        "source": "exec",
        "ephemeral": False,
        "cwd": str(cwd),
        "status": {"type": "notLoaded"},
        "path": str(_rollout(codex_home, thread_id, cwd)),
    }
    item.update(changes)
    return item


def _replace_with_special_file(path: Path, kind: str) -> None:
    if kind == "fifo":
        path.unlink()
        os.mkfifo(path)
        return
    target = path.with_name(f"target-{path.name}")
    path.rename(target)
    path.symlink_to(target)


def _single_page(threads: list[dict[str, object]]) -> dict[PageKey, dict[str, object]]:
    return {
        (False, None): {"data": threads, "nextCursor": None},
        (True, None): {"data": [], "nextCursor": None},
    }


@pytest.mark.asyncio
async def test_invalid_app_server_ndjson_is_reported_as_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeAppServer(tmp_path / "codex-home", pages={})

    def send_invalid_utf8(
        message: dict[str, Any],
        stdout: asyncio.StreamReader,
    ) -> None:
        assert message["method"] == "initialize"
        stdout.feed_data(b"\xff\n")

    monkeypatch.setattr(server, "receive", send_invalid_utf8)
    _install_app_server(monkeypatch, server)

    with pytest.raises(CodexSessionCleanupError, match="returned invalid JSON"):
        await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_cleanup_does_not_wait_forever_for_inherited_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakeAppServer(
        tmp_path / "codex-home",
        pages=_single_page([]),
    )

    async def create_subprocess_exec(
        *args: object,
        **kwargs: object,
    ) -> _FakeProcess:
        del args, kwargs
        return _FakeProcess(server, close_stderr=False)

    monkeypatch.setattr(
        sessions_module.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    monkeypatch.setattr(sessions_module, "_STDERR_DRAIN_TIMEOUT_SECONDS", 0.01)

    async with asyncio.timeout(1):
        result = await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )

    assert result.scanned_threads == 0


@pytest.mark.asyncio
async def test_close_failure_does_not_replace_in_flight_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellingClient:
        def __init__(self, codex_path: str) -> None:
            del codex_path

        async def start(self) -> None:
            raise asyncio.CancelledError

        async def close(self) -> None:
            raise CodexSessionCleanupError("close failed")

    monkeypatch.setattr(
        sessions_module,
        "_CodexAppServerClient",
        CancellingClient,
    )

    with pytest.raises(asyncio.CancelledError):
        await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_protocol_handshake_notifications_and_active_archive_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    active = _thread(
        codex_home,
        _THREAD_IDS[0],
        tmp_path / "youtube-fetch-codex-active-old",
    )
    archived = _thread(
        codex_home,
        _THREAD_IDS[1],
        tmp_path / "youtube-fetch-agent-workspace",
    )
    archived["path"] = str(
        _rollout(
            codex_home,
            _THREAD_IDS[1],
            tmp_path / "youtube-fetch-agent-workspace",
            payload_changes={"originator": "codex_sdk_py"},
        )
    )
    pages = {
        (False, None): {"data": [active], "nextCursor": "active-page-2"},
        (False, "active-page-2"): {"data": [], "nextCursor": None},
        (True, None): {"data": [], "nextCursor": "archive-page-2"},
        (True, "archive-page-2"): {"data": [archived], "nextCursor": None},
    }
    server = _FakeAppServer(codex_home, pages=pages)
    invocations = _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="/opt/codex-test",
        timeout_seconds=5,
    )

    assert result.scanned_threads == 2
    assert result.deleted_threads == 2
    assert server.deleted_thread_ids == [_THREAD_IDS[0], _THREAD_IDS[1]]
    assert server.notifications_sent == 15
    assert server.messages[0]["method"] == "initialize"
    assert server.messages[0]["params"]["clientInfo"] == {
        "name": "youtube_fetch_session_cleanup",
        "title": "YouTube Fetch session cleanup",
        "version": "1",
    }
    assert server.messages[1] == {"method": "initialized"}
    pagination = [None, "active-page-2", None, "archive-page-2"]
    assert [item["params"].get("cursor") for item in server.list_messages] == (
        pagination * 3
    )
    archived_filters = [False, False, True, True]
    assert [item["params"]["archived"] for item in server.list_messages] == (
        archived_filters * 3
    )
    assert [item["params"]["useStateDbOnly"] for item in server.list_messages] == [
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    ]
    for item in server.list_messages:
        assert set(item["params"]["sourceKinds"]) == _SOURCE_KINDS
    assert invocations[0][0] == (
        "/opt/codex-test",
        "app-server",
        "--listen",
        "stdio://",
    )


@pytest.mark.asyncio
async def test_cleanup_only_deletes_threads_with_strict_pipeline_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    workspace_root = tmp_path / "workspaces"
    valid = _thread(
        codex_home,
        _THREAD_IDS[0],
        workspace_root / "youtube-fetch-codex-valid",
    )
    foreign_source = _thread(
        codex_home,
        _THREAD_IDS[1],
        workspace_root / "youtube-fetch-codex-foreign-source",
        source="vscode",
    )
    ephemeral = _thread(
        codex_home,
        _THREAD_IDS[2],
        workspace_root / "youtube-fetch-codex-ephemeral",
        ephemeral=True,
    )
    non_v7 = _thread(
        codex_home,
        "a1337d2f-c0c8-4bcf-96b6-8e07465be55d",
        workspace_root / "youtube-fetch-codex-non-v7",
    )
    foreign_workspace = _thread(
        codex_home,
        _THREAD_IDS[3],
        workspace_root / "another-program-workspace",
    )

    unverified: list[dict[str, object]] = []
    payload_changes = (
        {"id": "different-thread"},
        {"originator": "codex_cli_rs"},
        {"source": "vscode"},
        {"session_id": "different-session"},
        {"cwd": "/different/cwd"},
    )
    for offset, changes in enumerate(payload_changes, start=4):
        thread_id = _THREAD_IDS[offset]
        cwd = workspace_root / f"youtube-fetch-codex-bad-meta-{offset}"
        item = _thread(codex_home, thread_id, cwd)
        item["path"] = str(
            _rollout(codex_home, thread_id, cwd, payload_changes=changes)
        )
        unverified.append(item)

    outside_id = _THREAD_IDS[9]
    outside_cwd = workspace_root / "youtube-fetch-codex-outside-rollout"
    outside = _thread(codex_home, outside_id, outside_cwd)
    outside["path"] = str(
        _rollout(
            codex_home,
            outside_id,
            outside_cwd,
            outside_home=tmp_path / "foreign-codex-home",
        )
    )
    corrupt_id = _THREAD_IDS[10]
    corrupt_cwd = workspace_root / "youtube-fetch-codex-corrupt-rollout"
    corrupt = _thread(codex_home, corrupt_id, corrupt_cwd)
    corrupt["path"] = str(
        _rollout(
            codex_home,
            corrupt_id,
            corrupt_cwd,
            raw_metadata=b"{not-json}\n",
        )
    )
    threads = [
        valid,
        foreign_source,
        ephemeral,
        non_v7,
        foreign_workspace,
        *unverified,
        outside,
        corrupt,
    ]
    server = _FakeAppServer(codex_home, pages=_single_page(threads))
    _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="codex",
        timeout_seconds=5,
    )

    assert result.scanned_threads == len(threads)
    assert result.deleted_threads == 1
    assert result.unverified_threads == 7
    assert server.deleted_thread_ids == [_THREAD_IDS[0]]


@pytest.mark.asyncio
async def test_cleanup_skips_live_workspaces_and_loaded_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    live_cwd = tmp_path / "youtube-fetch-codex-current"
    live_cwd.mkdir()
    live = _thread(codex_home, _THREAD_IDS[0], live_cwd)
    loaded = _thread(
        codex_home,
        _THREAD_IDS[1],
        tmp_path / "youtube-fetch-codex-loaded",
        status={"type": "loaded"},
    )
    status_missing = _thread(
        codex_home,
        _THREAD_IDS[2],
        tmp_path / "youtube-fetch-codex-status-missing",
    )
    status_missing.pop("status")
    server = _FakeAppServer(
        codex_home,
        pages=_single_page([live, loaded, status_missing]),
    )
    _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="codex",
        timeout_seconds=5,
    )

    assert result.skipped_live_threads == 1
    assert result.skipped_loaded_threads == 2
    assert result.deleted_threads == 0
    assert server.deleted_thread_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["loaded", "descendant"])
async def test_cleanup_refreshes_codex_state_immediately_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_change: str,
) -> None:
    codex_home = tmp_path / "codex-home"
    candidate = _thread(
        codex_home,
        _THREAD_IDS[0],
        tmp_path / "youtube-fetch-codex-raced",
    )

    def active_page(params: dict[str, object]) -> dict[str, object]:
        if params["useStateDbOnly"] is False:
            return {"data": [candidate], "nextCursor": None}
        if concurrent_change == "loaded":
            current = dict(candidate)
            current["status"] = {"type": "loaded"}
            return {"data": [current], "nextCursor": None}
        child = {
            "id": _THREAD_IDS[1],
            "source": "vscode",
            "ephemeral": False,
            "cwd": str(tmp_path / "unrelated-project"),
            "status": {"type": "notLoaded"},
            "path": str(tmp_path / "unrelated-rollout.jsonl"),
            "parentThreadId": _THREAD_IDS[0],
        }
        return {"data": [candidate, child], "nextCursor": None}

    server = _FakeAppServer(
        codex_home,
        pages={
            (False, None): active_page,
            (True, None): {"data": [], "nextCursor": None},
        },
    )
    _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="codex",
        timeout_seconds=5,
    )

    assert result.deleted_threads == 0
    assert result.skipped_loaded_threads == (concurrent_change == "loaded")
    assert result.skipped_descendant_threads == (
        concurrent_change == "descendant"
    )
    assert server.deleted_thread_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("special_file", ["fifo", "symlink"])
async def test_cleanup_rejects_non_regular_or_symlink_rollouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    special_file: str,
) -> None:
    codex_home = tmp_path / "codex-home"
    candidate = _thread(
        codex_home,
        _THREAD_IDS[0],
        tmp_path / "youtube-fetch-codex-special-file",
    )
    rollout = Path(str(candidate["path"]))
    await asyncio.to_thread(_replace_with_special_file, rollout, special_file)
    server = _FakeAppServer(codex_home, pages=_single_page([candidate]))
    _install_app_server(monkeypatch, server)

    async with asyncio.timeout(1):
        result = await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )

    assert result.unverified_threads == 1
    assert result.deleted_threads == 0
    assert server.deleted_thread_ids == []


@pytest.mark.asyncio
async def test_repeated_cursor_aborts_before_any_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    candidate = _thread(
        codex_home,
        _THREAD_IDS[0],
        tmp_path / "youtube-fetch-codex-candidate",
    )
    pages = {
        (False, None): {"data": [candidate], "nextCursor": "repeated"},
        (False, "repeated"): {"data": [], "nextCursor": "repeated"},
    }
    server = _FakeAppServer(codex_home, pages=pages)
    _install_app_server(monkeypatch, server)

    with pytest.raises(
        CodexSessionCleanupError,
        match="repeated a pagination cursor",
    ):
        await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )

    assert server.deleted_thread_ids == []


@pytest.mark.asyncio
async def test_thread_limit_aborts_before_any_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    candidates = [
        _thread(
            codex_home,
            thread_id,
            tmp_path / f"youtube-fetch-codex-limit-{index}",
        )
        for index, thread_id in enumerate(_THREAD_IDS[:2])
    ]
    server = _FakeAppServer(codex_home, pages=_single_page(candidates))
    _install_app_server(monkeypatch, server)
    monkeypatch.setattr(sessions_module, "_MAX_THREAD_COUNT", 1)

    with pytest.raises(CodexSessionCleanupError, match="thread limit"):
        await cleanup_historical_agent_sessions(
            codex_path="codex",
            timeout_seconds=5,
        )

    assert server.deleted_thread_ids == []


@pytest.mark.asyncio
async def test_delete_failures_are_isolated_and_missing_rollout_is_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    threads = [
        _thread(
            codex_home,
            thread_id,
            tmp_path / f"youtube-fetch-codex-old-{index}",
        )
        for index, thread_id in enumerate(_THREAD_IDS[:3])
    ]
    missing_message = f"no rollout found for thread id {_THREAD_IDS[1]}"
    server = _FakeAppServer(
        codex_home,
        pages=_single_page(threads),
        delete_errors={
            _THREAD_IDS[0]: (-32000, "state database is busy"),
            _THREAD_IDS[1]: (-32600, missing_message),
        },
    )
    _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="codex",
        timeout_seconds=5,
    )

    assert server.deleted_thread_ids == list(_THREAD_IDS[:3])
    assert result.deleted_threads == 1
    assert result.already_missing_threads == 1
    assert result.failed_thread_ids == (_THREAD_IDS[0],)


@pytest.mark.asyncio
@pytest.mark.parametrize("relation_field", ["parentThreadId", "forkedFromId"])
async def test_external_descendant_prevents_recursive_parent_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation_field: str,
) -> None:
    codex_home = tmp_path / "codex-home"
    parent = _thread(
        codex_home,
        _THREAD_IDS[0],
        tmp_path / "youtube-fetch-codex-parent",
    )
    child = {
        "id": _THREAD_IDS[1],
        "source": "vscode",
        "ephemeral": False,
        "cwd": str(tmp_path / "unrelated-project"),
        "status": {"type": "notLoaded"},
        "path": str(tmp_path / "unrelated-rollout.jsonl"),
        relation_field: _THREAD_IDS[0],
    }
    server = _FakeAppServer(
        codex_home,
        pages=_single_page([parent, child]),
    )
    _install_app_server(monkeypatch, server)

    result = await cleanup_historical_agent_sessions(
        codex_path="codex",
        timeout_seconds=5,
    )

    assert result.deleted_threads == 0
    assert result.skipped_descendant_threads == 1
    assert server.deleted_thread_ids == []
    for item in server.list_messages:
        assert set(item["params"]["sourceKinds"]) == _SOURCE_KINDS
