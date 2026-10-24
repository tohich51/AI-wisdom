"""Offline real SDK/httpx2 seam. No socket, grant file or live graph access."""
import asyncio
import importlib.util
import inspect
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from importlib.metadata import version

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sdk_backend as sdk
import httpx2
from mcp.shared.auth import OAuthToken, OAuthClientInformationFull

URL = 'https://resource.invalid/mcp'
ISSUER = 'https://issuer.invalid/realm'
ENDPOINT = ISSUER + '/token'

class Storage:
    issuer = ISSUER
    def __init__(self, expired=True, stale=False):
        self.tokens = OAuthToken(access_token='synthetic-old', refresh_token='synthetic-refresh',
                                 token_type='Bearer', expires_in=300, scope=sdk.DEFAULT_SCOPE)
        self.expiry = time.time() + (-60 if expired else 3600)
        self.metadata = {'fetched_at': time.time() - (999999 if stale else 0),
            'auth_server_url': ISSUER,
            'protected_resource': {'resource': URL, 'authorization_servers': [ISSUER]},
            'authorization_server': {'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/authorize',
                                     'token_endpoint': ENDPOINT, 'response_types_supported': ['code']}}
    async def get_tokens(self): return self.tokens
    async def set_tokens(self, tokens): self.tokens = tokens
    async def get_token_expiry(self): return self.expiry
    async def get_cached_metadata(self): return self.metadata
    async def set_cached_metadata(self, metadata): self.metadata = metadata
    async def get_client_info(self):
        return OAuthClientInformationFull(client_id='synthetic-client', issuer=ISSUER,
            token_endpoint_auth_method='none', scope=sdk.DEFAULT_SCOPE)

async def no_browser(*args):
    raise AssertionError('interactive authorization forbidden in fixture')

def provider(expired=True, stale=False):
    return sdk.SdkOAuthProvider(server_url=URL, storage=Storage(expired, stale),
        client_metadata=sdk.OAuthClientMetadata(redirect_uris=['http://127.0.0.1/cb'],
            scope=sdk.DEFAULT_SCOPE), redirect_handler=no_browser, callback_handler=no_browser,
        pinned_scope=sdk.DEFAULT_SCOPE)

class Body(httpx2.AsyncByteStream):
    def __init__(self, release, events): self.release, self.events = release, events
    async def __aiter__(self):
        await self.release.wait()
        yield b'ok'
        self.events.append('drained')
    async def aclose(self): self.events.append('closed')

class Tests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, expired=True, enabled=True, warm_mode='ok', stale=False):
        events, requests = [], []
        warm_started, release = asyncio.Event(), asyncio.Event()
        p = provider(expired, stale)
        if stale:
            async def discover():
                events.append('discovery')
                return p.context.storage.metadata
            p._discover = discover
        overlap_expected = expired and enabled and not stale
        async def respond(req):
            requests.append(req)
            if str(req.url) == ENDPOINT:
                events.append('refresh-start')
                if overlap_expected:
                    await asyncio.wait_for(warm_started.wait(), 1)
                events.append('refresh-end')
                return httpx2.Response(200, json={'access_token': 'synthetic-new', 'token_type': 'Bearer',
                                                 'expires_in': 300})
            if req.url.path == '/healthz':
                events.append('warm-start')
                self.assertEqual(set(req.headers), {'host'})
                self.assertEqual(req.url, httpx2.URL('https://resource.invalid/healthz'))
                warm_started.set()
                if warm_mode == 'error': raise httpx2.ConnectError('synthetic', request=req)
                if warm_mode == 'cancel': raise asyncio.CancelledError()
                if warm_mode == 'redirect':
                    return httpx2.Response(302, headers={'location': 'https://foreign.invalid/'},
                                           stream=Body(release, events))
                return httpx2.Response(200, stream=Body(release, events))
            self.assertEqual(str(req.url), URL)
            events.append('mcp')
            self.assertEqual(req.headers['authorization'], 'Bearer ' + ('synthetic-new' if expired else 'synthetic-old'))
            self.assertEqual(req.headers[sdk.WIRE_HEADER], sdk.WIRE_VERSION)
            return httpx2.Response(200, json={'content': [{'type': 'text', 'text': 'synthetic-citation'}]})
        async with httpx2.AsyncClient(auth=p, transport=httpx2.MockTransport(respond),
              headers={sdk.WIRE_HEADER: sdk.WIRE_VERSION, 'Authorization': 'not-forwarded', 'X-private': 'not-forwarded'},
              cookies={'session': 'not-forwarded'}, follow_redirects=True) as client:
            async with sdk._RefreshOverlap(client, p, URL, enabled=enabled) as overlap:
                overlap.TIMEOUT = 0.08 if warm_mode == 'timeout' else 1.0
                task = asyncio.create_task(client.post(URL, json={'method': 'tools/call'}))
                if overlap_expected:
                    await asyncio.wait_for(warm_started.wait(), 1)
                    # Token transport is blocked until warm starts; MCP is blocked
                    # until warm drain completes. Barriers, not speed assertions.
                    await asyncio.sleep(0)
                    if warm_mode not in ('error', 'cancel'):
                        self.assertNotIn('mcp', events)
                    if warm_mode != 'timeout': release.set()
                response = await asyncio.wait_for(task, 2)
                self.assertEqual(response.json()['content'][0]['text'], 'synthetic-citation')
                await client.post(URL, json={'method': 'tools/call'})
                self.assertEqual(sum(r.url.path == '/healthz' for r in requests), int(overlap_expected))
                self.assertEqual(sum(str(r.url) == ENDPOINT for r in requests), int(expired))
                self.assertEqual(events.count('mcp'), 2)
                if overlap_expected:
                    self.assertLess(events.index('warm-start'), events.index('refresh-end'))
                    self.assertTrue(overlap.task.done())
                    if warm_mode in ('ok', 'redirect'):
                        self.assertLess(events.index('closed'), events.index('mcp'))
                self.assertIs(overlap.client, client)
            self.assertEqual(client.event_hooks['request'], [])
        return events

    async def test_expired_refresh_overlap(self): await self.exercise()
    async def test_valid_no_extra(self): await self.exercise(expired=False)
    async def test_default_disabled(self):
        self.assertIs(inspect.signature(sdk.SdkBackend).parameters['refresh_overlap'].default, False)
        await self.exercise(enabled=False)
    async def test_failed_warm_does_not_fail_call(self): await self.exercise(warm_mode='error')
    async def test_cancelled_warm_does_not_fail_call(self): await self.exercise(warm_mode='cancel')
    async def test_warm_timeout_bounded(self): await self.exercise(warm_mode='timeout')
    async def test_redirect_not_followed(self): await self.exercise(warm_mode='redirect')
    async def test_discovery_no_warm(self): await self.exercise(stale=True)

    async def test_exact_endpoint_and_refresh_only(self):
        p = provider()
        await p._initialize()
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(200))) as client:
            async with sdk._RefreshOverlap(client, p, URL, enabled=True) as overlap:
                for endpoint, grant in [(ENDPOINT + '/other', 'refresh_token'),
                                        (ENDPOINT + '?other=1', 'refresh_token'),
                                        ('https://foreign.invalid/token', 'refresh_token'),
                                        (ENDPOINT, 'authorization_code')]:
                    await overlap.on_request(httpx2.Request('POST', endpoint,
                        data={'grant_type': grant, 'code': 'synthetic'}))
                self.assertIsNone(overlap.task)

    async def test_invalid_metadata_fails_before_http(self):
        p = provider()
        p.context.storage.metadata['authorization_server']['token_endpoint'] = 'https://foreign.invalid/token'
        seen = []
        async with httpx2.AsyncClient(auth=p, transport=httpx2.MockTransport(lambda r: seen.append(r))) as client:
            async with sdk._RefreshOverlap(client, p, URL, enabled=True) as overlap:
                with self.assertRaises(sdk.OAuthFlowError): await client.post(URL)
                self.assertIsNone(overlap.task)
        self.assertEqual(seen, [])

    async def test_call_cancellation_cleans_pending_warm(self):
        p = provider()
        started = asyncio.Event()
        events = []
        async def respond(req):
            if req.url.path == '/healthz':
                started.set()
                return httpx2.Response(200, stream=Body(asyncio.Event(), events))
            if str(req.url) == ENDPOINT:
                await started.wait()
                await asyncio.Event().wait()
            self.fail('protected request must not dispatch')
        async with httpx2.AsyncClient(auth=p, transport=httpx2.MockTransport(respond)) as client:
            overlap = sdk._RefreshOverlap(client, p, URL, enabled=True)
            async def run():
                async with overlap: await client.post(URL)
            task = asyncio.create_task(run())
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 1)
            self.assertTrue(overlap.task.done())
            self.assertIn('closed', events)
            self.assertEqual(client.event_hooks['request'], [])

    async def test_refresh_transport_failure_no_replay(self):
        p = provider()
        seen = []
        async def respond(req):
            seen.append(req.url.path)
            if str(req.url) == ENDPOINT: raise httpx2.ReadTimeout('synthetic', request=req)
            if req.url.path == '/healthz': return httpx2.Response(200, text='ok')
            self.fail('protected call replayed after unknown refresh transport result')
        async with httpx2.AsyncClient(auth=p, transport=httpx2.MockTransport(respond)) as client:
            async with sdk._RefreshOverlap(client, p, URL, enabled=True):
                with self.assertRaises(httpx2.ReadTimeout): await client.post(URL)
        self.assertEqual(seen.count('/realm/token'), 1)
        self.assertNotIn('/mcp', seen)

    async def test_real_sdk_opt_in_lifetime_and_result(self):
        import threading
        from concurrent.futures import Future
        from copy import deepcopy
        fixture = [{'name': 'citation_probe', 'inputSchema': {'type': 'object',
            'properties': {'library': {'type': 'string', 'x-mcp-header': 'Library'}}}}]
        payload = {'resultType': 'complete', 'isError': False,
                   'content': [{'type': 'text', 'text': 'synthetic citation ref:fixture:1'}]}
        requests, clients = [], []
        warmed = asyncio.Event()
        async def respond(req):
            requests.append(req)
            if str(req.url) == ENDPOINT:
                await asyncio.wait_for(warmed.wait(), 1)
                return httpx2.Response(200, json={'access_token': 'synthetic-new',
                    'token_type': 'Bearer', 'expires_in': 300})
            if req.url.path == '/healthz':
                self.assertEqual(set(req.headers), {'host'})
                warmed.set()
                return httpx2.Response(200, text='ok')
            body = json.loads(req.content)
            self.assertEqual(body['method'], 'tools/call')
            self.assertEqual(req.headers['authorization'], 'Bearer synthetic-new')
            self.assertEqual(req.headers[sdk.WIRE_HEADER], sdk.WIRE_VERSION)
            self.assertEqual(req.headers['mcp-param-library'], 'synthetic-library')
            return httpx2.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': payload})
        real_client = httpx2.AsyncClient
        def factory(**kwargs):
            client = real_client(**kwargs, transport=httpx2.MockTransport(respond))
            clients.append(client)
            return client
        b = sdk.SdkBackend.__new__(sdk.SdkBackend)
        b.url, b.refresh_overlap = URL, True
        b.wire_version, b.connect_modes = sdk.WIRE_VERSION, sdk.CONNECT_MODES
        b._release_catalog, b._catalog = deepcopy(fixture), []
        b._ready, b._session_established = threading.Event(), False
        b._error = None
        with patch.object(sdk.httpx2, 'AsyncClient', side_effect=factory), \
             patch.object(b, '_provider', return_value=provider()):
            lifetime = asyncio.create_task(b._serve())
            async def ready():
                while not b._ready.is_set(): await asyncio.sleep(0)
            await asyncio.wait_for(ready(), 1)
            self.assertIsNone(b._error)
            self.assertEqual(requests, [])
            f = Future()
            b._queue.put_nowait(('citation_probe', {'library': 'synthetic-library'}, f))
            result = await asyncio.wait_for(asyncio.wrap_future(f), 2)
            self.assertEqual(result, payload)
            b._queue.put_nowait(None)
            await asyncio.wait_for(lifetime, 1)
        self.assertIsNone(b._error)
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(clients[0].event_hooks['request'], [])
        self.assertEqual([r.url.path for r in requests], ['/realm/token', '/healthz', '/mcp'])

    async def test_raw_auth_override_same_client(self):
        p = provider()
        calls = []
        class SpyClient(httpx2.AsyncClient):
            async def send(self, request, **kwargs):
                if request.url.path == '/healthz': calls.append(kwargs)
                return await super().send(request, **kwargs)
        async with SpyClient(transport=httpx2.MockTransport(lambda r: httpx2.Response(200))) as client:
            overlap = sdk._RefreshOverlap(client, p, URL, enabled=True)
            await overlap._warm()
        self.assertEqual(calls, [{'auth': None, 'follow_redirects': False, 'stream': True}])

if __name__ == '__main__':
    print('installed mcp=' + version('mcp') + ' httpx2=' + version('httpx2'), flush=True)
    unittest.main(verbosity=2)
