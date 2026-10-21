# ANALIS-BOT

Scouting koin otomatis tiap 1 jam. Bot menilai top 300 koin market cap dengan indikator RSI (candle 4H yang sudah close), memberi grade 1-10, lalu menampilkan koin dengan grade terbaik (default grade 6-10, maksimal 10 koin). Hasil dikirim ke Discord (dan Telegram jika diaktifkan).

## Aturan RSI
- RSI 35-45: sangat direkomendasikan (label SANGAT REKOMENDASI).
- RSI 45-60: rekomendasi biasa, grade turun sesuai kenaikan RSI.
- RSI 60-70: titik jenuh, koin dibatalkan (grade 0, tidak ditampilkan dan tidak masuk entry).
- RSI di luar 35-70: tidak dinilai.
- Skala grade: RSI 35 = 10 poin, RSI 70 = 1 poin, dibulatkan ke bilangan bulat.
- Hasil lengkap semua koin tersimpan di `results/ranking.csv`.

Breakout simulasi tetap menolak RSI jenuh atau di luar 35-70. Kandidat yang lolos diurutkan dari grade tertinggi. Ambang entry bisa diatur lewat `rsi.entry_grade_min` (default 1). Catatan: breakout biasanya membuat RSI naik, jadi jika ambang entry dinaikkan ke 6, simulasi hampir tidak akan pernah entry.

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
Cukup edit `config.json`, misalnya `universe.top_n`, `rsi.min`, `rsi.max`, `rsi.grade_min`, atau `rsi.show_top`. Tidak perlu mengubah kode.
