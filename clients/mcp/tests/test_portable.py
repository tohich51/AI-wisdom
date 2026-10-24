"""Offline portable configuration and issuer boundaries; no real sockets."""
import asyncio
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import broker
import sdk_backend as sdk
import graph_wire
import httpx2

URL = 'https://resource.example/mcp'
ISSUER = 'https://issuer.example/realm'

class PortableTests(unittest.TestCase):
    def args(self, *extra):
        return broker.parse_args(['daemon', '--socket', '/private/example/broker.sock',
            '--sdk-url', URL, '--sdk-issuer', ISSUER, '--sdk-state-dir', '/private/example/state', *extra])

    def test_cli_propagation_and_defaults(self):
        self.assertEqual(Path(sdk.__file__).resolve().parent, ROOT)
        self.assertEqual(Path(broker.__file__).resolve().parent, ROOT)
        self.assertEqual(Path(graph_wire.__file__).resolve().parent, ROOT)
        args = self.args()
        self.assertEqual(args.backend, 'sdk')
        self.assertIsNone(args.sdk_tool_catalog)
        self.assertFalse(args.sdk_refresh_overlap)
        self.assertIsNone(args.binary)
        self.assertEqual(args.cwd, str(ROOT))
        with patch.object(sdk, 'SdkBackend') as factory:
            broker.build_backend(args)
            self.assertEqual(factory.call_args.kwargs['issuer'], ISSUER)
            self.assertEqual(factory.call_args.kwargs['url'], URL)
            self.assertFalse(factory.call_args.kwargs['refresh_overlap'])
            broker.build_backend(self.args('--sdk-refresh-overlap'))
            self.assertTrue(factory.call_args.kwargs['refresh_overlap'])
        with self.assertRaises(SystemExit):
            broker.parse_args(['daemon', '--socket', '/private/example/s', '--sdk-url', URL,
                               '--sdk-state-dir', '/private/example/state'])

    def test_native_selection_never_imports_sdk(self):
        args = broker.parse_args(['daemon', '--socket', '/private/example/s',
                                 '--backend', 'native', '--binary', '/example/native'])
        native = SimpleNamespace(Backend=lambda *args: args)
        with patch.dict(sys.modules, {'native_backend': native}), \
             patch.object(broker, 'load_sdk_backend', side_effect=AssertionError('SDK imported')):
            self.assertEqual(broker.build_backend(args), ('/example/native', str(ROOT)))
            self.assertTrue(broker.startup_probe(args))
        self.assertNotIn('native_backend', sys.modules)

    def test_explicit_issuer_before_state_and_reuse_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'state'
            for issuer in (None, '', 'http://issuer.example', 'https://user:pass@issuer.example'):
                with self.assertRaises(ValueError):
                    sdk.SdkBackend(URL, target, issuer=issuer)
                self.assertFalse(target.exists())
            backend = sdk.SdkBackend(URL, target, issuer=ISSUER)
            self.assertEqual(backend._provider()._configured_issuer(), ISSUER)
            self.assertEqual(backend.catalog_source, 'remote')
            self.assertIsNone(backend._release_catalog)
            sdk.SdkBackend(URL, target, issuer=ISSUER)
            for changes in ({'issuer': 'https://other.example/realm'},
                            {'url': 'https://other.example/mcp'}, {'scope': 'openid'},
                            {'client_id': 'different'}):
                settings = dict(url=URL, state_dir=target, issuer=ISSUER)
                settings.update(changes)
                with self.assertRaises(sdk.OAuthFlowError):
                    sdk.SdkBackend(**settings)

    def test_first_oauth_rejects_foreign_issuer_and_endpoint(self):
        async def exercise(foreign_issuer):
            with tempfile.TemporaryDirectory() as tmp:
                backend = sdk.SdkBackend(URL, tmp, issuer=ISSUER)
                provider = backend._provider()
                seen = []
                async def reply(req):
                    seen.append(str(req.url))
                    if str(req.url) == URL:
                        return httpx2.Response(401)
                    if 'oauth-protected-resource' in req.url.path:
                        return httpx2.Response(200, json={'resource': URL,
                            'authorization_servers': ['https://evil.example/realm' if foreign_issuer else ISSUER]})
                    return httpx2.Response(200, json={'issuer': ISSUER,
                        'authorization_endpoint': ISSUER + '/authorize',
                        'token_endpoint': 'https://evil.example/token',
                        'response_types_supported': ['code']})
                with patch.object(backend, '_redirect_handler', side_effect=AssertionError('browser forbidden')):
                    async with httpx2.AsyncClient(auth=provider, transport=httpx2.MockTransport(reply)) as client:
                        with self.assertRaises(sdk.OAuthFlowError):
                            await client.post(URL)
                self.assertFalse(any('evil.example' in item for item in seen))
                self.assertEqual(seen.count(URL), 1)
        asyncio.run(exercise(True))
        asyncio.run(exercise(False))

    def test_normal_oauth_pkce_scope_resource_and_callback_binding(self):
        from urllib.parse import urlparse, parse_qs
        from mcp.shared.auth import AuthorizationCodeResult
        async def exercise(bad_state=False):
            with tempfile.TemporaryDirectory() as tmp:
                backend = sdk.SdkBackend(URL, tmp, issuer=ISSUER)
                provider = backend._provider()
                authorization = {}
                seen = []
                async def redirect(url):
                    authorization.update(parse_qs(urlparse(url).query))
                async def callback():
                    return AuthorizationCodeResult(code='synthetic-code',
                        state='wrong' if bad_state else authorization['state'][0], iss=ISSUER)
                provider.context.redirect_handler = redirect
                provider.context.callback_handler = callback
                async def reply(req):
                    seen.append(req)
                    if str(req.url) == URL:
                        return httpx2.Response(200 if 'authorization' in req.headers else 401)
                    if 'oauth-protected-resource' in req.url.path:
                        return httpx2.Response(200, json={'resource': URL,
                            'authorization_servers': [ISSUER], 'scopes_supported': ['kb:admin']})
                    if req.url.path.endswith('/token'):
                        form = parse_qs(req.content.decode())
                        self.assertEqual(form['resource'], [URL])
                        self.assertIn('code_verifier', form)
                        return httpx2.Response(200, json={'access_token': 'synthetic',
                            'token_type': 'Bearer', 'expires_in': 300})
                    return httpx2.Response(200, json={'issuer': ISSUER,
                        'authorization_endpoint': ISSUER + '/authorize',
                        'token_endpoint': ISSUER + '/token', 'response_types_supported': ['code']})
                async with httpx2.AsyncClient(auth=provider, transport=httpx2.MockTransport(reply)) as client:
                    if bad_state:
                        with self.assertRaises(sdk.OAuthFlowError): await client.post(URL)
                        self.assertFalse(any(r.url.path.endswith('/token') for r in seen))
                    else:
                        response = await client.post(URL)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(authorization['scope'], [sdk.DEFAULT_SCOPE])
                        self.assertEqual(authorization['code_challenge_method'], ['S256'])
                        self.assertEqual(authorization['resource'], [URL])
        asyncio.run(exercise())
        asyncio.run(exercise(True))

    def test_result_decode_and_integrity_no_replay(self):
        async def exercise():
            original = json.dumps({'citation': 'synthetic-reference ' * 1000})
            encoded = graph_wire.encode_text(original)
            self.assertNotEqual(encoded, original)
            corrupt = json.loads(encoded)
            corrupt['_kb_wire']['sha256'] = '0' * 64
            replies = [encoded, json.dumps(corrupt)]
            class Client:
                calls = 0
                async def call_tool(self, *args):
                    text = replies[self.calls]
                    self.calls += 1
                    return {'content': [{'type': 'text', 'text': text}], 'isError': False}
            backend = sdk.SdkBackend.__new__(sdk.SdkBackend)
            backend._queue = asyncio.Queue()
            futures = [Future(), Future()]
            for future in futures:
                backend._queue.put_nowait(('get_semantic_network', {}, future))
            backend._queue.put_nowait(None)
            client = Client()
            await backend._request_loop(client)
            self.assertEqual(futures[0].result()['content'][0]['text'], original)
            self.assertIsInstance(futures[1].exception(), sdk.SdkTransportError)
            self.assertEqual(client.calls, 2)
        asyncio.run(exercise())

if __name__ == '__main__':
    # Expected SDK OAuth failures otherwise log synthetic state and local paths.
    import logging
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
