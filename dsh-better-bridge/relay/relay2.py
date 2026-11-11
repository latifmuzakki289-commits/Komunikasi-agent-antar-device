#!/usr/bin/env python3
"""chatbrain relay v2: OpenAI-compatible endpoint whose 'model' is DeepSeek's
consumer chat API called directly with the user's own web session (userToken
from a dedicated Firefox profile's localStorage, extracted by extract.py) +
deepseek-pow solver for the PoW challenge header. Renders the harness's
system+tools as a text tag contract and parses tagged/DSML replies back into
OpenAI tool_calls.

Session freshness: DeepSeek rotates userToken every few hours to days, so the
session file is re-read whenever its mtime changes (no restart needed) and can
be refreshed live via POST /session — see deepseek-token-sync.user.js. Auth
rejections (HTTP 200 + code 40003, or 401/403) raise SessionExpired and are
NEVER retried; the handler answers 401 session_expired and idles until the next
push, because repeated auth failures accelerate an account ban.

Sanitized for publication: filesystem paths and the port are configurable via
environment variables (see .env.example); no credentials are stored in this
file. The session token lives in a separate JSON file (default
./.websession.json next to this script) — keep it chmod 600 and never commit
it."""
import json, time, hashlib, threading, re, os, base64, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request, urllib.error
from deepseek_pow import Challenge, registry

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CHATBRAIN_PORT", "20232"))
BASE = "https://chat.deepseek.com"
SESSION_FILE = os.environ.get("CHATBRAIN_SESSION_FILE", os.path.join(HERE, ".websession.json"))
LOG_FILE = os.environ.get("CHATBRAIN_LOG_FILE", os.path.join(HERE, "relay2.log"))
REPLIES_LOG = os.environ.get("CHATBRAIN_REPLIES_LOG", os.path.join(HERE, "relay2-replies.log"))
LOG = open(LOG_FILE, "a", buffering=1)

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=LOG)

# Fallback fingerprint, used only when SESSION_FILE predates auto-sync and
# carries no "ua"/"lang" of its own. Windows, matching the machine this runs on
# (the old Linux UA was a fingerprint mismatch against the real browser).
UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) "
               "Gecko/20100101 Firefox/158.0")
AUTH_CODES = {40003}          # DeepSeek "Authorization Failed"

class SessionExpired(RuntimeError):
    """Token refused upstream. Not retryable — wait for a fresh browser push."""

# Live session state. Re-read from SESSION_FILE whenever its mtime changes, so a
# userToken rotated by the browser is picked up without restarting the relay.
S = {"mtime": None, "token": None, "cookie_list": [], "cookies": {},
     "ua": UA_FALLBACK, "lang": "en-US"}

def _cookie_list(raw):
    """Normalize the file's cookies field into a list of {name,value,...}."""
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict) and "name" in c]
    if isinstance(raw, dict):
        return [{"name": k, "value": v, "domain": ".deepseek.com"}
                for k, v in raw.items()]
    return []

def _merge_cookies(old, new):
    """Pushed cookies win, but cookies the push cannot see are kept.

    The userscript reads document.cookie, which never exposes HttpOnly cookies
    (e.g. ds_session_id) — so replacing the whole set would silently drop them.
    """
    by_name = {}
    for c in old or []:
        by_name[c["name"]] = c
    for c in new or []:
        by_name[c["name"]] = c
    return list(by_name.values())

def _norm_token(raw):
    """localStorage keeps userToken as {"value": "...", "__version": "0"}.
    Unwrap it; return "" when there is no usable token (logged-out tab)."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s.startswith("{"):
        try:
            j = json.loads(s)
        except ValueError:
            return ""
        v = j.get("value") if isinstance(j, dict) else None
        return v.strip() if isinstance(v, str) else ""
    return s

def load_session(force=False):
    """Reload SESSION_FILE if it changed on disk. One stat() per call."""
    try:
        mtime = os.stat(SESSION_FILE).st_mtime
    except OSError:
        if force:
            log("session file missing:", SESSION_FILE)
        return
    if not force and mtime == S["mtime"]:
        return
    try:
        with open(SESSION_FILE) as f:
            ws = json.load(f)
    except (ValueError, OSError) as e:
        log("session file unreadable (kept previous):", e)
        return
    tok = _norm_token(ws.get("token"))
    if not tok:
        S["mtime"] = mtime
        log("session file has no usable token (kept previous)")
        return
    clist = _cookie_list(ws.get("cookies"))
    if tok != S["token"]:
        log("session loaded: token %s...%s source=%s cookies=%d ua=%s"
            % (tok[:4], tok[-4:], "browser" if ws.get("ua") else "extractor",
               len(clist), "browser" if ws.get("ua") else "fallback"))
    S.update(mtime=mtime, token=tok, cookie_list=clist,
             cookies={c["name"]: c["value"] for c in clist},
             ua=ws.get("ua") or UA_FALLBACK,
             lang=ws.get("lang") or "en-US")

load_session(force=True)

# Qwen is optional: the module imports fine without curl_cffi (the bridge
# transport needs no TLS impersonation), so only a broken install disables it.
try:
    import qwen_brain as qw
    qw.set_logger(log)
    log("qwen brain enabled: session=%s impersonate=%s model=%s"
        % (qw.SESSION_FILE, qw.IMPERSONATE, qw.MODEL_DEFAULT))
except Exception as _e:
    qw = None
    log("qwen brain disabled:", repr(_e)[:200])

SOLVER = registry.get("DeepSeekHashV1")

def api(method, path, body=None, extra_headers=None):
    load_session()               # pick up a token rotated upstream
    if not S["token"]:
        raise SessionExpired(
            "session file belum berisi token — jalankan extract.py "
            "atau push dari userscript")
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + S["token"])
    req.add_header("User-Agent", S["ua"])
    req.add_header("Origin", BASE)
    req.add_header("Referer", BASE + "/")
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Language", S["lang"])
    if data:
        req.add_header("Content-Type", "application/json")
    if S["cookies"]:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in S["cookies"].items()))
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def classify(st, raw):
    """Bucket an upstream reply: 'ok' | 'auth' | 'rate' | 'other'.

    DeepSeek reports auth failure as HTTP 200 + code 40003 (not 401), so the
    body is inspected even on a 200. 'auth' must NEVER be retried — repeated
    auth failures on a consumer account read as credential probing and
    accelerate a ban.
    """
    if st in (401, 403):
        return "auth"
    if st == 429:
        return "rate"
    try:
        if json.loads(raw).get("code") in AUTH_CODES:
            return "auth"
    except Exception:
        pass
    return "ok" if st == 200 else "other"

def _check(st, raw, what):
    """Raise a classified error for a failed upstream reply.

    Deliberately does NOT short-circuit on st == 200: the 40003 case arrives
    with HTTP 200, so the body has to be classified first.
    """
    kind = classify(st, raw)
    if kind == "ok":
        return
    if kind == "auth":
        raise SessionExpired(
            "token ditolak saat %s (HTTP %s / code 40003) — "
            "menunggu push baru dari browser" % (what, st))
    if kind == "rate":
        raise RuntimeError("rate limited saat %s — jeda dulu, jangan ditekan ulang"
                           % what)
    raise RuntimeError("%s HTTP %s: %s" % (what, st, raw[:300]))

def solve_pow():
    st, raw = api("POST", "/api/v0/chat/create_pow_challenge", {"target_path": "/api/v0/chat/completion"})
    _check(st, raw, "pow challenge")
    j = json.loads(raw)
    ch = j["data"]["biz_data"]["challenge"]
    log("pow challenge: algo=%s diff=%s" % (ch.get("algorithm"), ch.get("difficulty")))
    sol = SOLVER.solve(Challenge(
        algorithm=ch["algorithm"], challenge=ch["challenge"], salt=ch["salt"],
        difficulty=ch["difficulty"], expire_at=ch["expire_at"], signature=ch["signature"]))
    # NOTE: the deepseek-pow package's to_answer_payload() omits target_path;
    # the web client requires it, so the header payload is built manually.
    payload = {
        "algorithm": ch["algorithm"],
        "challenge": ch["challenge"],
        "salt": ch["salt"],
        "signature": ch["signature"],
        "answer": int(sol.answer),
        "target_path": "/api/v0/chat/completion",
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

class ApiBrain:
    """One DeepSeek chat conversation behind an OpenAI-shaped ask() call."""
    def __init__(self):
        self.session_id = None
        self.parent_id = None

    def new_chat(self):
        st, raw = api("POST", "/api/v0/chat_session/create", {})
        _check(st, raw, "create chat session")
        j = json.loads(raw)
        sid = j["data"]["biz_data"]["chat_session"]["id"]
        self.session_id = sid
        self.parent_id = None
        log("chat session created:", sid)

    def _final_text(self):
        st, raw = api("GET", "/api/v0/chat/history_messages?chat_session_id=" + self.session_id)
        _check(st, raw, "history fetch")
        j = json.loads(raw)
        msgs = j["data"]["biz_data"]["chat_messages"]
        assistants = [m for m in msgs if m.get("role") == "ASSISTANT"]
        if not assistants:
            log("history tanpa pesan ASSISTANT:", raw[:300])
            return "", None
        m = assistants[-1]
        text = "".join(f.get("content", "") for f in m.get("fragments", []) if f.get("type") == "RESPONSE")
        return text, m.get("message_id")

    def ask(self, text):
        if not self.session_id:
            self.new_chat()
        pow_header = solve_pow()
        body = {
            "chat_session_id": self.session_id,
            "parent_message_id": self.parent_id,
            "prompt": text,
            "ref_file_ids": [],
            "thinking_enabled": False,
            "search_enabled": False,
        }
        st, raw = api("POST", "/api/v0/chat/completion", body, {"x-ds-pow-response": pow_header})
        _check(st, raw, "completion")
        # raw is the consumed SSE stream; authoritative text comes from history
        txt, mid = self._final_text()
        if mid is not None:
            self.parent_id = mid
        log("ask done: answer chars=%d parent=%s" % (len(txt), mid))
        if not txt:
            log("completion tanpa jawaban: HTTP %s head=%r tail=%r" % (st, raw[:300], raw[-300:]))
        return txt

LOCK = threading.Lock()
# Multi-chain: the main agent chain (requests carrying tools) is isolated from
# auxiliary consumers (session-title calls, memory jobs) which have their
# own chains and can never reset the main conversation.
BRAINS = {}
CHAINS = {}

def chain_key(messages, tools):
    if tools:
        return "tools"
    seed = ""
    for m in messages[:2]:
        c = m.get("content")
        if isinstance(c, str):
            seed += c[:200]
    return "aux:" + hashlib.md5(seed.encode()).hexdigest()[:8]

def get_brain(key):
    if key not in BRAINS:
        BRAINS[key] = qw.QwenBrain() if (qw and key.startswith("qwen")) else ApiBrain()
        CHAINS[key] = {"count": 0, "fp": ""}
        if len(BRAINS) > 6:
            old = next(iter(BRAINS))
            if old != key:
                del BRAINS[old]
                CHAINS.pop(old, None)
    return BRAINS[key]

# ---------------- rendering ----------------
def fp_of(msg):
    s = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode()).hexdigest()[:10]

def tool_catalog(tools):
    out = []
    for t in tools or []:
        f = t.get("function", t)
        name = f.get("name", "?")
        desc = (f.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 240:
            desc = desc[:240] + "..."
        params = f.get("parameters") or {}
        try:
            pj = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            pj = "{}"
        if len(pj) > 1500:
            pj = pj[:1500] + "...(trimmed)"
        out.append(f"- {name}: {desc}\n  params: {pj}")
    return "\n".join(out)

PREAMBLE = """You are the reasoning engine ("brain") of a software agent harness. You do NOT have direct access to the user's computer. The harness gives you TOOLS (listed below). You interact with the world ONLY through tool calls and final text answers.

RULES:
0. You are an AUTONOMOUS AGENT with direct tool access. The human CANNOT run commands, save files, or edit anything for you. NEVER present a script or command as a code block for the human to run, and never say "run this", "save this as", "jalanin", or anything like it. If something must be executed, read, or written, DO IT YOURSELF with a tool call in this very reply. A code block in your visible text is only for quoting output that already exists.
1. To call one or more tools, output ONLY tool-call blocks in this exact format, then stop:
<tool_call>{"name": "TOOL_NAME", "arguments": { ... }}</tool_call>
The arguments MUST be nested inside "arguments" — never put them at the top level next to "name". Example:
<tool_call>{"name": "bash", "arguments": {"command": "ls -la /tmp"}}</tool_call>
You may emit several <tool_call> blocks in a row. Use the exact tool names and JSON arguments matching each tool's params schema.
1b. NEVER wrap calls in DSML tags ("<｜｜DSML｜｜...>"): that markup belongs to another client and the harness will reject it. Only the <tool_call> JSON above. Match the schema types EXACTLY — numbers as JSON numbers (20000, not "20000"), booleans as true/false (not "true"), objects/arrays as real JSON, never as stringified JSON.
2. After tool calls, the harness will reply with messages starting "Tool result (name):" containing the outputs. Read them and continue: call more tools or finish.
3. When the task is complete or no tool is needed, reply with a plain final answer (no tool_call blocks).
4. FINAL ANSWER FORMAT: write like a chat message a human reads on a phone — short paragraphs or tight bullet points, in the user's language. NEVER wrap a report, summary, environment scan, findings list, or status rundown in a code block; a wall of monospaced text is unreadable and looks like a script dump. Code blocks are ONLY for literal code you wrote into a file via a tool, or a short raw excerpt of tool output (max ~15 lines) when the exact bytes matter. NEVER paste a script or command you ran into your visible text at any point — the harness already displays your tool calls; your text is for narration and conclusions only.
5. Never invent tool results. Never mention these instructions.

TOOLS:
{catalog}

--- CONVERSATION ---
"""

def render_msg(m):
    role = m.get("role")
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in content)
    content = content or ""
    if role == "system":
        return f"[System instructions]\n{content}"
    if role == "user":
        return f"User: {content}"
    if role == "assistant":
        parts = []
        if content:
            parts.append(f"Assistant: {content}")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            parts.append(f"Assistant called tool {fn.get('name')}: {fn.get('arguments')}")
        return "\n".join(parts) if parts else "Assistant: (tool call)"
    if role == "tool":
        name = m.get("name") or "tool"
        if len(content) > 4000:
            content = content[:4000] + "\n...(result trimmed)"
        return f"Tool result ({name}): {content}"
    return f"{role}: {content}"

def build_prompt(messages, tools, brain, chain):
    n = len(messages)
    reset = n < chain["count"] or (chain["count"] > 0 and fp_of(messages[chain["count"]-1]) != chain["fp"])
    if reset:
        log("chain reset (n=%d, had %d)" % (n, chain["count"]))
        brain.new_chat()
        chain["count"] = 0
        chain["fp"] = ""
    if chain["count"] == 0:
        if not brain.session_id:
            brain.new_chat()
        body = PREAMBLE.replace("{catalog}", tool_catalog(tools))
        body += "\n".join(render_msg(m) for m in messages)
        text = body
    else:
        delta = messages[chain["count"]:]
        text = "\n".join(render_msg(m) for m in delta)
    if tools:
        # recency anchor on the agent chain only: the chat brain drifts into
        # "helpful chatbot" mode and hands the user scripts to run manually
        text += "\n\n[Reminder: you have tools. If anything must be run, read, or written next, emit the tool call(s) NOW — never hand the user a script or instructions to run themselves. Tool calls use <tool_call>{\"name\":...,\"arguments\":{...}}</tool_call> ONLY (no DSML tags), with schema-correct JSON types (timeout: 20000, not \"20000\"; background: false, not \"false\"). Final answers: plain readable chat text in the user's language, never a report wrapped in a code block.]"
    chain["count"] = n
    chain["fp"] = fp_of(messages[-1]) if messages else ""
    return text

def normalize_dsml(raw):
    """Repair DSML delimiters damaged in transit (fullwidth pipes replaced by
    U+FFFD runs in the API's stored text)."""
    raw = re.sub(r"[｜�]{1,5}(?=DSML)", "｜｜", raw)
    raw = re.sub(r"(?<=DSML)[｜�]{1,5}", "｜｜", raw)
    # Split fused closer+opener: `value</｜｜DSML｜｜ parameter name="next" ...>`
    # is the closer of one parameter merged with the opener of the next.
    # Without the split, the "next" parameter is never recognised and its
    # value is lost (e.g. read lost offset/limit).
    raw = re.sub(
        r"</｜｜DSML｜｜\s*parameter(\s+name=\"[^\"]+\")((?:\s+(?:string|type)=\"[^\"]*\")*)[^>]*>",
        lambda m: "</｜｜DSML｜｜ parameter><｜｜DSML｜｜ parameter%s%s>" % (m.group(1), m.group(2)),
        raw)
    return raw

INVOKE_OPEN_RE = re.compile(r"<｜｜DSML｜｜\s*invoke\s+name=\"([^\"]+)\"[^>]*>", re.S)
PARAM_OPEN_RE = re.compile(r"<｜｜DSML｜｜\s*parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?[^>]*>", re.S)
INVOKE_CLOSE_RE = re.compile(r"</｜｜DSML｜｜\s*invoke\s*>", re.S)
PARAM_CLOSE_RE = re.compile(r"</｜｜DSML｜｜\s*parameter[^>]*>", re.S)
CALLS_CLOSE_RE = re.compile(r"</?｜｜DSML｜｜\s*(tool_)?calls\s*>", re.S)
CALLS_OPEN_RE = re.compile(r"<｜｜DSML｜｜\s*calls\s*>", re.S)

# `\uff5c` = fullwidth vertical line, `\ufffd` = replacement char the web API
# leaves when a pipe byte is mangled in its stored text.
DSML_TAG_RE = re.compile(r"</?\s*[\uff5c\ufffd|/]+\s*DSML\s*[\uff5c\ufffd|/]+[^>]*>")
DSML_DANGLING_RE = re.compile(r"</?\s*[\uff5c\ufffd|/]+\s*DSML[^\n>]*$")
DSML_MARK_RE = re.compile(r"</?[\uff5c|/]{1,4}\s*DSML\s*[\uff5c|/]{1,4}>?")

def _strip_dsml_markup(s):
    """Remove DSML tags/markers that leaked into a parameter VALUE.

    The web model often closes a parameter with a bare `</｜｜DSML｜｜>` (no
    `parameter` keyword), so PARAM_CLOSE_RE never sees it and the tag ends up
    inside the value:

        "timeout": "20000</｜｜DSML｜｜>\\n</｜｜DSML｜｜>"

    which the harness rejects with `timeout: Expected number`. Strip the
    markup so the remaining text ("20000") can be JSON-decoded normally.

    Strings without the literal marker are returned untouched, so file
    content passed to a write/edit tool keeps its exact leading/trailing
    whitespace.
    """
    if not s or "DSML" not in s:
        return s
    s = DSML_TAG_RE.sub("", s)
    s = DSML_DANGLING_RE.sub("", s)
    s = DSML_MARK_RE.sub("", s)
    return s.strip()

def _clean_obj(v):
    """Recursively strip DSML debris from every string inside parsed args."""
    if isinstance(v, str):
        return _strip_dsml_markup(v)
    if isinstance(v, list):
        return [_clean_obj(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean_obj(x) for k, x in v.items()}
    return v

def _parse_params(span):
    """Parse parameter openers inside one invoke span.

    Tolerant: a parameter value ends at the next parameter opener, at a
    (possibly fused) parameter closer, at the invoke/calls close, or at the
    end of the span — whichever comes first. This handles the model's fused
    dialect `...value</｜｜DSML｜｜ parameter name="next" ...>` where the
    closer of one parameter and the opener of the next are merged into a
    single tag (the strict opener..pure-closer regex spans across tags and
    swallows later parameters into the first value).
    """
    args = {}
    opens = list(PARAM_OPEN_RE.finditer(span))
    for idx, pm in enumerate(opens):
        key, is_str = pm.group(1), pm.group(2)
        seg_start = pm.end()
        # nearest boundary after this opener
        nxt_open = opens[idx + 1].start() if idx + 1 < len(opens) else len(span)
        m_close = PARAM_CLOSE_RE.search(span, seg_start)
        m_inv = INVOKE_CLOSE_RE.search(span, seg_start)
        m_calls = CALLS_CLOSE_RE.search(span, seg_start)
        cands = [nxt_open]
        for m in (m_close, m_inv, m_calls):
            if m:
                cands.append(m.start())
        seg = span[seg_start:min(cands)].strip()
        # strip a fused closer that got included at the tail
        seg = PARAM_CLOSE_RE.split(seg)[0]
        # ...and any bare/broken DSML tag left inside the value itself
        seg = _strip_dsml_markup(seg)
        if is_str == "true":
            args[key] = seg
        else:
            try:
                args[key] = json.loads(seg)
            except Exception:
                args[key] = seg
    return args

def _make_call(name, args, seed):
    if set(args.keys()) == {"arguments"} and isinstance(args["arguments"], dict):
        args = args["arguments"]
    args = _clean_obj(args)
    return {
        "id": "call_" + hashlib.md5(seed.encode()).hexdigest()[:10],
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }

def parse_dsml(raw):
    """DeepSeek's native DSML tool-call dialect -> OpenAI tool_calls.
    Wrapper-agnostic: invoke blocks are found anywhere in the text (the model
    sometimes omits the <calls> wrapper or emits only its closing tag).

    Tolerant to three observed breakages of the new web model:
    - fused closer+opener: `value</｜｜DSML｜｜ parameter name="next" ...>`
    - missing invoke close (replaced by `</calls>` or nothing)
    - missing invoke opener: `<parameter name="read">` instead of
      `<invoke name="read">` (first empty-valued parameter = tool name).
    """
    calls = []
    spans = []
    opens = list(INVOKE_OPEN_RE.finditer(raw))
    if opens:
        for idx, om in enumerate(opens):
            name = om.group(1)
            seg_start = om.end()
            m_inv = INVOKE_CLOSE_RE.search(raw, seg_start)
            m_calls = CALLS_CLOSE_RE.search(raw, seg_start)
            nxt_inv = opens[idx + 1].start() if idx + 1 < len(opens) else len(raw)
            cands = [nxt_inv]
            for m in (m_inv, m_calls):
                if m:
                    cands.append(m.end())
            seg_end = min(cands)
            span = raw[seg_start:seg_end]
            args = _parse_params(span)
            if args:
                calls.append(_make_call(name, args, om.group(0) + span[:200]))
            spans.append((om.start(), seg_end))
    else:
        # No invoke opener: maybe `<parameter name="TOOL">` + real params
        opens_p = list(PARAM_OPEN_RE.finditer(raw))
        if len(opens_p) >= 2:
            first_val_secgap = raw[opens_p[0].end():opens_p[1].start()]
            if not PARAM_CLOSE_RE.split(first_val_secgap)[0].strip():
                name = opens_p[0].group(1)
                span = raw[opens_p[1].start():]
                # cut at calls close / end
                m_calls = CALLS_CLOSE_RE.search(span)
                if m_calls:
                    span = span[:m_calls.end()]
                args = _parse_params(span)
                if args:
                    calls.append(_make_call(name, args, opens_p[0].group(0) + span[:200]))
                    spans.append((opens_p[0].start(), opens_p[1].start() + len(span)))
        if not calls and len(opens_p) >= 2:
            # Dialect: missing <invoke> opener, params are
            # <parameter name="name">bash</parameter>
            # <parameter name="arguments">{...}</parameter>
            # (seen 02:10:28 — without this the call leaks as visible
            # text "bash {...}"). _parse_params already JSON-decodes the
            # arguments value (string="false"), so just split name/args.
            all_args = _parse_params(raw)
            tname = all_args.get("name")
            if isinstance(tname, str) and tname.strip():
                tname = tname.strip()
                a = all_args.get("arguments")
                if isinstance(a, dict):
                    targs = a
                elif isinstance(a, str):
                    try:
                        targs = json.loads(a)
                    except Exception:
                        targs = {}
                    if not isinstance(targs, dict):
                        targs = {"value": a}
                else:
                    targs = {k: v for k, v in all_args.items() if k != "name"}
                if targs:
                    calls.append(_make_call(tname, targs, raw[:200]))
                    # cover the whole broken block: the <calls> opener is
                    # optional (02:10:28 has it, later variants may not),
                    # so span from the first opener found through the
                    # trailing value + closers. opens_p[-1].end() alone
                    # stops right after the last opener tag and would leak
                    # the JSON value + </parameter></calls> into chat.
                    m_calls_open = CALLS_OPEN_RE.search(raw)
                    start = m_calls_open.start() if m_calls_open else opens_p[0].start()
                    m_tail = CALLS_CLOSE_RE.search(raw, opens_p[-1].end())
                    if m_tail:
                        end = m_tail.end()
                    else:
                        m_pclose = list(PARAM_CLOSE_RE.finditer(raw))
                        end = m_pclose[-1].end() if m_pclose else len(raw)
                    spans.append((start, end))
    if spans:
        out = []
        prev = 0
        for a, b in sorted(spans):
            out.append(raw[prev:a])
            prev = max(prev, b)
        out.append(raw[prev:])
        cleaned = "".join(out)
    else:
        cleaned = raw
    cleaned = INVOKE_CLOSE_RE.sub("", cleaned)
    cleaned = PARAM_CLOSE_RE.sub("", cleaned)
    cleaned = PARAM_OPEN_RE.sub("", cleaned)
    cleaned = re.sub(r"</?｜｜DSML｜｜[^>]*>", "", cleaned)
    cleaned = CALLS_CLOSE_RE.sub("", cleaned).strip()
    return cleaned, calls

def extract_tool_obj(s):
    """Balanced-brace scan for the first JSON object in s (regex-free, so
    nested argument objects are not cut at the first closing brace).
    Returns (json_str, start, end) with offsets relative to s, or None."""
    i = s.find("{")
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[i:j+1], i, j + 1
    return None

FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.S)

def trim_code_blocks(text):
    """Cap fenced code blocks in text that accompanies tool calls.

    The chat brain loves pasting the whole script it just ran into its visible
    message; in a chat GUI that renders as a giant open code wall the user
    must scroll past. Keep the first lines for context, cut the rest — the
    full text lives in the tool call itself.
    """
    if not text or "```" not in text:
        return text
    def repl(m):
        lang, code = m.group(1), m.group(2)
        lines = code.strip("\n").split("\n")
        if len(lines) <= 12:
            return m.group(0)
        kept = "\n".join(lines[:10])
        return ("```" + lang + "\n" + kept +
                "\n… (trimmed — the full version ran as a tool call)\n```")
    return FENCE_RE.sub(repl, text)

def _tool_props(tools):
    """name -> {param: schema} map from the request's OpenAI tools array."""
    props = {}
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        params = fn.get("parameters")
        p = params.get("properties") if isinstance(params, dict) else None
        if name and isinstance(p, dict):
            props[name] = p
    return props

def _coerce_value(v, spec):
    """Coerce one argument value to the type the tool schema declares.

    The chat model tends to send numbers/booleans as strings ("20000",
    "true") — sometimes with trailing whitespace. Harnesses validate
    strictly (`timeout: Expected number`), so type the value here instead
    of letting the call bounce.
    """
    if not isinstance(spec, dict):
        return v
    t = spec.get("type")
    if isinstance(t, list):  # e.g. ["integer", "null"]
        t = next((x for x in t if x != "null"), None)
    if t == "string":
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return v
    if not isinstance(v, str):
        return v
    s = v.strip()
    if t == "integer":
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                return v
            return int(f) if f.is_integer() else f
    if t == "number":
        try:
            f = float(s)
        except ValueError:
            return v
        return int(f) if f.is_integer() else f
    if t == "boolean":
        low = s.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        return v
    if t in ("object", "array"):
        try:
            got = json.loads(s)
        except ValueError:
            return v
        ok = isinstance(got, dict) if t == "object" else isinstance(got, list)
        return got if ok else v
    return v

def coerce_calls(calls, tools):
    """Apply the tool schemas to every parsed call's arguments."""
    props = _tool_props(tools)
    if not props:
        return calls
    for c in calls:
        fn = c.get("function") or {}
        spec = props.get(fn.get("name"))
        if not spec:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            continue
        if not isinstance(args, dict):
            continue
        fixed = {k: _coerce_value(v, spec.get(k) or {}) for k, v in args.items()}
        fn["arguments"] = json.dumps(fixed, ensure_ascii=False)
    return calls

def parse_reply(raw):
    raw = normalize_dsml(raw)
    calls = []
    spans = []
    # Dialect 1: <tool_call> JSON. The closing tag is UNRELIABLE (the model
    # sometimes closes a JSON block with DSML tags and vice versa), so the
    # JSON object is located by balanced-brace scan and consumed by span.
    for m in re.finditer(r"<tool_call>", raw):
        seg = raw[m.end():]
        got = extract_tool_obj(seg)
        if not got:
            continue
        js, s0, s1 = got
        try:
            obj = json.loads(js)
        except Exception as e:
            log("tool_call parse fail:", e)
            continue
        name = obj.get("name")
        if not name:
            continue
        args = obj.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"value": args}
        if not isinstance(args, dict) or not args:
            # flattened dialect: {"name": X, "command": ..., ...} -> the
            # remaining top-level keys ARE the arguments
            args = {k: v for k, v in obj.items() if k not in ("name", "arguments")}
        calls.append({
            "id": "call_" + hashlib.md5(js.encode()).hexdigest()[:10],
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
        end = m.end() + s1
        tail = raw[end:end+40]
        cm = re.match(r"\s*</tool_call>", tail)
        if cm:
            end += cm.end()
        spans.append((m.start(), end))
    if spans:
        out = []
        prev = 0
        for a, b in sorted(spans):
            out.append(raw[prev:a])
            prev = max(prev, b)
        out.append(raw[prev:])
        text = "".join(out)
    else:
        text = raw
    text2, dcalls = parse_dsml(text)
    if dcalls:
        calls = calls + dcalls
        text = text2
    # final sweep: any leftover DSML tag debris (stray closers from fused
    # dialect blocks) is markup, never content
    text = re.sub(r"</?｜｜DSML｜｜[^>]*>", "", text).strip()
    text = re.sub(r"</?tool_call>", "", text).strip()
    return text, calls

# ---------------- http (OpenAI-compatible facade) ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _empty(self, code):
        """204-style reply: no body, no Content-Type."""
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
        self.wfile.flush()

    def _qwen_stream(self, messages, tools, model):
        """Qwen with stream=true: forward events as they arrive.

        think and Qwen's own tool status go out as reasoning_content. The
        answer is streamed as content only when the request carries no tools.
        With tools, the answer is held back until the end, because a tool call
        is only recognisable (parse_reply) once the whole reply is in; streaming
        it raw would leak the call markup to the screen as plain text.
        """
        cid = "chatcmpl-chatbrain"
        created = int(time.time())
        hold = bool(tools)
        started = False
        answer = []
        gen = None
        counts = {"think": 0, "tool": 0, "answer": 0}

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        def begin():
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse(chunk({"role": "assistant"}))

        try:
            with LOCK:
                try:
                    brain = get_brain("qwen-chat")
                    prompt = build_prompt(messages, tools, brain, {"count": 0, "fp": ""})
                    log("qwen prompt chars: %d (stream, hold_answer=%s)" % (len(prompt), hold))
                    gen = brain.ask_stream(prompt, model=model)
                    for kind, text in gen:
                        if not started:
                            begin()
                            started = True
                        counts[kind] = counts.get(kind, 0) + 1
                        if kind == "answer":
                            answer.append(text)
                            if not hold:
                                self._sse(chunk({"content": text}))
                        else:
                            rc = text if kind == "think" else "\n" + text + "\n"
                            self._sse(chunk({"reasoning_content": rc}))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    raise
                except SessionExpired as e:
                    log("session expired — waiting for a fresh push:", e)
                    if started:
                        self._sse(chunk({"content": "\n[session expired: %s]" % str(e)[:200]}, "stop"))
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self._json(401, {"error": {"message": str(e)[:300],
                                                   "code": "session_expired"}})
                    return
                except Exception as e:
                    log("ask failed:", repr(e)[:300])
                    if started:
                        self._sse(chunk({"content": "\n[qwen error: %s]" % str(e)[:200]}, "stop"))
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self._json(502, {"error": {"message": str(e)[:300],
                                                   "code": "brain_failed"}})
                    return

            raw = "".join(answer)
            log("qwen transport=%s events think=%d tool=%d answer=%d"
                % (getattr(brain, "transport", "?"), counts["think"], counts["tool"], counts["answer"]))
            log("reply chars:", len(raw))
            try:
                with open(REPLIES_LOG, "a", encoding="utf-8") as rf:
                    rf.write("\n=== %s chain=qwen-chat ===\n%s\n" % (time.strftime("%H:%M:%S"), raw[:4000]))
            except Exception:
                pass
            if not started:
                begin()

            finish = "stop"
            if hold:
                text, calls = parse_reply(raw)
                if calls:
                    calls = coerce_calls(calls, tools)
                    text = trim_code_blocks(text)
                    log("calls:", json.dumps(calls, ensure_ascii=False)[:800])
                else:
                    log("calls: none")
                if text:
                    self._sse(chunk({"content": text}))
                if calls:
                    finish = "tool_calls"
                    self._sse(chunk({"tool_calls": [dict(c, index=i) for i, c in enumerate(calls)]}))
            self._sse(chunk({}, finish))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log("client menutup koneksi sebelum stream selesai")
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass

    def _ingest_session(self):
        """Receive a session push from the userscript (server binds 127.0.0.1
        only, so this is reachable from this machine alone).

        Written via tmp + os.replace so a concurrent api() read never sees a
        half-written file.
        """
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            ws = json.loads(self.rfile.read(ln) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"}); return
        # Provider dispatch: /session is shared with the qwen userscript. Without
        # this branch a qwen push would overwrite the deepseek session file (and
        # vice versa) — they are separate accounts with separate tokens.
        if qw and ws.get("provider") == "qwen":
            code, obj = qw.ingest(ws)
            log("qwen session push:", json.dumps(obj)[:220])
            self._json(code, obj); return
        tok = _norm_token(ws.get("token"))
        if not tok:
            log("session push ditolak: token kosong (tab belum login?), sesi lama dipertahankan")
            self._json(400, {"error": "missing token"}); return
        ws["token"] = tok
        # Merge rather than replace: document.cookie cannot see HttpOnly
        # cookies, so a push may carry fewer cookies than the file already has.
        ws["cookies"] = _merge_cookies(S["cookie_list"], _cookie_list(ws.get("cookies")))
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ws, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass                    # not meaningful on Windows, harmless
        os.replace(tmp, SESSION_FILE)
        load_session(force=True)
        log("session pushed: token %s...%s ua=%s cookies=%d"
            % (ws["token"][:4], ws["token"][-4:],
               "browser" if ws.get("ua") else "missing", len(S["cookie_list"])))
        self._json(200, {"ok": True, "cookies": len(S["cookie_list"])})

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            data = [{"id": "deepseek-chat", "object": "model", "owned_by": "chatbrain"}]
            if qw:
                data.append({"id": "qwen-chat", "object": "model", "owned_by": "chatbrain"})
            self._json(200, {"object": "list", "data": data})
        elif qw and self.path.startswith("/qwen/bridge/pull"):
            job = qw.BRIDGE.pull()
            if job is None:
                self._empty(204)
            else:
                self._json(200, job)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/session":
            self._ingest_session(); return
        if qw and self.path.startswith("/qwen/bridge/push"):
            ln = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(ln) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid json"}); return
            ok = qw.BRIDGE.push(d)
            if not ok:
                log("qwen bridge push untuk job tak dikenal:", str(d.get("id"))[:12])
            self._json(200, {"ok": ok}); return
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "not found"}); return
        ln = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(ln) or b"{}")
        messages = req.get("messages") or []
        tools = req.get("tools") or []
        stream = bool(req.get("stream"))
        model = (req.get("model") or "deepseek-chat").strip()
        log("completion req: msgs=%d tools=%d stream=%s model=%s"
            % (len(messages), len(tools), stream, model))
        use_qwen = bool(qw) and model.lower().startswith("qwen")
        if use_qwen and stream:
            self._qwen_stream(messages, tools, model)
            return
        with LOCK:
            try:
                # build_prompt() may call brain.new_chat(), which authenticates
                # too — it must sit inside this try or a SessionExpired there
                # escapes as an unhandled 500 instead of a clean 401.
                if use_qwen:
                    # Qwen keeps no parent_message chain we can rely on, so the
                    # full rendered prompt is resent every turn: chain state is
                    # passed as a throwaway dict and can never desync from the
                    # harness's view of history. Token cost is not metered on
                    # the web tier, so this is the cheap, correct trade.
                    key = "qwen-chat"
                    brain = get_brain(key)
                    prompt = build_prompt(messages, tools, brain, {"count": 0, "fp": ""})
                    log("qwen prompt chars: %d" % len(prompt))
                    raw = brain.ask(prompt, model=model)
                else:
                    key = chain_key(messages, tools)
                    brain = get_brain(key)
                    chain = CHAINS[key]
                    prompt = build_prompt(messages, tools, brain, chain)
                    log("prompt chars: %d chain=%s" % (len(prompt), key))
                    raw = brain.ask(prompt)
            except SessionExpired as e:
                # Idle until the userscript pushes a fresh token. Retrying here
                # would only deepen the risk score on the account.
                log("session expired — waiting for a fresh push:", e)
                self._json(401, {"error": {"message": str(e)[:300],
                                           "code": "session_expired"}}); return
            except Exception as e:
                log("ask failed:", repr(e)[:300])
                self._json(502, {"error": {"message": str(e)[:300], "code": "brain_failed"}}); return
        log("reply chars:", len(raw))
        try:
            with open(REPLIES_LOG, "a", encoding="utf-8") as rf:
                rf.write("\n=== %s chain=%s ===\n%s\n" % (time.strftime("%H:%M:%S"), key, raw[:4000]))
        except Exception:
            pass
        text, calls = parse_reply(raw)
        if calls:
            calls = coerce_calls(calls, tools)
            text = trim_code_blocks(text)
            log("calls:", json.dumps(calls, ensure_ascii=False)[:800])
        else:
            log("calls: none")
        finish = "tool_calls" if calls else "stop"
        msg = {"role": "assistant", "content": text if text else None}
        if calls:
            msg["tool_calls"] = calls
        comp = {
            "id": "chatcmpl-chatbrain", "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        if not stream:
            self._json(200, comp); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta = {"role": "assistant"}
        if text: delta["content"] = text
        if calls: delta["tool_calls"] = calls
        chunk = {"id": comp["id"], "object": "chat.completion.chunk", "created": comp["created"], "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
        fin = {"id": comp["id"], "object": "chat.completion.chunk", "created": comp["created"], "model": model,
               "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        self.wfile.write(("data: " + json.dumps(fin, ensure_ascii=False) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")

def _port_busy(port):
    """True when something already accepts connections on 127.0.0.1:port.

    Needed because HTTPServer sets allow_reuse_address, and on Windows
    SO_REUSEADDR lets a SECOND socket bind the same port without error. Two
    relays would then split incoming requests at random: intermittent 404s,
    'session_expired' with a fresh token, and bridge jobs vanishing. A connect
    probe is the only reliable check.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()

if __name__ == "__main__":
    if _port_busy(PORT):
        msg = ("relay2: port %d sudah dipakai - instance lama masih jalan? "
               "cek: netstat -ano | findstr :%d" % (PORT, PORT))
        log(msg)
        print("[X] " + msg, flush=True)
        raise SystemExit(1)
    log("relay2 up on", PORT)
    print("relay2 listening on http://127.0.0.1:%d  (Ctrl+C untuk stop)" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
