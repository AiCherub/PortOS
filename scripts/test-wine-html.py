#!/usr/bin/env python3
"""Run inside an isolated session image (no visitor/session required).

docker run --rm --entrypoint python3 -v ./scripts:/tests:ro \
    portos/session:latest /tests/test-wine-html.py

Checks real MSHTML rendering/JavaScript via a page callback, not process exit.
"""
import http.server
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import urllib.parse

os.environ.update(DISPLAY=":1", WINEPREFIX="/opt/wineprefix-template")
results = queue.Queue()
page = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>PortOS HTML test</title><style>
body { background: #eef5ff; font: 24px sans-serif; padding: 35px; }
#check { color: rgb(12, 34, 56); width: 320px; }
</style></head><body><h1>Wine HTML / 中文网页</h1>
<div id="check">中文显示正常</div><p id="status">JavaScript pending</p>
<script>window.onload = function () {
 var el = document.getElementById('check');
 var style = window.getComputedStyle(el, null);
 var ok = el.textContent === '中文显示正常' &&
          style.color === 'rgb(12, 34, 56)' && el.offsetWidth === 320;
 document.getElementById('status').innerHTML = ok ? 'JavaScript + CSS PASS' : 'FAIL';
 document.title = ok ? 'PORTOS_HTML_PASS' : 'PORTOS_HTML_FAIL';
 window.report = new Image();
 window.report.src = 'http://127.0.0.1:8765/result?ok=' + ok;
};</script></body></html>""".encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/result?'):
            results.put(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(page)

    def log_message(self, *args):
        pass


def run(*args):
    return subprocess.run(args, check=True, timeout=30, capture_output=True)


def stop_wine(proc):
    subprocess.run(['wineserver', '-k'], timeout=15)
    run('wineserver', '-w')
    proc.wait(timeout=15)


server = http.server.ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
log = open('/tmp/wine-html-test.log', 'w')
x = subprocess.Popen(['Xvnc', ':1', '-geometry', '1024x768', '-depth', '24',
                      '-SecurityTypes', 'None', '-localhost'], stdout=log, stderr=log)
wm = None
passed = False
try:
    for _ in range(50):
        if subprocess.run(['xdpyinfo'], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(.1)
    else:
        raise RuntimeError('X server failed to start')
    wm = subprocess.Popen(['openbox'], stdout=log, stderr=log)
    local = Path('/tmp/portos html.html')
    local.write_bytes(page)
    url = 'http://127.0.0.1:8765/page'
    for arch, exe in [('64', r'C:\Program Files\Internet Explorer\iexplore.exe'),
                      ('32', r'C:\Program Files (x86)\Internet Explorer\iexplore.exe')]:
        for kind, target in [('file', 'file:///Z:/tmp/portos%20html.html'),
                             ('http', url)]:
            proc = subprocess.Popen(['wine', exe, target], stdout=log, stderr=log)
            try:
                assert results.get(timeout=45).get('ok') == ['true'], (arch, kind)
                time.sleep(.5)
                run('python3', '/opt/portos/session/screenshot.py',
                    f'/tmp/wine-html-{arch}-{kind}.png')
                print(f'PASS: {arch}-bit IE {kind}, Chinese DOM, JavaScript, CSS layout', flush=True)
            finally:
                run('python3', '/opt/portos/session/screenshot.py',
                    f'/tmp/wine-html-{arch}-{kind}-last.png')
                stop_wine(proc)
    for kind, command in [('file association', ['start', '/wait', '/unix', str(local)]),
                          ('http association', ['start', '/wait', url])]:
        proc = subprocess.Popen(['wine', *command], stdout=log, stderr=log)
        try:
            assert results.get(timeout=45).get('ok') == ['true'], kind
            print(f'PASS: {kind}', flush=True)
        finally:
            stop_wine(proc)
    # HTTPS uses the same handler; check its registration without depending
    # on a public site's availability or accepting an untrusted certificate.
    https = run('wine', 'reg', 'query',
                r'HKCR\https\shell\open\command', '/ve')
    assert b'iexplore.exe' in https.stdout
    print('PASS: HTTPS handler registration (external TLS not tested)', flush=True)
    passed = True
finally:
    subprocess.run(['wineserver', '-k'], timeout=15)
    if wm:
        wm.terminate()
    x.terminate()
    server.shutdown()
    log.close()
    if not passed:
        print(Path('/tmp/wine-html-test.log').read_text(errors='replace')[-4000:])
