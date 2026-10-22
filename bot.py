#!/usr/bin/env python3
"""bot.py - dijalankan tiap jam.
1. Lacak posisi simulasi yang sudah ada (harga realtime).
2. Nilai top koin market cap dengan RSI 1H, beri grade, tampilkan ranking terbaik.
3. Breakout yang lolos: SINYAL BARU untuk slot kosong, CALON ENTRY untuk kandidat berikutnya
   (tampilan saja, posisi tidak dibuka).
Output adalah hasil analisis otomatis, BUKAN saran investasi."""

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

import core as C

CFG = C.load_config()
IND, RSI_, RK, CO = CFG["indicators"], CFG["rsi"], CFG["risk"], CFG["costs"]
LQ, UNI, DLY, NOTIFY, REG = (CFG["liquidity"], CFG["universe"], CFG["delays"],
                             CFG["notify"], CFG["regime"])
EXCL = {s.upper() for s in UNI["exclude_symbols"]}
STABLE = {s.lower() for s in UNI["stablecoins"]}

DATA = C.BASE / "data"
STATE, TRADES = DATA / "state.json", DATA / "trades.csv"
RESULTS = C.BASE / "results"
TRADE_FIELDS = ["symbol", "exchange", "entry_utc", "exit_utc", "entry_fill", "slip_in_pct",
                "exit_price", "reason", "bars", "qty", "risk_usd", "gross_pct", "net_pct",
                "r_multiple", "pnl_usd", "equity_after"]
MAX_DEPTH_CALLS = 15

DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


# ================= DATA =================
def top_coins():
    """Top N market cap dari CoinGecko, tanpa stablecoin dan koin yang dikecualikan."""
    coins, page = [], 1
    while len(coins) < UNI["top_n"]:
        data = C.http_get(CFG["sources"]["coingecko"] + "coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc", "per_page": 100, "page": page})
        if not data:
            break
        coins += data
        page += 1
        time.sleep(DLY["coingecko_page_sec"])
    out = []
    for c in coins:
        rank = c.get("market_cap_rank")
        sym = c["symbol"].upper()
        if rank is None or rank > UNI["top_n"]:
            continue
        if c["symbol"].lower() in STABLE or sym in EXCL:
            continue
        out.append({"rank": rank, "symbol": sym})
    return out


def scan(mk, listed, vols, now_ms):
    """Nilai koin yang listing di minimal satu bursa dan lolos likuiditas."""
    rows = []
    stats = {"tanpa_bursa": 0, "likuiditas": 0, "gagal": 0}
    for coin in top_coins():
        base = coin["symbol"]
        if base not in listed:
            stats["tanpa_bursa"] += 1
            continue
        if vols.get(base, 0) < LQ["min_quote_volume_24h_usd"]:
            stats["likuiditas"] += 1
            continue
        try:
            cs, src = mk.klines(base, IND["candles"])
        except Exception as e:
            stats["gagal"] += 1
            print(f"[skip] {base}: {e}")
            continue
        time.sleep(DLY["request_sec"])
        ev = C.evaluate(cs, IND, RSI_, now_ms)
        if ev:
            rows.append({"rank": coin["rank"], "symbol": base, "src": src, **ev})
    return rows, stats


# ================= STATE & CSV =================
def load_state():
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
        st.setdefault("positions", [])
        st.setdefault("cooldown", {})
        return st
    return {"equity_usd": RK["equity_usd"], "positions": [], "cooldown": {}}


def save_state(st):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def append_trade(rec):
    DATA.mkdir(parents=True, exist_ok=True)
    new = not TRADES.exists() or TRADES.stat().st_size == 0
    with TRADES.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(rec)


def save_ranking_csv(rows):
    RESULTS.mkdir(exist_ok=True)
    ordered = sorted(rows, key=lambda r: (-r["grade"], r["rsi"]))
    with (RESULTS / "ranking.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["rank_mcap", "symbol", "bursa", "rsi", "grade", "label", "breakout"])
        for r in ordered:
            w.writerow([r["rank"], r["symbol"], r["src"], f"{r['rsi']:.2f}", r["grade"],
                        r["label"], "ya" if r["sig"] else "tidak"])


# ================= FORMAT & NOTIFIKASI =================
def price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def blok_entry(judul, p):
    return [
        judul,
        f"GRADE          : {p['grade_label'].lower()} ({p['grade']})",
        f"NAMA KOIN      : {p['symbol']}/USDT",
        f"HARGA REF      : {price(p['mid'])}",
        f"OPEN POSISI    : {price(p['entry_fill'])} (fill order book)",
        f"SLIPPAGE MASUK : {p['slip_in_pct']:+.3f}%",
        f"STOP LOSS      : {price(p['sl'])}",
        f"TAKE PROFIT    : {price(p['tp'])}",
        "",
    ]


def kirim_discord(text):
    if not (NOTIFY["discord"] and DISCORD_URL):
        return
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > NOTIFY["discord_max_chars"] and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    for part in parts:
        payload = {"username": NOTIFY["discord_username"], "content": f"```\n{part}\n```"}
        for _ in range(3):
            try:
                r = requests.post(DISCORD_URL, json=payload, timeout=15)
            except requests.RequestException as e:
                print(f"[warn] Discord gagal ({type(e).__name__})")
                break
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            if r.status_code >= 400:
                print(f"[warn] Discord HTTP {r.status_code}")
            break


def kirim_telegram(text):
    if not (NOTIFY["telegram"] and TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"[warn] Telegram gagal ({type(e).__name__})")


# ================= ENTRY =================
def pilih_entry(mk, ready, st, free, now_ms):
    """Kandidat berurutan. Slot kosong -> SINYAL BARU. Setelah itu 'preview_top' kandidat
    berikutnya -> CALON ENTRY (tidak dibuka, tidak masuk state)."""
    entries, previews, depth_calls = [], [], 0
    for r in ready:
        if len(entries) >= free and len(previews) >= RSI_["preview_top"]:
            break
        if depth_calls >= MAX_DEPTH_CALLS:
            break
        depth_calls += 1
        try:
            (bids, asks), src = mk.depth(r["symbol"], LQ["depth_levels"], prefer=r["src"])
        except Exception:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        pos, err = C.plan_entry(r["symbol"], r["sig"], mid, asks, st["equity_usd"], RK, now_ms, src)
        if err:
            continue
        pos["grade"], pos["grade_label"] = r["sig"]["grade"], r["sig"]["label"]
        if len(entries) < free:
            entries.append(pos)
            st["positions"].append(pos)
        elif len(previews) < RSI_["preview_top"]:
            previews.append(pos)
    return entries, previews


# ================= UTAMA =================
def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    wib = now + timedelta(hours=7)
    print(f"=== analis 1 jam | {now:%Y-%m-%d %H:%M} UTC ===")

    st = load_state()
    mk = C.Market(CFG["sources"])
    listed = mk.init()
    if not listed:
        raise RuntimeError("tidak ada bursa yang bisa diakses")

    # 1. Lacak posisi terbuka (realtime, bursa yang sama lebih dulu)
    exits, still = [], []
    for pos in st["positions"]:
        try:
            tr = C.track(mk, pos, now_ms, RK, CO)
        except Exception as e:
            print(f"[warn] tracking {pos['symbol']} gagal: {e}")
            still.append(pos)
            continue
        if tr:
            st["equity_usd"] += tr["pnl_usd"]
            tr["equity_after"] = round(st["equity_usd"], 2)
            append_trade(tr)
            st["cooldown"][pos["symbol"]] = tr["exit_ms"]
            exits.append(tr)
        else:
            still.append(pos)
    st["positions"] = still

    # 2. Regime BTC (1H, realtime)
    try:
        btc, _ = mk.klines("BTC", IND["candles"], prefer="binance")
        reg_ok = C.regime_ok(btc, REG["btc_ema"])
        reg_txt = "ON" if reg_ok else "OFF"
    except Exception as e:
        reg_ok, reg_txt = False, f"GAGAL ({e})"

    # 3. Nilai semua koin
    vols = mk.volumes()
    rows, stats = scan(mk, listed, vols, now_ms)
    print(f"Koin dinilai: {len(rows)} | tanpa bursa: {stats['tanpa_bursa']} | "
          f"likuiditas rendah: {stats['likuiditas']} | gagal: {stats['gagal']}")
    print(f"Candle/order book tidak wajar dibuang: {mk.report()}")
    top = sorted((r for r in rows if r["grade"] >= RSI_["grade_min"]),
                 key=lambda r: (-r["grade"], r["rsi"]))[:RSI_["show_top"]]
    save_ranking_csv(rows)

    # 4. Kandidat breakout: diurutkan grade, lalu kekuatan breakout
    held = {p["symbol"] for p in st["positions"]}
    cool_ms = RK["cooldown_bars"] * C.HOUR_MS
    ready = []
    if reg_ok:
        ready = sorted((r for r in rows
                        if r["sig"] is not None
                        and r["symbol"] not in held
                        and now_ms - st["cooldown"].get(r["symbol"], -10**18) >= cool_ms),
                       key=lambda r: (r["sig"]["grade"], r["sig"]["strength"]), reverse=True)
    free = max(RK["max_positions"] - len(st["positions"]), 0)
    entries, previews = pilih_entry(mk, ready, st, free, now_ms) if ready else ([], [])
    save_state(st)

    # 5. Laporan
    n_breakout = sum(1 for r in rows if r["sig"] is not None)
    lines = [
        f"ANALIS 1 JAM {now:%Y-%m-%d %H:%M} UTC ({wib:%H:%M} WIB)",
        f"Regime BTC: {reg_txt} | Equity: ${st['equity_usd']:,.2f} | "
        f"Posisi terbuka: {len(st['positions'])}/{RK['max_positions']}",
        f"Universe: top {UNI['top_n']} market cap | dinilai {len(rows)} | "
        f"tanpa bursa {stats['tanpa_bursa']} | likuiditas rendah {stats['likuiditas']} | "
        f"gagal {stats['gagal']}",
        f"Breakout lolos filter: {n_breakout} | calon entry tampil: {len(previews)} "
        f"(preview_top={RSI_['preview_top']})",
        "",
        f"RANKING RSI (grade {RSI_['grade_min']}-10 | RSI {RSI_['min']}-{RSI_['max']}, "
        f"di atas {RSI_['max']} dibatalkan)",
    ]
    if top:
        lines.append(f"{'#':>2}  {'KOIN':<6} {'RSI':>5}  {'GRADE':>5}  STATUS")
        for i, r in enumerate(top, 1):
            lines.append(f"{i:>2}  {r['symbol']:<6} {r['rsi']:>5.1f}  {r['grade']:>2}/10  {r['label']}")
    else:
        lines.append(f"Tidak ada koin dengan grade >= {RSI_['grade_min']} saat ini.")
    lines.append("")
    if not reg_ok:
        lines += ["Entry dinonaktifkan karena regime BTC tidak ON.", ""]
    for p in entries:
        lines += blok_entry("SINYAL BARU", p)
    for p in previews:
        lines += blok_entry("CALON ENTRY", p)
    for t in exits:
        lines.append(f"TRADE SELESAI  {t['symbol']} | {t['reason']} | net {t['net_pct']:+.2f}% | "
                     f"R {t['r_multiple']:+.2f} | equity ${t['equity_after']:,.2f}")
    if not entries and not exits and not previews:
        lines.append("Tidak ada breakout baru atau posisi yang selesai.")
    text = "\n".join(lines)

    print("\n" + text)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.txt").write_text(text, encoding="utf-8")

    if top or entries or exits or previews or NOTIFY["send_when_empty"]:
        kirim_discord(text)
        kirim_telegram(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        raise SystemExit(1)
