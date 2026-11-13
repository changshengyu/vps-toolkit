#!/usr/bin/env python3
"""Validate the production Caddy config, then test real trusted TLS 1.3/H2/static serving.

Runtime uses a private fixture CA and loopback port 18443, never a real domain,
public ACME request, live user service or firewall. Production uses public ACME.
"""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_module', ROOT / 'lib/sni/site.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True)


def main():
    with tempfile.TemporaryDirectory(prefix='sni-check-') as temp:
        root = Path(temp)
        p = s.Paths(root)
        for path in p.directories() + (p.runtime,):
            path.mkdir(parents=True, exist_ok=True)
        supplied = os.environ.get('CADDY_TEST_BINARY')
        binary = Path(supplied) if supplied else p.binary
        if not supplied:
            s.download_binary(binary)
        value = s.configuration('site.example.com', p)
        s.json_save(p.document, value)
        env = dict(os.environ, XDG_DATA_HOME=str(p.data), XDG_CONFIG_HOME=str(p.data / 'config'))
        checked = subprocess.run([str(binary), 'validate', '--config', str(p.document)],
                                 env=env, capture_output=True, text=True, timeout=30)
        assert checked.returncode == 0, checked.stderr
        policy = value['apps']['tls']['automation']['policies'][0]
        assert policy['issuers'][0]['challenges']['http']['disabled'] is True
        assert value['apps']['http']['servers']['site']['automatic_https']['disable_redirects'] is True
        assert value['apps']['http']['servers']['site']['protocols'] == ['h1', 'h2']
        print('PASS: real pinned Caddy validates production TLS-ALPN-only ACME, TCP 443, no redirect or HTTP/3 config', flush=True)
        ca, ca_key, cert, key = [root / name for name in ('ca.pem', 'ca-key.pem', 'cert.pem', 'key.pem')]
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-subj', '/CN=SNI site fixture CA', '-days', '1', '-keyout', ca_key, '-out', ca,
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                '-addext', 'subjectKeyIdentifier=hash')
        csr = root / 'site.csr'
        openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-subj', '/CN=site.example.com', '-keyout', key, '-out', csr)
        ext = root / 'extensions'
        ext.write_text('subjectAltName=DNS:site.example.com\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n')
        openssl('x509', '-req', '-in', csr, '-CA', ca, '-CAkey', ca_key, '-set_serial', '1',
                '-days', '1', '-extfile', ext, '-out', cert)
        runtime = copy.deepcopy(value)
        runtime['apps']['http']['servers']['site']['listen'] = ['127.0.0.1:18443']
        runtime['apps']['tls'] = {'certificates': {'load_files': [{'certificate': str(cert), 'key': str(key)}]}}
        runtime_path = p.config / 'runtime.json'
        s.json_save(runtime_path, runtime)
        (p.web / 'index.html').write_text(s.render_page('云间手记'))
        (p.web / '.vps-toolkit-owned').write_text(s.MARKER)
        free_tcp = []
        for port in (80, 2019):
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                    free_tcp.append(port)
                except OSError:
                    pass
        log = root / 'caddy.log'
        with log.open('w') as stream:
            process = subprocess.Popen([str(binary), 'run', '--config', str(runtime_path)],
                                       env=env, stdout=stream, stderr=subprocess.STDOUT)
        context = ssl.create_default_context(cafile=str(ca))
        context.verify_flags |= ssl.VERIFY_X509_STRICT
        try:
            last_probe = ''
            for _ in range(80):
                try:
                    expiry = s.probe('site.example.com', port=18443, context=context)
                    assert expiry
                    break
                except (OSError, s.Error) as exc:
                    last_probe = str(exc)
                    if process.poll() is not None:
                        raise AssertionError(log.read_text())
                    time.sleep(0.2)
            else:
                raise AssertionError('TLS probe failed: ' + last_probe + '\n' + log.read_text())
            for port in free_tcp:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', port))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', 18443))
            # Editing a static file should be reflected without restarting the server.
            s.atomic(p.web / 'index.html', '<!doctype html><html><body>replaced page</body></html>')
            client = ssl.create_default_context(cafile=str(ca))
            client.set_alpn_protocols(['http/1.1'])
            with client.wrap_socket(socket.create_connection(('127.0.0.1', 18443)), server_hostname='site.example.com') as conn:
                conn.sendall(b'GET / HTTP/1.1\r\nHost: site.example.com\r\nConnection: close\r\n\r\n')
                response = b''
                while True:
                    block = conn.recv(8192)
                    if not block:
                        break
                    response += block
                assert b'replaced page' in response
            with client.wrap_socket(socket.create_connection(('127.0.0.1', 18443)), server_hostname='site.example.com') as conn:
                conn.sendall(b'GET /.vps-toolkit-owned HTTP/1.1\r\nHost: site.example.com\r\nConnection: close\r\n\r\n')
                assert conn.recv(4096).startswith(b'HTTP/1.1 404 ')
            print('PASS: real trusted certificate/SNI, TLS 1.3, HTTP/2 ALPN, HTTP 200, live static page replacement, no extra TCP 80/2019 or UDP listener')
        except Exception:
            print(log.read_text())
            raise
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
