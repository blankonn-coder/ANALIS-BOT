#!/usr/bin/env python3
"""
analis.py - Scouting koin otomatis (4H, Peak Trading Hours).
Konfigurasi ada di config.json. Webhook Discord dari env DISCORD_WEBHOOK_URL.
Output: 2 HIGH, 2 MEDIUM, 2 LOW (berdasarkan urutan skor kandidat).
Output adalah hasil analisis otomatis, BUKAN saran investasi.
"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ================= KONFIG =================
BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

UNI = CFG["universe"]
FLT = CFG["filters"]
IND = CFG["indicators"]
ONC = CFG["onchain"]
SENT = CFG["sentiment"]
SC = CFG["scoring"]
OUT = CFG["output"]
DLY = CFG["delays"]
SRC = CFG["sources"]
NOTIFY = CFG["notify"]

STABLE = {s.lower() for s in UNI["stablecoins"]}
EXCLUDE = {s.upper() for s in UNI["exclude_symbols"]}

CQ_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "analis-bot/1.2"
BASE = None  # diisi oleh binance_init()


# ================= HTTP =================
def get(url, params=None, headers=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
                last = "429 rate limit"
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = type(e).__name__  # jangan cetak URL/kunci ke log
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Gagal GET {url.split('?')[0]}: {last}")


def binance_init():
    """Pilih base URL Binance yang bisa diakses, lalu ambil daftar pasangan USDT aktif."""
    global BASE
    for b in SRC["binance"]:
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
    """Ambil Top N market cap dari CoinGecko, buang stablecoin."""
    top_n = UNI["top_n"]
    coins, page = [], 1
    while len(coins) < top_n:
        data = get(SRC["coingecko"] + "coins/markets", params={
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
        if rank is None or rank > top_n:
            continue
        if c["symbol"].lower() in STABLE:
            continue
        res.append({"rank": rank, "symbol": c["symbol"].upper(), "name": c["name"]})
    return res


def klines(symbol, limit=FLT["candles"]):
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
            "qv": float(k[7]), "tbq": float(k[10]),
        })
    return out


def whale_4h(symbol):
    """Proksi whale: ada transaksi tunggal >= batas dalam N trade terakhir."""
    try:
        trades = binance("aggTrades", {"symbol": symbol, "limit": ONC["aggtrades_limit"]})
        return any(float(t["p"]) * float(t["q"]) >= ONC["whale_usd"] for t in trades)
    except Exception:
        return False


def netflow_4h(asset):
    """Netflow exchange 4 jam terakhir dari CryptoQuant (butuh API key).
    Verifikasi path & field sesuai dokumentasi plan CryptoQuant kamu."""
    if not (ONC["use_netflow"] and CQ_KEY):
        return None
    try:
        d = get(SRC["cryptoquant"] + f"{asset.lower()}/exchange-flows/netflow",
                params={"window": ONC["netflow_window"], "limit": ONC["netflow_limit"]},
                headers={"Authorization": f"Bearer {CQ_KEY}"})
        rows = d["result"]["data"]
        return sum(float(x["netflow_total"]) for x in rows)
    except Exception:
        return None


def tvl_map():
    """Peta simbol -> protokol DefiLlama (TVL tertinggi per simbol)."""
    try:
        protocols = get(SRC["defillama"] + "protocols")
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


def atr_pct(cs, n=IND["atr_period"]):
    """ATR Wilder sebagai persen dari harga close."""
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i]["h"], cs[i]["l"], cs[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr / cs[-1]["c"] * 100


def rsi(closes, n=IND["rsi_period"]):
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
    """Breakout/support + volume >= vol_mult x MA + RSI di rentang yang ditentukan."""
    n = IND["ma_period"]
    last = cs[-1]
    prev = cs[-(n + 1):-1]
    pts, flags = 0, []

    hi = max(c["h"] for c in prev)
    lo = min(c["l"] for c in prev)
    if last["c"] > hi:
        pts += SC["breakout"]; flags.append("breakout")
    elif last["c"] <= lo * 1.02:
        pts += SC["near_support"]; flags.append("dekat support")

    vma = sum(c["v"] for c in prev) / len(prev)
    if last["v"] >= IND["vol_mult"] * vma:
        pts += SC["volume"]; flags.append(f"volume>{IND['vol_mult']}x")

    r = rsi([c["c"] for c in cs])
    if IND["rsi_lo"] <= r <= IND["rsi_hi"]:
        pts += SC["rsi"]; flags.append(f"RSI {r:.0f}")
    return pts, flags


def taker_points(taker):
    """Skor taker buy ratio kontinu: 0 di batas bawah, penuh di batas atas."""
    lo, hi = SENT["taker_floor"], SENT["taker_cap"]
    frac = min(max((taker - lo) / (hi - lo), 0.0), 1.0)
    return round(frac * SC["taker"], 1)


# ================= OUTPUT =================
def fmt(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def assign_grades(ranked):
    """Ambil kandidat berurutan skor tertinggi, lalu isi slot grade.
    Dengan slot 2/2/2: 2 teratas = HIGH, 2 berikutnya = MEDIUM, 2 berikutnya = LOW."""
    result, idx = [], 0
    for label, n in OUT["slots"]:
        for _ in range(n):
            if idx >= len(ranked):
                return result
            result.append((label, ranked[idx]))
            idx += 1
    return result


def split_text(text, limit):
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


def kirim_discord(text):
    if not (NOTIFY["discord"] and DISCORD_URL):
        return
    for part in split_text(text, NOTIFY["discord_max_chars"]):
        payload = {"username": NOTIFY["discord_username"],
                   "content": f"```\n{part}\n```"}
        for _ in range(3):
            try:
                r = SESSION.post(DISCORD_URL, json=payload, timeout=15)
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
                print(f"[warn] Discord HTTP {r.status_code}: {r.text[:200]}")
            break


def kirim_telegram(text):
    if not (NOTIFY["telegram"] and TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"[warn] Telegram gagal ({type(e).__name__})")


def kirim_semua(text):
    kirim_discord(text)
    kirim_telegram(text)


# ================= MAIN =================
def main():
    now = datetime.now(timezone.utc)
    wib = now + timedelta(hours=7)
    print(f"=== analis.py | {now:%Y-%m-%d %H:%M} UTC ===")

    pairs = binance_init()
    coins = [c for c in top_coins()
             if c["symbol"] + "USDT" in pairs and c["symbol"] not in EXCLUDE]
    print(f"Koin dengan pasangan USDT: {len(coins)}")

    btc = klines("BTCUSDT")
    btc_close = {c["t"]: c["c"] for c in btc}
    tvl = tvl_map()

    w = FLT["corr_window"]
    atr_lo, atr_hi = FLT["atr_mid"]
    kandidat = []

    for coin in coins:
        sym = coin["symbol"] + "USDT"
        try:
            cs = klines(sym)
            time.sleep(DLY["request_sec"])
        except Exception as e:
            print(f"[skip] {sym}: {e}")
            continue
        if len(cs) < FLT["min_candles"]:
            continue

        # Filter 1: korelasi terhadap BTC 4H
        cc = {c["t"]: c["c"] for c in cs}
        ts = [t for t in cc if t in btc_close][-(w + 1):]
        if len(ts) < w // 2:
            continue
        r, beta = corr_beta(returns([cc[t] for t in ts]),
                            returns([btc_close[t] for t in ts]))
        if r < FLT["corr_min"]:
            continue

        # Filter 2: volatilitas ATR 4H
        atr = atr_pct(cs)
        if coin["rank"] <= UNI["top_tier"]:
            if not (atr_lo <= atr <= atr_hi):
                continue
        else:
            if atr <= atr_hi:
                continue

        # Layer 1: Technical
        t_pts, t_flags = technical(cs)

        # Layer 2: On-chain
        flow = netflow_4h(coin["symbol"])
        whale = whale_4h(sym)
        tv = tvl.get(coin["symbol"])
        tvl_g = tv.get("change_7d") if tv else None

        o_pts, o_notes = 0, []
        if flow is None:
            o_notes.append("netflow n/a")
        elif flow < 0:
            o_pts += SC["netflow"]; o_notes.append("netflow<0")
        if whale:
            o_pts += SC["whale"]; o_notes.append(f"whale>{ONC['whale_usd']:,}")
        if tvl_g is not None and tvl_g > ONC["tvl_min_pct"]:
            o_pts += SC["tvl"]; o_notes.append(f"TVL {tvl_g:+.1f}%")

        # Layer 3: Sentiment (taker buy ratio candle terakhir, kontinu)
        last = cs[-1]
        taker = last["tbq"] / last["qv"] if last["qv"] else 0
        s_pts = taker_points(taker)

        # Bonus korelasi
        c_pts = round(min(r, 1.0) * SC["corr_bonus_max"], 1)

        score = round(t_pts + o_pts + s_pts + c_pts, 1)
        kandidat.append({
            "symbol": coin["symbol"], "rank": coin["rank"], "close": last["c"],
            "score": score, "corr": r, "beta": beta, "atr": atr,
            "tech": t_flags, "onchain": o_notes, "taker": taker,
        })
        print(f"[ok] {coin['symbol']:<6} rank {coin['rank']:<3} skor {score:>5} "
              f"corr {r:.2f} ATR {atr:.2f}%")

    kandidat.sort(key=lambda x: x["score"], reverse=True)
    picks = assign_grades(kandidat)

    os.makedirs("results", exist_ok=True)

    if not picks:
        msg = f"Tidak ada koin yang lolos filter pada {now:%Y-%m-%d %H:%M} UTC."
        print(msg)
        Path("results/latest.txt").write_text(msg, encoding="utf-8")
        kirim_semua(msg)
        return

    tp_m = 1 + OUT["tp_pct"] / 100
    sl_m = 1 - OUT["sl_pct"] / 100

    blocks, detail = [], []
    for grade, k in picks:
        op = k["close"]
        blocks.append(
            f"NAMA KOIN     : {k['symbol']}/USDT\n"
            f"GRADE PELUANG : {grade}\n"
            f"OPEN POSISI   : {fmt(op)}\n"
            f"TAKE PROFIT   : {fmt(op * tp_m)}\n"
            f"STOP LOSS     : {fmt(op * sl_m)}\n"
        )
        detail.append(
            f"{grade:<6} {k['symbol']}: skor {k['score']} | rank {k['rank']} | "
            f"corr {k['corr']:.2f} | beta {k['beta']:.2f} | ATR {k['atr']:.2f}% | "
            f"taker {k['taker']:.1%} | teknikal {', '.join(k['tech']) or '-'} | "
            f"on-chain {', '.join(k['onchain']) or '-'}"
        )

    header = (f"ANALISIS {now:%Y-%m-%d %H:%M} UTC ({wib:%H:%M} WIB)\n"
              f"Kandidat lolos filter: {len(kandidat)}\n\n")
    if len(picks) < sum(n for _, n in OUT["slots"]):
        header += f"(Hanya {len(picks)} koin yang tersedia untuk slot grade)\n\n"
    text = header + "\n".join(blocks)

    print("\n" + text)
    print("--- DETAIL ---")
    print("\n".join(detail))

    stamp = now.strftime("%Y%m%d_%H%M")
    Path(f"results/hasil_{stamp}.txt").write_text(
        text + "\n--- DETAIL ---\n" + "\n".join(detail), encoding="utf-8")
    Path("results/latest.txt").write_text(text, encoding="utf-8")

    kirim_semua(text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        raise SystemExit(1)
