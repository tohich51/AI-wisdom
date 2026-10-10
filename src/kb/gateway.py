"""FastAPI application factory for the knowledge-base gateway."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kb.catalog.storage import LocalBlobStore
from kb.http import content, libraries, members, projects, uploads
from kb.http.auth_config import AuthSettings
from kb.mcp.server import build_mcp
from kb.runtime_auth import install_auth

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _refuse_unsupported_features() -> None:
    for name in ("KB_INDEXING_ENABLED", "KB_SEMANTIC_SEARCH_ENABLED"):
        if os.environ.get(name, "").strip().lower() in _TRUE_VALUES:
            raise RuntimeError(f"{name} must be false")


def _database_not_ready() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"status": "database_not_ready"},
    )


def create_app() -> FastAPI:
    _refuse_unsupported_features()

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
        _refuse_unsupported_features()
        try:
            blob_store = LocalBlobStore(
                Path(os.environ["KB_BLOB_ROOT"]),
            )
            pool.open(wait=True)
            app.state.db_pool = pool
            app.state.blob_store = blob_store
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
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

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

            return JSONResponse(
                content={
                    "status": "ready",
                    "semantic_search_enabled": False,
                    "embedding_provider": "unconfigured",
                    "release": os.environ.get("KB_RELEASE_SHA"),
                }
            )
        except Exception:
            return _database_not_ready()

    mcp_server, child = build_mcp(app, allowed_hosts)
    app.mount("/", child)
    return app
