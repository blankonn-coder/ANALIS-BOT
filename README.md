markdown
# Scout_ndul

Scouting koin otomatis 4H. Output 2 HIGH / 2 MEDIUM / 2 LOW.

## Setup
1. `pip install requests`
2. Sesuaikan `config.json`.
3. Set env: `DISCORD_WEBHOOK_URL`, opsional `CRYPTOQUANT_API_KEY`,
   `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
4. `DRY_RUN=1 python3 analis.py` untuk uji tanpa kirim notifikasi.

Output tersimpan di `results/`.

## Disclaimer
Hasil analisis otomatis, BUKAN saran investasi.
