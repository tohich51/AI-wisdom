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

from kb.catalog.storage import LocalBlobStore
from kb.http import content, libraries, members, projects, research, uploads
from kb.http.auth_config import AuthSettings
from kb.mcp.server import build_mcp
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.semantic_search import SemanticSearch
from kb.retrieval.vector_reader import OpenVikingReader
from kb.runtime_auth import install_auth

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
    http_client = httpx.Client(timeout=10.0, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _validate_semantic_configuration()
        try:
            blob_store = LocalBlobStore(
                Path(os.environ["KB_BLOB_ROOT"]),
            )
            pool.open(wait=True)
            app.state.db_pool = pool
            app.state.blob_store = blob_store
            app.state.semantic_search = None
            if _semantic_enabled():
                with pool.connection() as conn:
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
            await anyio.to_thread.run_sync(http_client.close)
            pool.close()

    app = FastAPI(lifespan=lifespan)
    install_auth(app, pool, http_client, settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.include_router(libraries.router, prefix="/api")
    app.include_router(projects.router, prefix="/api")
    app.include_router(members.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")

    app.include_router(content.router, prefix="/api")
    app.include_router(research.router, prefix="/api")
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    index_html = (static / "index.html").read_text(encoding="utf-8")
    for asset in (
        "app.js",
        "completion.js",
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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        try:
            pool: ConnectionPool = request.app.state.db_pool
            with pool.connection() as connection:
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
