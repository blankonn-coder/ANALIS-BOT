#!/usr/bin/env python3
"""
backtest.py - simulasi strategi di data historis Binance (4H).
Memakai fungsi yang sama dengan analis.py (core.py), sehingga logikanya identik.
Perbedaan: slippage masuk dipakai dari config (tidak ada order book historis).

Contoh:
  python backtest.py --start 2023-01-01 --end 2024-12-31
  python backtest.py --start 2025-01-01 --end 2025-06-30   # out-of-sample
"""

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import core as C

H4 = C.H4_MS


def to_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_history(bn, symbol, start_ms, end_ms, pause=0.2):
    """Ambil semua candle 4H di rentang [start, end) dengan paging."""
    out, cur = [], start_ms
    while cur < end_ms:
        batch = bn.klines(symbol, limit=1000, start_ms=cur)
        if not batch:
            break
        out += [c for c in batch if c["close_ms"] < end_ms]
        nxt = batch[-1]["t"] + H4
        if len(batch) < 1000 or nxt <= cur:
            break
        cur = nxt
        time.sleep(pause)
    return out


def run_backtest(data, btc, cfg, equity0=None):
    """data: {pair: [candle...]}, btc: [candle...], keduanya terurut waktu.
    Urutan per candle T: (1) cek exit posisi, (2) regime, (3) entry di open T."""
    S, RK, CO = cfg["signal"], cfg["risk"], cfg["costs"]
    LQ, BT, REG = cfg["liquidity"], cfg["backtest"], cfg["regime"]
    equity = RK["equity_usd"] if equity0 is None else equity0
    start_equity = equity

    idx = {p: {c["t"]: i for i, c in enumerate(cs)} for p, cs in data.items()}
    btc_closes = [c["c"] for c in btc]
    warm = max(S["ema_period"], S["breakout_lookback"] + 1, S["rsi_period"] * 3,
               REG["btc_ema"] + 1, 7)
    cool_ms = RK["cooldown_bars"] * H4
    slip_in = CO["backtest_entry_slip_bps"] / 1e4

    positions, cooldown, trades = {}, {}, []
    curve = [equity]
    skipped_regime = 0

    for i, c in enumerate(btc):
        T = c["t"]
        if i < warm:
            continue

        # 1. exit posisi yang sudah dibuka sebelum T
        for pair in list(positions):
            j = idx[pair].get(T)
            if j is None:
                continue
            pos = positions[pair]
            pos["bars"] += 1
            res = C.check_exit(pos, data[pair][j], pos["bars"],
                               RK["max_hold_bars"], CO["exit_slip_bps"])
            if res:
                px, reason = res
                tr = C.settle(pos, px, reason, T + H4, CO["fee_bps"])
                equity += tr["pnl_usd"]
                tr["equity_after"] = round(equity, 2)
                trades.append(tr)
                curve.append(equity)
                cooldown[pair] = T
                del positions[pair]

        # 2. regime dari BTC candle sebelum T
        if not C.ema(btc_closes[:i], REG["btc_ema"]) < btc_closes[i - 1]:
            skipped_regime += 1
            continue
        if len(positions) >= RK["max_positions"]:
            continue

        # 3. kumpulkan sinyal dari candle sebelum T, lalu entry di open T
        cands = []
        for pair, cs in data.items():
            if pair in positions:
                continue
            if pair in cooldown and T - cooldown[pair] < cool_ms:
                continue
            j = idx[pair].get(T)
            if j is None or j < 6:
                continue
            qv24 = sum(x["qv"] for x in cs[max(0, j - 6):j])  # 6 candle 4H = 24 jam
            if qv24 < LQ["min_quote_volume_24h_usd"]:
                continue
            window = cs[max(0, j - S["candles"]):j]
            sig = C.signal(window, S)
            if sig:
                cands.append((sig["strength"], pair, sig, cs[j]))
        cands.sort(key=lambda x: x[0], reverse=True)

        free = RK["max_positions"] - len(positions)
        for _, pair, sig, cand in cands[:free]:
            mid = cand["o"]
            fill = mid * (1 + slip_in)
            dist = RK["sl_atr"] * sig["atr"]
            qty = C.size_position(equity, RK["risk_pct"], RK["max_notional_pct"],
                                  fill, fill - dist)
            if qty <= 0:
                continue
            positions[pair] = {
                "symbol": pair, "entry_run_ms": T, "bars": 0,
                "entry_fill": fill, "slip_in_pct": slip_in * 100, "qty": qty,
                "sl": fill - dist, "tp": fill + RK["tp_atr"] * sig["atr"],
                "risk_usd": qty * dist,
            }

    # posisi yang masih terbuka di akhir periode ditutup di close terakhir
    for pair, pos in list(positions.items()):
        last = data[pair][-1]
        px = last["c"] * (1 - CO["exit_slip_bps"] / 1e4)
        tr = C.settle(pos, px, "END", last["close_ms"], CO["fee_bps"])
        equity += tr["pnl_usd"]
        tr["equity_after"] = round(equity, 2)
        trades.append(tr)
        curve.append(equity)

    return {"trades": trades, "curve": curve, "start_equity": start_equity,
            "end_equity": equity, "skipped_regime": skipped_regime}


def metrics(res):
    tr = res["trades"]
    n = len(tr)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in tr if t["pnl_usd"] > 0]
    losses = [t for t in tr if t["pnl_usd"] <= 0]
    gw = sum(t["pnl_usd"] for t in wins)
    gl = abs(sum(t["pnl_usd"] for t in losses))
    peak, mdd = res["curve"][0], 0.0
    for e in res["curve"]:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0)
    reasons = {}
    for t in tr:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "avg_R": round(sum(t["r_multiple"] for t in tr) / n, 3),
        "profit_factor": round(gw / gl, 3) if gl > 0 else float("inf"),
        "avg_slip_in_pct": round(sum(t["slip_in_pct"] for t in tr) / n, 4),
        "max_drawdown_pct": round(mdd * 100, 2),
        "return_pct": round((res["end_equity"] / res["start_equity"] - 1) * 100, 2),
        "end_equity": round(res["end_equity"], 2),
        "exit_reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=str(C.BASE_DIR / "data" / "backtest_trades.csv"))
    args = ap.parse_args()

    cfg = C.load_config()
    start = to_ms(args.start or cfg["backtest"]["start"])
    end = to_ms(args.end or cfg["backtest"]["end"])

    bn = C.Binance(cfg["sources"]["binance"])
    bn.init()
    print("Mengambil BTCUSDT ...")
    btc = fetch_history(bn, "BTCUSDT", start, end)
    data = {}
    for pair in cfg["backtest"]["symbols"]:
        try:
            cs = fetch_history(bn, pair, start, end)
        except Exception as e:
            print(f"[skip] {pair}: {type(e).__name__}")
            continue
        if len(cs) > 50:
            data[pair] = cs
            print(f"[ok] {pair}: {len(cs)} candle")
    if not data or not btc:
        raise SystemExit("Data historis kosong")

    res = run_backtest(data, btc, cfg)
    m = metrics(res)
    print(f"\nPeriode {args.start or cfg['backtest']['start']} s/d "
          f"{args.end or cfg['backtest']['end']} | pair: {len(data)}")
    for k, v in m.items():
        print(f"{k:>18}: {v}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=C.TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in res["trades"]:
            w.writerow(t)
    print(f"Riwayat trade: {out}")


if __name__ == "__main__":
    main()
