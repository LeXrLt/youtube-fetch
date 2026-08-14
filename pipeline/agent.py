from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from codex_sdk import (
    ApprovalMode,
    Codex,
    ModelReasoningEffort,
    SandboxMode,
    WebSearchMode,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from codex_sessions import AGENT_SESSION_ORIGINATOR, AGENT_WORKSPACE_PREFIX
from config import AgentSettings
from models import AgentInvocation, AgentResponse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.12 is required by this project.
    import tomli as tomllib


_DISABLED_AGENT_FEATURES = {
    "apps": False,
    "browser_use": False,
    "browser_use_external": False,
    "browser_use_full_cdp_access": False,
    "code_mode_host": False,
    "computer_use": False,
    "hooks": False,
    "image_generation": False,
    "in_app_browser": False,
    "multi_agent": False,
    "plugins": False,
    "remote_plugin": False,
    "shell_snapshot": False,
    "shell_tool": False,
    "skill_mcp_dependency_install": False,
    "skill_search": False,
    "tool_search_always_defer_mcp_tools": False,
    "tool_suggest": False,
    "unified_exec": False,
    "workspace_dependencies": False,
}
_SAFE_CONFIG_NAME = re.compile(r"[A-Za-z0-9_-]+")


class AgentInvocationError(RuntimeError):
    """Raised when a Codex invocation fails, with its complete audit record."""

    def __init__(self, message: str, invocation: AgentInvocation) -> None:
        super().__init__(message)
        self.invocation = invocation


class AgentCancelledError(asyncio.CancelledError):
    """Raised when a Codex invocation is cancelled, with its audit record."""

    def __init__(self, message: str, invocation: AgentInvocation) -> None:
        super().__init__(message)
        self.invocation = invocation


class AgentOutputError(AgentInvocationError, ValueError):
    """Raised when a Codex turn does not return valid structured output."""


class AgentConfigurationError(ValueError):
    """Raised when Codex cannot be restricted to the analysis tool boundary."""


class StructuredAgent(Protocol):
    async def run(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
    ) -> AgentResponse: ...


class CodexStructuredAgent:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self._workspace = tempfile.TemporaryDirectory(prefix=AGENT_WORKSPACE_PREFIX)
        self._working_directory = self._workspace.name
        options: dict[str, Any] = {
            "env": {
                **os.environ,
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": AGENT_SESSION_ORIGINATOR,
            },
            "config": {
                "features": _DISABLED_AGENT_FEATURES,
                "mcp_servers": _disabled_mcp_servers(),
            }
        }
        if settings.codex_path:
            options["codex_path_override"] = settings.codex_path
        self._codex = Codex(options)

    async def run(
        self,
        prompt: str,
        output_schema: dict[str, object],
        *,
        enable_web_search: bool,
        stage: str,
        sequence_number: int,
        agent_input: dict[str, Any],
    ) -> AgentResponse:
        if not stage.strip():
            raise ValueError("stage must not be blank")
        if sequence_number < 1:
            raise ValueError("sequence_number must be positive")

        started_at = datetime.now(UTC)
        thread_options: dict[str, Any] = {
            "working_directory": self._working_directory,
            "skip_git_repo_check": True,
            "sandbox_mode": SandboxMode.READ_ONLY,
            "approval_policy": ApprovalMode.NEVER,
            "model_reasoning_effort": ModelReasoningEffort(self._settings.reasoning_effort),
            "network_access_enabled": False,
            "web_search_mode": WebSearchMode(
                self._settings.analysis_web_search
                if enable_web_search
                else WebSearchMode.DISABLED
            ),
        }
        if self._settings.model:
            thread_options["model"] = self._settings.model

        intermediate_events: list[dict[str, Any]] = []
        final_response: str | None = None
        output_payload: object | None = None
        usage: dict[str, Any] | None = None
        thread_id: str | None = None
        last_stream_error: str | None = None
        try:
            thread = self._codex.start_thread(thread_options)
            turn_completed = False
            async with asyncio.timeout(self._settings.turn_timeout_seconds):
                streamed_turn = await thread.run_streamed(
                    prompt,
                    {"output_schema": output_schema},
                )
                async for event in streamed_turn.events:
                    serialized_event = dict(event)
                    intermediate_events.append(serialized_event)
                    event_type = serialized_event.get("type")
                    if event_type == "thread.started":
                        started_thread_id = serialized_event.get("thread_id")
                        if isinstance(started_thread_id, str):
                            thread_id = started_thread_id
                    elif event_type == "item.completed":
                        item = serialized_event.get("item")
                        if isinstance(item, Mapping) and item.get("type") == "agent_message":
                            text = item.get("text")
                            if isinstance(text, str):
                                final_response = text
                    elif event_type == "turn.completed":
                        event_usage = serialized_event.get("usage")
                        usage = dict(event_usage) if isinstance(event_usage, Mapping) else None
                        turn_completed = True
                    elif event_type == "turn.failed":
                        error = serialized_event.get("error")
                        message = error.get("message") if isinstance(error, Mapping) else None
                        raise RuntimeError(message or "Codex turn failed")
                    elif event_type == "error":
                        message = serialized_event.get("message")
                        last_stream_error = (
                            message if isinstance(message, str) else "Codex stream failed"
                        )

            if not turn_completed:
                raise RuntimeError(
                    last_stream_error or "Codex event stream ended before turn.completed"
                )
            if final_response is None:
                raise AgentOutputError(
                    "Codex returned no final response",
                    _invocation(
                        stage=stage,
                        sequence_number=sequence_number,
                        status="failed",
                        thread_id=thread_id,
                        agent_input=agent_input,
                        prompt=prompt,
                        intermediate_events=intermediate_events,
                        final_response=None,
                        output_payload=None,
                        usage=usage,
                        error_message="Codex returned no final response",
                        started_at=started_at,
                    ),
                )

            try:
                output_payload = json.loads(final_response)
            except json.JSONDecodeError as exc:
                message = "Codex returned invalid JSON"
                raise AgentOutputError(
                    message,
                    _invocation(
                        stage=stage,
                        sequence_number=sequence_number,
                        status="failed",
                        thread_id=thread_id,
                        agent_input=agent_input,
                        prompt=prompt,
                        intermediate_events=intermediate_events,
                        final_response=final_response,
                        output_payload=None,
                        usage=usage,
                        error_message=message,
                        started_at=started_at,
                    ),
                ) from exc
            if not isinstance(output_payload, dict):
                message = "Codex output must be a JSON object"
                raise AgentOutputError(
                    message,
                    _invocation(
                        stage=stage,
                        sequence_number=sequence_number,
                        status="failed",
                        thread_id=thread_id,
                        agent_input=agent_input,
                        prompt=prompt,
                        intermediate_events=intermediate_events,
                        final_response=final_response,
                        output_payload=output_payload,
                        usage=usage,
                        error_message=message,
                        started_at=started_at,
                    ),
                )

            try:
                Draft202012Validator(output_schema).validate(output_payload)
            except ValidationError as exc:
                path = "/".join(str(part) for part in exc.absolute_path)
                location = f" at /{path}" if path else ""
                message = f"Codex output failed schema validation{location}: {exc.message}"
                raise AgentOutputError(
                    message,
                    _invocation(
                        stage=stage,
                        sequence_number=sequence_number,
                        status="failed",
                        thread_id=thread_id,
                        agent_input=agent_input,
                        prompt=prompt,
                        intermediate_events=intermediate_events,
                        final_response=final_response,
                        output_payload=output_payload,
                        usage=usage,
                        error_message=message,
                        started_at=started_at,
                    ),
                ) from exc
        except asyncio.CancelledError as exc:
            message = "Codex invocation cancelled"
            invocation = _invocation(
                stage=stage,
                sequence_number=sequence_number,
                status="cancelled",
                thread_id=thread_id,
                agent_input=agent_input,
                prompt=prompt,
                intermediate_events=intermediate_events,
                final_response=final_response,
                output_payload=output_payload,
                usage=usage,
                error_message=message,
                started_at=started_at,
            )
            raise AgentCancelledError(message, invocation) from exc
        except AgentInvocationError:
            raise
        except Exception as exc:
            message = _error_message(exc, self._settings.turn_timeout_seconds)
            invocation = _invocation(
                stage=stage,
                sequence_number=sequence_number,
                status="failed",
                thread_id=thread_id,
                agent_input=agent_input,
                prompt=prompt,
                intermediate_events=intermediate_events,
                final_response=final_response,
                output_payload=output_payload,
                usage=usage,
                error_message=message,
                started_at=started_at,
            )
            raise AgentInvocationError(message, invocation) from exc

        invocation = _invocation(
            stage=stage,
            sequence_number=sequence_number,
            status="succeeded",
            thread_id=thread_id,
            agent_input=agent_input,
            prompt=prompt,
            intermediate_events=intermediate_events,
            final_response=final_response,
            output_payload=output_payload,
            usage=usage,
            error_message=None,
            started_at=started_at,
        )
        return AgentResponse(
            payload=output_payload,
            thread_id=thread_id,
            usage=usage,
            invocation=invocation,
        )


def _invocation(
    *,
    stage: str,
    sequence_number: int,
    status: str,
    thread_id: str | None,
    agent_input: dict[str, Any],
    prompt: str,
    intermediate_events: list[dict[str, Any]],
    final_response: str | None,
    output_payload: object | None,
    usage: dict[str, Any] | None,
    error_message: str | None,
    started_at: datetime,
) -> AgentInvocation:
    return AgentInvocation(
        stage=stage,
        sequence_number=sequence_number,
        status=status,
        thread_id=thread_id,
        agent_input=dict(agent_input),
        full_prompt=prompt,
        intermediate_events=intermediate_events.copy(),
        final_response=final_response,
        output_payload=output_payload,
        usage=usage.copy() if usage is not None else None,
        error_message=error_message,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def _error_message(exc: Exception, timeout_seconds: float) -> str:
    if isinstance(exc, TimeoutError):
        return f"Codex turn timed out after {timeout_seconds:g} seconds"
    message = str(exc).strip()
    return message or f"Codex invocation failed with {type(exc).__name__}"


def _disabled_mcp_servers() -> dict[str, dict[str, bool]]:
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return {}

    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentConfigurationError(f"Cannot inspect Codex config: {config_path}") from exc

    servers = config.get("mcp_servers", {})
    if not isinstance(servers, Mapping):
        raise AgentConfigurationError("Codex mcp_servers config must be a table")

    disabled: dict[str, dict[str, bool]] = {}
    for name in servers:
        if not isinstance(name, str) or _SAFE_CONFIG_NAME.fullmatch(name) is None:
            raise AgentConfigurationError(
                "Codex MCP server names must use letters, numbers, underscores, or hyphens"
            )
        disabled[name] = {"enabled": False}
    return disabled
