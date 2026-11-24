#!/usr/bin/env python3
"""TV downloader. UI at / to upload an APK or add a direct link -> short code.
/<code> redirects to the target; uploaded files are served at /<file>.
Data in $DATA_DIR: links.json + apks/. No auth: LAN only.
Run: DATA_DIR=./data python3 server.py [port]"""
import json, html, os, sys, pathlib, threading
from urllib.parse import urlparse, parse_qs, quote, unquote
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
LINKS, APKS, LOCK = ROOT / "links.json", ROOT / "apks", threading.Lock()
APKS.mkdir(parents=True, exist_ok=True)

def load():
    return json.loads(LINKS.read_text()) if LINKS.exists() else {}

def save(d):
    tmp = LINKS.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=2)); tmp.replace(LINKS)

def add(name, url):
    with LOCK:
        d = load()
        code = next(str(i) for i in range(1, len(d) + 2) if str(i) not in d)  # smallest free: short codes
        d[code] = {"name": name, "url": url}; save(d)
    return code

PAGE = """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>TV Downloader</title>
<style>body{font:16px system-ui;max-width:640px;margin:2rem auto;padding:0 1rem}
form{display:grid;gap:.5rem;margin-bottom:1.5rem}input,button{font:inherit;padding:.5rem}
li{margin:.6rem 0}code{background:#8882;padding:.1rem .4rem;border-radius:4px}#msg{color:#c33}</style>
<h1>TV Downloader</h1><p id=msg></p>
<form id=up><b>Upload APK</b><input name=name placeholder="Name (optional)"><input type=file name=f required><button>Upload</button></form>
<form id=ln><b>Direct link</b><input name=name placeholder="Name (optional)"><input name=url type=url placeholder="https://..." required><button>Add</button></form>
<ul>%s</ul>
<script>
const msg=t=>document.getElementById('msg').textContent=t||'';
async function post(u,o){const r=await fetch(u,{method:'POST',...o});if(!r.ok){msg(await r.text());return}location.reload()}
up.onsubmit=e=>{e.preventDefault();const f=up.f.files[0];
 post('/api/upload?filename='+encodeURIComponent(f.name)+'&name='+encodeURIComponent(up.name.value),{body:f})}
ln.onsubmit=e=>{e.preventDefault();post('/api/link',{body:JSON.stringify({name:ln.name.value,url:ln.url.value})})}
function del(c){if(confirm('Delete '+c+'?'))post('/api/delete?code='+c,{})}
document.querySelectorAll('[data-c]').forEach(e=>e.textContent=location.host+'/'+e.dataset.c)
</script>"""

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(APKS), **k)

    def reply(self, code, text="", ctype="text/plain"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        links = load()
        if path in links:
            self.send_response(302); self.send_header("Location", links[path]["url"]); self.end_headers()
        elif path == "":
            rows = "".join(
                f"<li><b>{html.escape(l['name'])}</b> <code data-c='{html.escape(c)}'></code> "
                f"<a href='{html.escape(l['url'])}'>open</a> <a href='#' onclick=\"del('{html.escape(c)}');return false\">delete</a></li>"
                for c, l in links.items())
            self.reply(200, PAGE % (rows or "<li>Nothing yet.</li>"), "text/html; charset=utf-8")
        else:
            super().do_GET()  # serves ./apks (stdlib blocks path traversal)

    def do_POST(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length", 0))
        if u.path == "/api/upload":
            fn = os.path.basename(q.get("filename", "")).replace("\x00", "")
            if not fn or fn.startswith("."): return self.reply(400, "bad filename")
            with open(APKS / fn, "wb") as f:
                while n > 0:
                    chunk = self.rfile.read(min(n, 1 << 20))
                    if not chunk: break
                    f.write(chunk); n -= len(chunk)
            add(q.get("name") or fn, "/" + quote(fn))
        elif u.path == "/api/link":
            try: d = json.loads(self.rfile.read(n)); url = d["url"].strip()
            except (ValueError, KeyError): return self.reply(400, "bad request")
            if not url.startswith(("http://", "https://")): return self.reply(400, "url must be http(s)")
            add(d.get("name") or url, url)
        elif u.path == "/api/delete":
            with LOCK:
                d = load(); e = d.pop(q.get("code", ""), None); save(d)
            # remove the uploaded file too, unless another entry still points at it
            if e and e["url"].startswith("/") and not any(x["url"] == e["url"] for x in d.values()):
                (APKS / os.path.basename(unquote(e["url"][1:]))).unlink(missing_ok=True)
        else:
            return self.reply(404, "not found")
        self.reply(200, "ok")

ThreadingHTTPServer(("", int(sys.argv[1]) if len(sys.argv) > 1 else 8080), H).serve_forever()
