"""FastAPI application factory."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

from langstage_core.adapters import SessionAdapter
from langstage_core.tasks import TaskRunner, set_runner

from langstage.config import AppConfig
from langstage.server.middleware import add_middleware
from langstage.server.models import HealthResponse
from langstage.server.routes_config import create_config_router
from langstage.server.routes_files import create_files_router
from langstage.server.routes_canvas import create_canvas_router
from langstage.server.routes_session import create_session_router
from langstage.server.routes_chat import create_chat_router
from langstage.server.routes_cron import create_cron_router
from langstage.server.routes_tasks import create_tasks_router
from langstage.scheduler import CronScheduler, set_scheduler
from langstage.tasks import SqliteTaskStore
from langstage.workspace.file_manager import FileManager, _SESSION_CV
from langstage.workspace.canvas_manager import CanvasManager

# ag-ui-langgraph's LangGraphAgent is constructed by SessionAdapter without
# forwarding the graph's own .config attribute, so recursion_limit and
# callbacks set on agent.config are silently lost — ensure_config then stamps
# in the LangGraph default of 25. Patch __init__ once at module load so any
# LangGraphAgent created from a graph that carries a .config inherits it.
try:
    # noqa: mid-file-import
    from ag_ui_langgraph import LangGraphAgent as _LangGraphAgent
    _orig_langgraph_agent_init = _LangGraphAgent.__init__

    def _patched_langgraph_agent_init(self, *, name, graph, description=None, config=None, **kwargs):
        if config is None:
            config = dict(getattr(graph, "config", None) or {})
        _orig_langgraph_agent_init(self, name=name, graph=graph, description=description, config=config, **kwargs)

    _LangGraphAgent.__init__ = _patched_langgraph_agent_init  # type: ignore[method-assign]
except (ImportError, AttributeError):
    pass


def _app_version() -> str:
    """The installed ``langstage`` version, for the health payload (gh #67)."""
    try:
        from langstage import __version__

        return __version__
    except Exception:  # noqa: BLE001 - never let the health probe fail on this
        return "0.0.0+unknown"


def _static_dir() -> Path:
    """Directory holding the pre-built React SPA (``langstage/static``).

    Populated at packaging time by the ``hatch_build.py`` build hook (gh #94), so a
    wheel installed from PyPI serves the real UI. A single source of truth for the
    path — also the seam the packaging/serving tests patch (see
    ``tests/test_frontend_packaging.py``)."""
    return Path(__file__).parent.parent / "static"


def frontend_bundled() -> bool:
    """Is the built SPA actually present in this install?

    The one predicate behind every place the missing-frontend condition surfaces —
    the serving branch below, the startup warning in ``app.py``, the ``/api/health``
    readiness payload, and ``langstage check`` (gh #96). They previously could not
    all agree because only the serving branch tested for it, so a frontend-less
    wheel served a JSON placeholder while the terminal, the health probe, and the
    doctor all reported a clean bill of health (gh #94).

    Routed through :func:`_static_dir` so the existing test seam keeps working.
    """
    static_dir = _static_dir()
    return static_dir.exists() and (static_dir / "index.html").exists()


def create_fastapi_app(
    agent,
    workspace: Path,
    config: AppConfig,
    stream_parser_config: dict | None = None,
    icon_local_path: str | None = None,
    custom_css_content: str | None = None,
) -> FastAPI:
    """Create a FastAPI app with SSE streaming, REST, and static file serving."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await task_store.setup()
        # Upgrade an auto-attached in-memory checkpointer to a durable SQLite
        # one so conversation/interrupt state + the task review gate survive a
        # restart (and orphaned tasks resume from their last checkpoint). Only
        # touches checkpointers LangStage attached (the sentinel) — never a
        # user-supplied one.
        if getattr(agent, "_langstage_auto_checkpointer", False) is True:
            try:
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                ckpt_db = workspace / ".langstage" / "checkpoints.db"
                ckpt_db.parent.mkdir(parents=True, exist_ok=True)
                ckpt_cm = AsyncSqliteSaver.from_conn_string(str(ckpt_db))
                saver = await ckpt_cm.__aenter__()
                await saver.setup()
                agent.checkpointer = saver
                app.state._ckpt_cm = ckpt_cm
            except Exception:  # noqa: BLE001 - keep the in-memory saver on failure
                import logging
                logging.getLogger(__name__).warning(
                    "Durable checkpointer unavailable; keeping in-memory.",
                    exc_info=True,
                )
        await runner.start()
        scheduler.start()

        try:
            yield
        finally:
            # Mirrors the old @app.on_event("shutdown") semantics: these run
            # unconditionally on shutdown, not just on a clean exit.
            scheduler.shutdown()
            await runner.shutdown()
            await task_store.close()
            ckpt_cm = getattr(app.state, "_ckpt_cm", None)
            if ckpt_cm is not None:
                try:
                    await ckpt_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            set_scheduler(None)
            set_runner(None)

    # Real package version (not a hardcoded "2.0.0") so the built-in OpenAPI schema
    # (/openapi.json, /docs, /redoc) reports the version users actually run (gh #71).
    app = FastAPI(
        title=f"{config.title} REST API",
        version=_app_version(),
        description=(
            "The LangStage web-stage REST API — chat (SSE), files, canvas, cron, and the "
            "task board. Interactive docs at /docs and /redoc; schema at /openapi.json. "
            "All endpoints require Basic Auth when --auth-password is set, except /api/health."
        ),
        lifespan=lifespan,
    )

    # Middleware
    add_middleware(
        app,
        debug=config.debug,
        auth_username=config.auth_username,
        auth_password=config.auth_password,
    )

    def _extract_authentik_info(request: Request) -> tuple[dict, set[str], dict]:
        email = (
            request.headers.get("x-authentik-email", "")
            or request.headers.get("x-forwarded-email", "")
            or request.headers.get("x-auth-request-email", "")
            or request.headers.get("x-authentik-preferred-username", "")
        ).strip().lower()
        username = (request.headers.get("x-authentik-username", "") or "").strip().lower()
        name = (request.headers.get("x-authentik-name", "") or "").strip()
        uid = (request.headers.get("x-authentik-uid", "") or "").strip().lower()
        groups = (request.headers.get("x-authentik-groups", "") or "").strip()

        info = {
            "email": email,
            "username": username,
            "name": name,
            "uid": uid,
            "groups": groups,
        }
        identity_candidates = {v for v in (email, username, uid) if v}
        raw_headers = {
            k: v
            for k, v in request.headers.items()
            if k.startswith("x-authentik-") or k.startswith("x-auth-request-") or k == "x-forwarded-email"
        }
        return info, identity_candidates, raw_headers

    async def _fetch_playground_session(session_id: str) -> tuple[dict | None, int]:
        import asyncio
        import json
        import os
        from urllib import error as url_error, parse as url_parse
        from urllib import request as url_request

        api_base = os.getenv(
            "SKILLS_HUB_API_BASE_URL",
            os.getenv("ARLIE_BASE_URL", "https://cvchatapp.commvault.com/api/v1"),
        ).rstrip("/")
        api_key = os.getenv("ARLIE_API_KEY", "")
        sid = url_parse.quote(session_id, safe="")
        url = f"{api_base}/skills-hub/playground/session/{sid}"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key

        def _do_request():
            req = url_request.Request(url=url, method="GET", headers=headers)
            try:
                with url_request.urlopen(req, timeout=8) as resp:
                    return json.loads(resp.read().decode("utf-8")), resp.status
            except url_error.HTTPError as exc:
                return None, exc.code
            except Exception:
                return None, 0

        return await asyncio.to_thread(_do_request)

    def _is_session_expired(data: dict, http_status: int) -> tuple[bool, str, str]:
        from datetime import datetime, timezone

        owner_id = (
            data.get("owner_id")
            or data.get("ownerId")
            or data.get("owner_email")
            or data.get("ownerEmail")
            or ""
        ).strip().lower()
        status = str(data.get("status", "") or "").strip().lower()
        expires_at = (data.get("expires_at") or data.get("expiresAt") or "").strip()

        expired = http_status == 410 or status in ("expired", "closed")
        if not expired and expires_at:
            try:
                normalized_expires_at = expires_at.replace("Z", "+00:00")
                exp = datetime.fromisoformat(normalized_expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expired = datetime.now(timezone.utc) >= exp
            except ValueError:
                pass

        return expired, owner_id, status

    async def _authorize_session_request(request: Request, session_id: str) -> tuple[bool, bool, str, str, dict, dict, str]:
        authentik_info, identity_candidates, raw_headers = _extract_authentik_info(request)
        if not identity_candidates:
            return False, False, "", "", authentik_info, raw_headers, "unauthenticated"

        data, http_status = await _fetch_playground_session(session_id)
        if data is None and http_status == 404:
            return False, False, "", "", authentik_info, raw_headers, "session_not_found"
        if data is None or http_status not in (200, 410):
            return False, False, "", "", authentik_info, raw_headers, "upstream_error"

        expired, owner_id, status = _is_session_expired(data, http_status)
        authorized_owner = bool(identity_candidates) and bool(owner_id) and owner_id in identity_candidates
        if not authorized_owner:
            return False, False, owner_id, status, authentik_info, raw_headers, "session_owner_mismatch"
        authorized = not expired
        if expired:
            return False, True, owner_id, status, authentik_info, raw_headers, "session_expired"
        return True, False, owner_id, status, authentik_info, raw_headers, "ok"

    @app.middleware("http")
    async def _session_file_middleware(request, call_next):
        path = request.url.path or ""
        # playground_session_id is the Arlie-assigned ID that the ownership API
        # knows about. session_id is the LangStage-internal UUID (used for adapter
        # routing); it is NOT registered with the Arlie API, so using it for an
        # ownership check always returns 404 / session_not_found.
        # Priority: playground_session_id → session_id → Referer sessionId.
        sid = request.query_params.get("playground_session_id", "") or request.query_params.get("session_id", "")
        if not sid:
            # Fallback for clients that omit session_id on file endpoints:
            # derive from the page URL query (sessionId) carried in Referer.
            referer = request.headers.get("referer", "")
            if referer:
                try:
                    qs = parse_qs(urlparse(referer).query)
                    sid = (qs.get("sessionId") or [""])[0]
                except Exception:
                    sid = ""

        is_files_api = path == "/api/files" or path.startswith("/api/files/")
        is_stream_api = path == "/api/stream" or path.startswith("/api/stream/")
        is_chat_api = path == "/api/chat" or path.startswith("/api/chat/")
        needs_owner_guard = is_files_api or is_stream_api or is_chat_api

        if (path == "/api/files" or path.startswith("/api/files/")) and not sid:
            return JSONResponse({"detail": "missing session_id for file request"}, status_code=400)

        if needs_owner_guard and sid:
            authorized, expired, owner_id, _, _, _, reason = await _authorize_session_request(request, sid)
            if not authorized:
                detail = "session_expired" if expired else reason
                return JSONResponse({"detail": detail, "owner_id": owner_id}, status_code=403)

        token = _SESSION_CV.set(sid)
        try:
            return await call_next(request)
        finally:
            _SESSION_CV.reset(token)

    # Shared services
    file_manager = FileManager(workspace)
    canvas_manager = CanvasManager(workspace)

    # One session-scoped streaming adapter owns agent execution + the SSE pipe.
    # Since core 1.0 (ADR 0003) it streams every turn through the in-process AG-UI
    # adapter — the only path — emitting the same SSE frames, so the frontend is
    # unchanged. max_result_len is large so the UI shows full tool output.
    adapter = SessionAdapter(
        graph=agent,
        max_result_len=50_000,
        **(stream_parser_config or {}),
    )

    # REST API routes (mounted first — take precedence over static)
    app.include_router(create_config_router(config))
    app.include_router(create_files_router(file_manager))
    app.include_router(create_canvas_router(canvas_manager))
    app.include_router(create_session_router(adapter))
    app.include_router(create_chat_router(adapter, file_manager=file_manager))

    # Durable task board: a SQLite-backed store + the shared TaskRunner worker
    # pool. The runner owns async task execution (queued → ongoing → done);
    # registered process-globally so agent tools can reach it later.
    task_db = workspace / ".langstage" / "tasks.db"
    task_db.parent.mkdir(parents=True, exist_ok=True)
    task_store = SqliteTaskStore(task_db)
    # Resolved through the unified config chain (defaults < langstage.toml < env <
    # overrides), so it appears in --show-config and a malformed value is caught by
    # the resolver — not a raw os.getenv here that dumped an unhandled ValueError
    # traceback at startup where a bad --port is reported cleanly. (gh #102)
    runner = TaskRunner(adapter, task_store, concurrency=config.task_concurrency)
    set_runner(runner)
    app.include_router(create_tasks_router(runner, task_store))

    # In-memory cron schedules — now a *producer* that enqueues onto the runner.
    # Registered process-globally so the agent's schedule_run tool can reach it.
    scheduler = CronScheduler(runner)
    set_scheduler(scheduler)
    app.include_router(create_cron_router(scheduler))

    # Dedicated health/readiness endpoint under /api/* (so it never collides with the
    # SPA catch-all) and exempt from Basic Auth in the middleware, so a reverse proxy /
    # k8s / uptime probe always has an endpoint — even with auth on (gh #67).
    @app.get("/api/health", response_model=HealthResponse, response_model_exclude_unset=True)
    async def health(ready: int = 0) -> Response:
        """Liveness (default) or readiness (`?ready=1`).

        Liveness just proves the process is up. Readiness reflects real backend state:
        200 only if the agent object loaded AND the task store is reachable, else 503 —
        fixing the false-positive where the always-served SPA shell made ``/health``
        report healthy regardless of backend state.
        """
        payload = {"status": "ok", "version": _app_version()}
        if not ready:
            return JSONResponse(payload)

        # "loaded" (`is not None`) is vacuous — a failed load aborts startup, so a
        # serving process always has a non-None agent. And "loaded" ≠ "runnable": an
        # uncompiled StateGraph (the common BYO slip) loads fine but dies every turn
        # with no `astream`. Gate readiness on runnability — the exact check
        # `langstage check` uses (gh #39) — so a probe can't mark an unrunnable server
        # Ready (gh #69).
        agent_runnable = callable(getattr(agent, "astream", None))
        checks = {"agent": "ok" if agent_runnable else "not_runnable"}
        try:
            # A bounded query (filtered to a sentinel parent → empty) that still
            # exercises the DB connection, without loading the whole task table.
            await task_store.list(parent_id="__health_probe__")
            checks["task_store"] = "ok"
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            checks["task_store"] = f"unreachable: {type(exc).__name__}"

        # Readiness gate is computed BEFORE the frontend check is added, deliberately.
        # A missing SPA makes the web UI unavailable but leaves the REST/WS surface
        # fully functional, and a backend-only install is a supported configuration
        # (the packaging hook honours LANGSTAGE_SKIP_FRONTEND_BUILD=1). Failing
        # readiness on it would mark those deployments permanently un-Ready and pull
        # a working API out of a load balancer. So it is *reported*, not *gating* —
        # a monitor that cares can assert on `checks.frontend` explicitly. (gh #96)
        ok = all(v == "ok" for v in checks.values())
        checks["frontend"] = "ok" if frontend_bundled() else "missing"
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "version": _app_version(), "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/api/me")
    async def me(request: Request):
        """Return the authenticated user's identity from authentik proxy headers.

        Reads the standard headers injected by the authentik forward-auth outpost
        (``x-authentik-email``, ``x-authentik-username``, ``x-authentik-name``,
        ``x-authentik-uid``) plus common proxy alternatives so the UI can display
        "logged in as …" without needing a separate identity round-trip.
        """
        email = (
            request.headers.get("x-authentik-email", "")
            or request.headers.get("x-forwarded-email", "")
            or request.headers.get("x-auth-request-email", "")
            or request.headers.get("x-authentik-preferred-username", "")
        ).strip().lower()
        return JSONResponse({
            "email": email,
            "username": (request.headers.get("x-authentik-username", "") or "").strip(),
            "name": (request.headers.get("x-authentik-name", "") or "").strip(),
            "uid": (request.headers.get("x-authentik-uid", "") or "").strip(),
        })

    @app.get("/api/skills")
    async def skills_list():
        """Return available skill folder names for slash-command autocomplete."""
        skills_root = Path(os.getenv("PLAYGROUND_SKILLS_DIR", "/opt/data/skills-shared"))
        if not skills_root.exists() or not skills_root.is_dir():
            return JSONResponse({"skills": []})

        skills: list[str] = []
        try:
            for entry in sorted(skills_root.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("."):
                    continue
                skill_doc = entry / "SKILL.md"
                if not skill_doc.exists():
                    continue
                skills.append(entry.name)
        except OSError:
            return JSONResponse({"skills": []})

        return JSONResponse({"skills": skills})

    @app.get("/api/session-status")
    async def session_status(request: Request):
        session_id = request.query_params.get("session_id", "")
        authentik_info, _, authentik_raw_headers = _extract_authentik_info(request)

        def _reason_message(reason: str) -> str:
            if reason == "session_expired":
                return "This session is expired. Create a new one."
            if reason == "unauthenticated":
                return "Unauthenticated. Please sign in to continue."
            if reason == "session_owner_mismatch":
                return "This session belongs to another user."
            if reason == "session_not_found":
                return "Session not found. It may have expired."
            if reason == "upstream_error":
                return "Could not validate session right now."
            if reason == "missing_session_id":
                return "Missing session id."
            return "OK"

        if not session_id:
            return JSONResponse(
                {
                    "authorized": False,
                    "expired": False,
                    "error": "missing_session_id",
                    "reason": "missing_session_id",
                    "message": _reason_message("missing_session_id"),
                    "authentik": authentik_info,
                    "authentik_raw_headers": authentik_raw_headers,
                },
                status_code=400,
            )

        authorized, expired, owner_id, status, _, _, reason = await _authorize_session_request(request, session_id)
        data, http_status = await _fetch_playground_session(session_id)
        if data is None:
            data = {}
        expires_at = (data.get("expires_at") or data.get("expiresAt") or "").strip()
        authorized_owner = reason in ("ok", "session_expired")

        return JSONResponse(
            {
                "authorized": authorized,
                "authorized_owner": authorized_owner,
                "expired": expired,
                "expires_at": expires_at,
                "owner_id": owner_id,
                "session_status": status,
                "reason": reason,
                "message": _reason_message(reason),
                "authentik": authentik_info,
                "authentik_raw_headers": authentik_raw_headers,
            }
        )

    # Expose for testing
    app.state.session_adapter = adapter
    app.state.scheduler = scheduler
    app.state.task_runner = runner
    app.state.task_store = task_store

    # Serve custom CSS (always register so it returns 404 instead of SPA catch-all)
    # Stylesheet, not JSON — declare the real media type rather than letting the
    # schema advertise an empty application/json body (gh #98).
    @app.get(
        "/api/custom-css",
        response_class=Response,
        responses={200: {"content": {"text/css": {}}}},
    )
    async def get_custom_css():
        if not custom_css_content:
            return Response(status_code=404)
        return Response(content=custom_css_content, media_type="text/css")

    # Serve local icon file if configured
    if icon_local_path:
        @app.get("/api/icon")
        async def get_icon():
            return FileResponse(icon_local_path)

    # Static file serving (pre-built React app)
    static_dir = _static_dir()
    if frontend_bundled():
        # Serve static assets
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # Serve public branding assets copied by Vite build (e.g. /branding/*.svg).
        branding_dir = static_dir / "branding"
        if branding_dir.exists():
            app.mount("/branding", StaticFiles(directory=str(branding_dir)), name="branding")

        # Favicon
        favicon = static_dir / "favicon.ico"
        if favicon.exists():
            @app.get("/favicon.ico")
            async def get_favicon():
                return FileResponse(str(favicon))

        # Catch-all: serve index.html for client-side routing. But NOT for the
        # API/WS namespaces — an unknown /api/* path must be a JSON 404, not a
        # 200 + the SPA HTML shell (which silently breaks programmatic clients
        # doing content-negotiation / error handling) (gh #-dogfood).
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            if full_path == "api" or full_path.startswith(("api/", "ws/")):
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(str(static_dir / "index.html"))
    else:
        # No pre-built frontend — serve a placeholder
        @app.get("/")
        async def placeholder():
            return {
                "message": "LangStage backend is running.",
                "sse": "/api/stream?session_id=...",
                "api": {
                    "config": "/api/config",
                    "chat": "/api/chat",
                    "files": "/api/files/tree",
                    "canvas": "/api/canvas/items",
                },
                "note": "Build the frontend with: cd frontend && npm run build",
            }

    return app
