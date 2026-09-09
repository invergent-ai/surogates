"""Task boundaries exercised through real local HTTP requests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from eogbench.proxy import GymProxy, context_headers


@pytest.mark.parametrize('sse', [False, True])
def test_discovery_execution_identity_and_task_switch(sse):
    seen = []

    class Gym(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((message, dict(self.headers)))
            result = {'tools': [{'name': name} for name in ('find_user', 'update', 'admin')], 'nextCursor': 'page2'}
            payload = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            if sse:
                payload = b'event: message\ndata: ' + payload + b'\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if sse else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Mcp-Session-Id', 'fixture-session')
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Gym)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = GymProxy(f'http://127.0.0.1:{server.server_address[1]}').start()
    try:
        proxy.configure_task('db-a', ['find_user', 'update'], ['update'],
                             context={'user_id': 'task-user'}, auth_config={'type': 'bearer', 'token': 'task-token'})
        with httpx.Client(base_url=proxy.local_url, timeout=5) as client:
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
            response = client.post('/mcp', json=request, headers={'x-user-id': 'attacker', 'x-database-id': 'other', 'Authorization': 'other'})
            assert response.json()['result'] == {'tools': [{'name': 'find_user'}], 'nextCursor': 'page2'}
            assert response.headers['Mcp-Session-Id'] == 'fixture-session'
            headers = {k.lower(): v for k, v in seen[-1][1].items()}
            assert headers['x-user-id'] == 'task-user'
            assert headers['x-database-id'] == 'db-a'
            assert headers['authorization'] == 'Bearer task-token'
            for name in ('update', 'admin'):
                result = client.post('/mcp', json={**request, 'method': 'tools/call', 'params': {'name': name}})
                assert result.json()['error']['code'] == -32602
            assert client.post('/mcp', json=[request]).json()['error']
            assert client.post('/create_database', json=request).status_code == 403
            assert client.post('/mcp?bypass=1', json=request).status_code == 403
            assert len(seen) == 1
            client.post('/mcp', json={**request, 'method': 'tools/call', 'params': {'name': 'find_user'}})
            assert len(seen) == 2
            proxy.set_database_id(None)
            assert client.post('/mcp', json=request).status_code == 403
            proxy.configure_task('db-b', ['update'])
            assert client.post('/mcp', json=request).json()['result']['tools'] == [{'name': 'update'}]
            headers = {k.lower(): v for k, v in seen[-1][1].items()}
            assert headers['x-database-id'] == 'db-b'
            assert 'x-user-id' not in headers and 'authorization' not in headers
    finally:
        proxy.stop()
        server.shutdown()
        server.server_close()


def test_identity_cannot_override_database_or_inject_headers():
    for context in ({'database_id': 'other'}, {'user_id': 'user\r\nx-other: injected'}):
        with pytest.raises(ValueError):
            context_headers(context)
