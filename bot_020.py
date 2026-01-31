import os
import time
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# =========================
# ENV
# =========================
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").strip()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")

TOP_N = int(os.getenv("TOP_N", "50"))                  # Top 50
NEAR_PCT = float(os.getenv("NEAR_PCT", "0.10"))        # 0.10%

# pacing
LOOP_SLEEP_SEC = float(os.getenv("LOOP_SLEEP_SEC", "2"))
PRICE_REFRESH_SEC = float(os.getenv("PRICE_REFRESH_SEC", "8"))

# candle close checks
DAY_CHECK_SEC = float(os.getenv("DAY_CHECK_SEC", "60"))     # 1d closeTime check
WEEK_CHECK_SEC = float(os.getenv("WEEK_CHECK_SEC", "120"))  # 1w closeTime check

# top list refresh
TOP_REFRESH_SEC = float(os.getenv("TOP_REFRESH_SEC", "300"))  # 5 min

# batch loading (klines)
BATCH_1D = int(os.getenv("BATCH_1D", "12"))
BATCH_4H = int(os.getenv("BATCH_4H", "18"))

# reference symbol for candle closeTime (should exist always)
REF_SYMBOL = (os.getenv("REF_SYMBOL", "BTCUSDT") or "BTCUSDT").strip().upper()

# leveraged/stable filters
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "top50-weekly-daily-4h-high-near/1.0"})


# =========================
# TELEGRAM
# =========================
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=25,
        )
        if r.status_code != 200:
            print("[TG SEND ERROR]", r.status_code, r.text)
    except Exception as e:
        print("[TG SEND EXC]", e)


# =========================
# BINANCE HELPERS
# =========================
def fetch_json(url: str, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int = 2):
    return fetch_json(f"{BINANCE_BASE_URL}/api/v3/klines",
                      {"symbol": symbol, "interval": interval, "limit": limit})

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

def last_closed_close_time(symbol: str, interval: str) -> int:
    kl = fetch_klines(symbol, interval, 2)
    last_closed = kline_to_ohlc(kl[-2])
    return last_closed[5]

def fetch_all_prices() -> Dict[str, float]:
    arr = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/price")
    out = {}
    for x in arr:
        try:
            out[x["symbol"]] = float(x["price"])
        except:
            pass
    return out

def get_top_gainers_usdt(top_n: int) -> List[str]:
    """
    24h gainers (USDT) top N.
    SPOT statusni exchangeInfo bilan tekshirmaymiz (tezlik),
    kline/price ishlamasa baribir skip bo'ladi.
    """
    data = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/24hr")
    items = []
    for d in data:
        sym = d.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym in STABLE_STABLE or any(x in sym for x in BAD_PARTS):
            continue
        try:
            pct = float(d.get("priceChangePercent", "0"))
        except:
            continue
        items.append((pct, sym))

    items.sort(reverse=True, key=lambda x: x[0])
    return [s for _, s in items[:top_n]]

def remaining_pct(price: float, level: float) -> float:
    # 0 -> levelga yetdi yoki tepada
    if level <= 0:
        return 999.0
    return ((level - price) / level) * 100.0


# =========================
# STATE MODELS
# =========================
@dataclass
class LevelWatch:
    level: float
    sent: bool = False
    broken_up: bool = False   # price >= level bo'lib tepaga yorib o'tganmi (keyin retestda ham signal bermaslik)

@dataclass
class SymbolWatches:
    # armed after weekly close: watch last closed 1D high
    d1: Optional[LevelWatch] = None
    # armed after daily close: watch last closed 4H high
    h4: Optional[LevelWatch] = None

@dataclass
class BotState:
    top_symbols: List[str] = field(default_factory=list)
    last_top_refresh_ts: float = 0.0

    last_week_close_time: Optional[int] = None
    last_day_close_time: Optional[int] = None

    # loaders
    need_load_d1: bool = False
    load_d1_index: int = 0

    need_load_h4: bool = False
    load_h4_index: int = 0

    sym: Dict[str, SymbolWatches] = field(default_factory=dict)

ST = BotState()


# =========================
# ARM EVENTS
# =========================
def refresh_top50(force: bool = False):
    now = time.time()
    if (not force) and ST.top_symbols and (now - ST.last_top_refresh_ts) < TOP_REFRESH_SEC:
        return
    ST.top_symbols = get_top_gainers_usdt(TOP_N)
    ST.last_top_refresh_ts = now

def on_weekly_close():
    """
    1W yopilganda:
    - top50 refresh
    - 1D high watchersni yangidan yuklashni boshlaymiz
    - eski d1 watcherlar reset (cycle yangilandi)
    """
    refresh_top50(force=True)

    # d1 cycle reset
    for s in ST.top_symbols:
        ST.sym.setdefault(s, SymbolWatches()).d1 = None

    ST.need_load_d1 = True
    ST.load_d1_index = 0

def on_daily_close():
    """
    1D yopilganda:
    - top50 refresh (ixtiyoriy, lekin foydali)
    - 4H high watchersni yangidan yuklashni boshlaymiz
    - eski h4 watcherlar reset
    """
    refresh_top50(force=True)

    # h4 cycle reset
    for s in ST.top_symbols:
        ST.sym.setdefault(s, SymbolWatches()).h4 = None

    ST.need_load_h4 = True
    ST.load_h4_index = 0


# =========================
# LOADERS
# =========================
def load_batch_d1_high():
    if not ST.need_load_d1:
        return
    syms = ST.top_symbols
    if not syms:
        return

    end = min(ST.load_d1_index + BATCH_1D, len(syms))
    batch = syms[ST.load_d1_index:end]

    for sym in batch:
        try:
            kl = fetch_klines(sym, "1d", 2)
            last_closed = kline_to_ohlc(kl[-2])
            _, _, high, _, _, _ = last_closed
            ST.sym.setdefault(sym, SymbolWatches()).d1 = LevelWatch(level=high)
        except Exception:
            pass

    ST.load_d1_index = end
    if ST.load_d1_index >= len(syms):
        ST.need_load_d1 = False

def load_batch_h4_high():
    if not ST.need_load_h4:
        return
    syms = ST.top_symbols
    if not syms:
        return

    end = min(ST.load_h4_index + BATCH_4H, len(syms))
    batch = syms[ST.load_h4_index:end]

    for sym in batch:
        try:
            kl = fetch_klines(sym, "4h", 2)
            last_closed = kline_to_ohlc(kl[-2])
            _, _, high, _, _, _ = last_closed
            ST.sym.setdefault(sym, SymbolWatches()).h4 = LevelWatch(level=high)
        except Exception:
            pass

    ST.load_h4_index = end
    if ST.load_h4_index >= len(syms):
        ST.need_load_h4 = False


# =========================
# SIGNAL LOGIC
# =========================
def check_one_watch(sym: str, price: float, w: LevelWatch, label: str):
    """
    label: "1d" or "4h"
    Qoidalar:
    - narx levelga 0.10% qolganda -> 1 marta BUY
    - narx levelni tepaga yorib o'tsa -> broken_up=True
    - keyin pastga qaytib leveldan pastga o'tishi (retest) -> BUY bermaydi
    """
    # once broken above, never signal again in this cycle
    if price >= w.level:
        w.broken_up = True
        return

    if w.sent:
        return
    if w.broken_up:
        return

    rem = remaining_pct(price, w.level)
    if 0.0 <= rem <= NEAR_PCT:
        w.sent = True
        tg_send(
            f"📈 <b>BUY</b> <b>{sym}</b>\n"
            f"<b>{label} high near</b> ({NEAR_PCT:.2f}%)\n"
            f"Qolgan: <b>{rem:.4f}%</b>"
        )

def check_signals(price_map: Dict[str, float]):
    for sym in ST.top_symbols:
        price = price_map.get(sym)
        if price is None:
            continue

        sw = ST.sym.get(sym)
        if not sw:
            continue

        if sw.d1 is not None:
            check_one_watch(sym, price, sw.d1, "1d")

        if sw.h4 is not None:
            check_one_watch(sym, price, sw.h4, "4h")


# =========================
# MAIN
# =========================
def main():
    tg_send("✅ Top50 SPOT: 1W→1D high near & 1D→4H high near bot start.")

    last_price_ts = 0.0
    last_week_check_ts = 0.0
    last_day_check_ts = 0.0
    backoff = 1.0

    # initial
    refresh_top50(force=True)

    while True:
        try:
            now = time.time()

            # refresh top50 periodically
            refresh_top50(force=False)

            # check weekly closeTime
            if (now - last_week_check_ts) >= WEEK_CHECK_SEC:
                last_week_check_ts = now
                wk_close = last_closed_close_time(REF_SYMBOL, "1w")

                if ST.last_week_close_time is None:
                    ST.last_week_close_time = wk_close
                    on_weekly_close()
                elif wk_close != ST.last_week_close_time:
                    ST.last_week_close_time = wk_close
                    on_weekly_close()

            # check daily closeTime
            if (now - last_day_check_ts) >= DAY_CHECK_SEC:
                last_day_check_ts = now
                d_close = last_closed_close_time(REF_SYMBOL, "1d")

                if ST.last_day_close_time is None:
                    ST.last_day_close_time = d_close
                    on_daily_close()
                elif d_close != ST.last_day_close_time:
                    ST.last_day_close_time = d_close
                    on_daily_close()

            # batch load levels
            load_batch_d1_high()
            load_batch_h4_high()

            # prices and signals
            if (now - last_price_ts) >= PRICE_REFRESH_SEC:
                last_price_ts = now
                prices = fetch_all_prices()
                check_signals(prices)

            backoff = 1.0

        except Exception as e:
            print("[ERROR]", e)
            time.sleep(min(60.0, backoff))
            backoff *= 2.0

        time.sleep(LOOP_SLEEP_SEC)


if __name__ == "__main__":
    main()
