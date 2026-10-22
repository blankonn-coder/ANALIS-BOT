#!/usr/bin/env python3
"""
analis.py - Scouting koin otomatis (4H, Peak Trading Hours).
Konfigurasi ada di config.json. Webhook Discord dari env DISCORD_WEBHOOK_URL.
Output: 2 HIGH, 2 MEDIUM, 2 LOW (berdasarkan urutan skor kandidat).
Output adalah hasil analisis otomatis, BUKAN saran investasi.

Env opsional:
  DRY_RUN=1   -> tidak kirim ke Discord/Telegram (tulis file tetap jalan)

Config opsional (default masuk akal bila tidak ada; keputusan akhir di trader):
  sentiment.taker_lookback     : jumlah candle utk rata-rata taker (default 5)
  output.sl_atr_mult           : SL = mult * ATR%. Bila di-set bersama tp_atr_mult,
                                 mode ATR dipakai; jika tidak, fallback ke sl_pct.
  output.tp_atr_mult           : TP = mult * ATR%
  output.sl_pct_min / sl_pct_max : clamp SL% (default 1.5 / 12.0)
  output.tp_pct_min / tp_pct_max : clamp TP% (default 3.0 / 30.0)
  scoring.min_score_per_grade  : dict ambang skor per grade
                                 (default: 50% / 30% / 0 dari skor max teoritis).
                                 Set HIGH/MEDIUM/LOW ke 0 utk melumpuhkan filter.
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    WIB = ZoneInfo("Asia/Jakarta")
except Exception:  # Python < 3.9 atau tzdata tidak tersedia
    WIB = timezone(timedelta(hours=7))

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

RESULTS_DIR = BASE_DIR / "results"

CQ_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "analis-bot/1.4"
BASE = None  # diisi oleh binance_init()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("analis")


# ================= PARAMETER EFEKTIF =================
def _default_min_score_per_grade():
    """Default ambang per grade berdasarkan skala maksimum teoritis dari SC."""
    weights = {k: v for k, v in SC.items()
               if k != "min_score_per_grade" and isinstance(v, (int, float))}
    max_score = sum(weights.values()) or 100.0
    return {
        "HIGH":   round(max_score * 0.50, 1),
        "MEDIUM": round(max_score * 0.30, 1),
        "LOW":    0.0,
    }


MIN_SCORE_PER_GRADE = SC.get("min_score_per_grade")
if MIN_SCORE_PER_GRADE is None:
    MIN_SCORE_PER_GRADE = _default_min_score_per_grade()

TAKER_LOOKBACK = max(1, int(SENT.get("taker_lookback", 5)))

SL_ATR_MULT = OUT.get("sl_atr_mult")
TP_ATR_MULT = OUT.get("tp_atr_mult")
TP_SL_MODE = "ATR" if (SL_ATR_MULT and TP_ATR_MULT) else "fixed%"

SL_PCT_MIN = float(OUT.get("sl_pct_min", 1.5))
SL_PCT_MAX = float(OUT.get("sl_pct_max", 12.0))
TP_PCT_MIN = float(OUT.get("tp_pct_min", 3.0))
TP_PCT_MAX = float(OUT.get("tp_pct_max", 30.0))


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
            try:
                return r.json()
            except ValueError:
                last = "invalid JSON response"
                time.sleep(2 * (i + 1))
                continue
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Gagal GET {url.split('?')[0]}: {last}")


def binance_init():
    global BASE
    for b in SRC["binance"]:
        try:
            info = get(b + "exchangeInfo")
            BASE = b
            LOG.info(f"Binance aktif via: {b}")
            return {s["symbol"] for s in info["symbols"]
                    if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
        except Exception as e:
            LOG.warning(f"{b} tidak bisa diakses: {e}")
    raise RuntimeError("Semua endpoint Binance gagal diakses")


def binance(path, params=None):
    return get(BASE + path, params=params)


# ================= DATA =================
def top_coins():
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


def recent_block_trade(symbol):
    """Proksi whale: ada satu trade tunggal >= whale_usd di window aggTrades terakhir.

    CATATAN: window aggTrades default jauh lebih pendek dari 4 jam, jadi ini
    BUKAN agregat 4H. Untuk agregat 4H yang benar perlu pagination by timestamp.
    """
    try:
        trades = binance("aggTrades", {"symbol": symbol, "limit": ONC["aggtrades_limit"]})
        return any(float(t["p"]) * float(t["q"]) >= ONC["whale_usd"] for t in trades)
    except Exception as e:
        LOG.debug(f"aggTrades {symbol} gagal: {type(e).__name__}")
        return False


_NETFLOW_WARNED = False


def netflow_4h(asset):
    global _NETFLOW_WARNED
    if not ONC["use_netflow"]:
        return None
    if not CQ_KEY:
        if not _NETFLOW_WARNED:
            LOG.warning("netflow: CQ_KEY tidak diset — layer on-chain jalan tanpa netflow")
            _NETFLOW_WARNED = True
        return None
    try:
        d = get(SRC["cryptoquant"] + f"{asset.lower()}/exchange-flows/netflow",
                params={"window": ONC["netflow_window"], "limit": ONC["netflow_limit"]},
                headers={"Authorization": f"Bearer {CQ_KEY}"})
        rows = d["result"]["data"]
        return sum(float(x["netflow_total"]) for x in rows)
    except Exception as e:
        if not _NETFLOW_WARNED:
            LOG.warning(f"netflow {asset}: gagal ({type(e).__name__}) — cek path/field API")
            _NETFLOW_WARNED = True
        return None


def tvl_map():
    try:
        protocols = get(SRC["defillama"] + "protocols")
    except Exception as e:
        LOG.warning(f"defillama gagal: {type(e).__name__}")
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
    assert len(a) == len(b), f"panjang beda: {len(a)} vs {len(b)}"
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return 0.0, 0.0
    return cov / math.sqrt(va * vb), cov / vb


def atr_pct(cs, n=IND["atr_period"]):
    if len(cs) < n + 2:
        raise ValueError(f"atr_pct: candle kurang ({len(cs)} < {n + 2})")
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i]["h"], cs[i]["l"], cs[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr / cs[-1]["c"] * 100


def rsi(closes, n=IND["rsi_period"]):
    if len(closes) < n + 1:
        raise ValueError(f"rsi: candle kurang ({len(closes)} < {n + 1})")
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
    n = IND["ma_period"]
    if len(cs) < n + 2:
        raise ValueError(f"technical: candle kurang ({len(cs)} < {n + 2})")
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


def taker_ratio(cs, lookback=TAKER_LOOKBACK):
    n = max(1, min(lookback, len(cs)))
    recent = cs[-n:]
    tot_qv = sum(c["qv"] for c in recent)
    tot_tbq = sum(c["tbq"] for c in recent)
    return tot_tbq / tot_qv if tot_qv else 0.0


def taker_points(taker):
    lo, hi = SENT["taker_floor"], SENT["taker_cap"]
    frac = min(max((taker - lo) / (hi - lo), 0.0), 1.0)
    return round(frac * SC["taker"], 1)


def calc_tp_sl(close, atr_pct_val):
    if TP_SL_MODE == "ATR" and atr_pct_val > 0:
        sl_p = atr_pct_val * SL_ATR_MULT
        tp_p = atr_pct_val * TP_ATR_MULT
        sl_p = min(max(sl_p, SL_PCT_MIN), SL_PCT_MAX)
        tp_p = min(max(tp_p, TP_PCT_MIN), TP_PCT_MAX)
    else:
        sl_p = OUT["sl_pct"]
        tp_p = OUT["tp_pct"]
    return {
        "tp": close * (1 + tp_p / 100),
        "sl": close * (1 - sl_p / 100),
        "sl_pct": sl_p,
        "tp_pct": tp_p,
    }


# ================= OUTPUT =================
def fmt(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}"


def assign_grades(ranked):
    picks, diag = [], {}
    idx = 0
    for label, n in OUT["slots"]:
        min_s = MIN_SCORE_PER_GRADE.get(label, 0)
        filled, reason = 0, None
        while filled < n and idx < len(ranked):
            if ranked[idx]["score"] < min_s:
                reason = f"skor < {min_s}"
                break
            picks.append((label, ranked[idx]))
            idx += 1
            filled += 1
        if filled < n and reason is None:
            reason = "kandidat habis"
        diag[label] = {"filled": filled, "needed": n,
                       "reason": reason if filled < n else None}
    return picks, diag


def split_text(text, limit):
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


def kirim_discord(text):
    if DRY_RUN:
        LOG.info("[dry-run] Discord skip")
        return
    if not (NOTIFY["discord"] and DISCORD_URL):
        return
    for part in split_text(text, NOTIFY["discord_max_chars"]):
        payload = {"username": NOTIFY["discord_username"],
                   "content": f"```\n{part}\n```"}
        for _ in range(3):
            try:
                r = SESSION.post(DISCORD_URL, json=payload, timeout=15)
            except requests.RequestException as e:
                LOG.warning(f"Discord gagal ({type(e).__name__})")
                break
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            if r.status_code >= 400:
                LOG.warning(f"Discord HTTP {r.status_code}: {r.text[:200]}")
            break


def kirim_telegram(text):
    if DRY_RUN:
        LOG.info("[dry-run] Telegram skip")
        return
    if not (NOTIFY["telegram"] and TG_TOKEN and TG_CHAT):
        return
    try:
        r = SESSION.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                         data={"chat_id": TG_CHAT, "text": text}, timeout=15)
        if r.status_code >= 400:
            LOG.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        LOG.warning(f"Telegram gagal ({type(e).__name__})")


def kirim_semua(text):
    kirim_discord(text)
    kirim_telegram(text)


# ================= MAIN =================
def main():
    now = datetime.now(timezone.utc)
    wib = now.astimezone(WIB)
    LOG.info(f"=== analis.py | {now:%Y-%m-%d %H:%M} UTC ===")
    LOG.info(f"SL/TP mode: {TP_SL_MODE} | ambang grade: {MIN_SCORE_PER_GRADE} | "
             f"taker lookback: {TAKER_LOOKBACK}c")

    min_needed = max(
        FLT["min_candles"],
        IND["ma_period"] + 2,
        IND["atr_period"] + 2,
        IND["rsi_period"] + 2,
    )

    pairs = binance_init()
    coins = [c for c in top_coins()
             if c["symbol"] + "USDT" in pairs and c["symbol"] not in EXCLUDE]
    LOG.info(f"Koin dengan pasangan USDT: {len(coins)}")

    btc = klines("BTCUSDT")
    btc_close = {c["t"]: c["c"] for c in btc}

    btc_ret_cache = {}

    def btc_returns_for(ts):
        key = tuple(ts)
        if key not in btc_ret_cache:
            btc_ret_cache[key] = returns([btc_close[t] for t in ts])
        return btc_ret_cache[key]

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
            LOG.warning(f"[skip] {sym}: {e}")
            continue
        if len(cs) < min_needed:
            LOG.warning(f"[skip] {sym}: candle kurang ({len(cs)} < {min_needed})")
            continue

        cc = {c["t"]: c["c"] for c in cs}
        ts = [t for t in cc if t in btc_close][-(w + 1):]
        if len(ts) < w // 2:
            continue
        r, beta = corr_beta(returns([cc[t] for t in ts]), btc_returns_for(ts))
        if r < FLT["corr_min"]:
            continue

        atr = atr_pct(cs)
        if coin["rank"] <= UNI["top_tier"]:
            if not (atr_lo <= atr <= atr_hi):
                continue
        else:
            if atr <= atr_hi:
                continue

        t_pts, t_flags = technical(cs)

        flow = netflow_4h(coin["symbol"])
        whale = recent_block_trade(sym)
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

        taker = taker_ratio(cs)
        s_pts = taker_points(taker)

        c_pts = round(min(r, 1.0) * SC["corr_bonus_max"], 1)

        score = round(t_pts + o_pts + s_pts + c_pts, 1)
        kandidat.append({
            "symbol": coin["symbol"], "rank": coin["rank"], "close": cs[-1]["c"],
            "score": score, "corr": r, "beta": beta, "atr": atr,
            "tech": t_flags, "onchain": o_notes, "taker": taker,
        })
        LOG.info(f"[ok] {coin['symbol']:<6} rank {coin['rank']:<3} skor {score:>5} "
                 f"corr {r:.2f} ATR {atr:.2f}% taker {taker:.1%}")

    kandidat.sort(key=lambda x: x["score"], reverse=True)
    picks, diag = assign_grades(kandidat)

    RESULTS_DIR.mkdir(exist_ok=True)

    if not picks:
        msg = (f"Tidak ada kandidat yang lolos filter/ambang grade "
               f"pada {now:%Y-%m-%d %H:%M} UTC.")
        LOG.info(msg)
        (RESULTS_DIR / "latest.txt").write_text(msg, encoding="utf-8")
        kirim_semua(msg)
        return

    blocks, detail = [], []
    for grade, k in picks:
        ts_calc = calc_tp_sl(k["close"], k["atr"])
        blocks.append(
            f"NAMA KOIN     : {k['symbol']}/USDT\n"
            f"GRADE PELUANG : {grade}\n"
            f"OPEN POSISI   : {fmt(k['close'])}\n"
            f"TAKE PROFIT   : {fmt(ts_calc['tp'])} (+{ts_calc['tp_pct']:.2f}%)\n"
            f"STOP LOSS     : {fmt(ts_calc['sl'])} (-{ts_calc['sl_pct']:.2f}%)\n"
        )
        detail.append(
            f"{grade:<6} {k['symbol']}: skor {k['score']} | rank {k['rank']} | "
            f"corr {k['corr']:.2f} | beta {k['beta']:.2f} | ATR {k['atr']:.2f}% | "
            f"SL {ts_calc['sl_pct']:.2f}% / TP {ts_calc['tp_pct']:.2f}% | "
            f"taker {k['taker']:.1%} | teknikal {', '.join(k['tech']) or '-'} | "
            f"on-chain {', '.join(k['onchain']) or '-'}"
        )

    total_slots = sum(n for _, n in OUT["slots"])
    header = (f"ANALISIS {now:%Y-%m-%d %H:%M} UTC ({wib:%H:%M} WIB)\n"
              f"SL/TP mode: {TP_SL_MODE} | Kandidat lolos filter: {len(kandidat)}\n\n")
    if len(picks) < total_slots:
        missing = [f"{label} {diag[label]['filled']}/{diag[label]['needed']} "
                   f"({diag[label]['reason']})"
                   for label, _ in OUT["slots"] if diag[label]["filled"] < diag[label]["needed"]]
        header += f"(Slot kosong: {'; '.join(missing)})\n\n"
    text = header + "\n".join(blocks)

    print("\n" + text)
    print("--- DETAIL ---")
    print("\n".join(detail))

    stamp = now.strftime("%Y%m%d_%H%M%S")
    (RESULTS_DIR / f"hasil_{stamp}.txt").write_text(
        text + "\n--- DETAIL ---\n" + "\n".join(detail), encoding="utf-8")
    (RESULTS_DIR / "latest.txt").write_text(text, encoding="utf-8")

    kirim_semua(text)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("ERROR")
        raise SystemExit(1)
