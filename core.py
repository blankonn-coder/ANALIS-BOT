"""core.py - data pasar dari beberapa bursa (Binance, Bybit, OKX, Gate.io), indikator,
grade RSI, sinyal, dan simulasi eksekusi. Candle 1 jam yang sedang berjalan ikut dihitung
dengan harga terakhir (realtime). Output adalah hasil simulasi otomatis, BUKAN saran investasi."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
HOUR_MS = 3_600_000
TIMEOUT = 10

_session = requests.Session()
_session.headers["User-Agent"] = "analis-bot/3.0"


def load_config():
    return json.loads((BASE / "config.json").read_text(encoding="utf-8"))


def http_get(url, params=None, retries=3):
    """GET dengan retry, dipakai untuk CoinGecko."""
    last = "?"
    for i in range(retries):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 429:
                last = "429"
                time.sleep(10 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET gagal {url.split('?')[0]}: {last}")


def fmt_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


# ================= BURSA =================
def _candle(t, o, h, l, c, v, qv):
    t = int(t)
    return {"t": t, "close_ms": t + HOUR_MS - 1, "o": float(o), "h": float(h),
            "l": float(l), "c": float(c), "v": float(v), "qv": float(qv)}


def _book(bids, asks):
    return ([(float(x[0]), float(x[1])) for x in bids],
            [(float(x[0]), float(x[1])) for x in asks])


MAX_BAD_FRAC = 0.10   # di atas 10% candle rusak, seluruh data bursa itu ditolak
MAX_JUMP = 0.50      # perubahan close antar jam lebih dari 50% dianggap tidak wajar


def candle_ok(c):
    vals = (c["o"], c["h"], c["l"], c["c"], c["v"], c["qv"])
    if not all(math.isfinite(x) for x in vals):
        return False
    if min(c["o"], c["h"], c["l"], c["c"]) <= 0 or c["v"] < 0:
        return False
    if c["h"] < max(c["o"], c["c"]) or c["l"] > min(c["o"], c["c"]):
        return False
    return True


def clean_series(cs):
    """Buang candle yang tidak wajar. Mengembalikan (data_bersih, jumlah_dibuang).
    Raise jika data kosong atau terlalu banyak yang rusak, sehingga bursa berikutnya dipakai."""
    if not cs:
        raise ValueError("data candle kosong")
    out, dropped, prev = [], 0, None
    for c in cs:
        jump_bad = prev is not None and abs(c["c"] / prev - 1) > MAX_JUMP
        if candle_ok(c) and not jump_bad:
            out.append(c)
            prev = c["c"]
        else:
            dropped += 1
    if dropped / len(cs) > MAX_BAD_FRAC:
        raise ValueError(f"{dropped} dari {len(cs)} candle tidak wajar")
    return out, dropped


def clean_book(bids, asks):
    if not bids or not asks:
        raise ValueError("order book kosong")
    if any(p <= 0 or q <= 0 for p, q in bids + asks):
        raise ValueError("harga atau jumlah order book tidak wajar")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("bid >= ask, order book tidak wajar")
    return bids, asks


class Provider:
    """Satu bursa dengan satu atau lebih base URL (mirror).
    Mirror yang gagal ditandai dan dilewati untuk sisa run ini."""
    name = ""

    def __init__(self, bases):
        self.bases = list(bases)
        self.bad = set()

    def get(self, path, params=None):
        last = "tidak ada mirror"
        for b in self.bases:
            if b in self.bad:
                continue
            try:
                r = _session.get(b + path, params=params, timeout=TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                self.bad.add(b)
                last = type(e).__name__
        raise RuntimeError(f"{self.name} tidak merespon ({last})")


class Binance(Provider):
    name = "binance"

    def listing(self):
        d = self.get("exchangeInfo")
        return {s["baseAsset"]: s["symbol"] for s in d["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}

    def volumes(self):
        return {r["symbol"][:-4]: float(r["quoteVolume"]) for r in self.get("ticker/24hr")
                if r["symbol"].endswith("USDT")}

    def klines(self, inst, limit):
        rows = self.get("klines", {"symbol": inst, "interval": "1h", "limit": limit})
        return [_candle(r[0], r[1], r[2], r[3], r[4], r[5], r[7]) for r in rows]

    def depth(self, inst, limit):
        d = self.get("depth", {"symbol": inst, "limit": limit})
        return _book(d["bids"], d["asks"])


class Bybit(Provider):
    name = "bybit"

    def _result(self, path, params):
        d = self.get(path, params)
        if d.get("retCode") != 0:
            raise RuntimeError(f"bybit: {d.get('retMsg')}")
        return d["result"]

    def listing(self):
        r = self._result("market/instruments-info", {"category": "spot"})
        return {s["baseCoin"]: s["symbol"] for s in r["list"]
                if s["quoteCoin"] == "USDT" and s["status"] == "Trading"}

    def volumes(self):
        r = self._result("market/tickers", {"category": "spot"})
        return {s["symbol"][:-4]: float(s["turnover24h"]) for s in r["list"]
                if s["symbol"].endswith("USDT")}

    def klines(self, inst, limit):
        r = self._result("market/kline", {"category": "spot", "symbol": inst,
                                          "interval": "60", "limit": limit})
        rows = sorted(r["list"], key=lambda x: int(x[0]))
        return [_candle(x[0], x[1], x[2], x[3], x[4], x[5], x[6]) for x in rows]

    def depth(self, inst, limit):
        r = self._result("market/orderbook", {"category": "spot", "symbol": inst, "limit": limit})
        return _book(r["b"], r["a"])


class Okx(Provider):
    name = "okx"

    def _data(self, path, params):
        d = self.get(path, params)
        if d.get("code") != "0":
            raise RuntimeError(f"okx: {d.get('msg')}")
        return d["data"]

    def listing(self):
        rows = self._data("public/instruments", {"instType": "SPOT"})
        return {s["baseCcy"]: s["instId"] for s in rows
                if s["quoteCcy"] == "USDT" and s["state"] == "live"}

    def volumes(self):
        rows = self._data("market/tickers", {"instType": "SPOT"})
        return {r["instId"].split("-")[0]: float(r["volCcy24h"]) for r in rows
                if r["instId"].endswith("-USDT")}

    def klines(self, inst, limit):
        rows = self._data("market/candles", {"instId": inst, "bar": "1H", "limit": limit})
        rows = sorted(rows, key=lambda x: int(x[0]))
        return [_candle(x[0], x[1], x[2], x[3], x[4], x[5], x[7]) for x in rows]

    def depth(self, inst, limit):
        d = self._data("market/books", {"instId": inst, "sz": limit})[0]
        return _book(d["bids"], d["asks"])


class Gate(Provider):
    name = "gate"

    def listing(self):
        rows = self.get("spot/currency_pairs")
        return {r["base"]: r["id"] for r in rows
                if r["quote"] == "USDT" and r.get("trade_status") == "tradable"}

    def volumes(self):
        rows = self.get("spot/tickers")
        return {r["currency_pair"].split("_")[0]: float(r["quote_volume"]) for r in rows
                if r["currency_pair"].endswith("_USDT")}

    def klines(self, inst, limit):
        rows = self.get("spot/candlesticks",
                        {"currency_pair": inst, "interval": "1h", "limit": limit})
        rows = sorted(rows, key=lambda x: int(x[0]))
        # format Gate: [detik, vol quote, close, high, low, open, vol base, closed]
        return [_candle(int(x[0]) * 1000, x[5], x[3], x[4], x[2], x[6], x[1]) for x in rows]

    def depth(self, inst, limit):
        d = self.get("spot/order_book", {"currency_pair": inst, "limit": limit})
        return _book(d["bids"], d["asks"])


PROVIDERS = {"binance": Binance, "bybit": Bybit, "okx": Okx, "gate": Gate}


class Market:
    """Gabungan semua bursa. Setiap request dicoba ke bursa berikutnya jika gagal.
    Urutan bursa dan mirror diatur di config 'sources'."""

    def __init__(self, sources):
        self.order = sources["order"]
        self.p = {n: PROVIDERS[n](sources[n]) for n in self.order}
        self.listed = {}  # base -> [(bursa, instrumen), ...] sesuai urutan
        self.dropped = {n: 0 for n in self.order}   # candle dibuang per bursa (data sebagian)
        self.rejected = {n: 0 for n in self.order}  # request ditolak utuh per bursa

    def init(self):
        for n in self.order:
            try:
                items = self.p[n].listing()
            except Exception as e:
                print(f"[warn] {n} gagal: {e}")
                continue
            print(f"{n}: {len(items)} pasangan USDT")
            for base, inst in items.items():
                self.listed.setdefault(base, []).append((n, inst))
        return set(self.listed)

    def volumes(self):
        """Volume 24 jam (USD) terbesar dari bursa yang listing koin tersebut."""
        best = {}
        for n in self.order:
            try:
                vols = self.p[n].volumes()
            except Exception as e:
                print(f"[warn] volume {n} gagal: {e}")
                continue
            for base, v in vols.items():
                best[base] = max(best.get(base, 0.0), v)
        return best

    def _try(self, base, prefer, call, check):
        errs = []
        for n, inst in sorted(self.listed.get(base, []), key=lambda x: x[0] != prefer):
            try:
                raw = call(self.p[n], inst)
            except Exception as e:
                errs.append(f"{n}: {e}")
                continue
            try:
                return check(n, raw), n
            except ValueError as e:
                self.rejected[n] += 1
                errs.append(f"{n} data tidak wajar: {e}")
        raise RuntimeError(f"{base} tidak tersedia ({'; '.join(errs) or 'tidak listing'})")

    def _check_klines(self, n, cs):
        clean, dropped = clean_series(cs)
        self.dropped[n] += dropped
        return clean

    def _check_depth(self, n, book):
        return clean_book(*book)

    def klines(self, base, limit, prefer=None):
        return self._try(base, prefer, lambda p, i: p.klines(i, limit), self._check_klines)

    def depth(self, base, limit, prefer=None):
        return self._try(base, prefer, lambda p, i: p.depth(i, limit), self._check_depth)

    def report(self):
        """Ringkasan data tidak wajar per bursa untuk log (tidak masuk notifikasi)."""
        parts = [f"{n}: {self.dropped[n]} candle dibuang, {self.rejected[n]} request ditolak"
                 for n in self.order if self.dropped[n] or self.rejected[n]]
        return "; ".join(parts) or "tidak ada"


# ================= INDIKATOR =================
def ema(xs, n):
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def atr(cs, n):
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
               abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def rsi(closes, n):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# ================= GRADE RSI =================
def rsi_grade(r, R):
    """Grade 0-10 dari RSI, semua aturan diatur di config 'rsi':
    - RSI < min   -> 0, di bawah rentang
    - RSI > max   -> 0, jenuh, dibatalkan
    - min..max    -> linear dari grade_at_min ke grade_at_max, dibulatkan.
    RSI <= sangat_max diberi label SANGAT REKOMENDASI."""
    lo, hi = R["min"], R["max"]
    if r < lo:
        return {"rsi": r, "grade": 0, "label": "DI BAWAH RENTANG"}
    if r > hi:
        return {"rsi": r, "grade": 0, "label": "JENUH - DIBATALKAN"}
    g_lo, g_hi = R["grade_at_min"], R["grade_at_max"]
    grade = int(round(g_lo - (r - lo) * (g_lo - g_hi) / (hi - lo)))
    label = "SANGAT REKOMENDASI" if r <= R["sangat_max"] else "REKOMENDASI"
    return {"rsi": r, "grade": grade, "label": label}


# ================= EVALUASI KOIN =================
def evaluate(cs, I, R, now_ms):
    """RSI-grade dan sinyal breakout dari candle 1H. cs[-1] boleh candle yang sedang berjalan.
    Volume candle berjalan diproyeksikan ke satu jam penuh."""
    need = max(I["ema_period"], I["breakout_lookback"] + 1, I["rsi_period"] * 3, I["atr_period"] + 1)
    if len(cs) < need:
        return None
    closes = [c["c"] for c in cs]
    g = rsi_grade(rsi(closes, I["rsi_period"]), R)
    last = cs[-1]
    prev = cs[-(I["breakout_lookback"] + 1):-1]
    hi = max(c["h"] for c in prev)
    vma = sum(c["v"] for c in prev) / len(prev)
    frac = min(1.0, max((now_ms - last["t"]) / HOUR_MS, 0.05))
    vr = (last["v"] / frac) / vma if vma > 0 else 0.0
    a = atr(cs, I["atr_period"])
    sig = None
    if (last["c"] > hi and a > 0 and vr >= I["vol_mult"]
            and g["grade"] >= R["entry_grade_min"]
            and last["c"] > ema(closes, I["ema_period"])):
        sig = {"atr": a, "grade": g["grade"], "label": g["label"],
               "strength": (last["c"] - hi) / a}
    return {"rsi": g["rsi"], "grade": g["grade"], "label": g["label"], "sig": sig}


def regime_ok(btc, n):
    closes = [c["c"] for c in btc]
    return len(closes) >= n and closes[-1] > ema(closes, n)


# ================= EKSEKUSI & SLIPPAGE =================
def buy_vwap(asks, qty):
    rem, cost = qty, 0.0
    for price, avail in asks:
        take = min(avail, rem)
        cost += take * price
        rem -= take
        if rem <= 1e-12:
            return cost / qty
    return None


def size_position(equity, risk_pct, max_notional_pct, entry, sl):
    per_unit = entry - sl
    if per_unit <= 0 or entry <= 0:
        return 0.0
    qty = equity * risk_pct / 100 / per_unit
    cap = equity * max_notional_pct / 100 / entry
    return min(qty, cap)


def plan_entry(base, sig, mid, asks, equity, RK, now_ms, src):
    """Rencana entry dengan harga fill dari order book bursa src. Mengembalikan (posisi, error)."""
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
    entry_ms = now_ms // HOUR_MS * HOUR_MS
    return {
        "symbol": base, "src": src, "entry_ms": entry_ms, "last_ms": entry_ms, "bars": 0,
        "entry_run_ms": now_ms, "mid": mid, "entry_fill": fill, "slip_in_pct": slip,
        "qty": qty, "atr": sig["atr"], "sl": fill - dist,
        "tp": fill + RK["tp_atr"] * sig["atr"], "risk_usd": qty * dist,
        "grade": sig["grade"], "grade_label": sig["label"],
    }, None


# ================= EXIT & PnL =================
def check_exit(pos, c, bars, max_hold, exit_slip_bps):
    sl, tp = pos["sl"], pos["tp"]
    out = 1 - exit_slip_bps / 1e4
    if c["o"] <= sl:
        return c["o"] * out, "SL"
    if c["l"] <= sl:
        return sl * out, "SL"
    if c["o"] >= tp:
        return c["o"], "TP"
    if c["h"] >= tp:
        return tp, "TP"
    if bars >= max_hold:
        return c["c"] * out, "TIME"
    return None


def settle(pos, exit_px, reason, exit_ms, fee_bps):
    q, e = pos["qty"], pos["entry_fill"]
    pnl = q * (exit_px - e) - fee_bps / 1e4 * q * (e + exit_px)
    risk = pos["risk_usd"]
    return {
        "symbol": pos["symbol"], "exchange": pos.get("src", ""),
        "entry_utc": fmt_utc(pos["entry_run_ms"]), "exit_utc": fmt_utc(exit_ms),
        "entry_fill": round(e, 8), "slip_in_pct": round(pos["slip_in_pct"], 4),
        "exit_price": round(exit_px, 8), "reason": reason, "bars": pos["bars"],
        "qty": round(q, 6), "risk_usd": round(risk, 4),
        "gross_pct": round((exit_px / e - 1) * 100, 4),
        "net_pct": round(pnl / (q * e) * 100, 4),
        "r_multiple": round(pnl / risk, 4) if risk > 0 else 0.0,
        "pnl_usd": round(pnl, 4), "exit_ms": exit_ms,
    }


def track(mk, pos, now_ms, RK, CO):
    """Lacak posisi. Candle yang sudah close dihitung per jam (bar). Candle yang sedang
    berjalan dicek dengan harga terakhir, sehingga SL/TP realtime.
    Mengembalikan trade jika selesai, None jika masih terbuka. Error diteruskan ke pemanggil."""
    out = 1 - CO["exit_slip_bps"] / 1e4
    cs, _ = mk.klines(pos["symbol"], 200, prefer=pos.get("src"))
    for c in cs:
        if c["t"] < pos["entry_ms"]:
            continue
        if c["close_ms"] <= now_ms:
            if c["t"] <= pos["last_ms"]:
                continue
            pos["bars"] += 1
            pos["last_ms"] = c["t"]
            hit = check_exit(pos, c, pos["bars"], RK["max_hold_bars"], CO["exit_slip_bps"])
            if hit:
                return settle(pos, hit[0], hit[1], c["close_ms"], CO["fee_bps"])
        elif c["c"] <= pos["sl"]:
            return settle(pos, c["c"] * out, "SL", now_ms, CO["fee_bps"])
        elif c["c"] >= pos["tp"]:
            return settle(pos, c["c"], "TP", now_ms, CO["fee_bps"])
    return None
