"""FastAPI application factory for the knowledge-base gateway."""

import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kb.adapters.originals import LocalBlobStore
from kb.adapters.vector_reader import OpenVikingReader
from kb.http.auth_config import AuthSettings
from kb.http.router import mount_api
from kb.mcp.server import build_mcp
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.semantic_search import SemanticSearch
from kb.runtime_auth import install_auth
from kb.runtime_identity import build_connection_factory

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _semantic_enabled() -> bool:
    return os.environ.get("KB_SEMANTIC_SEARCH_ENABLED", "").strip().lower() in _TRUE_VALUES


def _validate_semantic_configuration() -> None:
    if _semantic_enabled():
        if not os.environ.get("KB_OPENVIKING_URL") or not os.environ.get("KB_READ_KEYS_ROOT"):
            raise RuntimeError("semantic_configuration_missing")


def _database_not_ready() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "database_not_ready"},
    )


def create_app() -> FastAPI:
    _validate_semantic_configuration()

    allowed_hosts = [
        host.strip() for host in os.environ["KB_ALLOWED_HOSTS"].split(",") if host.strip()
    ]
    if not allowed_hosts:
        raise RuntimeError("KB_ALLOWED_HOSTS must contain at least one host")

    settings = AuthSettings.from_env()
    pool = ConnectionPool(os.environ["KB_DATABASE_URL"], min_size=1, max_size=5, open=False)
    connections = build_connection_factory(pool)
    http_client = httpx.Client(timeout=10.0, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _validate_semantic_configuration()
        try:
            blob_store = LocalBlobStore(
                Path(os.environ["KB_BLOB_ROOT"]),
            )
            pool.open(wait=True)
            app.state.db_connection = connections
            app.state.blob_store = blob_store
            app.state.semantic_search = None
            if _semantic_enabled():
                with connections() as conn:
                    row = conn.execute(
                        "SELECT dimension FROM kb.embedding_profile WHERE profile_hash=%s",
                        (SELECTED_PROFILE.profile_hash,),
                    ).fetchone()
                    if row != (SELECTED_PROFILE.dimension,):
                        raise RuntimeError("embedding_profile_mismatch")
                app.state.semantic_search = SemanticSearch(
                    OpenVikingReader(
                        os.environ["KB_OPENVIKING_URL"], Path(os.environ["KB_READ_KEYS_ROOT"])
                    )
                )
            async with mcp_server.session_manager.run():
                yield
        finally:
            # The semantic service owns pooled index clients and the event
            # loop that drives them. Nothing else in this process knows about
            # either, so this is the one place they can be closed before the
            # process goes away rather than at interpreter exit. Run in a
            # thread because it joins a loop thread, and run first because
            # closing it may still want the database lease this lifespan owns.
            semantic = getattr(app.state, "semantic_search", None)
            if semantic is not None:
                await anyio.to_thread.run_sync(semantic.close)
            await anyio.to_thread.run_sync(http_client.close)
            pool.close()

    app = FastAPI(lifespan=lifespan)
    install_auth(app, connections, http_client, settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # /api/v1 is canonical; /api stays as a byte-compatible alias over the
    # same router object, so an operation is declared and dispatched once.
    mount_api(app)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    for asset in (
        "app.js",
        "completion.js",
        "rule-evidence.js",
        "project-context.js",
        "shared-experience.js",
        "style.css",
        "research.js",
        "research-result.js",
        "maps.js",
        "graph-canvas.js",
        "vendor/cytoscape-3.34.3.min.js",
    ):
        digest = hashlib.sha256((static / asset).read_bytes()).hexdigest()[:16]
        index_html = index_html.replace(f"/static/{asset}", f"/static/{asset}?v={digest}")

    @app.get("/", include_in_schema=False)
    def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    # The SPA shell, served for the screen addresses the contract names:
    # /app/libraries/:id/{sources,knowledge,rules}, /app/projects/:id/context,
    # /app/research/:runId, /app/maps, /app/experience, /app/settings/access.
    # A client-side router owns those paths, so a deep link or a reload arrives
    # here as a GET that matches no API route and must still return the shell.
    #
    # Scoped to a literal /app prefix on purpose. A root-wide catch-all would
    # also answer /api, /mcp, /auth, /static, /healthz and every typo, and the
    # contract asks for the fallback under /app/* and nowhere else. Registered
    # before the root MCP mount below, because a mount at "/" matches every
    # path and would otherwise swallow these first.
    #
    # no-cache on both: a Reload has to pick up a new shell, and the asset
    # digests in the body are what make the assets cacheable.
    @app.get("/app", include_in_schema=False)
    def app_entry() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    @app.get("/app/{screen_path:path}", include_in_schema=False)
    def app_screen(screen_path: str) -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        try:
            factory = request.app.state.db_connection
            with factory() as connection:
                connection.execute("SELECT 1").fetchone()
                role = connection.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
                if role is None or bool(role[0]) or bool(role[1]):
                    return _database_not_ready()

            if _semantic_enabled():
                response = http_client.get(os.environ["KB_OPENVIKING_URL"].rstrip("/") + "/ready")
                if response.status_code != 200 or response.json().get("status") != "ready":
                    return JSONResponse(status_code=503, content={"status": "index_not_ready"})
            return JSONResponse(
                content={
                    "status": "ready",
                    "semantic_search_enabled": _semantic_enabled(),
                    "embedding_provider": "openrouter" if _semantic_enabled() else "unconfigured",
                    "embedding_model": SELECTED_PROFILE.model if _semantic_enabled() else None,
                    "release": os.environ.get("KB_RELEASE_SHA"),
                }
            )
        except Exception:
            return _database_not_ready()

    mcp_server, child = build_mcp(app, allowed_hosts)
    app.mount("/", child)
    return app
