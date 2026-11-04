#!/bin/bash
# GLM web bridge (Z.AI chat -> OpenAI-compatible :20234, agent mode)
# Token disimpan di file .zai-token (chmod 600, JANGAN commit) — sama pola
# dengan .websession.json milik chatbrain.
#   echo "eyJ..." > .zai-token && chmod 600 .zai-token && ./start-glm.sh
cd "$(dirname "$0")"

TOKEN_FILE="${ZAI_TOKEN_FILE:-$PWD/.zai-token}"
if [ ! -s "$TOKEN_FILE" ]; then
  echo "ERROR: $TOKEN_FILE kosong/tidak ada. Isi dulu: echo 'eyJ...' > $TOKEN_FILE"
  exit 1
fi

# hentikan instance sebelumnya via pidfile (grep ps bisa salah sasaran)
PIDFILE="$PWD/zai-api.pid"
if [ -f "$PIDFILE" ]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null
  rm -f "$PIDFILE"
  sleep 1
fi

export PORT="${GLM_PORT:-20234}"
# GLM_HOST=127.0.0.1 -> hanya bisa dipakai di HP ini.
# GLM_HOST=0.0.0.0   -> bisa dipakai device lain di jaringan yang sama (butuh AUTH_TOKEN kuat).
export HOST="${GLM_HOST:-0.0.0.0}"
# Proxy keluar (opsional). Set GLM_HTTPS_PROXY untuk meneruskan ke upstream z.ai.
if [ -n "$GLM_HTTPS_PROXY" ]; then
  export HTTPS_PROXY="$GLM_HTTPS_PROXY"
  export HTTP_PROXY="$GLM_HTTPS_PROXY"
  export ALL_PROXY="$GLM_HTTPS_PROXY"
  echo "menggunakan proxy keluar: $GLM_HTTPS_PROXY"
fi
export ZAI_TOKEN="$(cat "$TOKEN_FILE")"
# Token API (Bearer) untuk klien. Prioritas: env GLM_AUTH_TOKEN > file
# .glm-auth-token > default lama "glm-local".
AUTH_FILE="$PWD/.glm-auth-token"
if [ -n "$GLM_AUTH_TOKEN" ]; then
  export AUTH_TOKEN="$GLM_AUTH_TOKEN"
elif [ -s "$AUTH_FILE" ]; then
  export AUTH_TOKEN="$(cat "$AUTH_FILE")"
else
  export AUTH_TOKEN="glm-local"
fi

if [ "$HOST" != "127.0.0.1" ] && [ "$AUTH_TOKEN" = "glm-local" ]; then
  echo "PERINGATAN: bridge terbuka ke jaringan dengan token default 'glm-local'."
  echo "            Ganti dulu: GLM_AUTH_TOKEN='token-rahasia-kamu' ./start-glm.sh"
fi

nohup ./zai-api --agent-mode > glm-api.log 2>&1 &
echo $! > "$PIDFILE"
chmod 600 "$PIDFILE"
echo "glm-api pid $!"
sleep 2
python3 - <<'PY'
import urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:20234/health', timeout=8).read().decode()[:120])
except Exception as e:
    print('health FAIL:', e, '(lihat glm-api.log)')
PY
