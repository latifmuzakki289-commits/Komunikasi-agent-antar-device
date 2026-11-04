# DeepSeek Web Free + GLM Web Free Gateway

Gabungan relay DeepSeek, bridge GLM, dan OpenAI-compatible gateway agar agen
seperti Hermes dapat mengakses model lewat satu base URL. **Source code saja**;
credential web tidak disertakan.

## Struktur

- `gateway.py`, `selfcheck.py`, `start.sh` — gateway gabungan dan pemeriksaan.
- `dsh-better-bridge/relay/` — relay DeepSeek web berbasis sesi akun sendiri.
- `glm-free-api/` — source bridge Z.AI/GLM dan token collector, berdasar proyek
  [GLM-Free-API](https://github.com/izaart95-jpg/GLM-Free-API).
- `docs/PELAJARAN-PEMASANGAN.md` — catatan teknis hasil pemasangan.

## Persyaratan

- Python 3.10+ dan `pip install -r requirements.txt`
- Go sesuai `glm-free-api/go.mod` untuk membangun bridge GLM
- Login pengguna sendiri ke chat.deepseek.com dan chat.z.ai. Jangan commit
  token atau cookie.

## Setup ringkas

1. DeepSeek: simpan token dan cookie browser ke
   `dsh-better-bridge/relay/.websession.json` dengan bentuk
   `{"token":"...","cookies":{}}`; batasi permission (`chmod 600`).
2. GLM: simpan JWT web ke `glm-free-api/.zai-token` (`chmod 600`). Siapkan
   token device melalui `token-collector` sesuai README GLM. Salin source
   build dari laptop/host yang memiliki versi Go yang cocok:

   ```sh
   cd glm-free-api
   go build -o zai-api .
   ```

3. Buat token klien lokal di `glm-free-api/.glm-auth-token`, lalu jalankan
   `./start-glm.sh` atau gunakan `./start.sh` setelah binary dan kredensial
   siap. Token tersebut untuk autentikasi ke bridge, bukan token akun.
4. Lindungi gateway bila dibuka ke LAN: tetapkan token acak kuat, jangan pakai
   nilai contoh.

## Hermes

Gunakan provider OpenAI-compatible:

```text
Base URL: http://127.0.0.1:8080/v1
API key : token gateway yang Anda tetapkan
Model   : deepseek-chat atau glm-5.3 (cek GET /v1/models)
```

Jika Hermes berjalan di perangkat berbeda, gunakan IP LAN host gateway dan
pastikan host mengizinkan koneksi port 8080. Untuk repo GitHub private, akun
Hermes/operator perlu mendapat izin baca; GitHub tidak membuat repo private
terbaca anonim.

## File rahasia yang tidak boleh masuk Git

`.websession.json`, `.zai-token`, `.glm-auth-token`, database token, log,
pidfile, binary hasil build, `.env`, dan backup credential. `.gitignore`
repo ini mengecualikan semuanya. Tetap periksa `git status` sebelum push.

## Catatan lisensi

Komponen berasal dari beberapa proyek open source. Lihat lisensi masing-masing
komponen di subfolder dan pertahankan atribusi serta lisensinya.
