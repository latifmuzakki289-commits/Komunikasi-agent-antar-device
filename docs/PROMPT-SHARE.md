# Prompt siap tempel — menghubungkan DeepSeek Free + GLM Web Free ke agen lain

Dokumen ini berisi **instruksi prompt** yang bisa kamu tempel ke agen AI lain
(Hermes, Cline, OpenHands, OpenCode, Cursor, SDK OpenAI, dll) supaya agen itu
tahu cara memakai gateway ini.

Semua nilai di bawah sudah diverifikasi hidup pada 2026-11-04 09:2x WIB.

---

## 0. Fakta koneksi (yang harus dipegang agen)

| Item | Nilai |
|---|---|
| Base URL (di HP ini) | `http://127.0.0.1:8080/v1` |
| Base URL (dari device lain di LAN) | `http://10.5.10.4:8080/v1` |
| Token gateway | `sk-free-local` (ganti sebelum dibuka ke jaringan) |
| Protokol | OpenAI-compatible (`/v1/chat/completions`, `/v1/models`) |
| Anthropic-compatible | `POST /v1/messages` (diteruskan ke GLM) |
| Model DeepSeek | `deepseek-chat` |
| Model GLM | `glm-5.3`, `glm-5.2`, `glm-5.3-flash`, `glm-4.7`, `x-preview-l` |
| Nama ber-prefix | `deepseek-free/deepseek-chat`, `glm-free/glm-5.3` |
| Rantai failover | `deepseek-free/glm-free` (coba DeepSeek, gagal → GLM) |
| Health | `GET /health` (tanpa token) |
| Status upstream | `GET /v1/upstreams` (butuh token) |

---

## 1. Prompt universal (tempel apa adanya ke agen mana pun)

```
Kamu punya akses ke satu endpoint LLM OpenAI-compatible yang menggabungkan
dua model gratis:

  BASE_URL = http://127.0.0.1:8080/v1
  API_KEY  = sk-free-local

Aturan pakai:
1. Selalu kirim header: Authorization: Bearer sk-free-local
2. Ambil daftar model dari GET /v1/models. Jangan menebak nama model.
3. Model yang tersedia: deepseek-chat (DeepSeek web), glm-5.3 / glm-5.2 /
   glm-5.3-flash / glm-4.7 (GLM web). Pakai "glm-free/glm-5.3" kalau ingin
   memaksa upstream GLM, "deepseek-free/glm-free" untuk failover otomatis.
4. Streaming didukung: set "stream": true, baca SSE sampai "data: [DONE]".
5. Function calling / tools didukung pada deepseek-chat, glm-5.3, glm-5.2.
   Kirim field "tools" format OpenAI biasa.
6. Kalau dapat HTTP 502 dengan "attempts", artinya semua upstream menolak —
   laporkan isi attempts, jangan retry lebih dari 2x.
7. Jangan pernah menulis token gateway ke log atau repo publik.
```

---

## 2. Prompt khusus Hermes

Hermes memakai konfigurasi provider bergaya OpenAI. Tempel ini ke sesi
Hermes-mu (atau ke file config-nya lewat agen):

```
Tambahkan provider OpenAI-compatible baru ke konfigurasi Hermes:

  name:     free-llm-gateway
  base_url: http://127.0.0.1:8080/v1
  api_key:  sk-free-local
  models:
    - deepseek-chat        (default, cepat)
    - glm-5.3              (reasoning lebih kuat)
    - glm-5.2
    - glm-5.3-flash        (paling ringan)
    - glm-4.7
  failover_model: deepseek-free/glm-free

Lalu:
1. Verifikasi dengan GET {base_url}/models memakai header Bearer.
2. Jalankan satu chat uji: "Balas satu kata: pong".
3. Kalau Hermes berjalan di container/host lain, ganti 127.0.0.1 menjadi
   10.5.10.4 (IP HP di LAN) dan pastikan port 8080 tidak diblokir firewall.
```

Kalau Hermes-mu pakai variabel lingkungan, set:

```
OPENAI_BASE_URL=http://127.0.0.1:8080/v1
OPENAI_API_KEY=sk-free-local
OPENAI_MODEL=deepseek-chat
```

---

## 3. Prompt untuk agen coding berbasis config file (Cline / Roo / Continue)

```
Provider: OpenAI Compatible
Base URL: http://127.0.0.1:8080/v1
API Key : sk-free-local
Model ID: glm-5.3
Context window: 128000
Dukungan tools: ya (function calling OpenAI)
Dukungan vision: tidak (gateway ini teks saja)
Dukungan streaming: ya
```

---

## 4. Prompt verifikasi mandiri (suruh agen mengetes sendiri)

```
Sebelum memakai endpoint ini, jalankan verifikasi berikut dan laporkan
hasilnya dalam tabel:

1. GET  {BASE}/health                          -> harus status "ok"
2. GET  {BASE}/v1/models (Bearer)              -> hitung jumlah model
3. POST {BASE}/v1/chat/completions
   body: {"model":"deepseek-chat","messages":[{"role":"user",
         "content":"Balas satu kata: pong"}]}
   -> harus berisi "pong"
4. Ulangi langkah 3 dengan "stream": true      -> harus ada "data: [DONE]"
5. POST {BASE}/v1/chat/completions dengan tools sederhana
   (get_weather{city}) dan prompt "Cuaca Jakarta?" 
   -> periksa apakah response punya tool_calls
6. POST {BASE}/v1/chat/completions model
   "deepseek-free/glm-free"                    -> harus 200

Kalau ada langkah gagal, kirim body error mentah ke operator,
jangan menebak penyebabnya.
```

---

## 5. Prompt troubleshooting (kalau agen lain gagal konek)

```
Diagnosa koneksi ke free-llm-gateway secara berurutan:

a. Gateway mati?        -> GET {BASE}/health, kalau gagal minta operator
                           jalankan ./start.sh di folder free-llm-gateway.
b. 401 Unauthorized?    -> header Authorization salah/kosong.
                           Nilai benar: "Bearer sk-free-local".
c. Connection refused?  -> 127.0.0.1 hanya untuk proses di HP yang sama.
                           Dari luar HP pakai 10.5.10.4:8080.
d. Timeout > 60s?       -> upstream web (chat.deepseek.com / chat.z.ai)
                           sedang lambat atau kena rate-limit; coba model
                           lain atau rantai failover deepseek-free/glm-free.
e. 502 attempts=[...]   -> baca detail per upstream. Kalau GLM bilang
                           "waf_block", tunggu retryIn lalu ulangi.
                           Kalau DeepSeek bilang 401, token web-nya expired:
                           operator harus ekstrak ulang .websession.json.
f. Model tidak dikenal? -> GET /v1/models dulu; daftar berubah dinamis
                           mengikuti upstream.
```

---

## 6. Prompt keamanan (wajib kalau dibagikan ke jaringan)

```
Sebelum membuka gateway ke perangkat/agen lain:

1. Ganti GATEWAY_TOKENS menjadi string acak >= 32 karakter, contoh:
   GATEWAY_TOKENS=$(openssl rand -hex 24)
2. Jangan pernah memakai "sk-free-local" di jaringan publik.
3. Batasi bind: GATEWAY_HOST=10.5.10.4 (bukan 0.0.0.0) kalau cukup LAN.
4. Token upstream (.websession.json, .zai-token) tetap di HP; jangan
   disalin ke agen lain. Gateway yang memegang kredensial, bukan agen.
5. Rotasi token kalau perangkat lain hilang/dicuri.
```

---

## 7. Ringkasan satu baris untuk ditempel di system prompt agen

```
LLM endpoint: http://127.0.0.1:8080/v1 | Bearer sk-free-local |
models: deepseek-chat, glm-5.3, glm-5.2, glm-5.3-flash, glm-4.7 |
failover: deepseek-free/glm-free | OpenAI-compatible, streaming + tools OK.
```