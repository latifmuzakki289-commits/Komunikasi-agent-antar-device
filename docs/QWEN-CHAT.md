# Qwen Chat (chat.qwen.ai) lewat relay2

Backend ketiga setelah DeepSeek dan GLM. Agen memanggil `model: "qwen-chat"`
ke relay `http://127.0.0.1:20232/v1`. Tanpa API key resmi: memakai sesi akun
sendiri yang diambil dari browser.

Diverifikasi hidup 2026-11-12, frontend Qwen versi 0.3.40.

## File

- `dsh-better-bridge/relay/qwen_brain.py` : modul Qwen (sesi, token, direct, bridge).
- `dsh-better-bridge/relay/relay2.py` : dispatch provider, rute `/session` dan `/qwen/bridge`.
- `dsh-better-bridge/userscripts/qwen-token-relay.user.js` : Violentmonkey, jalan di tab `chat.qwen.ai`.
- `dsh-better-bridge/relay/start-relay.bat` : launcher Windows (ada guard port).

## Setup

1. `pip install curl_cffi` (TLS Firefox, perlu untuk jalur direct).
2. Jalankan relay: `python relay2.py` atau `start-relay.bat`.
3. Pasang `qwen-token-relay.user.js` di Violentmonkey, buka `chat.qwen.ai`, login.
4. Cek panel userscript: `sesi tersinkron #N`. Log relay: `qwen session push ... exp_in`.
5. Uji:

```sh
curl http://127.0.0.1:20232/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen-chat","stream":false,"messages":[{"role":"user","content":"ping"}]}'
```

`qwen-chat` dan `qwen` dipetakan ke `qwen3.7-plus`. Id lain diteruskan apa adanya
(`qwen3.8-max`, `qwen3.7-max`, `qwen3.6-plus`, dst; lihat `GET /api/models`).

## Dua jalur transport

- direct : relay memanggil Qwen sendiri via `curl_cffi` + cookie WAF dari tab. Cepat, tidak perlu tab terbuka saat request, tapi butuh token push yang masih hidup.
- bridge : relay menaruh job di antrean, userscript di tab mengambil lalu `fetch` dari dalam halaman. Kebal fingerprint, tapi butuh tab terbuka.

Bridge dicoba lebih dulu kalau ada tab yang polling. Direct jadi cadangan.

## Pelajaran penting (urut dari yang paling mahal)

1. Header `Version: 0.3.40` wajib. Tanpa itu completion membalas
   `Bad_Request - Internal error...` untuk body yang benar. Body identik
   dengan header itu langsung berhasil. Header ini disetel interceptor axios
   frontend (`L1`), bersama `Timezone`, `source: web`, `X-Request-Id`.
   `Timezone` tidak wajib. Hampir semua iterasi body di awal mengejar hantu.
   Kalau Qwen merilis versi baru, set `QWEN_APP_VERSION`.
2. Pesan error menyesatkan. Validasi body mengeluh field yang kurang, tapi
   header salah hanya menghasilkan `Internal error...`. Bandingkan header dulu.
3. Chat harus dibuat dulu. `POST /api/v2/chats/new` dengan
   `{chatId:"", models:[id], timestamp:<ms>, chat_type:"t2t", chat_mode:"normal"}`
   mengembalikan `data.id`, dipakai sebagai `chat_id`. `/api/chats/new` (tanpa v2) = 500.
4. Dua token, jangan tertukar:
   - token push (~13 menit) dari userscript : inilah yang diterima completion.
   - token 30 hari dari `GET /api/v1/auths/` : ditolak completion (401). Hanya cadangan, disimpan sebagai `long_token`, tidak boleh menimpa token push.
   Bug awal: `refresh_token()` menimpa token valid dengan token 30 hari, jadi direct selalu 401.
5. Server kadang membalas HTTP 200 dengan envelope `{"code":"unauthorized"}`, bukan 401. Parser harus memeriksa isi.
6. Token push mati kalau tab diam. App hanya me-refresh saat ada aktivitas. Userscript v1.6.0 memanggil `GET https://auth.qwen.ai/api/v2/auths/refresh` (cookie sesi, header `x-request-origin`) saat sisa token < 120 detik. Dari terminal endpoint itu 401, jadi refresh hanya bisa dari browser.
7. SSE: Qwen mengirim delta lalu satu entri penuh. Menjumlahkan keduanya menggandakan jawaban. Pakai delta, teks penuh hanya kalau tidak ada delta. Fase `think` dilewati.
8. Timestamp: body completion pakai detik, `/chats/new` pakai milidetik.
9. Firefox + Violentmonkey: sniffer harus dipasang di `unsafeWindow`. `window.fetch` di sandbox bukan fetch halaman.
10. WAF: `/api/v2/chat/completions` ditantang tanpa cookie `x5sec`/`acw_sc__v2` + TLS browser. `/api/models`, `/api/v1/auths/`, `/api/v2/chats/*` tidak. Jangan banjiri direct setelah ditantang; ada cooldown `QWEN_WAF_COOLDOWN`.
11. Windows: `SO_REUSEADDR` membolehkan dua relay bind port yang sama. `_port_busy()` mencegahnya.

## Bentuk body completion (hasil tangkapan app)

Kunci: `stream, version("2.1"), incremental_output, chatId, parentId(""), chat_id, chat_mode, model, parent_id(null), timestamp, messages`.
Tidak ada field `headers`. Pesan membawa `fid, role, content, user_action:"chat", files:[], timestamp, models:[model], model:"", chat_type:"t2t", feature_config{...}, extra.meta.subChatType, sub_chat_type`.

## Variabel lingkungan

`QWEN_SESSION_FILE`, `QWEN_IMPERSONATE` (default `firefox`), `QWEN_MODEL`,
`QWEN_TIMEOUT`, `QWEN_BRIDGE_TIMEOUT`, `QWEN_WAF_COOLDOWN`, `QWEN_APP_VERSION`.

## Keamanan

- `.websession-qwen.json` berisi token dan cookie akun. Sudah di `.gitignore`. Jangan commit, jangan tempel ke chat atau issue.
- Panel userscript menampilkan JSON sesi lengkap. Bagikan baris status saja.
- Relay bind `127.0.0.1` tanpa autentikasi. Jangan buka ke LAN tanpa token.
- Memakai sesi web konsumen bisa melanggar ketentuan layanan Qwen dan akun bisa kena batas atau blokir. Pakai akun sendiri, secukupnya.

## Pemecahan masalah

| Gejala | Penyebab | Tindakan |
|---|---|---|
| `Bad_Request - Internal error...` | header `Version` hilang/usang | cek `APP_VERSION` vs versi frontend |
| `unauthorized` / 401 | token push basi | buka tab `chat.qwen.ai`, cek `exp_in` di log |
| `tidak ada tab yang polling bridge` | tab tertutup / userscript mati | buka tab, reload |
| respons HTML / `x5secdata` | cookie WAF basi | reload tab agar cookie baru terdorong |
| jawaban dobel | parser SSE lama | pakai `_sse_text` versi delta-first |
| DeepSeek `session_expired` 40003 | token DeepSeek basi | login ulang di tab DeepSeek |
