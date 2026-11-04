#!/usr/bin/env python3
"""free-llm-gateway — satu endpoint OpenAI-compatible untuk dua model gratis.

    DeepSeek web free  (chatbrain relay, relay2.py, :20232)  ->  "deepseek-free"
    GLM web free       (zai-api agent mode,     :20234)      ->  "glm-free"

Kenapa ada file ini: relay2 hanya melayani satu model (deepseek-chat) dan
zai-api memakai nama model Z.AI (glm-5.3 dsb). Agen lain (Hermes, Cline,
OpenHands, SDK OpenAI, ...) butuh satu base_url + satu daftar model + satu
token. Gateway ini:

  * menggabungkan /v1/models dari kedua upstream (dinamis, di-cache),
  * meneruskan /v1/chat/completions ke upstream yang sesuai nama model,
  * streaming SSE byte-for-byte, non-stream dibaca lalu ditulis ulang,
  * route opsional lintas-model: "glm-free/deepseek-free" -> coba berurutan
    (failover), dipakai kalau salah satu kuota web sedang diblokir,
  * token klien (gateway token) -> token upstream yang berbeda per upstream,
  * endpoint Anthropic /v1/messages juga diteruskan (untuk klien Claude-like),
  * tanpa dependensi selain stdlib.

Jalankan:  python3 gateway.py            (konfigurasi via env, lihat README)
"""
import json
import os
import sys
import time
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "8080"))
# Token yang dipakai klien untuk memanggil gateway ini. Kosong = terbuka.
GATEWAY_TOKENS = [t.strip() for t in os.environ.get("GATEWAY_TOKENS", "").split(",") if t.strip()]
# Token per-upstream; dipakai bila klien tidak mengirim kredensial upstream.
UPSTREAM_AUTH = os.environ.get("UPSTREAM_AUTH", "auto").lower()  # auto|passthrough|off

DEFAULT_UPSTREAMS = [
    {
        "name": "deepseek-free",
        "base": "http://127.0.0.1:20232",
        "models": ["deepseek-chat"],
        "auth": "",
    },
    {
        "name": "glm-free",
        "base": "http://127.0.0.1:20234",
        "models": ["glm-5.3", "glm-5.2", "glm-5.3-flash", "glm-4.7"],
        # Token klien untuk bridge GLM (zai-api AUTH_TOKEN). Isi dari env
        # GLM_AUTH_TOKEN; kosong = bridge membalas 401, jadi wajib diisi.
        "auth": os.environ.get("GLM_AUTH_TOKEN", ""),
    },
]

MODELS_CACHE_TTL = float(os.environ.get("MODELS_CACHE_TTL", "300"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "600"))
LOG_FILE = os.environ.get("GATEWAY_LOG", "")

_state_lock = threading.Lock()
_models_cache = {"at": 0.0, "data": None}


def load_upstreams():
    raw = os.environ.get("UPSTREAMS_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:  # konfigurasi rusak -> fallback, jangan mati diam
            log("UPSTREAMS_JSON invalid (%r), pakai default" % (e,))
    return DEFAULT_UPSTREAMS


UPSTREAMS = load_upstreams()
BY_NAME = {u["name"]: u for u in UPSTREAMS}


def log(*a):
    line = "%s %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    print(line, file=sys.stderr, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


# --------------------------------------------------------------------------
# HTTP ke upstream
# --------------------------------------------------------------------------
def http_call(upstream, method, path, body=None, headers=None, stream=False, timeout=None):
    url = upstream["base"].rstrip("/") + path
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None and not req.has_header("Content-Type"):
        req.add_header("Content-Type", "application/json")
    auth = client_auth_for(upstream)
    if auth:
        req.add_header("Authorization", "Bearer " + auth)
        req.add_header("x-api-key", auth)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout or UPSTREAM_TIMEOUT)
        return resp.status, resp.headers, resp
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e


_passthrough_auth = threading.local()


def set_passthrough_auth(token):
    _passthrough_auth.token = token


def client_auth_for(upstream):
    if UPSTREAM_AUTH == "off":
        return ""
    if UPSTREAM_AUTH == "passthrough":
        return getattr(_passthrough_auth, "token", "")
    return upstream.get("auth") or ""


# --------------------------------------------------------------------------
# Resolusi model -> upstream / rantai failover
# --------------------------------------------------------------------------
def normalize_model(name):
    """glm-free/glm-5.3 -> (['glm-free'], 'glm-5.3'); glm-free/deepseek-free -> rantai."""
    if not name:
        return [], ""
    parts = [p for p in str(name).split("/") if p]
    prefixes, rest = [], []
    for p in parts:
        if p in BY_NAME and not rest:
            prefixes.append(p)
        else:
            rest.append(p)
    return prefixes, "/".join(rest)


def resolve_chain(name):
    """Kembalikan list [(upstream, model)] untuk dicoba berurutan."""
    prefixes, rest = normalize_model(name)
    if not prefixes:
        # tanpa prefix: cari upstream yang mengiklankan model ini
        base = rest or name
        hits = [(u, base) for u in UPSTREAMS if base in u.get("models", [])]
        if not hits:
            hits = [(UPSTREAMS[0], base)]
        return hits
    if len(prefixes) > 1:
        # rantai failover: tiap upstream memakai model default-nya
        return [(BY_NAME[p], None) for p in prefixes if p in BY_NAME]
    up = BY_NAME[prefixes[0]]
    return [(up, rest or (up.get("models") or [None])[0])]


def model_for(upstream, requested):
    if requested:
        return requested
    return (upstream.get("models") or ["default"])[0]


# --------------------------------------------------------------------------
# Daftar model gabungan
# --------------------------------------------------------------------------
def fetch_models():
    out = []
    for u in UPSTREAMS:
        ids = list(u.get("models") or [])
        try:
            st, _h, resp = http_call(u, "GET", "/v1/models", timeout=10)
            if st == 200:
                body = json.loads(resp.read().decode() or "{}")
                live = [m.get("id") for m in body.get("data", []) if m.get("id")]
                if live:
                    ids = live
        except Exception as e:
            log("models %s gagal live (%r), pakai daftar statis" % (u["name"], e))
        for mid in ids:
            out.append({
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": u["name"],
                "namespaced_id": "%s/%s" % (u["name"], mid),
            })
        if ids:  # alias failover "a/b"
            pass
    chains = [u["name"] for u in UPSTREAMS]
    if len(chains) > 1:
        out.append({
            "id": "/".join(chains),
            "object": "model",
            "created": 0,
            "owned_by": "gateway",
            "namespaced_id": "/".join(chains),
        })
    return out


def models_payload():
    with _state_lock:
        fresh = (time.time() - _models_cache["at"]) < MODELS_CACHE_TTL
        if fresh and _models_cache["data"] is not None:
            return _models_cache["data"]
        data = fetch_models()
        _models_cache.update({"at": time.time(), "data": data})
        return data


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "free-llm-gateway/1.0"

    def log_message(self, fmt, *args):  # jangan banjiri stderr dengan log default
        pass

    # -- util --------------------------------------------------------------
    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _err(self, code, msg, etype="invalid_request_error"):
        self._json(code, {"error": {"message": msg, "type": etype, "code": code}})

    def _cors_preflight(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _check_token(self):
        if not GATEWAY_TOKENS:
            return True
        got = (self.headers.get("Authorization") or "").replace("Bearer", "").strip()
        if not got:
            got = (self.headers.get("x-api-key") or "").strip()
        return got in GATEWAY_TOKENS

    def _read_body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(ln) if ln else b""

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/") or "/"
        if p in ("/", "/health"):
            self._json(200, {
                "status": "ok",
                "gateway": "free-llm-gateway",
                "upstreams": [u["name"] for u in UPSTREAMS],
                "auth_required": bool(GATEWAY_TOKENS),
            })
            return
        if p == "/v1/models":
            if not self._check_token():
                self._err(401, "invalid gateway token", "authentication_error")
                return
            data = models_payload()
            self._json(200, {"object": "list", "data": data})
            return
        if p == "/v1/upstreams":
            if not self._check_token():
                self._err(401, "invalid gateway token", "authentication_error")
                return
            out = []
            for u in UPSTREAMS:
                entry = {"name": u["name"], "base": u["base"], "models": u.get("models", [])}
                try:
                    st, _h, r = http_call(u, "GET", "/v1/models", timeout=8)
                    entry["reachable"] = st == 200
                    entry["status"] = st
                except Exception as e:
                    entry["reachable"] = False
                    entry["status"] = repr(e)[:120]
                out.append(entry)
            self._json(200, {"upstreams": out})
            return
        self._err(404, "not found")

    def do_OPTIONS(self):
        self._cors_preflight()

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        try:
            if p == "/v1/chat/completions":
                self._handle_chat()
            elif p == "/v1/messages":
                self._handle_messages()
            elif p == "/v1/models/refresh":
                with _state_lock:
                    _models_cache.update({"at": 0.0, "data": None})
                self._json(200, {"ok": True, "models": len(models_payload())})
            else:
                self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            log("handler error:", repr(e)[:300])
            try:
                self._err(500, "gateway error: %r" % (e,), "server_error")
            except Exception:
                pass

    def _handle_messages(self):
        """Anthropic /v1/messages: teruskan ke upstream yang mendukungnya."""
        raw = self._read_body()
        if not self._check_token():
            self._err(401, "invalid gateway token", "authentication_error")
            return
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._err(400, "body bukan JSON valid")
            return
        chain = resolve_chain(body.get("model", ""))
        if not chain:
            self._err(400, "model tidak dikenali")
            return
        last = None
        for up, model in chain:
            payload = dict(body)
            payload["model"] = model_for(up, model)
            st, hdr, resp = http_call(up, "POST", "/v1/messages", payload, self._fwd_headers(), stream=bool(body.get("stream")))
            if st < 400:
                self._relay_response(st, hdr, resp, stream=bool(body.get("stream")), anth=True)
                return
            last = (st, resp.read()[:400])
            log("messages %s -> %s" % (up["name"], st))
        self._err(last[0] if last else 502, "semua upstream menolak: %r" % (last,), "upstream_error")

    def _fwd_headers(self):
        keep = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("authorization", "x-api-key", "host", "content-length", "accept-encoding"):
                continue
            keep[k] = v
        keep["Accept-Encoding"] = "identity"
        return keep

    def _handle_chat(self):
        raw = self._read_body()
        if not self._check_token():
            self._err(401, "invalid gateway token", "authentication_error")
            return
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._err(400, "body bukan JSON valid")
            return
        stream = bool(body.get("stream"))
        chain = resolve_chain(body.get("model", ""))
        if not chain:
            self._err(400, "model tidak dikenali; cek /v1/models")
            return
        errors = []
        for idx, (up, model) in enumerate(chain):
            payload = dict(body)
            payload["model"] = model_for(up, model)
            payload["stream"] = stream
            t0 = time.time()
            st, hdr, resp = http_call(up, "POST", "/v1/chat/completions", payload,
                                      self._fwd_headers(), stream=stream)
            if st < 400:
                log("chat %s model=%s stream=%s %d" % (up["name"], payload["model"], stream, st))
                self._relay_response(st, hdr, resp, stream=stream)
                return
            detail = b""
            try:
                detail = resp.read()[:600]
            except Exception:
                pass
            errors.append({"upstream": up["name"], "status": st, "detail": detail.decode("utf-8", "replace")})
            log("chat %s gagal %d: %s" % (up["name"], st, detail[:200]))
            if idx + 1 < len(chain):
                continue
        self._json(502, {"error": {"message": "semua upstream gagal", "type": "upstream_error",
                                   "code": 502, "attempts": errors}})

    def _relay_response(self, st, hdr, resp, stream, anth=False):
        if not stream:
            data = resp.read()
            self.send_response(st)
            ct = hdr.get("Content-Type") if hdr else None
            self.send_header("Content-Type", ct or ("application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(st)
        ct = hdr.get("Content-Type") if hdr else None
        self.send_header("Content-Type", ct or "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        while True:
            chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log("free-llm-gateway up on %s:%d | upstreams=%s | auth=%s" % (
        HOST, PORT, ",".join(u["name"] for u in UPSTREAMS), "on" if GATEWAY_TOKENS else "off"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()