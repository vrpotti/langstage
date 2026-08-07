"""SSE streaming + REST endpoints for chat.

Backed by ``langstage_core.adapters.SessionAdapter`` — the per-session
queue, cancellation, and SSE plumbing that used to live in cowork's own
``stream/`` package now come from the shared runtime.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib import error as urllib_error, parse as urllib_parse
from urllib import request as urllib_request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from langstage.server.models import ChatComplete, SessionAck
from langstage.oneturn import complete_turn

from langstage_core import workspace_root
from langstage_core.adapters import SessionAdapter

from langstage.workspace.file_manager import FileManager

logger = logging.getLogger(__name__)

# `/skill-name rest of message` — skill names match Agentskills / deepagents
# conventions (lowercase, digits, hyphens; 1-64 chars).
_SLASH_SKILL_RE = re.compile(
    r"^/([a-zA-Z0-9][\w-]{0,63})(?:\s+(.*))?$",
    re.DOTALL,
)


def _expand_slash_skill(content: str) -> str:
    """Rewrite `/skill-name …` into an explicit skill-activation instruction.

    Relying on the model alone to notice the slash and call ``read_file`` on
    ``SKILL.md`` is unreliable — it often free-forms an answer instead. Expand
    the message so the first tool call must be skill activation.
    """
    text = (content or "").lstrip()
    match = _SLASH_SKILL_RE.match(text)
    if not match:
        return content

    skill = match.group(1)
    remainder = (match.group(2) or "").strip()
    task = remainder if remainder else "(no additional user input — follow the skill defaults)"
    skill_md = f"/skills/{skill}/SKILL.md"

    return (
        "SKILL INVOCATION (mandatory — do not skip):\n"
        f"The user invoked `/{skill}`. Activate that skill before any other work.\n"
        f"1. Immediately call `read_file` on `{skill_md}`.\n"
        "2. Follow that SKILL.md exactly — do not answer from general knowledge.\n"
        f"3. For log analysis, call `inspect_log_bundle` with skill_name=`{skill}`.\n"
        f"4. User input for this skill:\n{task}"
    )


def _history_api_base() -> str:
    return os.getenv(
        "SKILLS_HUB_API_BASE_URL",
        os.getenv("ARLIE_BASE_URL", "https://cvchatapp.commvault.com/api/v1"),
    ).rstrip("/")


def _history_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("ARLIE_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _history_url(session_id: str) -> str:
    sid = urllib_parse.quote(session_id, safe="")
    return f"{_history_api_base()}/skills-hub/playground/history/{sid}"


def _history_api_fetch(session_id: str) -> list[dict]:
    req = urllib_request.Request(
        url=_history_url(session_id),
        method="GET",
        headers=_history_headers(),
    )
    try:
        with urllib_request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            messages = payload.get("messages", [])
            return messages if isinstance(messages, list) else []
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("history fetch failed for %s: %s", session_id, exc)
    return []


def _history_api_append(session_id: str, role: str, content: str) -> None:
    if not session_id or not role or not content:
        return
    body = json.dumps(
        {"session_id": session_id, "role": role, "content": content}
    ).encode("utf-8")
    req = urllib_request.Request(
        url=f"{_history_api_base()}/skills-hub/playground/history",
        method="POST",
        data=body,
        headers=_history_headers(),
    )
    try:
        with urllib_request.urlopen(req, timeout=10):
            pass
    except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError):
        # Do not block the chat turn on history persistence failures.
        return


def _parse_sse_data(frame: str) -> dict:
    if not isinstance(frame, str):
        return {}
    data_lines: list[str] = []
    for line in frame.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return {}
    payload = "\n".join(data_lines)
    try:
        parsed = json.loads(payload)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _set_playground_session_cv(session_id: str) -> None:
    if not session_id:
        return
    try:
        import sys

        if "/opt/skill-hub-playground" not in sys.path:
            sys.path.insert(0, "/opt/skill-hub-playground")
        from _session_ctx import PLAYGROUND_SESSION_CV

        PLAYGROUND_SESSION_CV.set(session_id)
    except Exception:
        # Optional integration point; do not fail chat if unavailable.
        return


class ChatRequest(BaseModel):
    session_id: str
    content: str
    cwd: str | None = None
    # Arlie playground session ID — lets the server scope the agent workspace
    # even before the SSE stream has set file_session_id on the session object.
    playground_session_id: str | None = None


class ChatCompleteRequest(BaseModel):
    """Body for the buffered one-turn endpoint. ``session_id`` is optional — omit
    it for a stateless call (a fresh session is created and returned); pass one to
    continue an existing thread. No pre-opened SSE stream required."""

    content: str
    session_id: str | None = None
    cwd: str | None = None


class InterruptRequest(BaseModel):
    session_id: str
    decisions: list[dict]
    playground_session_id: str | None = None


class CancelRequest(BaseModel):
    session_id: str
    playground_session_id: str | None = None


def context_parts(cwd: str | None = None) -> list[str]:
    """Context lines prepended to each user message (current time + working dir).

    Forwarded to ``SessionAdapter.submit_message(context_parts=...)``, which
    feeds them through ``prepare_agent_input``.

    ``cwd`` is the file browser's current folder as a *virtual* path (``/`` = the
    workspace root). We report the **real filesystem** working directory the agent
    operates in — the resolved workspace (``core.workspace_root()``) with that
    virtual subfolder applied — not the raw virtual path. Reporting the raw ``/``
    told the agent its working directory was the filesystem root (misleading, and
    actively wrong for a bring-your-own agent that resolves paths against it).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [f"[Current time: {now}]"]
    root = workspace_root()
    sub = (cwd or "").strip("/\\")
    working_dir = (root / sub) if sub else root
    parts.append(f"[Working directory: {working_dir}]")
    return parts


def create_chat_router(
    adapter: SessionAdapter,
    file_manager: FileManager | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["chat"])

    # Server-sent events, not JSON — say so in the schema (gh #98).
    @router.get(
        "/stream",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def sse_stream(request: Request, session_id: str | None = None):
        """SSE endpoint: the client opens this as an EventSource.

        Agent events and out-of-band file-change events are multiplexed onto
        one stream via the session's queue.
        """
        session = adapter.get_or_create(session_id)
        # Keep file sandbox scoped to the playground session from the URL, but
        # keep conversation history scoped to each LangStage chat session so a
        # user-created "New chat" starts clean.
        file_session_id = request.query_params.get("playground_session_id") or session.id
        history_key = request.query_params.get("playground_history_id") or session.id
        session.file_session_id = file_session_id
        session.history_key = history_key
        skill_names_raw = request.query_params.get("playground_skill_names") or request.query_params.get("skillNames") or ""
        skill_names = [part.strip() for part in skill_names_raw.split(",") if part.strip()]
        if skill_names:
            session.skill_names = skill_names
        skill_key = request.query_params.get("playground_skill_name") or request.query_params.get("skillName") or ""
        if not skill_key and skill_names:
            skill_key = skill_names[0]
        if skill_key:
            session.skill_name = skill_key

        if history_key and not getattr(session, "history_loaded", False):
            session.history_loaded = True
            history_messages = await asyncio.to_thread(_history_api_fetch, history_key)
            if history_messages:
                for message in history_messages:
                    role = (message.get("role") or "").lower()
                    content = message.get("content") or ""
                    if role not in {"user", "assistant"} or not content:
                        continue
                    if role == "user":
                        session.push({"type": "user_message", "content": content})
                    else:
                        session.push({"type": "content", "content": content, "role": "assistant"})
                        session.push({"type": "complete"})

        async def event_generator():
            # File watcher pushes file_changed events into the same session queue.
            file_watch_task = None
            assistant_parts: list[str] = []
            if file_manager:
                file_watch_task = asyncio.create_task(
                    _push_file_changes(adapter, session.id, file_manager)
                )
            try:
                async for frame in adapter.sse(session.id):
                    event_data = _parse_sse_data(frame)
                    kind = event_data.get("type")
                    if kind == "content":
                        role = (event_data.get("role") or "assistant").lower()
                        if role == "assistant":
                            assistant_parts.append(event_data.get("content") or "")
                    elif kind == "complete":
                        if assistant_parts:
                            _history_api_append(history_key, "assistant", "".join(assistant_parts))
                            assistant_parts = []
                    elif kind == "error":
                        assistant_parts = []
                    yield frame
            finally:
                if file_watch_task:
                    file_watch_task.cancel()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    @router.post("/chat", response_model=SessionAck, response_model_exclude_unset=True)
    async def send_message(body: ChatRequest):
        """Send a user message and start agent streaming."""
        session = adapter.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        history_key = getattr(session, "history_key", body.session_id)
        # Prefer the playground_session_id from the request body — it is always
        # correct (set from the page URL) and eliminates the race where a chat
        # POST arrives before the SSE stream has set file_session_id on the session.
        file_session_id = (
            body.playground_session_id
            or getattr(session, "file_session_id", None)
            or body.session_id
        )
        skill_name = (getattr(session, "skill_name", "") or "").strip()

        user_content = body.content
        message_to_send = user_content
        if skill_name and not user_content.lstrip().startswith("/"):
            message_to_send = f"/{skill_name}\n\n{user_content}"
        # Expand `/skill-name …` into an explicit "read SKILL.md first" instruction
        # so the model cannot free-form past the skill.
        message_to_send = _expand_slash_skill(message_to_send)

        _history_api_append(history_key, "user", user_content)
        _set_playground_session_cv(file_session_id)
        adapter.submit_message(
            body.session_id, message_to_send, context_parts=context_parts(body.cwd)
        )
        return {"status": "ok", "session_id": body.session_id}

    @router.post(
        "/chat/complete",
        response_model=ChatComplete,
        response_model_exclude_unset=True,
    )
    async def chat_complete(body: ChatCompleteRequest):
        """Run ONE turn to completion and return the whole assistant reply as a
        single JSON response — the synchronous, non-SSE sibling of the streaming
        chat pair.

        Removes all the ordering the SSE path requires: there is **no** persistent
        ``GET /api/stream`` to open first (that's what creates a session for the
        streaming path, so a bare ``POST /api/chat`` 404s without it), no SSE frames
        to parse, and no task row persisted. Creates the session when ``session_id``
        is absent, drives the turn on the same ``SessionAdapter`` the streaming
        routes use, and returns ``{session_id, content, tool_calls}``. A turn that
        errors is surfaced as HTTP 500; one that pauses for human review returns 200
        with the assembled-so-far reply plus ``outcome`` + ``interrupt``.
        """
        result = await complete_turn(
            adapter,
            body.content,
            session_id=body.session_id,
            context_parts=context_parts(body.cwd),
        )
        if result.outcome == "error":
            raise HTTPException(
                status_code=500, detail=result.error or "agent turn failed"
            )
        payload: dict = {
            "session_id": result.session_id,
            "content": result.content,
            "tool_calls": result.tool_calls,
        }
        # A one-shot path can't resume a review gate; surface it (200) so the caller
        # knows the reply is partial rather than silently returning it as complete.
        if result.outcome != "complete":
            payload["outcome"] = result.outcome
            if result.interrupt is not None:
                payload["interrupt"] = result.interrupt
        return payload

    @router.post("/chat/interrupt", response_model=SessionAck, response_model_exclude_unset=True)
    async def respond_to_interrupt(body: InterruptRequest):
        """Resume the agent from an interrupt with user decisions."""
        session = adapter.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        file_session_id = (
            body.playground_session_id
            or getattr(session, "file_session_id", None)
            or body.session_id
        )
        _set_playground_session_cv(file_session_id)
        adapter.submit_decisions(body.session_id, body.decisions)
        return {"status": "ok", "session_id": body.session_id}

    @router.post("/chat/cancel", response_model=SessionAck, response_model_exclude_unset=True)
    async def cancel_stream(body: CancelRequest):
        """Cancel the in-flight agent stream for a session."""
        if adapter.get(body.session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        adapter.cancel(body.session_id)
        return {"status": "ok", "session_id": body.session_id}

    return router


async def _push_file_changes(
    adapter: SessionAdapter, session_id: str, file_manager: FileManager
) -> None:
    """Watch the workspace and push file-change events into the session stream."""
    try:
        async for change in file_manager.watch():
            adapter.push_event(session_id, {
                "type": "file_changed",
                "event": change.event_type,
                "path": change.path,
            })
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("File watcher error")
