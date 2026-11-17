"""Exercise two starts of the pinned Agent against a loopback registration stub."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

bin_dir, work = map(Path, sys.argv[1:])
identity = work / 'auto-discovery.json'
calls = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if not self.path.startswith('/api/clients/register?'):
            return self.send_error(404)
        calls.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"success","data":{"uuid":"test-id","token":"test-token"}}')

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.umask(0o077)
for first in (True, False):
    with subprocess.Popen([str(bin_dir / 'komari-agent'), '--auto-discovery', 'fixture-key',
                           '--endpoint', 'http://127.0.0.1:' + str(server.server_port), '--disable-auto-update'],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        try:
            for _ in range(50):
                if identity.exists():
                    break
                time.sleep(0.1)
            time.sleep(0.5)
        finally:
            proc.terminate()
            output = proc.communicate(timeout=5)[0]
        assert (b'Successfully registered' if first else b'Using existing auto-discovery token') in output
    assert json.loads(identity.read_bytes())['uuid'] == 'test-id'
    assert len(calls) == 1
print('Two Agent starts; one registration with read-only bin directory.')
