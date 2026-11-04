# Pelajaran Pemasangan — DeepSeek Web Free + GLM Web Free

Dokumen ini adalah **hasil belajar** dari pemasangan nyata di HP ini
(2026-11-01 s/d 2026-11-04). Tujuannya: agen lain bisa mengulang proses yang
sama tanpa menebak-nebak.

Diverifikasi hidup: `relay2.py` (pid 3638, 12m), `zai-api --agent-mode`
(pid 3657, 12m), `gateway.py` (pid 7016, 7m), 2026-11-04 09:4x WIB.

---

## 1. Arsitektur yang bekerja

```
Agen lain (Hermes/Cline/dll)
        │  OpenAI /v1/chat/completions
        ▼
  free-llm-gateway  :8080        <- satu pintu, satu token
        │
   ┌────┴─────────────────────┐
   ▼                          ▼
DeepSeek relay :20232      GLM bridge :20234
(relay2.py)                (zai-api --agent-mode)
   │                          │
   ▼                          ▼
chat.deepseek.com          chat.z.ai
/api/v0/chat/completion    /api/v2/chat/completions
+ PoW challenge            + captcha + HMAC signature
+ userToken web            + ZAI_TOKEN (JWT web)
```

Tiga lapis, masing-masing punya masalah sendiri:

| Lapis | Masalah utama | Solusi yang dipakai |
|---|---|---|
| DeepSeek | PoW wajib per request | `deepseek-pow` (registry `DeepSeekHashV1`) + payload `target_path` manual |
| GLM | Captcha Aliyun + signature + WAF | `token-collector` (device token) + HMAC bucket key + circuit breaker |
| Gabungan | Dua auth berbeda, dua nama model | gateway memetakan model → upstream |

---

## 2. Pelajaran DeepSeek web free

**Yang benar-benar dibutuhkan:**
- `userToken` dari `localStorage["userToken"].value` di chat.deepseek.com
- cookies (terutama `cf_clearance` bila kena Cloudflare)
- solver PoW: `pip install deepseek-pow` (versi 0.0.1 di mesin ini)

**Temuan penting yang tidak ada di dokumentasi upstream:**

1. `deepseek-pow` punya method `to_answer_payload()`, tapi **tidak
   menyertakan `target_path`**. Web client DeepSeek mewajibkan field itu ada.
   Akibatnya payload harus dibangun manual:
   `{algorithm, challenge, salt, signature, answer:int, target_path}`,
   lalu `base64(json.dumps(payload, separators=(",",":")))`.
   Kalau lupa `separators`, header tetap terkirim tapi ditolak.

2. Endpoint completion mengembalikan **SSE yang sudah dikonsumsi**. Teks
   final yang otoritatif harus diambil dari
   `GET /api/v0/chat/history_messages?chat_session_id=...`, ambil pesan
   `role=ASSISTANT` terakhir, gabung fragment `type=RESPONSE`.
   Membaca stream langsung tidak reliable.

3. Session percakapan **harus dibuat eksplisit** dulu:
   `POST /api/v0/chat_session/create` → `data.biz_data.chat_session.id`.
   `parent_message_id` di-update dari `message_id` jawaban terakhir, kalau
   tidak konteks percakapan hilang.

4. Multi-chain itu wajib. Request agen (yang membawa `tools`) harus
   diisolasi dari request aux (judul sesi, memory). Tanpa itu, panggilan
   aux akan me-reset/mengacaukan percakapan utama. Implementasinya:
   `chain_key = "tools"` bila ada tools, selain itu hash 200 char pertama.

5. Token web kedaluwarsa tanpa pemberitahuan (401 di completion, bukan di
   `/v1/models`). Simpan backup `.websession.json.bak-*`; kalau 401, ekstrak
   ulang dari browser.

**Yang TIDAK perlu:** Selenium/geckodriver untuk operasi normal. `extract.py`
hanya dipakai sekali saat mengambil token; runtime relay murni HTTP.

---

## 3. Pelajaran GLM web free

**Yang benar-benar dibutuhkan:**
- Go 1.25+ untuk build `zai-api` (di mesin ini Go sempat tidak ada; modcache
  ada tapi binary harus dibangun ulang)
- `ZAI_TOKEN` dari `localStorage["token"]` di chat.z.ai
- token device dari `token-collector` (untuk captcha), kecuali memakai JWT
  yang masih hidup

**Temuan penting:**

1. Captcha Aliyun punya rantai panjang: `InitCaptchaV3` → ambil `certifyId`
   → `Track` JSON + `ali_hash` → zlib-compress → base64 → RC4 pass kedua →
   `VerifyCaptchaV3` dengan device token → `securityToken` → base64 final.
   Device token **sekali pakai** (dihapus setelah dipakai, 5 retry).

2. Signature request = `HMAC-SHA256(sortedPayload | promptBase64 |
   timestamp)` dengan bucket key bersalt. Kalau salah urutan field,
   hasilnya 403 tanpa penjelasan.

3. `token-collector` **harus headful** (xvfb-run -a di server). Headless
   ditolak risk layer, token tidak akan verify. Satu sesi browser bisa
   mencetak ratusan token dan tetap valid ≥35 menit setelah browser ditutup.

4. Endpoint Z.AI **hanya menerima `role="user"`**. Kirim `system`,
   `assistant`, `tool`, atau `tools` mentah → `INTERNAL_ERROR`. Karena itu
   `--agent-mode` wajib untuk function calling. Shim modern memakai tag XML
   (`<system>`, `<tools>`, `<history_summary>`, `<recent>`,
   `<current_task>`, `<output_rules>`) dan mengubah `<<<TOOL_CALL>>>` di
   output menjadi `tool_calls` native.

5. WAF Aliyun memblokir **per-IP, per-path**, dan bertahan melewati request.
   Gejalanya `405` + HTML `"blocked as it may cause potential threats"`,
   sementara `GET /` dan `/api/models` tetap 200. Solusinya circuit breaker:
   fail-fast 503 dengan `Retry-After`, probe background dengan backoff
   berlipat 60s → 30m.

6. Session pool: chat harus dihapus setelah selesai (default 10 request per
   chat), kalau tidak kuota web penuh. `--sync-mode` hanya untuk debug.

**Yang TIDAK perlu:** browser automation saat runtime. Semua murni HTTP/2.

---

## 4. Pelajaran gateway penggabung

1. **Satu token, dua kredensial upstream.** Klien hanya tahu
   `sk-free-local`; gateway yang menyuntikkan `AUTH_TOKEN` GLM dan tidak
   menyentuh token DeepSeek (relay2 sudah memegangnya dari file). Jangan
   pernah meneruskan token upstream ke klien.

2. **Nama model harus dinamis.** Jangan hardcode daftar model GLM — Z.AI
   mengubahnya (`glm-5.3`, `x-preview-l` muncul belakangan). `/v1/models`
   gateway harus menarik `/v1/models` upstream dengan cache 5 menit dan
   fallback ke daftar statis kalau upstream mati.

3. **Namespacing menyelesaikan tabrakan nama.** `glm-free/glm-5.3` dan
   `deepseek-free/deepseek-chat` memaksa upstream. Tanpa prefix, gateway
   mencari upstream yang mengiklankan model itu.

4. **Failover lintas-upstream = rantai prefix.** `deepseek-free/glm-free`
   berarti coba DeepSeek dulu; kalau status ≥400, coba GLM dengan model
   default-nya. Ini yang menyelamatkan saat salah satu kuota web diblokir.

5. **Streaming harus byte-for-byte.** Jangan parse lalu render ulang SSE
   upstream; teruskan chunk apa adanya dan set `Connection: close` +
   `X-Accel-Buffering: no`. Kalau di-parse, `edit_content`/`edit_index` GLM
   (UTF-16 offset) akan merusak teks.

6. **`Connection: keep-alive` + HTTP/1.1 di handler butuh
   `Content-Length` yang benar.** Untuk stream, pakai `close_connection`.

7. **Tool calling tidak 100% di semua model.** Hasil uji: `deepseek-chat`,
   `glm-5.3`, `glm-5.2` memanggil tool dengan benar; `x-preview-l` hanya
   membalas teks. Gateway tidak boleh mengklaim semua model support tools.

8. **Uji wajib sebelum dibagikan:** health, models, chat non-stream,
   streaming (harus ada `data: [DONE]`), tools, dan failover. Skrip
   `selfcheck.py` melakukan keenamnya; hasil terakhir 12 lulus / 0 gagal.

---

## 5. Jebakan lingkungan (proot Android)

- Tidak ada sandbox backend; semua `bash` harus `danger-full-access`.
- `curl` tidak terpasang — pakai `python3 -c "import urllib.request..."`.
- Port dicek lewat `/proc/net/tcp` atau `ss`; `ss` kadang kosong di proot,
  lebih aman `python3 socket` atau langsung `urllib`.
- `hostname -I` memberi IP LAN (`10.5.10.4`). `127.0.0.1` hanya untuk proses
  di HP yang sama.
- Proses harus di-`nohup` + pidfile; shell tiap tool call adalah shell baru.
- Go tidak selalu ada; `glm-free-api` butuh `go build -o zai-api .` sebelum
  `start-glm.sh` bisa jalan.

---

## 6. Urutan pemasangan dari nol (yang terbukti)

1. `pip install deepseek-pow` (atau `requirements.txt` deepseek4free).
2. Ambil `userToken` DeepSeek dari browser → tulis `.websession.json`
   `{token, cookies}` → `chmod 600`.
3. Jalankan `relay2.py`; cek `GET :20232/v1/models`.
4. Ambil `token` Z.AI dari `localStorage` → `.zai-token` → `chmod 600`.
5. Build `zai-api` (Go) → `./start-glm.sh` dengan `GLM_HOST=127.0.0.1`,
   `GLM_AUTH_TOKEN` kuat → cek `GET :20234/health`.
6. Jalankan `gateway.py` dengan `GATEWAY_TOKENS` acak.
7. `GW=... GATEWAY_TOKEN=... python3 selfcheck.py` → harus 0 gagal.
8. Baru bagikan base URL + token gateway ke agen lain.

Kalau langkah 4–5 gagal karena tidak ada browser/Go, lewati GLM dulu:
gateway tetap hidup dengan satu upstream (`deepseek-free`) dan
`/v1/models` hanya berisi model DeepSeek.

---

## 7. Yang belum tercakup

- Provider lain (Gemini/Claude web) **belum** ada di gateway ini; jangan
  mengklaim sebaliknya.
- Vision: relay DeepSeek tidak mengirim gambar; GLM mendukung upload gambar
  tapi gateway belum meneruskan `image_url` dengan benar.
- Rotasi token otomatis belum ada; kedaluwarsa harus ditangani operator.
- Belum ada systemd/autostart; proses mati kalau HP reboot, jalankan
  `./start.sh` lagi.