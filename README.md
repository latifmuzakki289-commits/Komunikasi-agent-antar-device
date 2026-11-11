# Free Web LLM Gateway

Menghubungkan agen AI (Hermes, Cline, OpenCode, SDK OpenAI, dan sejenisnya) ke
chat web **DeepSeek**, **GLM (Z.AI)**, dan **Qwen** lewat API yang kompatibel
dengan OpenAI (`/v1/chat/completions`). Memakai sesi login akun sendiri, bukan
API key resmi.

> **Hanya source code.** Token, cookie, dan database sesi tidak disertakan dan
> tidak boleh masuk Git.

## Isi

| Backend | Model | Port | Komponen |
|---|---|---|---|
| DeepSeek web | `deepseek-chat` | 20232 | `dsh-better-bridge/relay/relay2.py` |
| Qwen Chat | `qwen-chat` (alias `qwen3.7-plus`) dan id Qwen lain | 20232 | `relay2.py` + `qwen_brain.py` |
| GLM (Z.AI) | `glm-5.3`, `glm-5.2`, `glm-5.3-flash`, `glm-4.7` | 20234 | `glm-free-api/` (Go) |
| Gateway gabungan | semua di atas | 8080 | `gateway.py` |

DeepSeek dan Qwen berbagi satu relay (`relay2.py`). Relay memilih backend dari
awalan nama model: `qwen*` ke Qwen, selain itu ke DeepSeek.

```text
Agen (Hermes / Cline / ...)
        |  OpenAI /v1/chat/completions
        v
  gateway.py :8080 --------+-----------------------+
                           |                       |
                           v                       v
                  relay2.py :20232          zai-api :20234
                  DeepSeek  |  Qwen              GLM
                           ^
                           | token + cookie dari browser
                  userscript (Violentmonkey)
```

## Persyaratan

- Python 3.10+ dan `pip install -r requirements.txt`
- Qwen: `pip install curl_cffi` (sidik TLS browser; diuji dengan 0.17.0)
- GLM: Go sesuai `glm-free-api/go.mod`
- Akun sendiri di chat.deepseek.com, chat.qwen.ai, dan/atau chat.z.ai
- Qwen dan DeepSeek: browser dengan Violentmonkey/Tampermonkey

## Mulai cepat

### Qwen (diuji di Windows)

1. Jalankan relay: `python dsh-better-bridge/relay/relay2.py`, atau klik
   `dsh-better-bridge/relay/start-relay.bat`.
2. Pasang `dsh-better-bridge/userscripts/qwen-token-relay.user.js` di
   Violentmonkey, buka `chat.qwen.ai`, login, biarkan tab terbuka.
3. Cek panel userscript menampilkan `sesi tersinkron`, lalu uji:

```sh
curl http://127.0.0.1:20232/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-chat","stream":false,"messages":[{"role":"user","content":"ping"}]}'
```

Panduan lengkap, daftar model, dan pelajaran teknis: [`docs/QWEN-CHAT.md`](docs/QWEN-CHAT.md).

### DeepSeek

Simpan token dan cookie browser ke `dsh-better-bridge/relay/.websession.json`
dengan bentuk `{"token":"...","cookies":{}}` (`chmod 600`), lalu jalankan
`relay2.py`. Token DeepSeek bisa berputar sendiri. Relay menerima token baru
lewat `POST /session` dan membaca ulang file sesi saat berubah, tetapi
userscript pendorong untuk DeepSeek **tidak disertakan** di repo ini.

### GLM

1. Simpan JWT web ke `glm-free-api/.zai-token` (`chmod 600`).
2. Siapkan token device lewat `token-collector` (lihat `glm-free-api/README.md`).
3. Bangun bridge: `cd glm-free-api && go build -o zai-api .`
4. Buat token klien lokal di `glm-free-api/.glm-auth-token`, lalu jalankan
   `./start-glm.sh`.

### Gateway gabungan

```sh
export GATEWAY_TOKENS="isi-token-acak-yang-kuat"
./start.sh          # relay + bridge GLM + gateway
./start.sh stop
```

`start.sh` menolak jalan tanpa `GATEWAY_TOKENS`. Port dan upstream ada di
`gateway.py` (`DEFAULT_UPSTREAMS`).

> **Catatan Qwen:** `DEFAULT_UPSTREAMS` belum memuat Qwen, jadi `GET /v1/models`
> di gateway tidak menampilkannya. Pakai relay langsung di
> `http://127.0.0.1:20232/v1` untuk Qwen. Request `qwen-*` lewat gateway belum
> saya uji.

## Pemakaian di agen

```text
Base URL: http://127.0.0.1:8080/v1      (gateway)  atau
          http://127.0.0.1:20232/v1     (relay langsung, wajib untuk Qwen)
API key : token gateway yang Anda tetapkan
Model   : deepseek-chat | glm-5.3 | qwen-chat   (cek GET /v1/models)
```

Qwen mendukung streaming: proses berpikir dan status tool bawaan Qwen dikirim
sebagai `reasoning_content`, jawaban sebagai `content`. Tool call dari agen
tetap berjalan lewat format `<tool_call>` di prompt.

## Dokumentasi

- [`docs/QWEN-CHAT.md`](docs/QWEN-CHAT.md): setup Qwen, transport, token, streaming, troubleshooting.
- [`docs/PELAJARAN-PEMASANGAN.md`](docs/PELAJARAN-PEMASANGAN.md): catatan pemasangan DeepSeek dan GLM.
- [`docs/PROMPT-SHARE.md`](docs/PROMPT-SHARE.md): prompt siap tempel agar agen lain memakai gateway ini.

## Struktur repo

```text
gateway.py, selfcheck.py, start.sh, requirements.txt
dsh-better-bridge/
  relay/            relay2.py, qwen_brain.py, extract.py, start-relay.bat
  userscripts/      qwen-token-relay.user.js
glm-free-api/       bridge Z.AI/GLM (Go) dan token collector
docs/               panduan dan catatan teknis
```

## Keamanan

- Relay dan gateway dirancang untuk `127.0.0.1`. Relay `:20232` **tidak punya
  autentikasi**. Jangan buka ke LAN atau internet tanpa lapisan token di depannya.
- File yang tidak boleh masuk Git: `.websession.json`, `.websession-qwen.json`,
  `.zai-token`, `.glm-auth-token`, `.env`, log, pidfile, binary hasil build, dan
  backup credential. `.gitignore` mengecualikan semuanya; tetap periksa
  `git status` sebelum push.
- Panel userscript Qwen menampilkan JSON sesi lengkap (token dan cookie).
  Jangan menempelkannya ke chat, issue, atau tangkapan layar.
- Kalau token pernah terlanjur dibagikan, logout lalu login ulang di situs
  terkait untuk merotasi sesi.

## Batasan yang perlu diketahui

- **Ketentuan layanan.** Memakai sesi web konsumen lewat skrip mungkin
  melanggar ketentuan DeepSeek, Alibaba (Qwen), atau Z.AI. Akun bisa dibatasi
  atau diblokir. Pakai akun sendiri dan secukupnya. Proyek ini bukan produk
  resmi dan tidak berafiliasi dengan penyedia mana pun.
- **Rapuh terhadap perubahan situs.** Qwen mewajibkan header `Version` yang
  mengikuti versi frontend (0.3.40 saat diuji); versi baru bisa memutus
  request sampai `QWEN_APP_VERSION` diperbarui. WAF Alibaba bisa menantang
  request jalur direct.
- **Token Qwen pendek** (sekitar 13 menit) dan bergantung pada tab yang terbuka.
  Refresh otomatis di userscript belum teruji di browser.
- Relay memproses satu request completion pada satu waktu (satu `LOCK`).
- Belum teruji: tampilan `reasoning_content` di semua klien, dan tool call
  agen lewat jalur Qwen yang menahan jawaban.

## Lisensi

Belum ada `LICENSE` di root repo. Komponen berasal dari proyek lain dan membawa
lisensi MIT masing-masing; pertahankan atribusinya:

- `dsh-better-bridge/LICENSE`: MIT, Samir Jamal
- `glm-free-api/LICENSE`: MIT, Lelouch (berdasar
  [GLM-Free-API](https://github.com/izaart95-jpg/GLM-Free-API))

Kode Qwen (`qwen_brain.py`, userscript, dan perubahan pada `relay2.py`) belum
punya lisensi tersendiri.
