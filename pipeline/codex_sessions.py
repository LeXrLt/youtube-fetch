from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

AGENT_WORKSPACE_PREFIX = "youtube-fetch-codex-"
AGENT_SESSION_ORIGINATOR = "youtube_" + "fetch_pipeline"

_CLIENT_NAME = "youtube_fetch_session_cleanup"
_CLIENT_TITLE = "YouTube Fetch session cleanup"
_CLIENT_VERSION = "1"
_LEGACY_WORKSPACE_NAMES = frozenset({"youtube-fetch-agent-workspace"})
_LEGACY_SESSION_ORIGINATORS = frozenset({"codex_sdk_py"})
_SESSION_ORIGINATORS = _LEGACY_SESSION_ORIGINATORS | {AGENT_SESSION_ORIGINATOR}
_THREAD_SOURCE_KINDS = (
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
)
_LIST_PAGE_SIZE = 1000
_MAX_THREAD_COUNT = 10_000
_MAX_SESSION_META_BYTES = 8 * 1024 * 1024
_PROCESS_CLOSE_TIMEOUT_SECONDS = 2.0
_STDERR_DRAIN_TIMEOUT_SECONDS = 1.0
_STDERR_TAIL_BYTES = 64 * 1024
_STREAM_LIMIT_BYTES = 16 * 1024 * 1024


class CodexSessionCleanupError(RuntimeError):
    """Raised when Codex session discovery cannot complete safely."""


class _CodexResponseError(CodexSessionCleanupError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Codex app-server error {code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CodexSessionCleanupResult:
    scanned_threads: int
    matched_threads: int
    deleted_threads: int
    already_missing_threads: int
    skipped_live_threads: int
    skipped_loaded_threads: int
    skipped_descendant_threads: int
    unverified_threads: int
    failed_thread_ids: tuple[str, ...]


class _ThreadApi(Protocol):
    codex_home: Path

    async def request(self, method: str, params: dict[str, object]) -> object: ...


class _CodexAppServerClient:
    def __init__(self, codex_path: str) -> None:
        self._codex_path = codex_path
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._next_request_id = 1
        self.codex_home = Path()

    async def start(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self._codex_path,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT_BYTES,
            )
        except (OSError, ValueError) as exc:
            raise CodexSessionCleanupError(
                f"Cannot start Codex app-server with {self._codex_path!r}: {exc}"
            ) from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            await process.wait()
            raise CodexSessionCleanupError("Codex app-server pipes are unavailable")

        self._process = process
        self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        initialized = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "title": _CLIENT_TITLE,
                    "version": _CLIENT_VERSION,
                }
            },
        )
        if not isinstance(initialized, Mapping):
            raise CodexSessionCleanupError("Codex initialize result must be an object")
        codex_home = initialized.get("codexHome")
        if not isinstance(codex_home, str) or not Path(codex_home).is_absolute():
            raise CodexSessionCleanupError("Codex initialize returned an invalid codexHome")
        self.codex_home = Path(codex_home)
        await self._send({"method": "initialized"})

    async def request(self, method: str, params: dict[str, object]) -> object:
        request_id = self._next_request_id
        self._next_request_id += 1
        await self._send({"method": method, "id": request_id, "params": params})

        while True:
            message = await self._read_message()
            message_id = message.get("id")
            if message_id == request_id:
                error = message.get("error")
                if error is not None:
                    if not isinstance(error, Mapping):
                        raise CodexSessionCleanupError(
                            "Codex app-server returned a malformed error"
                        )
                    code = error.get("code")
                    detail = error.get("message")
                    if not isinstance(code, int) or isinstance(code, bool):
                        raise CodexSessionCleanupError(
                            "Codex app-server returned an invalid error code"
                        )
                    if not isinstance(detail, str):
                        raise CodexSessionCleanupError(
                            "Codex app-server returned an invalid error message"
                        )
                    raise _CodexResponseError(code, detail)
                if "result" not in message:
                    raise CodexSessionCleanupError(
                        "Codex app-server response has no result"
                    )
                return message["result"]

            if "method" in message and message_id is None:
                continue
            raise CodexSessionCleanupError(
                f"Codex app-server returned an unexpected message id: {message_id!r}"
            )

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await asyncio.wait_for(
                        process.stdin.wait_closed(),
                        timeout=_PROCESS_CLOSE_TIMEOUT_SECONDS,
                    )
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass

            if process.returncode is None:
                await self._stop_process(process)
            await self._finish_stderr_task()
        finally:
            stderr_task = self._stderr_task
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            self._stderr_task = None
            self._process = None

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_CLOSE_TIMEOUT_SECONDS,
            )
            return
        except TimeoutError:
            pass

        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_CLOSE_TIMEOUT_SECONDS,
            )
            return
        except TimeoutError:
            pass

        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_PROCESS_CLOSE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise CodexSessionCleanupError(
                "Codex app-server could not be stopped"
            ) from exc

    async def _finish_stderr_task(self) -> None:
        stderr_task = self._stderr_task
        if stderr_task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(stderr_task),
                timeout=_STDERR_DRAIN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        except OSError as exc:
            raise CodexSessionCleanupError(
                "Cannot read Codex app-server diagnostics"
            ) from exc

    async def _send(self, message: dict[str, object]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            process.stdin.write(encoded)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise CodexSessionCleanupError(
                f"Codex app-server closed its input: {self._stderr_text()}"
            ) from exc

    async def _read_message(self) -> dict[str, Any]:
        process = self._require_process()
        assert process.stdout is not None
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise CodexSessionCleanupError(
                "Codex app-server response exceeded the stream limit"
            ) from exc
        if not line:
            return_code = await process.wait()
            raise CodexSessionCleanupError(
                "Codex app-server closed its output "
                f"with status {return_code}: {self._stderr_text()}"
            )
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise CodexSessionCleanupError(
                "Codex app-server returned invalid JSON"
            ) from exc
        if not isinstance(message, dict):
            raise CodexSessionCleanupError(
                "Codex app-server message must be an object"
            )
        return message

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        while chunk := await stderr.read(4096):
            self._stderr_tail.extend(chunk)
            overflow = len(self._stderr_tail) - _STDERR_TAIL_BYTES
            if overflow > 0:
                del self._stderr_tail[:overflow]

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise CodexSessionCleanupError("Codex app-server has not started")
        return self._process

    def _stderr_text(self) -> str:
        text = self._stderr_tail.decode("utf-8", errors="replace").strip()
        return text or "no stderr output"


async def cleanup_historical_agent_sessions(
    *,
    codex_path: str,
    timeout_seconds: float,
) -> CodexSessionCleanupResult:
    client = _CodexAppServerClient(codex_path or "codex")
    cancellation_in_flight = False
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.start()
            threads = await _collect_threads(client)
            _thread_ids_with_descendants(tuple(threads.values()))
            (
                candidates,
                skipped_live,
                skipped_loaded,
                unverified,
            ) = await asyncio.to_thread(
                _classify_threads,
                tuple(threads.values()),
                client.codex_home,
            )

            deleted = 0
            already_missing = 0
            skipped_descendants = 0
            failures: list[str] = []
            for candidate in candidates:
                current_threads = await _collect_threads(
                    client,
                    use_state_db_only=True,
                )
                thread_ids_with_descendants = _thread_ids_with_descendants(
                    tuple(current_threads.values())
                )
                thread_id = candidate["id"]
                thread = current_threads.get(thread_id)
                if thread is None or not _has_pipeline_workspace(thread):
                    unverified += 1
                    continue
                cwd = Path(thread["cwd"])
                if not _path_is_confirmed_missing(cwd):
                    skipped_live += 1
                    continue
                status = thread.get("status")
                status_type = status.get("type") if isinstance(status, Mapping) else None
                if status_type != "notLoaded":
                    skipped_loaded += 1
                    continue
                if thread_id in thread_ids_with_descendants:
                    skipped_descendants += 1
                    continue
                still_safe = await asyncio.to_thread(
                    _is_verified_historical_thread,
                    thread,
                    client.codex_home,
                )
                if not still_safe:
                    unverified += 1
                    continue
                try:
                    result = await client.request(
                        "thread/delete",
                        {"threadId": thread_id},
                    )
                except _CodexResponseError as exc:
                    if _is_missing_rollout_error(exc, thread_id):
                        already_missing += 1
                    else:
                        failures.append(thread_id)
                    continue
                if not isinstance(result, Mapping):
                    raise CodexSessionCleanupError(
                        "Codex thread/delete result must be an object"
                    )
                deleted += 1

            return CodexSessionCleanupResult(
                scanned_threads=len(threads),
                matched_threads=len(candidates),
                deleted_threads=deleted,
                already_missing_threads=already_missing,
                skipped_live_threads=skipped_live,
                skipped_loaded_threads=skipped_loaded,
                skipped_descendant_threads=skipped_descendants,
                unverified_threads=unverified,
                failed_thread_ids=tuple(failures),
            )
    except asyncio.CancelledError:
        cancellation_in_flight = True
        raise
    except TimeoutError as exc:
        raise CodexSessionCleanupError(
            f"Codex session cleanup timed out after {timeout_seconds:g} seconds"
        ) from exc
    finally:
        try:
            await client.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not cancellation_in_flight:
                raise


async def _collect_threads(
    client: _ThreadApi,
    *,
    use_state_db_only: bool = False,
) -> dict[str, dict[str, Any]]:
    threads: dict[str, dict[str, Any]] = {}
    for archived in (False, True):
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, object] = {
                "archived": archived,
                "limit": _LIST_PAGE_SIZE,
                "sourceKinds": list(_THREAD_SOURCE_KINDS),
                "sortDirection": "asc",
                "sortKey": "created_at",
                "useStateDbOnly": use_state_db_only,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await client.request("thread/list", params)
            if not isinstance(result, Mapping):
                raise CodexSessionCleanupError("Codex thread/list result must be an object")
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexSessionCleanupError("Codex thread/list data must be an array")
            for item in data:
                if not isinstance(item, Mapping):
                    raise CodexSessionCleanupError(
                        "Codex thread/list entries must be objects"
                    )
                thread_id = item.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise CodexSessionCleanupError(
                        "Codex thread/list entry has an invalid id"
                    )
                if thread_id in threads:
                    raise CodexSessionCleanupError(
                        f"Codex thread/list returned duplicate thread id {thread_id}"
                    )
                if len(threads) >= _MAX_THREAD_COUNT:
                    raise CodexSessionCleanupError(
                        "Codex thread/list exceeded the cleanup thread limit"
                    )
                threads[thread_id] = dict(item)

            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CodexSessionCleanupError(
                    "Codex thread/list returned an invalid cursor"
                )
            if next_cursor in seen_cursors:
                raise CodexSessionCleanupError(
                    "Codex thread/list repeated a pagination cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    return threads


def _thread_ids_with_descendants(
    threads: Sequence[Mapping[str, Any]],
) -> set[str]:
    parent_ids: set[str] = set()
    for thread in threads:
        for field in ("parentThreadId", "forkedFromId"):
            parent_id = thread.get(field)
            if parent_id is None:
                continue
            if not isinstance(parent_id, str) or not parent_id:
                raise CodexSessionCleanupError(
                    f"Codex thread/list entry has an invalid {field}"
                )
            parent_ids.add(parent_id)
    return parent_ids


def _classify_threads(
    threads: Sequence[dict[str, Any]],
    codex_home: Path,
) -> tuple[list[dict[str, Any]], int, int, int]:
    candidates: list[dict[str, Any]] = []
    skipped_live = 0
    skipped_loaded = 0
    unverified = 0
    for thread in threads:
        if not _has_pipeline_workspace(thread):
            continue
        cwd = Path(thread["cwd"])
        if not _path_is_confirmed_missing(cwd):
            skipped_live += 1
            continue
        status = thread.get("status")
        status_type = status.get("type") if isinstance(status, Mapping) else None
        if status_type != "notLoaded":
            skipped_loaded += 1
            continue
        if not _is_verified_historical_thread(thread, codex_home):
            unverified += 1
            continue
        candidates.append(thread)
    return candidates, skipped_live, skipped_loaded, unverified


def _has_pipeline_workspace(thread: Mapping[str, Any]) -> bool:
    if thread.get("source") != "exec" or thread.get("ephemeral") is not False:
        return False
    thread_id = thread.get("id")
    if not isinstance(thread_id, str):
        return False
    try:
        parsed_thread_id = UUID(thread_id)
        if parsed_thread_id.version != 7 or str(parsed_thread_id) != thread_id:
            return False
    except ValueError:
        return False
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return False
    name = Path(cwd).name
    return (
        name.startswith(AGENT_WORKSPACE_PREFIX)
        and len(name) > len(AGENT_WORKSPACE_PREFIX)
    ) or name in _LEGACY_WORKSPACE_NAMES


def _is_verified_historical_thread(
    thread: Mapping[str, Any],
    codex_home: Path,
) -> bool:
    if not _has_pipeline_workspace(thread):
        return False
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not _path_is_confirmed_missing(Path(cwd)):
        return False
    thread_id = thread.get("id")
    rollout_path = thread.get("path")
    if not isinstance(thread_id, str) or not isinstance(rollout_path, str):
        return False

    path = Path(rollout_path)
    if not path.is_absolute():
        return False
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode):
            return False
        resolved_path = path.resolve(strict=True)
        allowed_roots = (
            (codex_home / "sessions").resolve(),
            (codex_home / "archived_sessions").resolve(),
        )
    except (OSError, RuntimeError):
        return False
    if not any(resolved_path.is_relative_to(root) for root in allowed_roots):
        return False

    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            return False
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            return False
        with os.fdopen(descriptor, "rb") as rollout_file:
            descriptor = None
            raw_metadata = rollout_file.readline(_MAX_SESSION_META_BYTES + 1)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not raw_metadata or len(raw_metadata) > _MAX_SESSION_META_BYTES:
        return False
    try:
        metadata = json.loads(raw_metadata)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    if not isinstance(metadata, Mapping) or metadata.get("type") != "session_meta":
        return False
    payload = metadata.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("id") == thread_id
        and payload.get("session_id") == thread_id
        and payload.get("cwd") == cwd
        and payload.get("originator") in _SESSION_ORIGINATORS
        and payload.get("source") == "exec"
    )


def _path_is_confirmed_missing(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _is_missing_rollout_error(error: _CodexResponseError, thread_id: str) -> bool:
    return (
        error.code == -32600
        and error.message == f"no rollout found for thread id {thread_id}"
    )
