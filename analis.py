#!/usr/bin/env python3
"""
analis.py - Scouting koin otomatis (4H, Peak Trading Hours)
Dirancang untuk GitHub Actions (free tier).
Output adalah hasil analisis otomatis, BUKAN saran investasi.
"""

import os
import time
import math
from datetime import datetime, timezone

import requests

# ================= KONFIGURASI =================
BINANCE_BASES = [
    "https://api.binance.com/api/v3/",
    "https://data-api.binance.vision/api/v3/",  # fallback jika api.binance.com diblokir (mis. IP US)
]
COINGECKO = "https://api.coingecko.com/api/v3/"
DEFILLAMA = "https://api.llama.fi/"
CRYPTOQUANT = "https://api.cryptoquant.com/v1/"

CQ_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

TOP_N = 300
TOP_TIER = 100              # rank 1-100 = volatilitas sedang, 101-300 = tinggi
ATR_MID = (1.5, 3.0)        # ATR 4H (%) untuk rank 1-100
CORR_MIN = 0.70
CANDLES = 120
WHALE_USD = 100_000
TVL_MIN = 15.0              # % perubahan TVL (7 hari)
TAKER_MIN = 0.55
VOL_MULT = 1.5
RSI_LO, RSI_HI = 50, 65
TP_PCT, SL_PCT = 0.02, 0.01
TOP_OUTPUT = 4

STABLE = {"usdt", "usdc", "dai", "fdusd", "tusd", "usde", "pyusd", "busd",
          "usds", "usdd", "eurc", "wbtc", "weth", "steth", "wbeth", "cbbtc"}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "analis-bot/1.0"
BASE = None  # diisi otomatis oleh binance_init()


# ================= HTTP =================
def get(url, params=None, headers=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:          # rate limit
                time.sleep(10 * (i + 1))
                last = "429 rate limit"
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Gagal GET {url}: {last}")


def binance_init():
    """Pilih base URL Binance yang bisa diakses, lalu ambil daftar pasangan USDT aktif."""
    global BASE
    for b in BINANCE_BASES:
        try:
            info = get(b + "exchangeInfo")
            BASE = b
            print(f"Binance aktif via: {b}")
            return {s["symbol"] for s in info["symbols"]
                    if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        except Exception as e:
            print(f"[warn] {b} tidak bisa diakses: {e}")
    raise RuntimeError("Semua endpoint Binance gagal diakses")


def binance(path, params=None):
    return get(BASE + path, params=params)


# ================= DATA =================
def top_coins():
    """Ambil Top 300 market cap dari CoinGecko, buang stablecoin."""
    coins, page = [], 1
    while len(coins) < TOP_N:
        data = get(COINGECKO + "coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 100, "page": page})
        if not data:
            break
        coins += data
        page += 1
        time.sleep(2)  # sopan terhadap rate limit free tier

    res = []
    for c in coins:
        rank = c.get("market_cap_rank")
        if rank is None or rank > TOP_N:
            continue
        if c["symbol"].lower() in STABLE:
            continue
        res.append({"rank": rank, "symbol": c["symbol"].upper(), "name": c["name"]})
    return res


def klines(symbol, limit=CANDLES):
    """Candle 4H yang sudah CLOSE (candle berjalan dibuang)."""
    raw = binance("klines", {"symbol": symbol, "interval": "4h", "limit": limit})
    now_ms = int(time.time() * 1000)
    out = []
    for k in raw:
        if k[6] >= now_ms:
            continue
        out.append({
            "t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]),
            "qv": float(k[7]), "tbq": float(k[10]),  # quote volume & taker buy quote
        })
    return out


def whale_4h(symbol):
    """Proksi whale: ada transaksi tunggal >= $100k dalam 1000 trade terakhir."""
    try:
        trades = binance("aggTrades", {"symbol": symbol, "limit": 1000})
        return any(float(t["p"]) * float(t["q"]) >= WHALE_USD for t in trades)
    except Exception:
        return False


def netflow_4h(asset):
    """Netflow exchange 4 jam terakhir dari CryptoQuant (butuh API key).
    Verifikasi path & field sesuai dokumentasi plan CryptoQuant kamu."""
    if not CQ_KEY:
        return None
    try:
        d = get(CRYPTOQUANT + f"{asset.lower()}/exchange-flows/netflow",
                params={"window": "hour", "limit": 4},
                headers={"Authorization": f"Bearer {CQ_KEY}"})
        rows = d["result"]["data"]
        return sum(float(x["netflow_total"]) for x in rows)
    except Exception:
        return None


def tvl_map():
    """Peta simbol -> protokol DefiLlama (TVL tertinggi per simbol)."""
    try:
        protocols = get(DEFILLAMA + "protocols")
    except Exception:
        return {}
    m = {}
    for p in protocols:
        sym = (p.get("symbol") or "").upper()
        if not sym or sym == "-":
            continue
        if sym not in m or (p.get("tvl") or 0) > (m[sym].get("tvl") or 0):
            m[sym] = p
    return m


# ================= INDIKATOR =================
def returns(xs):
    return [xs[i] / xs[i - 1] - 1 for i in range(1, len(xs))]


def corr_beta(a, b):
    """Pearson correlation dan beta (a terhadap b)."""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return 0.0, 0.0
    return cov / math.sqrt(va * vb), cov / vb


def atr_pct(cs, n=14):
    """ATR Wilder sebagai persen dari harga close."""
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i]["h"], cs[i]["l"], cs[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr / cs[-1]["c"] * 100


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


def technical(cs):
    """Breakout/support + volume >= 1.5x MA20 + RSI 50-65. Maks 40 poin."""
    last = cs[-1]
    prev = cs[-21:-1]
    pts, flags = 0, []

    hi20 = max(c["h"] for c in prev)
    lo20 = min(c["l"] for c in prev)
    if last["c"] > hi20:
        pts += 15; flags.append("breakout")
    elif last["c"] <= lo20 * 1.02:
        pts += 8; flags.append("dekat support")

    vma = sum(c["v"] for c in prev) / len(prev)
    if last["v"] >= VOL_MULT * vma:
        pts += 10; flags.append("volume>1.5x")

    r = rsi([c["c"] for c in cs])
    if RSI_LO <= r <= RSI_HI:
        pts += 15; flags.append(f"RSI {r:.0f}")
    return pts, flags


# ================= OUTPUT =================
def grade(score):
    if score >= 70:
        return "HIGH"
    if score >= 45:
        return "MEDIUM"
    return "LOW"


def fmt(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def kirim_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except Exception as e:
        print(f"[warn] Telegram gagal: {e}")


# ================= MAIN =================
def main():
    now = datetime.now(timezone.utc)
    print(f"=== analis.py | {now:%Y-%m-%d %H:%M} UTC ===")

    pairs = binance_init()
    coins = [c for c in top_coins()
             if c["symbol"] + "USDT" in pairs and c["symbol"] != "BTC"]
    print(f"Koin dengan pasangan USDT: {len(coins)}")

    btc = klines("BTCUSDT")
    btc_close = {c["t"]: c["c"] for c in btc}
    tvl = tvl_map()

    kandidat = []
    for coin in coins:
        sym = coin["symbol"] + "USDT"
        try:
            cs = klines(sym)
            time.sleep(0.1)  # jaga weight Binance
        except Exception as e:
            print(f"[skip] {sym}: {e}")
            continue
        if len(cs) < 40:
            continue

        # --- Filter 1: korelasi terhadap BTC 4H ---
        cc = {c["t"]: c["c"] for c in cs}
        ts = [t for t in cc if t in btc_close][-61:]
        if len(ts) < 31:
            continue
        r, beta = corr_beta(returns([cc[t] for t in ts]),
                            returns([btc_close[t] for t in ts]))
        if r < CORR_MIN:
            continue

        # --- Filter 2: volatilitas ATR 4H ---
        atr = atr_pct(cs)
        if coin["rank"] <= TOP_TIER:
            if not (ATR_MID[0] <= atr <= ATR_MID[1]):
                continue
        else:
            if atr <= ATR_MID[1]:
                continue

        # --- Layer 1: Technical ---
        t_pts, t_flags = technical(cs)

        # --- Layer 2: On-chain ---
        flow = netflow_4h(coin["symbol"])
        whale = whale_4h(sym)
        tv = tvl.get(coin["symbol"])
        tvl_g = tv.get("change_7d") if tv else None

        o_pts, o_notes = 0, []
        if flow is None:
            o_notes.append("netflow n/a")
        elif flow < 0:
            o_pts += 10; o_notes.append("netflow<0")
        if whale:
            o_pts += 10; o_notes.append("whale>100k")
        if tvl_g is not None and tvl_g > TVL_MIN:
            o_pts += 10; o_notes.append(f"TVL {tvl_g:+.1f}%")

        # --- Layer 3: Sentiment (taker buy ratio candle terakhir) ---
        last = cs[-1]
        taker = last["tbq"] / last["qv"] if last["qv"] else 0
        s_pts = 20 if taker > TAKER_MIN else 0

        # --- Bonus korelasi (maks 10) ---
        c_pts = round(min(r, 1.0) * 10, 1)

        score = round(t_pts + o_pts + s_pts + c_pts, 1)
        kandidat.append({
            "symbol": coin["symbol"], "rank": coin["rank"], "close": last["c"],
            "score": score, "corr": r, "beta": beta, "atr": atr,
            "tech": t_flags, "onchain": o_notes,
            "taker": taker,
        })
        print(f"[ok] {coin['symbol']:<6} rank {coin['rank']:<3} skor {score:>5} "
              f"corr {r:.2f} ATR {atr:.2f}%")

    kandidat.sort(key=lambda x: x["score"], reverse=True)
    top = kandidat[:TOP_OUTPUT]

    if not top:
        msg = f"Tidak ada koin yang lolos filter pada {now:%Y-%m-%d %H:%M} UTC."
        print(msg)
        os.makedirs("results", exist_ok=True)
        open("results/latest.txt", "w", encoding="utf-8").write(msg)
        kirim_telegram(msg)
        return

    blocks, detail = [], []
    for k in top:
        op = k["close"]
        blocks.append(
            f"NAMA KOIN     : {k['symbol']}/USDT\n"
            f"GRADE PELUANG : {grade(k['score'])}\n"
            f"OPEN POSISI   : {fmt(op)}\n"
            f"TAKE PROFIT   : {fmt(op * (1 + TP_PCT))}\n"
            f"STOP LOSS     : {fmt(op * (1 - SL_PCT))}\n"
        )
        detail.append(
            f"{k['symbol']}: skor {k['score']} | rank {k['rank']} | "
            f"corr {k['corr']:.2f} | beta {k['beta']:.2f} | ATR {k['atr']:.2f}% | "
            f"taker {k['taker']:.1%} | teknikal {', '.join(k['tech']) or '-'} | "
            f"on-chain {', '.join(k['onchain']) or '-'}"
        )

    header = f"ANALISIS {now:%Y-%m-%d %H:%M} UTC ({now.hour + 7:02d}:{now:%M} WIB)\n\n"
    text = header + "\n".join(blocks)

    print("\n" + text)
    print("--- DETAIL ---")
    print("\n".join(detail))

    os.makedirs("results", exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")
    open(f"results/hasil_{stamp}.txt", "w", encoding="utf-8").write(
        text + "\n--- DETAIL ---\n" + "\n".join(detail))
    open("results/latest.txt", "w", encoding="utf-8").write(text)

    kirim_telegram(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
