#!/usr/bin/env python3
"""
analis.py - scan sinyal 4H live, simulasi entry dengan slippage order book,
dan pelacakan posisi (state di data/state.json, riwayat di data/trades.csv).
Output adalah hasil analisis otomatis, BUKAN saran investasi.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import core as C

cfg = C.load_config()
S, RK, CO = cfg["signal"], cfg["risk"], cfg["costs"]
LQ, UNI, DLY, NOTIFY = cfg["liquidity"], cfg["universe"], cfg["delays"], cfg["notify"]
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


def main():
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    wib = now + timedelta(hours=7)
    print(f"=== analis 4H | {now:%Y-%m-%d %H:%M} UTC ===")

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

    # 3. Cari sinyal baru dan masuk posisi
    entries, skipped = [], []
    free = RK["max_positions"] - len(st["positions"])
    if reg_ok and free > 0:
        held = {p["symbol"] for p in st["positions"]}
        vols = bn.quote_volume_24h()
        cool_ms = RK["cooldown_bars"] * C.H4_MS
        universe = []
        for coin in top_coins():
            pair = coin["symbol"] + "USDT"
            if pair not in pairs or pair in held:
                continue
            if vols.get(pair, 0) < LQ["min_quote_volume_24h_usd"]:
                continue
            if now_ms - st["cooldown"].get(pair, -10**18) < cool_ms:
                continue
            universe.append((coin, pair))

        cands = []
        for coin, pair in universe:
            try:
                cs = C.closed_only(bn.klines(pair, limit=S["candles"]), now_ms)
            except Exception as e:
                print(f"[skip] {pair}: {type(e).__name__}")
                continue
            time.sleep(DLY["request_sec"])
            sig = C.signal(cs, S)
            if sig:
                cands.append((sig["strength"], coin, pair, sig))
        cands.sort(key=lambda x: x[0], reverse=True)
        print(f"Kandidat sinyal: {len(cands)}")

        for _, coin, pair, sig in cands:
            if free <= 0:
                break
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
            st["positions"].append(pos)
            entries.append(pos)
            free -= 1

    C.save_state(STATE, st)

    # 4. Laporan
    lines = [f"ANALIS 4H {now:%Y-%m-%d %H:%M} UTC ({wib:%H:%M} WIB)",
             f"Regime BTC: {'ON' if reg_ok else 'OFF'} | Equity: ${st['equity_usd']:,.2f} | "
             f"Posisi terbuka: {len(st['positions'])}/{RK['max_positions']}", ""]
    for p in entries:
        base = p["symbol"][:-4]
        lines += [
            "SINYAL BARU",
            f"NAMA KOIN      : {base}/USDT",
            f"HARGA REF      : {price(p['mid'])}",
            f"OPEN POSISI    : {price(p['entry_fill'])} (fill order book)",
            f"SLIPPAGE MASUK : {p['slip_in_pct']:+.3f}%",
            f"STOP LOSS      : {price(p['sl'])}",
            f"TAKE PROFIT    : {price(p['tp'])}",
            f"UKURAN         : {p['qty']:.6f} {base} (${p['qty']*p['entry_fill']:,.2f})",
            f"RISIKO         : ${p['risk_usd']:,.2f}", ""]
    for t in exits:
        base = t["symbol"][:-4]
        lines.append(f"TRADE SELESAI  {base} | {t['reason']} | net {t['net_pct']:+.2f}% | "
                     f"R {t['r_multiple']:+.2f} | equity ${t['equity_after']:,.2f}")
    if skipped:
        lines += ["", "Sinyal dilewati: " + "; ".join(skipped)]
    if not entries and not exits:
        lines.append("Tidak ada sinyal baru atau posisi yang selesai.")
    text = "\n".join(lines)

    print("\n" + text)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.txt").write_text(text, encoding="utf-8")

    if entries or exits or NOTIFY["send_when_empty"]:
        kirim_discord(text)
        kirim_telegram(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        raise SystemExit(1)
