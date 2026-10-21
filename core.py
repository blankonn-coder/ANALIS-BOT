"""
core.py - logika bersama untuk analis (live) dan backtest.
Harga, candle, dan order book diambil dari API Binance.
Output adalah hasil simulasi otomatis, BUKAN saran investasi.
"""

import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
H4_MS = 4 * 60 * 60 * 1000

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "analis-bot/2.0"

TRADE_FIELDS = [
    "symbol", "entry_utc", "exit_utc", "entry_fill", "slip_in_pct", "exit_price",
    "reason", "bars", "qty", "risk_usd", "gross_pct", "net_pct", "r_multiple",
    "pnl_usd", "equity_after",
]


# ================= CONFIG & HTTP =================
def load_config(path=None):
    p = Path(path) if path else BASE_DIR / "config.json"
    return json.loads(p.read_text(encoding="utf-8"))


def http_get(url, params=None, headers=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
                last = "429"
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = type(e).__name__  # jangan cetak URL/kunci ke log
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET gagal {url.split('?')[0]}: {last}")


def fmt_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def next_boundary(ms):
    """Awal candle 4H berikutnya setelah waktu ms (00, 04, 08, ... UTC)."""
    return (ms // H4_MS + 1) * H4_MS


# ================= BINANCE =================
def parse_kline(k):
    return {
        "t": int(k[0]), "close_ms": int(k[6]),
        "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
        "v": float(k[5]), "qv": float(k[7]), "tbq": float(k[10]),
    }


def closed_only(cs, now_ms):
    """Buang candle yang belum close."""
    return [c for c in cs if c["close_ms"] <= now_ms]


class Binance:
    def __init__(self, bases):
        self.bases = bases
        self.base = None

    def init(self):
        """Pilih base URL yang bisa diakses, lalu kembalikan set pasangan USDT aktif."""
        for b in self.bases:
            try:
                info = http_get(b + "exchangeInfo")
                self.base = b
                print(f"Binance aktif via: {b}")
                return {s["symbol"] for s in info["symbols"]
                        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
            except Exception as e:
                print(f"[warn] {b} tidak bisa diakses: {type(e).__name__}")
        raise RuntimeError("Semua endpoint Binance gagal diakses")

    def _get(self, path, params=None):
        return http_get(self.base + path, params=params)

    def klines(self, symbol, limit=120, start_ms=None):
        p = {"symbol": symbol, "interval": "4h", "limit": limit}
        if start_ms is not None:
            p["startTime"] = int(start_ms)
        return [parse_kline(k) for k in self._get("klines", p)]

    def depth(self, symbol, limit=100):
        d = self._get("depth", {"symbol": symbol, "limit": limit})
        bids = [(float(p), float(q)) for p, q in d["bids"]]
        asks = [(float(p), float(q)) for p, q in d["asks"]]
        return bids, asks

    def quote_volume_24h(self):
        rows = self._get("ticker/24hr")
        return {r["symbol"]: float(r["quoteVolume"]) for r in rows
                if r["symbol"].endswith("USDT")}


# ================= INDIKATOR =================
def ema(xs, n):
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def atr(cs, n=14):
    """ATR Wilder dalam satuan harga (bukan persen)."""
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i]["h"], cs[i]["l"], cs[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


# ================= SINYAL & REGIME =================
def signal(cs, S):
    """Sinyal long dari candle yang sudah close (cs[-1] = candle sinyal).
    Syarat: breakout high N candle, volume >= vol_mult x rata-rata,
    RSI di rentang, dan close di atas EMA."""
    need = max(S["ema_period"], S["breakout_lookback"] + 1, S["rsi_period"] * 3)
    if len(cs) < need:
        return None
    last = cs[-1]
    prev = cs[-(S["breakout_lookback"] + 1):-1]
    hi = max(c["h"] for c in prev)
    closes = [c["c"] for c in cs]

    if not last["c"] > hi:
        return None
    a = atr(cs, S["atr_period"])
    if a <= 0:
        return None
    vma = sum(c["v"] for c in prev) / len(prev)
    vr = last["v"] / vma if vma > 0 else 0.0
    if vr < S["vol_mult"]:
        return None
    r = rsi(closes, S["rsi_period"])
    if not (S["rsi_lo"] <= r <= S["rsi_hi"]):
        return None
    if last["c"] <= ema(closes, S["ema_period"]):
        return None
    return {"atr": a, "vol_ratio": vr, "rsi": r,
            "strength": (last["c"] - hi) / a}


def regime_ok(btc_closed, cfg):
    """Long hanya jika close BTC terakhir di atas EMA-nya."""
    closes = [c["c"] for c in btc_closed]
    n = cfg["regime"]["btc_ema"]
    if len(closes) < n:
        return False
    return closes[-1] > ema(closes, n)


# ================= EKSEKUSI & SLIPPAGE =================
def buy_vwap(asks, qty):
    """Harga rata-rata (VWAP) jika membeli qty dengan menyapu order book ask.
    Mengembalikan None jika kedalaman order book tidak cukup."""
    rem, cost = qty, 0.0
    for price, avail in asks:
        take = min(avail, rem)
        cost += take * price
        rem -= take
        if rem <= 1e-12:
            return cost / qty
    return None


def size_position(equity, risk_pct, max_notional_pct, entry, sl):
    """Ukuran posisi: risiko risk_pct dari equity, dibatasi max_notional_pct."""
    per_unit = entry - sl
    if per_unit <= 0 or entry <= 0:
        return 0.0
    qty = equity * risk_pct / 100 / per_unit
    cap = equity * max_notional_pct / 100 / entry
    return min(qty, cap)


def plan_entry(pair, sig, mid, asks, equity, RK, now_ms):
    """Hitung posisi live. Harga fill dari order book (VWAP), bukan harga close.
    Slippage = (fill - mid) / mid. Posisi ditolak jika slippage melebihi batas."""
    dist = RK["sl_atr"] * sig["atr"]
    qty = size_position(equity, RK["risk_pct"], RK["max_notional_pct"], mid, mid - dist)
    if qty <= 0:
        return None, "ukuran posisi nol"
    fill = buy_vwap(asks, qty)
    if fill is None:
        return None, "kedalaman order book tidak cukup"
    slip = (fill - mid) / mid * 100
    if slip > RK["max_slippage_pct"]:
        return None, f"slippage {slip:.3f}% melebihi batas {RK['max_slippage_pct']}%"
    sl = fill - dist
    tp = fill + RK["tp_atr"] * sig["atr"]
    return {
        "symbol": pair, "entry_run_ms": now_ms,
        "track_from_ms": next_boundary(now_ms), "last_ms": 0, "bars": 0,
        "mid": mid, "entry_fill": fill, "slip_in_pct": slip, "qty": qty,
        "atr": sig["atr"], "sl": sl, "tp": tp, "risk_usd": qty * (fill - sl),
    }, None


# ================= EXIT & PnL =================
def check_exit(pos, c, bars, max_hold, exit_slip_bps):
    """Cek satu candle setelah entry. Mengembalikan (harga_exit, alasan) atau None.
    Jika SL dan TP tersentuh di candle yang sama, dihitung SL (konservatif).
    Stop (SL dan waktu) dikenai slippage keluar."""
    sl, tp = pos["sl"], pos["tp"]
    out = 1 - exit_slip_bps / 1e4
    if c["o"] <= sl:
        return c["o"] * out, "SL_GAP"
    if c["l"] <= sl:
        return sl * out, "SL"
    if c["o"] >= tp:
        return c["o"], "TP_GAP"
    if c["h"] >= tp:
        return tp, "TP"
    if bars >= max_hold:
        return c["c"] * out, "TIME"
    return None


def settle(pos, exit_px, reason, exit_ms, fee_bps):
    """Hitung PnL bersih setelah fee dua sisi, dinyatakan dalam USD dan kelipatan R."""
    q, e = pos["qty"], pos["entry_fill"]
    gross = q * (exit_px - e)
    fees = fee_bps / 1e4 * q * (e + exit_px)
    pnl = gross - fees
    risk = pos["risk_usd"]
    return {
        "symbol": pos["symbol"], "entry_utc": fmt_utc(pos.get("entry_run_ms", 0)),
        "exit_utc": fmt_utc(exit_ms), "entry_fill": round(e, 8),
        "slip_in_pct": round(pos["slip_in_pct"], 4), "exit_price": round(exit_px, 8),
        "reason": reason, "bars": pos["bars"], "qty": round(q, 6),
        "risk_usd": round(risk, 4), "gross_pct": round((exit_px / e - 1) * 100, 4),
        "net_pct": round(pnl / (q * e) * 100, 4), "r_multiple": round(pnl / risk, 4) if risk > 0 else 0.0,
        "pnl_usd": round(pnl, 4), "exit_ms": exit_ms,
    }


# ================= FILE STATE & CSV =================
def load_state(path, equity0):
    p = Path(path)
    if p.exists():
        st = json.loads(p.read_text(encoding="utf-8"))
        st.setdefault("positions", [])
        st.setdefault("cooldown", {})
        return st
    return {"equity_usd": equity0, "positions": [], "cooldown": {}}


def save_state(path, st):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")


def append_trade(path, rec):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(rec)
