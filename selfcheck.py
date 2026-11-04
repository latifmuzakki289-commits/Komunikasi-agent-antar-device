#!/usr/bin/env python3
"""selfcheck.py — uji gateway + upstream sebelum dipakai agen lain.

Jalankan setelah `./start.sh`:
    GATEWAY_TOKEN=sk-free-local python3 selfcheck.py
    GW=http://127.0.0.1:8080 GATEWAY_TOKEN=sk-free-local python3 selfcheck.py

Yang diuji: health, daftar model gabungan, reachability upstream, chat
non-stream per model, chat stream per model, tool calling, dan model failover.
Exit code 0 kalau semua yang dicoba lulus.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GW", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("GATEWAY_TOKEN", "")

ok = 0
fail = 0


def req(method, path, body=None, timeout=240):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if TOKEN:
        r.add_header("Authorization", "Bearer " + TOKEN)
    try:
        return 200, urllib.request.urlopen(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, e


def report(name, passed, detail=""):
    global ok, fail
    if passed:
        ok += 1
        print("PASS  %-30s %s" % (name, detail))
    else:
        fail += 1
        print("FAIL  %-30s %s" % (name, detail))


def main():
    st, r = req("GET", "/health", timeout=10)
    if st != 200:
        print("gateway tidak menjawab di", BASE)
        sys.exit(2)
    report("health", True, json.loads(r.read())["status"])

    st, r = req("GET", "/v1/models", timeout=40)
    models = []
    if st == 200:
        models = [m["id"] for m in json.loads(r.read())["data"]]
        report("models", bool(models), "%d: %s" % (len(models), ", ".join(models[:8])))
    else:
        report("models", False, str(st))

    st, r = req("GET", "/v1/upstreams", timeout=60)
    if st == 200:
        for u in json.loads(r.read())["upstreams"]:
            print("      upstream %-14s reachable=%s models=%s" % (
                u["name"], u.get("reachable"), ",".join(u.get("models", [])[:4])))

    targets = [m for m in models if "/" not in m]
    for model in targets:
        st, r = req("POST", "/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "Balas satu kata: pong"}],
            "max_tokens": 64,
        })
        if st != 200:
            report("chat " + model, False, "%s %s" % (st, r.read()[:200].decode("utf-8", "replace")))
            continue
        data = json.loads(r.read())
        txt = (data["choices"][0]["message"].get("content") or "").strip()
        report("chat " + model, bool(txt), repr(txt[:40]))

    for model in targets:
        st, r = req("POST", "/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "Hitung 2+2, jawab singkat."}],
            "stream": True,
        })
        if st != 200:
            report("stream " + model, False, str(st))
            continue
        chunks = 0
        done = False
        for line in r:
            line = line.strip()
            if line.startswith(b"data:"):
                chunks += 1
                if b"[DONE]" in line:
                    done = True
        report("stream " + model, chunks > 0, "%d chunk, done=%s" % (chunks, done))

    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Ambil cuaca kota",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }]
    tool_ok = 0
    for model in targets:
        st, r = req("POST", "/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": "Cuaca di Jakarta sekarang? Pakai tool."}],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": 256,
        })
        if st != 200:
            print("      tools %s -> %s %s" % (model, st, r.read()[:160].decode("utf-8", "replace")))
            continue
        msg = json.loads(r.read())["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if calls:
            tool_ok += 1
            fn = calls[0]["function"]
            print("      tools %s -> %s(%s)" % (model, fn.get("name"), fn.get("arguments")))
        else:
            print("      tools %s -> teks saja: %r" % (model, (msg.get("content") or "")[:80]))
    if targets:
        report("tool calling", tool_ok > 0, "%d/%d model memanggil tool" % (tool_ok, len(targets)))

    for chain in [m for m in models if "/" in m]:
        st, r = req("POST", "/v1/chat/completions", {
            "model": chain,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 32,
        })
        report("failover " + chain, st == 200, str(st))

    print("\n%d lulus, %d gagal" % (ok, fail))
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()