# ANALIS-BOT

Scouting koin otomatis tiap 4 jam. Hasil dikirim ke Discord (dan Telegram jika diaktifkan).

Output ini adalah hasil analisis otomatis, bukan saran investasi.

## Struktur
- `analis.py` : logika scouting
- `config.json` : semua filter, bobot skor, dan sumber API
- `.github/workflows/analis.yml` : jadwal GitHub Actions

## Setup
1. Buat webhook di Discord: Server Settings → Integrations → Webhooks → New Webhook, pilih channel, lalu Copy Webhook URL.
2. Di GitHub: Settings → Secrets and variables → Actions → New repository secret.
   - Nama: `DISCORD_WEBHOOK_URL`
   - Isi: URL webhook tadi
3. (Opsional) `CRYPTOQUANT_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Tes lewat Actions → analis-4h → Run workflow.

Webhook URL jangan ditaruh di `config.json` atau kode. Kalau pernah terlihat orang lain, hapus webhook itu di Discord dan buat yang baru.

## Mengubah parameter
Cukup edit `config.json`, misalnya `filters.corr_min` atau `order.tp_pct`. Tidak perlu mengubah kode.
