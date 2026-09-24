#!/usr/bin/env python3
"""pview - look at plot images in a browser, driven from the terminal.

The server listens on 127.0.0.1 only, so from another machine you reach it through an
ssh port forward (LocalForward 8765 127.0.0.1:8765).

  pview [PATH] [-r]       point the viewer at PATH (folder or image, default: cwd)
  pview push PANE [CWD]   point it at CWD and pin the image paths visible in tmux pane PANE
  pview serve             run the server in the foreground (normally auto-started)
  pview stop              stop the server
  pview status            say whether the server runs and whether a browser tab is connected

Folders it may serve are read from $PVIEW_ROOTS, else ~/.config/pview/roots (one path
per line), else your home folder. $PVIEW_PORT changes the port (default 8765).
"""

import argparse
import collections
import hashlib
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HOST = "127.0.0.1"
PORT = int(os.environ.get("PVIEW_PORT", "8765"))
HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.realpath(__file__))
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "pview")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "pview")
ROOTS_FILE = os.path.join(CONFIG_DIR, "roots")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "site-packages", "venv", ".venv",
             "env", ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", ".cache"}
MAX_IMAGES = 5000
MAX_DEPTH = 8
WALK_BUDGET = 2.0

def _configured_roots():
    """Folders pview may serve: $PVIEW_ROOTS, else ~/.config/pview/roots, else $HOME."""
    env = os.environ.get("PVIEW_ROOTS")
    if env:
        paths = env.split(os.pathsep)
    else:
        paths = []
        try:
            with open(ROOTS_FILE) as f:
                paths = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
        paths = paths or [HOME]
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        rp = os.path.realpath(os.path.expanduser(os.path.expandvars(p)))
        if rp not in seen and os.path.isdir(rp):
            seen.add(rp)
            out.append(rp)
    return out


ROOTS = _configured_roots()
ALLOWED_HOSTS = {"localhost:%d" % PORT, "127.0.0.1:%d" % PORT, "[::1]:%d" % PORT}
URL = "http://localhost:%d/" % PORT

mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/svg+xml", ".svg")

TOKEN_RE = re.compile(r"[A-Za-z0-9_./~+-]+\.(?:png|jpe?g|gif|webp|svg|bmp|avif)\b", re.I)

CHEATSHEET = """
open it
  on this machine      %(url)s
  over ssh             add to ~/.ssh/config under the host you connect to:
                           LocalForward %(port)d 127.0.0.1:%(port)d
                       with connection sharing it applies on the next fresh connection;
                       for a one-off, on the machine you sit at:
                           ssh -N -L %(port)d:127.0.0.1:%(port)d <host>
  from tmux            bind a key to `pview push`, then that key sends the pane's folder
                       and pins the image paths on its screen (see README)

in the page
  j k g G              move in the list        /   filter        ?  all keys
  l Enter h Backspace  in and out of folders   r   subfolders    s  sort time/name
  wheel or pinch       zoom at the pointer     drag  move the image
  double click         zoom in there, again to fit
  0 1 w + -            fit, actual size, fit width, zoom in, zoom out
  H J K L              move the image          o   open in a tab  y  copy its path

this install
  program              %(here)s
  folders served       %(roots)s
  settings             %(roots_file)s (one folder per line), $PVIEW_ROOTS, $PVIEW_PORT
  server log           %(log)s
""" % {"url": "http://localhost:%d/" % PORT, "port": PORT, "here": HERE,
       "roots": ", ".join(ROOTS) or "(none)", "roots_file": ROOTS_FILE,
       "log": os.path.join(CACHE_DIR, "server.log")}


# --------------------------------------------------------------------------- paths

def under_roots(path):
    """Realpath of `path` if it sits inside an allowed root, else None."""
    if not path:
        return None
    try:
        rp = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return None
    for r in ROOTS:
        if rp == r or rp.startswith(r + os.sep):
            return rp
    return None


def parent_of(dirpath):
    p = os.path.dirname(dirpath.rstrip(os.sep))
    if not p or p == dirpath:
        return None
    return p if under_roots(p) else None


def short(path):
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        return "~/" + path[len(HOME) + 1:]
    return path


# --------------------------------------------------------------------------- scanning

_cache = {}
_cache_lock = threading.Lock()


def _scan(dirpath, recursive):
    t0 = time.monotonic()
    images, dirs, truncated = [], [], False
    if not recursive:
        try:
            it = os.scandir(dirpath)
        except OSError as e:
            return {"error": str(e)}, 0.0
        with it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        if e.name not in SKIP_DIRS:
                            dirs.append({"name": e.name, "p": os.path.join(dirpath, e.name)})
                    elif e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                        st = e.stat()
                        images.append({"p": os.path.join(dirpath, e.name), "rel": e.name,
                                       "m": st.st_mtime, "s": st.st_size})
                except OSError:
                    continue
    else:
        if not os.path.isdir(dirpath):
            return {"error": "not a folder"}, 0.0
        # breadth-first: if the budget runs out on a huge tree, what we keep is the
        # part closest to the folder the user asked for
        stack = collections.deque([(dirpath, 0)])
        while stack:
            if time.monotonic() - t0 > WALK_BUDGET or len(images) >= MAX_IMAGES:
                truncated = True
                break
            cur, depth = stack.popleft()
            try:
                it = os.scandir(cur)
            except OSError:
                continue
            with it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in SKIP_DIRS and depth + 1 <= MAX_DEPTH:
                                stack.append((os.path.join(cur, e.name), depth + 1))
                        elif e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                            st = e.stat()
                            full = os.path.join(cur, e.name)
                            images.append({"p": full, "rel": os.path.relpath(full, dirpath),
                                           "m": st.st_mtime, "s": st.st_size})
                    except OSError:
                        continue
    images.sort(key=lambda i: (-i["m"], i["rel"]))
    dirs.sort(key=lambda d: d["name"].lower())
    h = hashlib.sha1()
    for i in images:
        h.update(("%s|%r|%d;" % (i["p"], i["m"], i["s"])).encode())
    h.update(b"#")
    for d in dirs:
        h.update((d["name"] + ";").encode())
    return {"dir": dirpath, "parent": parent_of(dirpath), "images": images, "dirs": dirs,
            "recursive": recursive, "truncated": truncated, "sig": h.hexdigest()[:16]}, time.monotonic() - t0


def listing(dirpath, recursive):
    """Cached directory scan. Recursive scans are cached longer the slower they are."""
    key = (dirpath, bool(recursive))
    now = time.monotonic()
    with _cache_lock:
        ent = _cache.get(key)
        if ent and ent[0] > now:
            return ent[1]
    data, dt = _scan(dirpath, recursive)
    ttl = max(1.0, min(20.0, dt * 8)) if recursive else 1.0
    with _cache_lock:
        _cache[key] = (now + ttl, data)
    return data


def stat_entry(path, base):
    try:
        st = os.stat(path)
    except OSError:
        return None
    rel = os.path.relpath(path, base) if base and path.startswith(base + os.sep) else os.path.basename(path)
    return {"p": path, "rel": rel, "m": st.st_mtime, "s": st.st_size}


# --------------------------------------------------------------------------- screen scraping

def resolve_from_text(text, cwd):
    """Pull image paths out of pane text and turn them into real files under cwd.

    Tokens are ordered newest-block-first: lines that mention images within 3 lines
    of each other form a block (e.g. the plot list at the end of a message), blocks
    are ordered bottom-of-screen first, and the order inside a block is kept.
    """
    last = {}
    for ln, line in enumerate(text.splitlines()):
        for m in TOKEN_RE.finditer(line):
            last[m.group(0)] = ln
    blocks = []
    for tok, ln in sorted(last.items(), key=lambda kv: kv[1]):
        if blocks and ln - blocks[-1][-1][1] <= 3:
            blocks[-1].append((tok, ln))
        else:
            blocks.append([(tok, ln)])
    tokens = [tok for blk in reversed(blocks) for tok, _ in blk]

    index = None
    out, seen = [], set()
    stats = {"tokens": len(tokens), "direct": 0, "search": 0, "ambiguous": 0, "unresolved": 0}
    for tok in tokens:
        cand = tok if tok.startswith(("/", "~")) else os.path.join(cwd, tok)
        rp = under_roots(cand)
        if rp and os.path.isfile(rp):
            stats["direct"] += 1
        else:
            if index is None:
                index = listing(cwd, True).get("images", [])
            suffix = os.sep + tok.lstrip("./")
            hits = [i for i in index if i["p"].endswith(suffix)]
            if not hits:
                stats["unresolved"] += 1
                continue
            if len(hits) > 1:
                stats["ambiguous"] += 1
            else:
                stats["search"] += 1
            rp = max(hits, key=lambda i: i["m"])["p"]
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out, stats


# --------------------------------------------------------------------------- state

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.view = None
        self.last_poll = 0.0

    def set_view(self, dirpath, recursive, pinned, select, source, stats=None):
        with self.lock:
            self.seq += 1
            self.view = {"dir": dirpath, "recursive": bool(recursive),
                         "pinned": [e for e in (stat_entry(p, dirpath) for p in pinned) if e],
                         "select": select, "source": source, "stats": stats, "ts": time.time()}
            return self.seq

    def snapshot(self):
        with self.lock:
            return self.seq, self.view

    def connected(self):
        with self.lock:
            return (time.monotonic() - self.last_poll) < 6.0

    def touch(self):
        with self.lock:
            self.last_poll = time.monotonic()


STATE = State()


# --------------------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pview"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if os.environ.get("PVIEW_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    # -- helpers
    def _send(self, code, ctype, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode(),
                   [("Cache-Control", "no-store")])

    def _asset(self, name, ctype, csp=None):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError:
            return self._send(500, "text/plain; charset=utf-8", b"missing asset\n")
        extra = [("Cache-Control", "no-cache")]
        if csp:
            extra.append(("Content-Security-Policy", csp))
        self._send(200, ctype, body, extra)

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _dir_arg(self, q):
        d = under_roots((q.get("d") or [""])[0])
        return d if d and os.path.isdir(d) else None

    # -- routes
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "text/plain; charset=utf-8", b"forbidden host\n")
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._asset("index.html", "text/html; charset=utf-8",
                               "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        if p == "/app.js":
            return self._asset("app.js", "application/javascript; charset=utf-8")
        if p == "/api/ping":
            return self._json({"ok": True, "pid": os.getpid(), "port": PORT})
        if p == "/api/view":
            seq, view = STATE.snapshot()
            return self._json({"seq": seq, "view": view, "connected": STATE.connected(),
                               "default_dir": ROOTS[0] if ROOTS else HOME})
        if p == "/api/poll":
            STATE.touch()
            seq, _ = STATE.snapshot()
            out = {"seq": seq}
            d = self._dir_arg(q)
            if d:
                data = listing(d, (q.get("r") or ["0"])[0] == "1")
                out["sig"] = data.get("sig")
                out["error"] = data.get("error")
            return self._json(out)
        if p == "/api/list":
            d = self._dir_arg(q)
            if not d:
                return self._json({"error": "folder not found or not allowed"}, 404)
            data = listing(d, (q.get("r") or ["0"])[0] == "1")
            return self._json(data)
        if p == "/img":
            return self._image(q)
        return self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "text/plain; charset=utf-8", b"forbidden host\n")
        if self.headers.get("X-Pview") != "1":
            return self._send(403, "text/plain; charset=utf-8", b"missing X-Pview header\n")
        u = urllib.parse.urlsplit(self.path)
        if u.path not in ("/api/push", "/api/clientlog"):
            return self._send(404, "text/plain; charset=utf-8", b"not found\n")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json({"error": "bad payload: %s" % e}, 400)

        if u.path == "/api/clientlog":
            # the page reports its own failures here, so a browser on another machine
            # can say what went wrong without anyone opening dev tools
            if payload.get("level") == "error" or os.environ.get("PVIEW_DEBUG"):
                sys.stderr.write("browser: %s\n" % str(payload.get("msg"))[:2000])
                sys.stderr.flush()
            return self._json({"ok": True})

        cwd = under_roots(payload.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            return self._json({"error": "folder not allowed: %s" % payload.get("cwd")}, 400)
        pinned, stats = [], None
        for f in payload.get("files") or []:
            rp = under_roots(f)
            if rp and os.path.isfile(rp):
                pinned.append(rp)
        text = payload.get("text")
        if text:
            found, stats = resolve_from_text(text, cwd)
            pinned.extend(p for p in found if p not in pinned)
        select = payload.get("select") or (pinned[0] if pinned else None)
        seq = STATE.set_view(cwd, payload.get("recursive", True), pinned, select,
                             payload.get("source"), stats)
        return self._json({"ok": True, "seq": seq, "pinned": len(pinned), "stats": stats,
                           "connected": STATE.connected(), "url": URL})

    def _image(self, q):
        rp = under_roots((q.get("p") or [""])[0])
        if not rp or os.path.splitext(rp)[1].lower() not in IMAGE_EXTS or not os.path.isfile(rp):
            return self._send(403, "text/plain; charset=utf-8", b"forbidden\n")
        st = os.stat(rp)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, "text/plain", b"", [("ETag", etag)])
        ctype = mimetypes.guess_type(rp)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if q.get("v") else "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(rp, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 256 * 1024)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def pid_file():
    return os.path.join(CACHE_DIR, "server.%d.pid" % PORT)


def cmd_serve(args):
    os.makedirs(CACHE_DIR, exist_ok=True)
    srv = Server((HOST, PORT), Handler)
    with open(pid_file(), "w") as f:
        f.write(str(os.getpid()))

    def bye(*_):
        try:
            os.unlink(pid_file())
        except OSError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    sys.stderr.write("pview serving %s (roots: %s)\n" % (URL, ", ".join(ROOTS)))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    finally:
        bye()
    return 0


# --------------------------------------------------------------------------- client side

def server_alive(timeout=0.4):
    try:
        with urllib.request.urlopen("http://%s:%d/api/ping" % (HOST, PORT), timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def ensure_server():
    if server_alive():
        return True
    os.makedirs(CACHE_DIR, exist_ok=True)
    log = open(os.path.join(CACHE_DIR, "server.log"), "ab")
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True, cwd="/")
    for _ in range(50):
        time.sleep(0.1)
        if server_alive():
            return True
    return False


def post(path, payload, timeout=10):
    req = urllib.request.Request("http://%s:%d%s" % (HOST, PORT, path),
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Pview": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tmux_msg(text):
    try:
        subprocess.run(["tmux", "display-message", text], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def cmd_push(args):
    cwd = os.path.realpath(args.cwd or os.getcwd())
    text = ""
    try:
        text = subprocess.run(["tmux", "capture-pane", "-p", "-J", "-S", "-%d" % args.lines,
                               "-t", args.pane], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        pass
    if not ensure_server():
        tmux_msg("pview: server would not start, see %s/server.log" % CACHE_DIR)
        return 1
    try:
        res = post("/api/push", {"cwd": cwd, "text": text, "recursive": True,
                                 "source": "pane %s" % args.pane})
    except Exception as e:
        tmux_msg("pview: %s" % e)
        return 1
    if res.get("error"):
        tmux_msg("pview: %s" % res["error"])
        return 1
    n = res.get("pinned", 0)
    msg = "pview: %s from screen · %s" % ("%d plot%s" % (n, "" if n == 1 else "s") if n else "no plots on screen",
                                          short(cwd))
    if not res.get("connected"):
        msg += " · open %s" % URL
    tmux_msg(msg)
    return 0


def cmd_show(args):
    target = os.path.realpath(os.path.expanduser(args.path or os.getcwd()))
    if os.path.isfile(target):
        payload = {"cwd": os.path.dirname(target), "files": [target], "recursive": args.recursive}
    else:
        payload = {"cwd": target, "files": [], "recursive": args.recursive}
    if not ensure_server():
        print("pview: server would not start, see %s/server.log" % CACHE_DIR, file=sys.stderr)
        return 1
    try:
        res = post("/api/push", payload)
    except Exception as e:
        print("pview: %s" % e, file=sys.stderr)
        return 1
    if res.get("error"):
        print("pview: %s" % res["error"], file=sys.stderr)
        return 1
    print(URL + ("" if res.get("connected") else "   (no viewer tab connected yet)"))
    return 0


def cmd_status(args):
    ping = server_alive(1.0)
    if not ping:
        print("server: not running   (starts on demand)   url %s" % URL)
        return 1
    try:
        with urllib.request.urlopen("http://%s:%d/api/view" % (HOST, PORT), timeout=2) as r:
            view = json.load(r)
    except Exception:
        view = {}
    v = view.get("view") or {}
    print("server: running (pid %s)   url %s" % (ping.get("pid"), URL))
    print("folder: %s%s" % (short(v.get("dir") or "-"), "  [recursive]" if v.get("recursive") else ""))
    print("pinned: %d   source: %s" % (len(v.get("pinned") or []), v.get("source") or "-"))
    print("tab:    %s" % ("connected" if view.get("connected") else "none polling right now"))
    print("help:   pview -h  (setup and keys)   ·   ? inside the page")
    return 0


def cmd_stop(args):
    try:
        with open(pid_file()) as f:
            pid = int(f.read().strip())
    except Exception:
        print("pview: no pid file; server probably not running")
        return 1
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            if b"pview" not in f.read():
                print("pview: pid %d is not the viewer" % pid)
                return 1
        os.kill(pid, signal.SIGTERM)
        print("pview: stopped (pid %d)" % pid)
        return 0
    except Exception as e:
        print("pview: %s" % e)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pview", description=__doc__, epilog=CHEATSHEET,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the server in the foreground")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("push", help="show a tmux pane's folder and the image paths on its screen")
    p.add_argument("pane")
    p.add_argument("cwd", nargs="?")
    p.add_argument("--lines", type=int, default=2000)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("status", help="server and viewer status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="stop the server")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("show", help="show a folder or image (default subcommand)")
    p.add_argument("path", nargs="?")
    p.add_argument("-r", "--recursive", action="store_true", help="include images in subfolders")
    p.set_defaults(func=cmd_show)

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("help", "keys", "--cheatsheet"):
        ap.print_help()
        return 0
    if not argv or (argv[0] not in sub.choices and not argv[0].startswith("-")):
        argv = ["show"] + argv
    elif argv and argv[0] in ("-r", "--recursive"):
        argv = ["show"] + argv
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
