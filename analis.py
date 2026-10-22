#!/usr/bin/env python3
"""
analis.py - dijalankan tiap 1 jam.
1. Lacak posisi yang sudah ada (state di data/state.json, riwayat di data/trades.csv).
2. Nilai top 300 koin market cap dengan RSI (candle 4H close), beri grade 1-10.
   Koin dengan grade terbaik (default 6-10) ditampilkan, maksimal 10 koin.
3. Entry simulasi hanya untuk breakout yang juga lolos grade RSI, dengan slippage order book.
Output adalah hasil analisis otomatis, BUKAN saran investasi.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests

import core as C

cfg = C.load_config()
S, RK, CO = cfg["signal"], cfg["risk"], cfg["costs"]
LQ, UNI, DLY, NOTIFY, R = (cfg["liquidity"], cfg["universe"], cfg["delays"],
                           cfg["notify"], cfg["rsi"])
EXCL = {s.upper() for s in UNI["exclude_symbols"]}
STABLE = {s.lower() for s in UNI["stablecoins"]}

STATE = C.BASE_DIR / "data" / "state.json"
TRADES = C.BASE_DIR / "data" / "trades.csv"
RESULTS = C.BASE_DIR / "results"

DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def top_coins():
    """Top N market cap dari CoinGecko, tanpa stablecoin."""
    coins, page = [], 1
    while len(coins) < UNI["top_n"]:
        data = C.http_get(cfg["sources"]["coingecko"] + "coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 100, "page": page})
        if not data:
            break
        coins += data
        page += 1
        time.sleep(DLY["coingecko_page_sec"])
    res = []
    for c in coins:
        rank = c.get("market_cap_rank")
        if rank is None or rank > UNI["top_n"]:
            continue
        if c["symbol"].lower() in STABLE or c["symbol"].upper() in EXCL:
            continue
        res.append({"rank": rank, "symbol": c["symbol"].upper()})
    return res


def price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def track(bn, pos, now_ms):
    """Cek candle 4H yang sudah close sejak posisi dibuka. Mengembalikan trade jika selesai."""
    cs = C.closed_only(bn.klines(pos["symbol"], limit=500,
                                 start_ms=pos["track_from_ms"]), now_ms)
    for c in cs:
        if c["t"] < pos["track_from_ms"] or c["t"] <= pos["last_ms"]:
            continue
        pos["bars"] += 1
        pos["last_ms"] = c["t"]
        res = C.check_exit(pos, c, pos["bars"], RK["max_hold_bars"], CO["exit_slip_bps"])
        if res:
            px, reason = res
            return C.settle(pos, px, reason, c["close_ms"], CO["fee_bps"])
    return None


def scan_universe(bn, pairs, vols, now_ms):
    """RSI + grade untuk top N koin yang punya pasangan USDT dan lolos likuiditas."""
    rows, no_pair, illiquid, failed = [], 0, 0, 0
    for coin in top_coins():
        pair = coin["symbol"] + "USDT"
        if pair not in pairs:
            no_pair += 1
            continue
        if vols.get(pair, 0) < LQ["min_quote_volume_24h_usd"]:
            illiquid += 1
            continue
        try:
            cs = C.closed_only(bn.klines(pair, limit=S["candles"]), now_ms)
        except Exception as e:
            failed += 1
            print(f"[skip] {pair}: {type(e).__name__}")
            continue
        time.sleep(DLY["request_sec"])
        info = C.rsi_info(cs, S, R)
        if info is None:
            continue
        rows.append({
            "rank": coin["rank"], "symbol": coin["symbol"], "pair": pair,
            "rsi": info["rsi"], "grade": info["grade"], "label": info["label"],
            "sig": C.signal(cs, S, R),
        })
    stats = {"dinilai": len(rows), "tanpa_usdt": no_pair,
             "likuiditas_rendah": illiquid, "gagal_ambil": failed}
    return rows, stats


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


def simpan_ranking_csv(rows):
    """Seluruh hasil penilaian (bukan hanya top) untuk ditelusuri."""
    import csv
    RESULTS.mkdir(exist_ok=True)
    ordered = sorted(rows, key=lambda r: (-r["grade"], r["rsi"]))
    with (RESULTS / "ranking.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank_mcap", "symbol", "rsi", "grade", "label", "breakout"])
        for r in ordered:
            w.writerow([r["rank"], r["symbol"], f"{r['rsi']:.2f}", r["grade"], r["label"],
                        "ya" if r["sig"] else "tidak"])


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    wib = now + timedelta(hours=7)
    print(f"=== analis 1 jam | {now:%Y-%m-%d %H:%M} UTC ===")

    st = C.load_state(STATE, RK["equity_usd"])
    bn = C.Binance(cfg["sources"]["binance"])
    pairs = bn.init()

    # 1. Lacak posisi yang sudah ada
    exits, still = [], []
    for pos in st["positions"]:
        try:
            tr = track(bn, pos, now_ms)
        except Exception as e:
            print(f"[warn] tracking {pos['symbol']} gagal: {type(e).__name__}")
            still.append(pos)
            continue
        if tr:
            st["equity_usd"] += tr["pnl_usd"]
            tr["equity_after"] = round(st["equity_usd"], 2)
            C.append_trade(TRADES, tr)
            st["cooldown"][pos["symbol"]] = tr["exit_ms"]
            exits.append(tr)
        else:
            still.append(pos)
    st["positions"] = still

    # 2. Regime BTC
    btc = C.closed_only(bn.klines("BTCUSDT", limit=S["candles"]), now_ms)
    reg_ok = C.regime_ok(btc, cfg)
    print(f"Regime BTC: {'ON' if reg_ok else 'OFF'}")

    # 3. Nilai semua koin (ranking RSI) - tetap jalan walau regime OFF
    vols = bn.quote_volume_24h()
    rows, stats = scan_universe(bn, pairs, vols, now_ms)
    print(f"Koin dinilai: {stats['dinilai']} | tanpa USDT: {stats['tanpa_usdt']} | "
          f"likuiditas rendah: {stats['likuiditas_rendah']} | gagal: {stats['gagal_ambil']}")
    top = [r for r in rows if r["grade"] >= R["grade_min"]]
    top.sort(key=lambda r: (-r["grade"], r["rsi"]))
    top = top[:R["show_top"]]
    simpan_ranking_csv(rows)

    # 4. Entry simulasi: breakout yang lolos grade RSI, diurutkan grade lalu kekuatan breakout
    entries, skipped = [], []
    free = RK["max_positions"] - len(st["positions"])
    if reg_ok and free > 0:
        held = {p["symbol"] for p in st["positions"]}
        cool_ms = RK["cooldown_bars"] * C.H4_MS
        cands = [r for r in rows
                 if r["sig"] is not None
                 and r["pair"] not in held
                 and now_ms - st["cooldown"].get(r["pair"], -10**18) >= cool_ms]
        cands.sort(key=lambda r: (r["sig"]["grade"], r["sig"]["strength"]), reverse=True)
        print(f"Kandidat breakout: {len(cands)}")

        for r in cands:
            if free <= 0:
                break
            pair, sig = r["pair"], r["sig"]
            try:
                bids, asks = bn.depth(pair, LQ["depth_levels"])
            except Exception as e:
                skipped.append(f"{pair}: depth gagal ({type(e).__name__})")
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            pos, err = C.plan_entry(pair, sig, mid, asks, st["equity_usd"], RK, now_ms)
            if err:
                skipped.append(f"{pair}: {err}")
                continue
            pos["grade"] = sig["grade"]
            pos["grade_label"] = sig["label"]
            st["positions"].append(pos)
            entries.append(pos)
            free -= 1

    C.save_state(STATE, st)

    # 5. Laporan
    lines = [f"ANALIS 1 JAM {now:%Y-%m-%d %H:%M} UTC ({wib:%H:%M} WIB)",
             f"Regime BTC: {'ON' if reg_ok else 'OFF'} | Equity: ${st['equity_usd']:,.2f} | "
             f"Posisi terbuka: {len(st['positions'])}/{RK['max_positions']}",
             f"Universe: top {UNI['top_n']} market cap | dinilai {stats['dinilai']} | "
             f"tanpa USDT {stats['tanpa_usdt']} | likuiditas rendah {stats['likuiditas_rendah']} | "
             f"gagal {stats['gagal_ambil']}",
             "",
             f"RANKING RSI (grade {R['grade_min']}-10 | RSI {R['min']}-{R['max']}, "
             f"{R['jenuh_min']}-{R['jenuh_max']} dibatalkan)"]
    if top:
        lines.append(f"{'#':>2}  {'KOIN':<6} {'RSI':>5}  {'GRADE':>5}  STATUS")
        for i, r in enumerate(top, 1):
            lines.append(f"{i:>2}  {r['symbol']:<6} {r['rsi']:>5.1f}  {r['grade']:>2}/10  {r['label']}")
    else:
        lines.append(f"Tidak ada koin dengan grade >= {R['grade_min']} saat ini.")
    lines.append("")

    for p in entries:
        base = p["symbol"][:-4]
        lines += [
            "SINYAL BARU",
            f"GRADE          : {p.get('grade_label', '-').lower()} ({p.get('grade', '-')})",
            f"NAMA KOIN      : {base}/USDT",
            f"HARGA REF      : {price(p['mid'])}",
            f"OPEN POSISI    : {price(p['entry_fill'])} (fill order book)",
            f"SLIPPAGE MASUK : {p['slip_in_pct']:+.3f}%",
            f"STOP LOSS      : {price(p['sl'])}",
            f"TAKE PROFIT    : {price(p['tp'])}",
            ""]
    for t in exits:
        base = t["symbol"][:-4]
        lines.append(f"TRADE SELESAI  {base} | {t['reason']} | net {t['net_pct']:+.2f}% | "
                     f"R {t['r_multiple']:+.2f} | equity ${t['equity_after']:,.2f}")
    if skipped:
        lines += ["", "Breakout dilewati: " + "; ".join(skipped)]
    if not entries and not exits:
        lines.append("Tidak ada breakout baru atau posisi yang selesai.")
    text = "\n".join(lines)

    print("\n" + text)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.txt").write_text(text, encoding="utf-8")

    if top or entries or exits or NOTIFY["send_when_empty"]:
        kirim_discord(text)
        kirim_telegram(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        raise SystemExit(1)
