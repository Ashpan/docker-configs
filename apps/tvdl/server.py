#!/usr/bin/env python3
"""TV downloader.
/            public: type a 4-digit code on the TV -> redirects to the target
/admin       manage codes: upload an APK or add a direct link (gate this one!)
/api/*       admin actions (gate these too)
/<file>      uploaded files, served from $DATA_DIR/apks
Data in $DATA_DIR: links.json + apks/. No built-in auth: the reverse proxy must protect /admin and /api/*.
Run: DATA_DIR=./data python3 server.py [port]"""
import json, html, os, re, secrets, sys, pathlib, threading
from urllib.parse import urlparse, parse_qs, quote, unquote
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = pathlib.Path(os.environ.get("DATA_DIR", "/data"))
LINKS, APKS, LOCK = ROOT / "links.json", ROOT / "apks", threading.Lock()
APKS.mkdir(parents=True, exist_ok=True)
CODE = re.compile(r"\d{4}")

def load():
    return json.loads(LINKS.read_text()) if LINKS.exists() else {}

def save(d):
    tmp = LINKS.with_suffix(".tmp"); tmp.write_text(json.dumps(d, indent=2)); tmp.replace(LINKS)

def add(name, url):
    with LOCK:
        d = load()
        if len(d) >= 10000: raise RuntimeError("all 10,000 codes are in use")
        while (code := f"{secrets.randbelow(10000):04d}") in d: pass
        d[code] = {"name": name, "url": url}; save(d)
    return code

STYLE = """
:root{color-scheme:dark;--bg:#15132b;--raised:#201d40;--line:#363163;--text:#f1eeff;--muted:#9b96c4;--amber:#ffb23e;--bad:#ff7a8a;
font:17px/1.5 ui-rounded,"Avenir Next","Segoe UI",system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);min-height:100vh}
h1{margin:0;font-weight:800;letter-spacing:-.02em}
:focus-visible{outline:3px solid var(--amber);outline-offset:3px}
button,input{font:inherit;color:inherit}
button{cursor:pointer;border:0;border-radius:10px;padding:.6rem 1.1rem;font-weight:700;background:var(--amber);color:#2a1a00}
button:disabled{opacity:.5;cursor:progress}
button.quiet{background:transparent;color:var(--muted);padding:.4rem .7rem;border:1px solid var(--line);font-weight:600}
button.quiet:hover{color:var(--text);border-color:var(--muted)}
button.quiet.danger:hover{color:var(--bad);border-color:var(--bad)}
"""

HOME = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'><title>TV Downloader</title>
<style>%(style)s
main{min-height:100vh;display:grid;place-content:center;justify-items:center;gap:2.2rem;padding:2rem;text-align:center}
h1{font-size:clamp(2.2rem,6vw,4rem)}
.slots{position:relative;display:flex;gap:clamp(.7rem,2vw,1.4rem)}
.slot{width:clamp(4.2rem,14vw,8rem);height:clamp(5.6rem,19vw,10.5rem);display:grid;place-items:center;
 background:var(--raised);border:3px solid var(--line);border-radius:18px;
 font-size:clamp(3rem,11vw,6.5rem);font-weight:800;font-variant-numeric:tabular-nums;transition:border-color .15s}
.slot.on{border-color:var(--amber)}
.slots.bad .slot{border-color:var(--bad)}
.slots input{position:absolute;inset:0;width:100%%;height:100%%;opacity:0;font-size:16px;cursor:text}
.slots:focus-within{outline:3px solid transparent}
#msg{min-height:1.6em;margin:0;color:var(--bad);font-size:1.15rem}
.hint{margin:0;color:var(--muted);font-size:1.15rem}
</style>
<main>
<h1>Enter your code</h1>
<form class=slots id=f autocomplete=off>
 <span class=slot></span><span class=slot></span><span class=slot></span><span class=slot></span>
 <input id=c inputmode=numeric pattern='[0-9]*' maxlength=4 autofocus aria-label='4-digit code'>
</form>
<p id=msg role=alert>%(msg)s</p>
<p class=hint>Your code opens a download or an app install.</p>
</main>
<script>
const c=document.getElementById('c'),f=document.getElementById('f'),s=[...document.querySelectorAll('.slot')];
function draw(){const v=c.value;s.forEach((e,i)=>{e.textContent=v[i]||'';e.classList.toggle('on',document.activeElement===c&&i===Math.min(v.length,3))})}
c.addEventListener('input',()=>{c.value=c.value.replace(/\\D/g,'').slice(0,4);f.classList.remove('bad');draw();
 if(c.value.length===4)location.href='/'+c.value});
['focus','blur','keyup'].forEach(e=>c.addEventListener(e,draw));
f.addEventListener('submit',e=>e.preventDefault());
if(%(bad)s)f.classList.add('bad');
draw();
</script></html>"""

ADMIN = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'><title>TV Downloader admin</title>
<style>%(style)s
main{max-width:680px;margin:0 auto;padding:2.5rem 1.2rem 4rem}
h1{font-size:2rem;margin-bottom:.3rem}
.sub{margin:0 0 2rem;color:var(--muted)}
.add{background:var(--raised);border:1px solid var(--line);border-radius:16px;padding:1.2rem}
.tabs{display:flex;gap:.4rem;margin-bottom:1rem}
.tabs button{background:transparent;color:var(--muted);border:1px solid var(--line);padding:.4rem .9rem}
.tabs button[aria-selected=true]{background:var(--text);color:var(--bg);border-color:var(--text)}
form{display:grid;gap:.7rem}
form[hidden]{display:none}
label{display:grid;gap:.25rem;color:var(--muted);font-size:.9rem}
input[type=text],input[type=url],input[type=file]{width:100%%;padding:.6rem .75rem;background:var(--bg);border:1px solid var(--line);border-radius:10px;color:var(--text)}
input::file-selector-button{margin-right:.8rem;border:0;border-radius:8px;padding:.4rem .8rem;background:var(--line);color:var(--text);font:inherit;cursor:pointer}
form button{justify-self:start}
#msg{min-height:1.4em;margin:.8rem 0 0;color:var(--bad)}
h2{margin:2.4rem 0 .8rem;font-size:1.15rem}
ul{list-style:none;margin:0;padding:0;display:grid;gap:.6rem}
li{display:grid;grid-template-columns:auto 1fr;gap:.2rem 1.1rem;align-items:center;padding:.9rem 1.1rem;background:var(--raised);border:1px solid var(--line);border-radius:14px}
.code{grid-row:span 2;font-size:2.1rem;font-weight:800;font-variant-numeric:tabular-nums;color:var(--amber);letter-spacing:.04em}
.name{font-weight:700;overflow-wrap:anywhere}
.target{color:var(--muted);font-size:.85rem;overflow-wrap:anywhere}
.acts{grid-column:2;display:flex;gap:.5rem;margin-top:.4rem;flex-wrap:wrap}
.empty{color:var(--muted);padding:1.2rem 0}
@media(max-width:480px){li{grid-template-columns:1fr}.code{grid-row:auto}.acts{grid-column:1}}
</style>
<main>
<h1>TV Downloader</h1>
<p class=sub>Add a file or link, then type its code on the TV at <b id=host></b>.</p>
<section class=add>
 <div class=tabs role=tablist>
  <button type=button role=tab aria-selected=true data-t=up>Upload APK</button>
  <button type=button role=tab aria-selected=false data-t=ln>Add link</button>
 </div>
 <form id=up><label>Name (optional)<input type=text name=name></label>
  <label>File<input type=file name=f required></label><button>Upload</button></form>
 <form id=ln hidden><label>Name (optional)<input type=text name=name></label>
  <label>Link<input type=url name=url placeholder="https://" required></label><button>Add link</button></form>
 <p id=msg role=alert></p>
</section>
<h2>Codes</h2>
<ul id=list>%(rows)s</ul>
</main>
<script>
const $=s=>document.querySelector(s),msg=t=>$('#msg').textContent=t||'';
$('#host').textContent=location.host;
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.tabs button').forEach(x=>x.setAttribute('aria-selected',x===b));
 $('#up').hidden=b.dataset.t!=='up';$('#ln').hidden=b.dataset.t!=='ln';msg()});
async function post(u,o,btn,busy){msg();const t=btn&&btn.textContent;if(btn){btn.disabled=true;btn.textContent=busy}
 try{const r=await fetch(u,{method:'POST',...o});if(!r.ok)throw new Error(await r.text());location.reload()}
 catch(e){msg(e.message||'Request failed. Try again.');if(btn){btn.disabled=false;btn.textContent=t}}}
$('#up').onsubmit=e=>{e.preventDefault();const f=e.target.f.files[0];
 post('/api/upload?filename='+encodeURIComponent(f.name)+'&name='+encodeURIComponent(e.target.name.value),{body:f},e.submitter,'Uploading…')};
$('#ln').onsubmit=e=>{e.preventDefault();
 post('/api/link',{body:JSON.stringify({name:e.target.name.value,url:e.target.url.value})},e.submitter,'Adding…')};
$('#list').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const c=b.closest('li').dataset.c;
 if(b.dataset.a==='del'){if(confirm('Delete code '+c+'?'))post('/api/delete?code='+c,{})}
 if(b.dataset.a==='copy'){await navigator.clipboard?.writeText(location.origin+'/'+c);b.textContent='Copied';setTimeout(()=>b.textContent='Copy link',1500)}};
</script></html>"""

def rows(links):
    if not links:
        return "<li class=empty style='display:block;border-style:dashed;background:none'>Nothing here yet. Upload an APK or add a link to get a code.</li>"
    out = []
    for c, l in sorted(links.items(), key=lambda x: x[1]["name"].lower()):
        e = html.escape
        out.append(
            f"<li data-c='{e(c)}'><span class=code>{e(c)}</span><span class=name>{e(l['name'])}</span>"
            f"<span class=target>{e(l['url'])}</span>"
            f"<span class=acts><button class=quiet data-a=copy>Copy link</button>"
            f"<button class=quiet data-a=open onclick=\"open('/{e(c)}')\">Open</button>"
            f"<button class='quiet danger' data-a=del>Delete</button></span></li>")
    return "".join(out)

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(APKS), **k)

    def reply(self, code, text="", ctype="text/plain; charset=utf-8"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers()
        self.wfile.write(text.encode())

    def redirect(self, to):
        self.send_response(302); self.send_header("Location", to); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path); path = u.path.strip("/"); links = load()
        if path == "":
            miss = parse_qs(u.query).get("miss", [""])[0]
            bad = bool(CODE.fullmatch(miss))
            self.reply(200, HOME % dict(style=STYLE, bad="true" if bad else "false",
                msg=f"No match for {miss}. Check the code and try again." if bad else ""), "text/html; charset=utf-8")
        elif path == "admin":
            self.reply(200, ADMIN % dict(style=STYLE, rows=rows(links)), "text/html; charset=utf-8")
        elif path in links:
            self.redirect(links[path]["url"])
        elif CODE.fullmatch(path):
            self.redirect("/?miss=" + path)
        else:
            super().do_GET()  # serves ./apks (stdlib blocks path traversal)

    def do_POST(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length", 0))
        if u.path == "/api/upload":
            fn = os.path.basename(q.get("filename", "")).replace("\x00", "")
            if not fn or fn.startswith(".") or fn == "admin" or CODE.fullmatch(fn): return self.reply(400, "That file name isn't allowed. Rename the file and try again.")
            with open(APKS / fn, "wb") as f:
                while n > 0:
                    chunk = self.rfile.read(min(n, 1 << 20))
                    if not chunk: break
                    f.write(chunk); n -= len(chunk)
            add(q.get("name") or fn, "/" + quote(fn))
        elif u.path == "/api/link":
            try: d = json.loads(self.rfile.read(n)); url = d["url"].strip()
            except (ValueError, KeyError): return self.reply(400, "Couldn't read that link. Check it and try again.")
            if not url.startswith(("http://", "https://")): return self.reply(400, "The link must start with http:// or https://")
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
