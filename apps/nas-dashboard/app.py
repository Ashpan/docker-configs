import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

SCAN_ROOT = os.environ.get("SCAN_ROOT", "/nas")
CACHE_PATH = os.environ.get("CACHE_PATH", "/data/cache.json")
SCAN_INTERVAL_HOURS = float(os.environ.get("SCAN_INTERVAL_HOURS", "12"))
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "6"))

QBT_BACKUP_DIR = os.environ.get("QBT_BACKUP_DIR", "")
COMPLETED_DIR = os.environ.get("COMPLETED_DIR", "")
RECLAIM_CACHE_PATH = os.environ.get("RECLAIM_CACHE_PATH", "/data/reclaimable.json")

app = Flask(__name__)

state_lock = threading.Lock()
state = {
    "scanning": False,
    "last_scan_started": None,
    "last_scan_finished": None,
    "last_scan_duration_sec": None,
    "last_error": None,
    "files_scanned": 0,
}
tree_lock = threading.Lock()
tree_cache = None

reclaim_state_lock = threading.Lock()
reclaim_state = {
    "scanning": False,
    "last_scan_started": None,
    "last_scan_finished": None,
    "last_scan_duration_sec": None,
    "last_error": None,
    "enabled": bool(QBT_BACKUP_DIR and COMPLETED_DIR),
}
reclaim_lock = threading.Lock()
reclaim_cache = None  # {"items": [...], "total_size": int, "tracked_count": int, "completed_count": int}


def bencode_decode(data, i=0):
    """Minimal bencode decoder — just enough to read qBittorrent .fastresume/.torrent files."""
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        lst = []
        while data[i:i + 1] != b"e":
            v, i = bencode_decode(data, i)
            lst.append(v)
        return lst, i + 1
    if c == b"d":
        i += 1
        d = {}
        while data[i:i + 1] != b"e":
            k, i = bencode_decode(data, i)
            v, i = bencode_decode(data, i)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise ValueError(f"bad bencode at offset {i}")


def get_tracked_completed_names(bt_backup_dir):
    """Names of torrents qBittorrent still tracks with save_path pointing at the completed dir."""
    tracked = set()
    try:
        fastresume_paths = [p for p in os.listdir(bt_backup_dir) if p.endswith(".fastresume")]
    except OSError:
        return tracked
    for fname in fastresume_paths:
        fpath = os.path.join(bt_backup_dir, fname)
        tpath = fpath[:-len(".fastresume")] + ".torrent"
        if not os.path.exists(tpath):
            continue
        try:
            with open(fpath, "rb") as f:
                fd, _ = bencode_decode(f.read())
            with open(tpath, "rb") as f:
                td, _ = bencode_decode(f.read())
            save_path = fd.get(b"save_path", b"")
            if b"completed" not in save_path:
                continue
            name = td.get(b"info", {}).get(b"name")
            if name:
                tracked.add(name.decode("utf-8", errors="replace"))
        except Exception:
            continue
    return tracked


def sole_copy_bytes(path):
    """Sum of bytes in files under path that have no other hardlink (nlink == 1)."""
    total = 0
    if os.path.isfile(path):
        try:
            st = os.stat(path)
            return st.st_size if st.st_nlink == 1 else 0
        except OSError:
            return 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for fn in files:
            try:
                st = os.stat(os.path.join(root, fn))
                if st.st_nlink == 1:
                    total += st.st_size
            except OSError:
                continue
    return total


def compute_reclaimable():
    tracked = get_tracked_completed_names(QBT_BACKUP_DIR)
    try:
        entries = os.listdir(COMPLETED_DIR)
    except OSError as e:
        raise RuntimeError(f"cannot list COMPLETED_DIR: {e}")

    items = []
    for name in entries:
        if name in tracked:
            continue
        path = os.path.join(COMPLETED_DIR, name)
        size = sole_copy_bytes(path)
        if size > 0:
            items.append({"name": name, "size": size})

    items.sort(key=lambda x: x["size"], reverse=True)
    return {
        "items": items,
        "total_size": sum(i["size"] for i in items),
        "tracked_count": len(tracked),
        "completed_count": len(entries),
    }


def scan_dir(path, depth):
    """Recursively compute a size tree. Beyond MAX_DEPTH, still sums size
    but stops recording children to bound memory/JSON size."""
    name = os.path.basename(path.rstrip("/")) or path
    node = {"name": name, "path": path, "size": 0, "is_dir": True, "children": []}
    try:
        entries = list(os.scandir(path))
    except (PermissionError, FileNotFoundError, OSError):
        return node

    children = []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth >= MAX_DEPTH:
                    size = dir_size_only(entry.path)
                    child = {"name": entry.name, "path": entry.path, "size": size, "is_dir": True, "children": []}
                else:
                    child = scan_dir(entry.path, depth + 1)
                node["size"] += child["size"]
                children.append(child)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                node["size"] += size
                with state_lock:
                    state["files_scanned"] += 1
                children.append({"name": entry.name, "path": entry.path, "size": size, "is_dir": False, "children": []})
        except (PermissionError, FileNotFoundError, OSError):
            continue

    children.sort(key=lambda c: c["size"], reverse=True)
    node["children"] = children
    return node


def dir_size_only(path):
    total = 0
    count = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f), follow_symlinks=False).st_size
                count += 1
            except OSError:
                continue
    with state_lock:
        state["files_scanned"] += count
    return total


def run_scan():
    global tree_cache
    with state_lock:
        if state["scanning"]:
            return False
        state["scanning"] = True
        state["last_scan_started"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None
        state["files_scanned"] = 0

    started = time.monotonic()
    try:
        tree = scan_dir(SCAN_ROOT, 0)
        with tree_lock:
            tree_cache = tree
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp_path = CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(tree, f)
        os.replace(tmp_path, CACHE_PATH)
    except Exception as e:
        with state_lock:
            state["last_error"] = str(e)
    finally:
        with state_lock:
            state["scanning"] = False
            state["last_scan_finished"] = datetime.now(timezone.utc).isoformat()
            state["last_scan_duration_sec"] = round(time.monotonic() - started, 1)
    return True


def start_scan_async():
    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return thread


def periodic_scan_loop():
    while True:
        time.sleep(SCAN_INTERVAL_HOURS * 3600)
        run_scan()


def run_reclaim_scan():
    global reclaim_cache
    with reclaim_state_lock:
        if reclaim_state["scanning"] or not reclaim_state["enabled"]:
            return False
        reclaim_state["scanning"] = True
        reclaim_state["last_scan_started"] = datetime.now(timezone.utc).isoformat()
        reclaim_state["last_error"] = None

    started = time.monotonic()
    try:
        result = compute_reclaimable()
        with reclaim_lock:
            reclaim_cache = result
        os.makedirs(os.path.dirname(RECLAIM_CACHE_PATH), exist_ok=True)
        tmp_path = RECLAIM_CACHE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(result, f)
        os.replace(tmp_path, RECLAIM_CACHE_PATH)
    except Exception as e:
        with reclaim_state_lock:
            reclaim_state["last_error"] = str(e)
    finally:
        with reclaim_state_lock:
            reclaim_state["scanning"] = False
            reclaim_state["last_scan_finished"] = datetime.now(timezone.utc).isoformat()
            reclaim_state["last_scan_duration_sec"] = round(time.monotonic() - started, 1)
    return True


def start_reclaim_scan_async():
    thread = threading.Thread(target=run_reclaim_scan, daemon=True)
    thread.start()
    return thread


def periodic_reclaim_loop():
    while True:
        time.sleep(SCAN_INTERVAL_HOURS * 3600)
        run_reclaim_scan()


def load_reclaim_cache_from_disk():
    global reclaim_cache
    if os.path.exists(RECLAIM_CACHE_PATH):
        try:
            with open(RECLAIM_CACHE_PATH) as f:
                with reclaim_lock:
                    reclaim_cache = json.load(f)
            return True
        except (json.JSONDecodeError, OSError):
            return False
    return False


def load_cache_from_disk():
    global tree_cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                with tree_lock:
                    tree_cache = json.load(f)
            return True
        except (json.JSONDecodeError, OSError):
            return False
    return False


def find_node(path):
    with tree_lock:
        if tree_cache is None:
            return None
        if path in (None, "", SCAN_ROOT, "/"):
            return tree_cache
        rel = path.strip("/")
        node = tree_cache
        target = os.path.normpath(os.path.join(SCAN_ROOT, rel))
        # walk down matching path segments
        cur_path = SCAN_ROOT
        for part in target[len(SCAN_ROOT):].strip("/").split("/"):
            if not part:
                continue
            match = next((c for c in node["children"] if c["name"] == part), None)
            if match is None:
                return None
            node = match
        return node


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with state_lock:
        s = dict(state)
    usage = shutil.disk_usage(SCAN_ROOT)
    s["disk_total"] = usage.total
    s["disk_used"] = usage.used
    s["disk_free"] = usage.free
    s["disk_percent_used"] = round(usage.used / usage.total * 100, 1) if usage.total else 0
    s["has_cache"] = tree_cache is not None
    s["scan_root"] = SCAN_ROOT
    return jsonify(s)


@app.route("/api/tree")
def api_tree():
    rel_path = request.args.get("path", "")
    limit = int(request.args.get("limit", "50"))
    node = find_node(rel_path)
    if node is None:
        return jsonify({"error": "not found or not yet scanned"}), 404
    children = node["children"]
    shown = children[:limit]
    remainder = children[limit:]
    remainder_size = sum(c["size"] for c in remainder)
    return jsonify({
        "name": node["name"],
        "path": node["path"].replace(SCAN_ROOT, "", 1) or "/",
        "size": node["size"],
        "is_dir": node["is_dir"],
        "children": [
            {"name": c["name"], "path": c["path"].replace(SCAN_ROOT, "", 1), "size": c["size"], "is_dir": c["is_dir"]}
            for c in shown
        ],
        "more_count": len(remainder),
        "more_size": remainder_size,
    })


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    started = start_scan_async()
    return jsonify({"started": True})


@app.route("/reclaimable")
def reclaimable_page():
    return render_template("reclaimable.html")


@app.route("/api/reclaimable")
def api_reclaimable():
    with reclaim_state_lock:
        s = dict(reclaim_state)
    with reclaim_lock:
        data = reclaim_cache
    s["has_cache"] = data is not None
    if data is not None:
        s["items"] = data["items"]
        s["total_size"] = data["total_size"]
        s["tracked_count"] = data["tracked_count"]
        s["completed_count"] = data["completed_count"]
    return jsonify(s)


@app.route("/api/reclaimable/rescan", methods=["POST"])
def api_reclaimable_rescan():
    if not reclaim_state["enabled"]:
        return jsonify({"started": False, "error": "QBT_BACKUP_DIR / COMPLETED_DIR not configured"}), 400
    start_reclaim_scan_async()
    return jsonify({"started": True})


if __name__ == "__main__":
    load_cache_from_disk()
    if tree_cache is None:
        start_scan_async()
    threading.Thread(target=periodic_scan_loop, daemon=True).start()

    load_reclaim_cache_from_disk()
    if reclaim_state["enabled"] and reclaim_cache is None:
        start_reclaim_scan_async()
    threading.Thread(target=periodic_reclaim_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=5000)
