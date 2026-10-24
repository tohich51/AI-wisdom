"""Owner-local MCP transport. No result cache, credentials, or network listener."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import sys
import time

from transport_errors import NativeRequestError

MAX_LINE = 8 * 1024 * 1024
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
BACKENDS = ("native", "sdk")


def load_sdk_backend():
    """Import the SDK backend only when the sdk backend was selected.

    The SDK pulls an HTTP stack and an OAuth client at import time. Keeping
    the import inside the call site keeps the native path free of them.
    """
    import sdk_backend
    return sdk_backend


def refusal_errors(args):
    """Errors that mean "the upstream answered and refused": keep serving.

    They are the non-fatal half of the contract. Every other exception is
    transport loss and is fatal. A refusal never triggers a replay and never
    switches backend.
    """
    if args.backend == "sdk":
        module = load_sdk_backend()
        # Present in some SDK revisions, absent in others; its absence is not
        # an error because the SDK already reports protocol refusals as
        # NativeRequestError.
        upstream = getattr(module, "SdkUpstreamError", None)
        if isinstance(upstream, type) and issubclass(upstream, BaseException):
            return (NativeRequestError, upstream)
    return (NativeRequestError,)


def build_backend(args):
    """Construct the backend the caller asked for. No fallback between them.

    A backend that cannot start raises; the daemon then fails closed instead
    of silently serving from the other backend.
    """
    if args.backend == "sdk":
        module = load_sdk_backend()
        return module.SdkBackend(
            url=args.sdk_url,
            state_dir=args.sdk_state_dir,
            client_id=args.sdk_client_id,
            scope=args.sdk_scope,
            issuer=args.sdk_issuer,
            tool_catalog=getattr(args, "sdk_tool_catalog", None),
            refresh_overlap=getattr(args, "sdk_refresh_overlap", False),
        )
    try:
        from native_backend import Backend
    except ImportError as exc:
        raise RuntimeError("Legacy native adapter is not included in the portable SDK package") from exc
    return Backend(args.binary, args.cwd)


def startup_probe(args):
    """Whether to perform the native authenticated startup read.

    SDK remote discovery already calls tools/list. Explicit release-snapshot
    mode instead reports local readiness only; the foreground SDK call must
    authenticate and pass the server's current authorization checks.
    The native backend is unchanged: one existing read, discarded.
    """
    return args.backend == "native"


def private_path(path, create=False):
    path = Path(path).absolute()
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise PermissionError("Socket parent must be an owned 0700 directory")
    return path


def strict_json(line):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Non-finite value")
    return json.loads(line, object_pairs_hook=pairs, parse_constant=constant)


class Bridge:
    def __init__(self, backend, diagnostics=False, refusals=(NativeRequestError,)):
        self.backend = backend
        self.tools = {}
        self.stop = asyncio.Event()
        self.slots = asyncio.Semaphore(1)
        self.waiting = 0
        self.diagnostics = diagnostics
        self.clients = set()
        self.heartbeat_running = False
        self.refusals = tuple(refusals)

    async def dispatch(self, request, initialized):
        ident = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if self.stop.is_set():
            return self.error(ident, -32000, "Transport stopped; call was not submitted")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
            return self.error(ident, -32600, "Invalid request")
        if method.startswith("notifications/") and "id" not in request:
            return None
        if "id" not in request or isinstance(ident, (dict, list, bool)):
            return self.error(None, -32600, "Invalid request identifier")
        if method == "initialize":
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "ai-wisdom-persistent", "version": "0.1.0"}}
        elif not initialized:
            return self.error(ident, -32002, "Initialize first")
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list(self.tools.values())}
        elif method == "tools/call":
            tool, arguments = params.get("name"), params.get("arguments", {})
            if tool not in self.tools or not isinstance(arguments, dict):
                return self.error(ident, -32602, "Unknown tool or invalid arguments")
            if self.waiting >= 32:
                return self.error(ident, -32000, "Request queue is full; call was not submitted")
            self.waiting += 1
            queued_at = time.monotonic()
            try:
                async with self.slots:
                    if self.stop.is_set():
                        return self.error(ident, -32000, "Transport stopped; call was not submitted")
                    submitted_at = time.monotonic()
                    result = await asyncio.to_thread(self.backend.call, tool, arguments)
                    if self.diagnostics:
                        print(json.dumps({"tool": tool, "queue_ms": round((submitted_at-queued_at)*1000, 3),
                            "native_rpc_ms": round((time.monotonic()-submitted_at)*1000, 3)}), file=sys.stderr, flush=True)
            except self.refusals:
                # A live server refused the request. The transport stays up and
                # nothing is replayed, because the execution may have completed.
                return self.error(ident, -32000, "Upstream rejected the request; execution may have completed. No automatic replay.")
            except Exception:
                self.stop.set()
                return self.error(ident, -32000, "Upstream unavailable; execution may have completed. No automatic replay.")
            finally:
                self.waiting -= 1
        else:
            return self.error(ident, -32601, "Method not supported")
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    async def heartbeat(self, backend_call):
        """Liveness probe that never competes with foreground work.

        Skips while a foreground call is queued or running, takes the same slot
        a foreground call takes, and rechecks the queue before probing.
        Returns "skipped", "healthy", "unavailable" or "failed". "unavailable"
        means the upstream answered but refused: the process is alive, so the
        daemon keeps the connection. Only "failed" (transport loss) stops it.
        No replay, no result reuse.
        """
        def note(status, reason, wait_ms, rpc_ms):
            if self.diagnostics:
                print(json.dumps({"event": "heartbeat", "status": status, "reason": reason,
                                  "slot_wait_ms": wait_ms, "rpc_ms": rpc_ms}),
                      file=sys.stderr, flush=True)
        if self.stop.is_set() or self.heartbeat_running:
            return "skipped"
        if self.waiting > 0:
            note("skipped", "foreground_busy", 0.0, 0.0)
            return "skipped"
        self.heartbeat_running = True
        started = time.monotonic()
        try:
            async with self.slots:
                if self.stop.is_set() or self.waiting > 0:
                    note("skipped", "foreground_busy", round((time.monotonic() - started) * 1000, 3), 0.0)
                    return "skipped"
                probe_at = time.monotonic()
                try:
                    health = await asyncio.to_thread(backend_call, "list_libraries", {"limit": 1, "offset": 0})
                except self.refusals:
                    note("unavailable", "protocol_error", round((probe_at - started) * 1000, 3),
                         round((time.monotonic() - probe_at) * 1000, 3))
                    return "unavailable"
                except Exception:
                    note("failed", "transport_error", round((probe_at - started) * 1000, 3),
                         round((time.monotonic() - probe_at) * 1000, 3))
                    return "failed"
                if not isinstance(health, dict):
                    note("failed", "unexpected_probe_shape", round((probe_at - started) * 1000, 3),
                         round((time.monotonic() - probe_at) * 1000, 3))
                    return "failed"
                if health.get("isError"):
                    note("unavailable", "tool_error", round((probe_at - started) * 1000, 3),
                         round((time.monotonic() - probe_at) * 1000, 3))
                    return "unavailable"
                del health
                note("healthy", "", round((probe_at - started) * 1000, 3),
                     round((time.monotonic() - probe_at) * 1000, 3))
                return "healthy"
        finally:
            self.heartbeat_running = False

    @staticmethod
    def error(ident, code, message):
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}

    async def client(self, reader, writer):
        self.clients.add(writer)
        initialized = False
        try:
            while not self.stop.is_set():
                line = await reader.readline()
                if not line:
                    break
                if len(line) > MAX_LINE:
                    break
                try:
                    request = strict_json(line)
                    if not isinstance(request, dict):
                        raise ValueError("Object required")
                    response = await self.dispatch(request, initialized)
                    if request.get("method") == "initialize" and response and "result" in response:
                        initialized = True
                except (ValueError, TypeError, KeyError):
                    response = self.error(None, -32700, "Invalid JSON-RPC document")
                if response is not None:
                    encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
                    if len(encoded) > MAX_LINE:
                        encoded = json.dumps(self.error(response.get("id"), -32000, "Response exceeds transport limit; no partial result returned")).encode() + b"\n"
                    writer.write(encoded)
                    await writer.drain()
        except (OSError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.clients.discard(writer)

    async def close_clients(self, server):
        server.close()
        clients = tuple(self.clients)
        for writer in clients:
            writer.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=3)
        except TimeoutError:
            for writer in clients:
                writer.transport.abort()
            await asyncio.wait_for(server.wait_closed(), timeout=1)


async def daemon(args):
    boot_at = time.monotonic()
    path = private_path(args.socket, create=True)
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_stat = os.fstat(lock)
    if lock_stat.st_uid != os.getuid() or stat.S_IMODE(lock_stat.st_mode) != 0o600:
        os.close(lock)
        raise PermissionError("Unsafe daemon lock")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backend = build_backend(args)
    server = None
    inode = None
    try:
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise PermissionError("Refusing to replace an unrelated socket path")
            check = socket.socket(socket.AF_UNIX)
            try:
                check.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise RuntimeError("An existing daemon is listening")
            finally:
                check.close()
        bridge = Bridge(backend, args.diagnostics, refusal_errors(args))
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, bridge.stop.set)
        await asyncio.to_thread(backend.start)
        if bridge.stop.is_set():
            return
        if startup_probe(args):
            # A small existing read verifies the real authenticated round trip.
            # Discard it: no target graph or tool result is cached.
            health = await asyncio.to_thread(backend.call, "list_libraries", {"limit": 1, "offset": 0})
            if health.get("isError"):
                raise RuntimeError("Authenticated round trip failed")
            del health
        bridge.tools = {tool["name"]: tool for tool in await asyncio.to_thread(backend.catalog)}
        if not bridge.tools:
            raise RuntimeError("No upstream tool catalog")
        server = await asyncio.start_unix_server(bridge.client, path=str(path), limit=MAX_LINE)
        os.chmod(path, 0o600)
        inode = path.lstat().st_ino
        print(json.dumps({"status": "ready", "tools": len(bridge.tools),
                          "bootstrap_ms": round((time.monotonic()-boot_at)*1000, 3)}), file=sys.stderr, flush=True)
        while not bridge.stop.is_set():
            try:
                await asyncio.wait_for(bridge.stop.wait(), timeout=45)
            except TimeoutError:
                # "unavailable" (a live upstream that refused) keeps the daemon up.
                if await bridge.heartbeat(backend.call) == "failed":
                    bridge.stop.set()
    finally:
        try:
            if server:
                await bridge.close_clients(server)
        finally:
            await asyncio.to_thread(backend.close)
            if inode is not None and path.exists() and path.lstat().st_ino == inode:
                path.unlink()
            os.close(lock)


def stdio(path):
    path = private_path(path)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("Unsafe daemon socket")
    with socket.socket(socket.AF_UNIX) as conn, selectors.DefaultSelector() as selector:
        conn.connect(str(path))
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "input")
        selector.register(conn, selectors.EVENT_READ, "socket")
        while True:
            for key, _ in selector.select():
                if key.data == "input":
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if chunk:
                        conn.sendall(chunk)
                    else:
                        return
                else:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("daemon", "stdio"))
    parser.add_argument("--socket", required=True)
    parser.add_argument("--backend", choices=BACKENDS, default="sdk")
    parser.add_argument("--binary")
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--sdk-url")
    parser.add_argument("--sdk-issuer", help="Required expected HTTPS OAuth issuer (including realm path)")
    parser.add_argument("--sdk-state-dir")
    parser.add_argument("--sdk-tool-catalog", help="Explicit release-pinned public catalog; not a remote grant check")
    parser.add_argument("--sdk-refresh-overlap", action="store_true", help="Opt in to SDK refresh/connection setup overlap")
    parser.add_argument("--sdk-client-id", default="kb-mcp")
    parser.add_argument("--sdk-scope", default="openid offline_access kb:read kb:contribute kb:experience kb:export")
    args = parser.parse_args(argv)
    if args.mode == "daemon":
        if args.backend == "native":
            if not args.binary:
                parser.error("--binary required for --backend native")
        else:
            missing = [name for name in ("sdk_url", "sdk_state_dir", "sdk_issuer") if not getattr(args, name)]
            if missing:
                parser.error("--" + " and --".join(name.replace("_", "-") for name in missing)
                             + f" required for --backend sdk")
    return args


def main():
    os.umask(0o077)
    args = parse_args()
    if args.mode == "daemon":
        asyncio.run(daemon(args))
    else:
        stdio(args.socket)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError):
        print("AI-wisdom local transport unavailable; no request replayed", file=sys.stderr)
        sys.exit(1)
