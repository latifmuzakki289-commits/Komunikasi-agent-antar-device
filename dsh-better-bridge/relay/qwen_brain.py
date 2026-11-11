#!/usr/bin/env python3
"""qwen_brain.py — Qwen Chat (chat.qwen.ai) brain for relay2.

WHY TWO TRANSPORTS
------------------
The Bearer token alone is not enough. Verified against the live service:

    GET  /api/v1/auths/            -> 200 with a bare token (and mints a ~30d token)
    GET  /api/models               -> 200 with a bare token
    POST /api/v2/chat/completions  -> Aliyun WAF challenge (x5sec / acw_sc__v2)

The chat route also fingerprints the TLS handshake, so replaying cookies from
`requests`/`urllib` is not enough. Hence, in order:

  1. direct — curl_cffi with a Firefox TLS profile + the WAF cookies the
              userscript pushes from the live tab. Fast, no tab needed, but it
              dies whenever Alibaba rotates the challenge or the cookies go
              stale.
  2. bridge — the relay parks the job in a queue; the userscript running inside
              chat.qwen.ai pulls it, performs the fetch in-page (same origin,
              real browser, real cookies) and posts the answer back. Slower and
              needs an open tab, but immune to fingerprinting because the
              request genuinely comes from the browser.

After a WAF block the direct path is parked for QWEN_WAF_COOLDOWN seconds. A
stale cookie must not turn into a burst of challenged requests — that is what
actually gets a consumer account flagged.

REQUIRED HEADERS (the whole reason this file exists)
----------------------------------------------------
POST /api/v2/chat/completions is rejected with the unhelpful envelope

    {"success":false,"data":{"code":"Bad_Request","details":"Internal error..."}}

when any of the headers the web client sets in its request interceptor is
missing. In particular `Version: <frontend version>` (0.3.40 at the time of
writing) is mandatory; `Timezone` and `source: web` ride along. Nothing about
the body matters until those headers are right — a byte-identical body that
fails without them succeeds with them.

TOKENS (two kinds, do not mix them up)
--------------------------------------
  token       short (~13 min), pushed by the userscript from the live tab.
              This is what the API actually accepts.
  long_token  ~30 days, minted by GET /api/v1/auths/ (needs only the cookies).
              Usable only until the browser rotates its session; after that it
              answers 401. Kept purely as a fallback for when no tab is open.

refresh_token() therefore stores into long_token and never overwrites token.

ENV
---
  QWEN_SESSION_FILE  default ./.websession-qwen.json
  QWEN_IMPERSONATE   default "firefox"   (curl_cffi TLS profile)
  QWEN_MODEL         default "qwen3.7-plus"
  QWEN_TIMEOUT       default 600         (seconds, direct call)
  QWEN_BRIDGE_TIMEOUT default 180        (seconds waiting for the tab)
  QWEN_WAF_COOLDOWN  default 120
"""

import base64
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

BASE = "https://chat.qwen.ai"
HERE = os.path.dirname(os.path.abspath(__file__))

SESSION_FILE = os.environ.get("QWEN_SESSION_FILE", os.path.join(HERE, ".websession-qwen.json"))
IMPERSONATE = os.environ.get("QWEN_IMPERSONATE", "firefox")
MODEL_DEFAULT = os.environ.get("QWEN_MODEL", "qwen3.7-plus")
# The facade advertises "qwen-chat"; upstream needs a real model id. Anything
# not in this table is passed through untouched, so a client that already knows
# a concrete id ("qwen3.7-plus", "qwen3.7-max") still works.
MODEL_ALIASES = {"qwen-chat": MODEL_DEFAULT, "qwen": MODEL_DEFAULT}

# Header "Version" WAJIB. Tanpa itu Qwen membalas
# "Bad_Request - Internal error..." untuk body yang benar sekalipun.
# Nilai ini ikut versi frontend Qwen (0.3.40 saat ini).
APP_VERSION = os.environ.get("QWEN_APP_VERSION", "0.3.40")


def _tz_header():
    """Same shape as JS Date.toString(): 'Thu Nov 12 2026 00:12:34 GMT+0700'."""
    now = datetime.now().astimezone()
    return now.strftime("%a %b %d %Y %H:%M:%S GMT%z")


def resolve_model(m):
    if not m:
        return MODEL_DEFAULT
    return MODEL_ALIASES.get(m.strip().lower(), m.strip())
TIMEOUT = float(os.environ.get("QWEN_TIMEOUT", "600"))
BRIDGE_TIMEOUT = float(os.environ.get("QWEN_BRIDGE_TIMEOUT", "180"))
WAF_COOLDOWN = float(os.environ.get("QWEN_WAF_COOLDOWN", "120"))
BRIDGE_GRACE = float(os.environ.get("QWEN_BRIDGE_GRACE", "20"))

UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
               "Gecko/20100101 Firefox/158.0")

# Cookies that Aliyun's challenge layer sets from JS. If none of these is being
# pushed, the direct transport will never work — the bridge is the only option.
WAF_COOKIE_RE = ("acw", "ssxmod", "tfstk", "x5sec", "cdn_sec")


class WafBlocked(RuntimeError):
    """Upstream refused the request at the anti-bot layer, not at auth."""


class QwenAuthFailed(RuntimeError):
    """Token refused. Not retryable — wait for a fresh push."""


class BridgeAbsent(RuntimeError):
    """No browser tab is polling the bridge."""


_log_fn = lambda *a: None


def set_logger(fn):
    """relay2 passes its own log() so both write to the same file."""
    global _log_fn
    _log_fn = fn


def _log(*a):
    try:
        _log_fn(*a)
    except Exception:
        pass


# ---------------------------------------------------------------- session ----
# token      : token pendek (~13 menit) yang dipush userscript. Inilah token
#              yang benar-benar diterima /api/v2/chat/completions.
# long_token : token ~30 hari dari /api/v1/auths/. HANYA cadangan - server
#              menolaknya dengan 401 begitu browser merotasi sesinya, jadi
#              token ini tidak boleh menimpa token pendek.
S = {"mtime": None, "token": None, "long_token": "", "cookie_list": [],
     "cookies": {}, "ua": UA_FALLBACK, "lang": "en-US"}


def _norm_token(raw):
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    if s.startswith("{"):
        try:
            j = json.loads(s)
        except ValueError:
            return ""
        if isinstance(j, dict):
            for k in ("value", "token", "access_token", "accessToken"):
                v = j.get(k)
                if isinstance(v, str) and v.count(".") == 2:
                    return v.strip()
        return ""
    return s if s.count(".") == 2 else ""


def _cookie_list(raw):
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict) and "name" in c]
    if isinstance(raw, dict):
        return [{"name": k, "value": v, "domain": ".qwen.ai"} for k, v in raw.items()]
    return []


def _merge(old, new):
    by = {c["name"]: c for c in old or []}
    for c in new or []:
        by[c["name"]] = c
    return list(by.values())


def _is_waf(name):
    n = name.lower()
    return any(n.startswith(p) for p in WAF_COOKIE_RE)


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0


def load_session(force=False):
    """Reload the qwen session file when it changes on disk."""
    try:
        mtime = os.stat(SESSION_FILE).st_mtime
    except OSError:
        return
    if not force and mtime == S["mtime"]:
        return
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            ws = json.load(f)
    except (ValueError, OSError) as e:
        _log("qwen session unreadable (kept previous):", e)
        return
    tok = _norm_token(ws.get("token"))
    S["mtime"] = mtime
    if not tok:
        _log("qwen session has no usable token (kept previous)")
        return
    clist = _cookie_list(ws.get("cookies"))
    if tok != S["token"]:
        _log("qwen session loaded: %s...%s cookies=%d waf=%d ua=%s"
             % (tok[:6], tok[-6:], len(clist), sum(1 for c in clist if _is_waf(c["name"])),
                "browser" if ws.get("ua") else "fallback"))
    S.update(token=tok, cookie_list=clist,
             cookies={c["name"]: c["value"] for c in clist},
             ua=ws.get("ua") or UA_FALLBACK,
             lang=ws.get("lang") or "en-US")
    long_tok = _norm_token(ws.get("long_token"))
    if long_tok:
        S["long_token"] = long_tok


def _write_session(extra=None):
    data = {"provider": "qwen", "token": S["token"],
            "long_token": S.get("long_token") or "",
            "cookies": S["cookie_list"],
            "ua": S["ua"], "lang": S["lang"], "ts": int(time.time() * 1000)}
    data.update(extra or {})
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SESSION_FILE)


def ingest(ws):
    """Accept a /session push from the userscript. Returns (status, payload)."""
    tok = _norm_token(ws.get("token"))
    if not tok:
        return 400, {"error": "missing token"}
    S["cookie_list"] = _merge(S["cookie_list"], _cookie_list(ws.get("cookies")))
    S["token"] = tok
    S["ua"] = ws.get("ua") or S["ua"]
    S["lang"] = ws.get("lang") or S["lang"]
    S["cookies"] = {c["name"]: c["value"] for c in S["cookie_list"]}
    _write_session()
    load_session(force=True)
    waf = sum(1 for c in S["cookie_list"] if _is_waf(c["name"]))
    exp = _jwt_exp(tok)
    return 200, {"ok": True, "provider": "qwen", "cookies": len(S["cookie_list"]),
                 "waf": waf,
                 "exp_in": (exp - int(time.time())) if exp else None}


# ------------------------------------------------------------------ token ----
def _http_get(path, timeout=20):
    req = urllib.request.Request(BASE + path, method="GET")
    req.add_header("Authorization", "Bearer " + (S["token"] or ""))
    req.add_header("User-Agent", S["ua"])
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Language", S["lang"])
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/")
    if S["cookies"]:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in S["cookies"].items()))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def refresh_token(min_left=2 * 86400):
    """Mint a ~30d token from /api/v1/auths/ and keep it as a fallback.

    /api/v1/auths/ is not behind the WAF, so plain urllib is enough. The token
    it returns is NOT what the app uses for completions: it keeps working only
    until the browser rotates its session, after which the server answers 401.
    So it is stored in long_token and never replaces the pushed short token.
    """
    long_tok = S.get("long_token") or ""
    if long_tok and _jwt_exp(long_tok) - time.time() > min_left:
        return False
    st, raw = _http_get("/api/v1/auths/")
    if st != 200:
        _log("qwen refresh failed: HTTP %s %s" % (st, raw[:120]))
        return False
    try:
        j = json.loads(raw)
    except ValueError:
        return False
    tok = _norm_token(j.get("token") or "")
    if not tok or tok == long_tok:
        return False
    S["long_token"] = tok
    _write_session()
    _log("qwen long token disimpan: exp in %.1f hari"
         % ((_jwt_exp(tok) - time.time()) / 86400))
    return True


def _token_candidates():
    """Token yang layak dicoba, urut dari yang paling mungkin diterima.

    Token push browser lebih dulu (itu yang dipakai app sendiri), token
    panjang hanya kalau token push sudah kedaluwarsa / tab ditutup.
    """
    now = time.time()
    out = []
    for tok in (S.get("token"), S.get("long_token")):
        if tok and tok not in out and (_jwt_exp(tok) - now) > 20:
            out.append(tok)
    if not out and S.get("cookies"):
        refresh_token()
        tok = S.get("long_token")
        if tok and (_jwt_exp(tok) - now) > 20:
            out.append(tok)
    return out


# ----------------------------------------------------------------- direct ----
_waf_until = 0.0


def _sse_text(lines):
    """Ambil teks jawaban dari stream SSE Qwen.

    Qwen mengirim delta per potongan, lalu satu entri terakhir yang membawa
    choices[0].message.content berisi teks lengkap. Kalau keduanya dijumlah,
    jawaban jadi dobel - jadi delta diutamakan dan teks lengkap hanya dipakai
    saat tidak ada delta sama sekali.
    """
    deltas = []
    whole = []
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        s = line.strip()
        if not s.startswith("data:"):
            continue
        p = s[5:].strip()
        if not p or p == "[DONE]":
            continue
        try:
            j = json.loads(p)
        except ValueError:
            continue
        if not isinstance(j, dict):
            continue
        ch = (j.get("choices") or [{}])[0]
        phase = str(ch.get("delta", {}).get("phase") or "")
        if "think" in phase:
            continue          # ringkasan proses berpikir, bukan jawaban
        d = (ch.get("delta") or {}).get("content")
        if isinstance(d, str) and d:
            deltas.append(d)
            continue
        c = (ch.get("message") or {}).get("content")
        if c is None:
            c = j.get("response")
        if isinstance(c, str) and c:
            whole.append(c)
    return "".join(deltas) if deltas else "".join(whole)


def _direct_new_chat(model, token=None):
    """POST /api/v2/chats/new — chat harus ada dulu sebelum menerima pesan.

    Tanpa ini, chat_id karangan membuat server Qwen menjawab
    'Bad_Request - Internal error...'. Best-effort: kalau gagal, pemanggil
    memakai uuid biasa dan error sebenarnya akan terlihat di completion.
    """
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return ""
    load_session()
    token = token or (S["token"] or "")
    if not token:
        return ""

    payload = {
        "chatId": "",
        "models": [model],
        "timestamp": int(time.time() * 1000),   # milidetik, seperti app
        "chat_type": "t2t",
        "chat_mode": "normal",
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": S["lang"],
        "source": "web",
        "Version": APP_VERSION,
        "Timezone": _tz_header(),
        "x-request-id": str(uuid.uuid4()),
        "Origin": BASE,
        "Referer": BASE + "/",
    }
    for imp in (IMPERSONATE, "firefox133", "chrome"):
        try:
            r = creq.post(BASE + "/api/v2/chats/new", impersonate=imp, cookies=S["cookies"],
                          headers=headers, json=payload, timeout=TIMEOUT)
        except Exception:
            continue
        if r.status_code in (401, 403):
            raise QwenAuthFailed("new chat HTTP %d: %s" % (r.status_code, r.text[:160]))
        if r.status_code != 200:
            _log("qwen new-chat HTTP %d: %s" % (r.status_code, r.text[:120]))
            return ""
        try:
            j = r.json() or {}
        except Exception:
            return ""
        cid = (j.get("data") or {}).get("id") or ""
        if cid:
            _log("qwen chat baru: %s" % cid[:8])
        return cid
    return ""


def _direct(body, token=None):
    """POST /api/v2/chat/completions with a browser TLS fingerprint."""
    global _waf_until
    if time.time() < _waf_until:
        raise WafBlocked("cooldown %.0fs (WAF sebelumnya)" % (_waf_until - time.time()))
    try:
        from curl_cffi import requests as creq
    except ImportError:
        raise WafBlocked("curl_cffi belum terpasang — pip install curl_cffi")

    load_session()
    token = token or (S["token"] or "")
    if not token:
        raise QwenAuthFailed("session qwen kosong — buka chat.qwen.ai dengan userscript aktif")

    url = BASE + "/api/v2/chat/completions?chat_id=" + (body.get("chat_id") or str(uuid.uuid4()))
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Language": S["lang"],
        "source": "web",
        "Version": APP_VERSION,
        "Timezone": _tz_header(),
        "x-request-id": str(uuid.uuid4()),
        "Origin": BASE,
        "Referer": BASE + "/",
    }

    r = None
    last_err = None
    for imp in (IMPERSONATE, "firefox133", "chrome"):
        try:
            r = creq.post(url, impersonate=imp, cookies=S["cookies"], headers=headers,
                          json=body, stream=True, timeout=TIMEOUT)
            break
        except Exception as e:
            msg = str(e).lower()
            if "impersonate" in msg or "profile" in msg or "browser" in msg:
                last_err = e
                continue
            raise WafBlocked("koneksi gagal: %s" % str(e)[:160])
    if r is None:
        raise WafBlocked("tidak ada profil TLS yang dikenali curl_cffi: %s" % last_err)

    try:
        lines = list(r.iter_lines())
    finally:
        try:
            r.close()
        except Exception:
            pass

    if r.status_code == 401 or r.status_code == 403:
        raise QwenAuthFailed("HTTP %d: %s" % (r.status_code, b"".join(lines)[:200]))
    if r.status_code != 200:
        _waf_until = time.time() + WAF_COOLDOWN
        raise WafBlocked("HTTP %d: %s" % (r.status_code, b"".join(lines)[:200]))

    blob = b"".join(lines)
    # Server kadang membalas HTTP 200 dengan envelope unauthorized, bukan 401.
    if b'"unauthorized"' in blob or b"401 Unauthorized" in blob:
        raise QwenAuthFailed("token ditolak: %s" % blob[:160])
    if b"x5secdata" in blob or b"_____tmd_____" in blob or b"<html" in blob.lower():
        _waf_until = time.time() + WAF_COOLDOWN
        raise WafBlocked("respons challenge HTML (WAF) — cookie basi")

    text = _sse_text(lines)
    if not text:
        raise WafBlocked("balasan kosong / format SSE tak dikenal: %s" % blob[:200])
    return text


# ----------------------------------------------------------------- bridge ----
class Bridge:
    """Job queue drained by the userscript running inside chat.qwen.ai."""

    def __init__(self):
        self._lk = threading.Lock()
        self._jobs = {}
        self._pending = []
        self.last_poll = 0.0

    def submit(self, body, timeout=None):
        timeout = BRIDGE_TIMEOUT if timeout is None else timeout
        with self._lk:
            if time.time() - self.last_poll > BRIDGE_GRACE:
                raise BridgeAbsent(
                    "tidak ada tab yang polling bridge (terakhir %s)"
                    % ("belum pernah" if not self.last_poll
                       else "%.0fs lalu" % (time.time() - self.last_poll)))
            jid = uuid.uuid4().hex
            q = queue.Queue(1)
            self._jobs[jid] = {"body": body, "q": q, "ts": time.time()}
            self._pending.append(jid)
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("tab tidak menjawab dalam %.0fs" % timeout)
        finally:
            with self._lk:
                self._jobs.pop(jid, None)
                if jid in self._pending:
                    self._pending.remove(jid)

    def pull(self):
        with self._lk:
            self.last_poll = time.time()
            while self._pending:
                jid = self._pending.pop(0)
                j = self._jobs.get(jid)
                if j:
                    return {"id": jid, "body": j["body"]}
        return None

    def push(self, d):
        jid = d.get("id") if isinstance(d, dict) else None
        with self._lk:
            j = self._jobs.get(jid)
        if not j:
            return False
        j["q"].put(d)
        return True

    def alive(self):
        return (time.time() - self.last_poll) < BRIDGE_GRACE


BRIDGE = Bridge()


# ------------------------------------------------------------------ brain ----
class QwenBrain:
    """Same ask(text) contract as relay2's ApiBrain."""

    def __init__(self):
        self.chat_id = None
        # relay2's build_prompt() probes brain.session_id, an ApiBrain detail.
        # Keep the same attribute so the shared prompt builder works unchanged.
        self.session_id = None

    def new_chat(self):
        # UUID bertanda hubung, sama seperti yang dibuat app Qwen sendiri.
        self.chat_id = str(uuid.uuid4())
        self.session_id = self.chat_id
        return self.chat_id

    def _bridge(self, text, model):
        res = BRIDGE.submit({"prompt": text, "model": model, "chat_id": self.chat_id})
        if not res.get("ok"):
            raise WafBlocked("bridge: %s" % str(res.get("error"))[:200])
        return res.get("text") or ""

    def ask(self, text, model=None):
        model = resolve_model(model)
        if not self.chat_id:
            self.new_chat()
        load_session()

        # Bentuk body disamakan dengan request asli app (tangkapan di panel).
        # Catatan: app TIDAK mengirim field "headers" di body, dan pesan
        # membawa files/extra/sub_chat_type/models serta feature_config lengkap.
        now = int(time.time())
        body = {
            "stream": True,
            "version": "2.1",
            "incremental_output": True,
            "chatId": self.chat_id,
            "parentId": "",
            "chat_id": self.chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": None,
            "timestamp": now,
            "messages": [{
                "id": None,
                "fid": str(uuid.uuid4()),
                "parentId": None,
                "parent_id": None,
                "childrenIds": [],
                "role": "user",
                "content": text,
                "user_action": "chat",
                "files": [],
                "timestamp": now,
                "models": [model],
                "model": "",
                "chat_type": "t2t",
                "feature_config": {
                    "thinking_enabled": False,
                    "output_schema": "phase",
                    "research_mode": "normal",
                    "auto_thinking": False,
                    "thinking_mode": "Fast",
                    "auto_search": True,
                },
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t",
            }],
        }

        # Order matters. While a tab is polling the bridge, prefer it: those
        # requests really come from the browser, so they cannot trip the WAF.
        # Every direct attempt that gets challenged raises the risk score on the
        # account, and a challenge is sticky — once triggered, the *next* bridge
        # request gets challenged too, even though it is legitimate. So never
        # poke the direct path while a working bridge exists.
        order = ["bridge", "direct"] if BRIDGE.alive() else ["direct", "bridge"]

        errors = []
        for how in order:
            try:
                if how == "bridge":
                    return self._bridge(text, model)

                # Token dicoba berurutan: token push browser dulu, token
                # panjang /auths/ hanya cadangan. Token yang ditolak server
                # (401) dilewati, bukan dipakai untuk semua percobaan.
                toks = _token_candidates()
                if not toks:
                    raise QwenAuthFailed(
                        "session qwen kosong / kedaluwarsa — buka chat.qwen.ai dengan userscript aktif")
                last = None
                for tok in toks:
                    try:
                        cid = _direct_new_chat(model, tok) or self.chat_id
                        body["chatId"] = cid
                        body["chat_id"] = cid
                        return _direct(body, tok)
                    except QwenAuthFailed as e:
                        last = e
                        _log("qwen token %s... ditolak, coba berikutnya" % tok[:8])
                raise last or QwenAuthFailed("semua token ditolak")
            except (WafBlocked, QwenAuthFailed, BridgeAbsent, TimeoutError) as e:
                errors.append("%s: %s" % (how, str(e)[:160]))
                _log("qwen %s gagal: %s" % (how, str(e)[:160]))
        raise QwenAuthFailed("qwen tidak bisa dipakai - " + " | ".join(errors))
