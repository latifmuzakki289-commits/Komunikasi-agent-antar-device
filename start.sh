#!/bin/bash
# start.sh — nyalakan DeepSeek relay + GLM bridge + gateway gabungan, lalu cek.
#
#   ./start.sh              # semua (default)
#   ./start.sh gateway      # hanya gateway (upstream diasumsikan sudah hidup)
#   ./start.sh stop         # matikan ketiganya
#
# Port: deepseek-free :20232 | glm-free :20234 | gateway :8080
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$HERE"
DS_RELAY="$WS/dsh-better-bridge/relay"
GLM_DIR="$WS/glm-free-api"
GW_PID="$HERE/gateway.pid"
GW_LOG="$HERE/gateway.log"

# ---- konfigurasi gateway -------------------------------------------------
export GATEWAY_PORT="${GATEWAY_PORT:-8080}"
export GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
# Wajib diisi operator; jangan gunakan token contoh/terbuka.
: "${GATEWAY_TOKENS:?Set GATEWAY_TOKENS ke token acak yang kuat sebelum start}"
export GATEWAY_TOKENS
# Token klien untuk bridge GLM (AUTH_TOKEN zai-api). Dibaca dari file
# $GLM_DIR/.glm-auth-token supaya gateway bisa meneruskan kredensial yang benar.
if [ -z "${GLM_AUTH_TOKEN:-}" ] && [ -s "$GLM_DIR/.glm-auth-token" ]; then
  GLM_AUTH_TOKEN="$(cat "$GLM_DIR/.glm-auth-token")"
fi
export GLM_AUTH_TOKEN="${GLM_AUTH_TOKEN:-glm-local}"
export GATEWAY_LOG="$GW_LOG"

stop_all() {
  [ -f "$GW_PID" ] && kill "$(cat "$GW_PID")" 2>/dev/null && rm -f "$GW_PID" && echo "gateway berhenti"
  [ -f "$GLM_DIR/zai-api.pid" ] && kill "$(cat "$GLM_DIR/zai-api.pid")" 2>/dev/null && rm -f "$GLM_DIR/zai-api.pid" && echo "glm berhenti"
  pkill -f "relay/relay2.py" 2>/dev/null && echo "deepseek relay berhenti"
}

case "${1:-all}" in
  stop) stop_all; exit 0 ;;
esac

# ---- 1. DeepSeek web relay ----------------------------------------------
if ! python3 -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:20232/v1/models',timeout=3)" 2>/dev/null; then
  if [ -s "$DS_RELAY/.websession.json" ]; then
    ( cd "$DS_RELAY" && nohup python3 relay2.py >> relay2.log 2>&1 & echo $! > "$DS_RELAY/relay2.pid" )
    echo "deepseek relay: dinyalakan"
  else
    echo "deepseek relay: SKIP — $DS_RELAY/.websession.json belum ada"
    echo "  ambil token: buka chat.deepseek.com, login, DevTools > Application >"
    echo "  Local Storage > userToken, lalu simpan {token,cookies} ke file itu (chmod 600)."
  fi
else
  echo "deepseek relay: sudah hidup"
fi

# ---- 2. GLM bridge ------------------------------------------------------
if ! python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:20234/health',timeout=3)" 2>/dev/null; then
  if [ -x "$GLM_DIR/zai-api" ] && [ -s "$GLM_DIR/.zai-token" ]; then
    GLM_PORT=20234 GLM_HOST=127.0.0.1 GLM_AUTH_TOKEN="$GLM_AUTH_TOKEN" "$GLM_DIR/start-glm.sh" >/dev/null 2>&1
    echo "glm bridge: dinyalakan (start-glm.sh)"
  else
    echo "glm bridge: SKIP — $GLM_DIR/zai-api belum dibangun atau .zai-token kosong"
    echo "  token: chat.z.ai > DevTools > Application > Local Storage > key 'token'"
    echo "  build: perlu Go (go build -o zai-api .), lihat README glm-free-api."
  fi
else
  echo "glm bridge: sudah hidup"
fi

# ---- 3. Gateway ---------------------------------------------------------
if [ -f "$GW_PID" ] && kill -0 "$(cat "$GW_PID")" 2>/dev/null; then
  echo "gateway: sudah hidup (pid $(cat "$GW_PID"))"
else
  nohup python3 "$HERE/gateway.py" >> "$GW_LOG" 2>&1 &
  echo $! > "$GW_PID"
  chmod 600 "$GW_PID"
  sleep 1
  echo "gateway: dinyalakan pid $(cat "$GW_PID")"
fi

# ---- 4. Cek -------------------------------------------------------------
echo "--- cek ---"
python3 - <<'PY'
import json, urllib.request, os
base = "http://127.0.0.1:%s" % os.environ.get("GATEWAY_PORT", "8080")
try:
    h = json.load(urllib.request.urlopen(base + "/health", timeout=5))
    print("gateway  :", h["status"], "| upstreams:", ",".join(h["upstreams"]))
except Exception as e:
    print("gateway  : GAGAL", e); raise SystemExit(1)
req = urllib.request.Request(base + "/v1/models")
req.add_header("Authorization", "Bearer " + os.environ.get("GATEWAY_TOKENS", "").split(",")[0])
try:
    d = json.load(urllib.request.urlopen(req, timeout=30))
    ids = [m["id"] for m in d["data"]]
    print("models   :", len(ids), "->", ", ".join(ids[:8]))
except Exception as e:
    print("models   : GAGAL", e)
PY
echo
echo "Base URL : http://127.0.0.1:$GATEWAY_PORT/v1"
echo "Token    : configured via GATEWAY_TOKENS (not displayed)"
echo "Uji penuh: GW=http://127.0.0.1:$GATEWAY_PORT GATEWAY_TOKEN=\$GATEWAY_TOKENS python3 $HERE/selfcheck.py"