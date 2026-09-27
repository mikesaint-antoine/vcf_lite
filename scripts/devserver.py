"""Serve the UI in a normal browser for development / screenshots.

    python scripts/devserver.py [port]

Then open http://127.0.0.1:8765/ .  The page gets a stand-in for
``window.pywebview.api`` that forwards calls over HTTP to the real Api.
URL parameters script a few actions (for headless screenshots):
    ?open=/path/file.vcf.gz&chrom=chr1&pos=100000&rsid=rs123&select=3&header=1&wait=800
"""

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from vcflite.api import Api  # noqa: E402

UI = ROOT / "vcflite" / "ui"
API = Api()

SHIM = """
<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
  const r = await fetch('/__api/' + name, { method: 'POST', body: JSON.stringify(args) });
  return r.json();
}})};
window.addEventListener('load', async () => {
  window.dispatchEvent(new Event('pywebviewready'));
  const p = new URLSearchParams(location.search);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (p.get('open')) { await window.app.openPath(p.get('open')); await sleep(+(p.get('wait') || 600)); }
  const $ = (id) => document.getElementById(id);
  if (p.get('chrom') || p.get('pos')) {
    if (p.get('chrom')) $('chrom').value = p.get('chrom');
    $('pos').value = p.get('pos') || '';
    $('goto-form').requestSubmit(); await sleep(+(p.get('wait') || 600)); }
  if (p.get('rsid')) { $('rsid').value = p.get('rsid'); $('rsid-form').requestSubmit(); await sleep(+(p.get('wait') || 600)); }
  if (p.get('hide')) { const h = document.getElementById('hide-ref'); h.checked = true; h.dispatchEvent(new Event('change')); await sleep(600); }
  if (p.get('select')) { const rows = document.querySelectorAll('#body tr[data-o]'); rows[+p.get('select')].click(); await sleep(400); }
  if (p.get('header')) { document.getElementById('header-btn').click(); }
  if (p.get('scroll')) { document.getElementById('table-wrap').scrollTop = +p.get('scroll'); await sleep(800); }
});
</script>
"""


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(UI), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?") or self.path.startswith("/index.html"):
            html = (UI / "index.html").read_text().replace('<script src="app.js">', SHIM + '<script src="app.js">')
            b = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        super().do_GET()

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        args = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "[]")
        if name.startswith("_") or not hasattr(API, name):
            res = {"error": "no such method"}
        elif name == "choose_file":
            res = None
        else:
            res = getattr(API, name)(*args)
        b = json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
