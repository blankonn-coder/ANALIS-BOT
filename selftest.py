#!/usr/bin/env python3
"""
selftest.py - uji logika offline (tanpa internet).
Jalankan: python selftest.py
"""
import math
import random

import backtest as B
import core as C

cfg = C.load_config()
S, RK, CO = cfg["signal"], cfg["risk"], cfg["costs"]
ok = 0


def check(name, cond):
    global ok
    assert cond, f"GAGAL: {name}"
    ok += 1
    print(f"ok  {name}")


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# 1. VWAP order book
asks = [(100.0, 1.0), (101.0, 2.0)]
check("vwap qty 2 = 100.5", approx(C.buy_vwap(asks, 2.0), 100.5))
check("vwap melebihi depth = None", C.buy_vwap(asks, 4.0) is None)

# 2. Sizing: equity 1000, risk 1%, entry 100, sl 98 -> risiko $10, qty 5, cap 20% -> 2
check("sizing dibatasi notional 20%", approx(C.size_position(1000, 1, 20, 100, 98), 2.0))
check("sizing tanpa cap", approx(C.size_position(1000, 1, 100, 100, 98), 5.0))

# 3. Exit logic
pos = {"sl": 95.0, "tp": 110.0}
c = lambda o, h, l, cl: {"o": o, "h": h, "l": l, "c": cl}
r = C.check_exit(pos, c(100, 101, 94, 99), 1, 12, 5)
check("SL kena dengan slippage keluar", r[1] == "SL" and approx(r[0], 95 * (1 - 0.0005)))
r = C.check_exit(pos, c(96, 111, 95.5, 109), 1, 12, 5)
check("TP kena di harga limit", r[1] == "TP" and r[0] == 110.0)
r = C.check_exit(pos, c(100, 111, 94, 109), 1, 12, 5)
check("SL & TP sama candle -> SL (konservatif)", r[1] == "SL")
r = C.check_exit(pos, c(112, 113, 111, 112), 1, 12, 5)
check("gap di atas TP -> TP_GAP di open", r[1] == "TP_GAP" and r[0] == 112)
r = C.check_exit(pos, c(100, 103, 98, 101), 12, 12, 5)
check("time stop di close", r[1] == "TIME")
check("tidak ada exit", C.check_exit(pos, c(100, 103, 98, 101), 3, 12, 5) is None)

# 4. PnL bersih: qty 1, entry 100, exit 102, fee 10 bps per sisi, risiko 2
t = C.settle({"qty": 1.0, "entry_fill": 100.0, "risk_usd": 2.0, "bars": 3,
              "slip_in_pct": 0.05, "symbol": "XUSDT", "entry_run_ms": 0},
             102.0, "TP", 0, 10)
check("pnl bersih = 2 - fee 0.202", approx(t["pnl_usd"], 1.798, 1e-3))
check("R = pnl / risiko", approx(t["r_multiple"], 0.899, 1e-3))

# 5. Batas waktu: next_boundary selalu kelipatan 4H berikutnya
b = C.next_boundary(C.H4_MS * 10 + 1)
check("track mulai candle 4H berikutnya", b == C.H4_MS * 11)

# 6. Regime & sinyal pada data sintetis
random.seed(7)


def synth(n, start=1_700_000_000_000 // C.H4_MS * C.H4_MS, p0=100.0, drift=0.0, vol=0.01):
    out, p = [], p0
    for i in range(n):
        o = p
        p = p * math.exp(random.gauss(drift, vol))
        h = max(o, p) * (1 + abs(random.gauss(0, vol / 2)))
        l = min(o, p) * (1 - abs(random.gauss(0, vol / 2)))
        v = random.uniform(800, 1200) * (3 if random.random() < 0.15 else 1)
        t0 = start + i * C.H4_MS
        qv = v * p * 1e5  # skala quote volume agar lolos filter likuiditas $20 juta
        out.append({"t": t0, "close_ms": t0 + C.H4_MS - 1, "o": o, "h": h, "l": l,
                    "c": p, "v": v, "qv": qv, "tbq": qv * 0.5})
    return out


btc_up = synth(300, drift=0.002)
check("regime ON saat BTC naik", C.regime_ok(btc_up, cfg))
btc_dn = synth(300, drift=-0.002)
check("regime OFF saat BTC turun", not C.regime_ok(btc_dn, cfg))

# 6b. Slippage saat open posisi (live): fill VWAP dari order book
sig = {"atr": 2.0, "strength": 1.0}
asks_ok = [(100.05, 10.0), (100.20, 10.0), (100.50, 10.0)]
mid = 100.0
pos, err = C.plan_entry("XUSDT", sig, mid, asks_ok, 1000, RK, 0)
check("plan_entry menerima order book cukup", err is None and pos is not None)
check("slippage masuk dihitung dari VWAP vs mid", pos["slip_in_pct"] > 0)
check("SL & TP dihitung dari fill, bukan mid",
      approx(pos["sl"], pos["entry_fill"] - RK["sl_atr"] * sig["atr"]) and
      approx(pos["tp"], pos["entry_fill"] + RK["tp_atr"] * sig["atr"]))
asks_thin = [(101.0, 0.5)]  # slippage besar -> harus ditolak
pos2, err2 = C.plan_entry("XUSDT", sig, mid, asks_thin, 1000, RK, 0)
check("slippage terlalu besar ditolak", pos2 is None and err2 is not None)

# 7. Backtest engine end-to-end (data sintetis, tanpa internet)
N = 900
btc = synth(N, drift=0.0005)
data = {}
for k in range(4):
    cs = synth(N, p0=10 + k, drift=0.0008, vol=0.02)
    # samakan timeline dengan BTC
    for i, x in enumerate(cs):
        x["t"] = btc[i]["t"]
        x["close_ms"] = btc[i]["close_ms"]
    data[f"COIN{k}USDT"] = cs
res = B.run_backtest(data, btc, cfg)
m = B.metrics(res)
check("engine backtest berjalan dan menghasilkan trade", m["trades"] > 0)
check("setiap trade punya alasan exit", all(t["reason"] for t in res["trades"]))
check("equity akhir konsisten dengan total PnL",
      approx(res["end_equity"], RK["equity_usd"] + sum(t["pnl_usd"] for t in res["trades"]), 1e-2))
print("\nMetrik sintetis:", {k: v for k, v in m.items() if k != "exit_reasons"})
print(f"\nSEMUA {ok} UJI LOLOS")
