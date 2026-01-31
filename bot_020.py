import os
import time
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

# =========================
# ENV
# =========================
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://data-api.binance.vision").strip()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()  # group: -100...

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")

NEAR_PCT = float(os.getenv("NEAR_PCT", "0.10"))  # 0.10%
LOOP_SLEEP_SEC = float(os.getenv("LOOP_SLEEP_SEC", "8"))

BATCH_SIZE_1D = int(os.getenv("BATCH_SIZE_1D", "20"))
BATCH_SIZE_4H = int(os.getenv("BATCH_SIZE_4H", "30"))

UNIVERSE_REFRESH_SEC = int(os.getenv("UNIVERSE_REFRESH_SEC", "3600"))

# “kun/hafta yopildi” ni tekshirish uchun referens symbol
REF_SYMBOL = (os.getenv("REF_SYMBOL", "BTCUSDT") or "BTCUSDT").strip().upper()

# Filters
BAD_PARTS = ("UPUSDT", "DOWNUSDT", "BULL", "BEAR", "3L", "3S", "5L", "5S")
STABLE_STABLE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}

SESSION = requests.Session()


# =========================
# TELEGRAM
# =========================
def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
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


# =========================
# BINANCE
# =========================
def fetch_json(url: str, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int = 2):
    return fetch_json(
        f"{BINANCE_BASE_URL}/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )

def fetch_price_map() -> Dict[str, float]:
    arr = fetch_json(f"{BINANCE_BASE_URL}/api/v3/ticker/price")
    out = {}
    for x in arr:
        try:
            out[x["symbol"]] = float(x["price"])
        except:
            pass
    return out

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

def get_last_closed_close_time(symbol: str, interval: str) -> int:
    """
    Binance'dan shu interval bo'yicha oxirgi yopilgan candle closeTime ni qaytaradi.
    """
    kl = fetch_klines(symbol, interval, 2)
    last_closed = kline_to_ohlc(kl[-2])
    return last_closed[5]


# =========================
# UNIVERSE (ALL SPOT USDT)
# =========================
@dataclass
class Universe:
    symbols: List[str] = field(default_factory=list)
    last_refresh_ts: float = 0.0

    def refresh_if_needed(self):
        now = time.time()
        if self.symbols and (now - self.last_refresh_ts) < UNIVERSE_REFRESH_SEC:
            return

        info = fetch_json(f"{BINANCE_BASE_URL}/api/v3/exchangeInfo")
        out: List[str] = []

        for s in info.get("symbols", []):
            sym = s.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if sym in STABLE_STABLE or any(x in sym for x in BAD_PARTS):
                continue
            if s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != "USDT":
                continue

            permissions = s.get("permissions", [])
            permission_sets = s.get("permissionSets", [])
            is_spot = ("SPOT" in permissions) or any(("SPOT" in ps) for ps in permission_sets)

            if is_spot:
                out.append(sym)

        out.sort()
        self.symbols = out
        self.last_refresh_ts = now


# =========================
# WATCH STATE
# =========================
@dataclass
class WatchState:
    # Binance candle closeTime (source of truth)
    last_1d_close_time: Optional[int] = None
    last_1w_close_time: Optional[int] = None

    # weekly close -> reload all last-closed 1D highs
    need_load_1d: bool = False
    load_1d_index: int = 0

    # daily close -> reload all last-closed 4H highs
    need_load_4h: bool = False
    load_4h_index: int = 0

    watch_1d_high: Dict[str, float] = field(default_factory=dict)
    watch_4h_high: Dict[str, float] = field(default_factory=dict)

    sent_1d: Set[str] = field(default_factory=set)
    sent_4h: Set[str] = field(default_factory=set)

ST = WatchState()


# =========================
# LOADERS
# =========================
def start_new_week():
    ST.need_load_1d = True
    ST.load_1d_index = 0
    ST.watch_1d_high.clear()
    ST.sent_1d.clear()

def start_new_day():
    ST.need_load_4h = True
    ST.load_4h_index = 0
    ST.watch_4h_high.clear()
    ST.sent_4h.clear()

def load_batch_1d_highs(universe: Universe):
    if not ST.need_load_1d:
        return

    syms = universe.symbols
    if not syms:
        return

    end = min(ST.load_1d_index + BATCH_SIZE_1D, len(syms))
    batch = syms[ST.load_1d_index:end]

    for sym in batch:
        try:
            kl = fetch_klines(sym, "1d", 2)
            last_closed = kline_to_ohlc(kl[-2])
            _, _, h, _, _, _ = last_closed
            ST.watch_1d_high[sym] = h
        except Exception:
            pass

    ST.load_1d_index = end
    if ST.load_1d_index >= len(syms):
        ST.need_load_1d = False

def load_batch_4h_highs(universe: Universe):
    if not ST.need_load_4h:
        return

    syms = universe.symbols
    if not syms:
        return

    end = min(ST.load_4h_index + BATCH_SIZE_4H, len(syms))
    batch = syms[ST.load_4h_index:end]

    for sym in batch:
        try:
            kl = fetch_klines(sym, "4h", 2)
            last_closed = kline_to_ohlc(kl[-2])
            _, _, h, _, _, _ = last_closed
            ST.watch_4h_high[sym] = h
        except Exception:
            pass

    ST.load_4h_index = end
    if ST.load_4h_index >= len(syms):
        ST.need_load_4h = False


# =========================
# SIGNAL CHECK
# =========================
def remaining_pct_to_level(price: float, level: float) -> float:
    if level <= 0:
        return 999.0
    return ((level - price) / level) * 100.0

def check_signals(price_map: Dict[str, float]):
    # 1D high near-break (armed after weekly close)
    for sym, lvl in ST.watch_1d_high.items():
        if sym in ST.sent_1d:
            continue
        price = price_map.get(sym)
        if price is None:
            continue

        rem = remaining_pct_to_level(price, lvl)
        if 0.0 <= rem <= NEAR_PCT:
            ST.sent_1d.add(sym)
            tg_send(f"📈 <b>{sym}</b>\n<b>1d high break</b>\nQolgan: <b>{rem:.4f}%</b>")

    # 4H high near-break (armed after daily close)
    for sym, lvl in ST.watch_4h_high.items():
        if sym in ST.sent_4h:
            continue
        price = price_map.get(sym)
        if price is None:
            continue

        rem = remaining_pct_to_level(price, lvl)
        if 0.0 <= rem <= NEAR_PCT:
            ST.sent_4h.add(sym)
            tg_send(f"📈 <b>{sym}</b>\n<b>4h high break</b>\nQolgan: <b>{rem:.4f}%</b>")


# =========================
# MAIN
# =========================
def main():
    uni = Universe()
    tg_send("✅ All-coins high-break bot start (Binance closeTime bilan).")

    while True:
        try:
            uni.refresh_if_needed()

            # 1) Binance close-time asosida "yangi kun/hafta yopildi" ni aniqlaymiz
            cur_1d_close = get_last_closed_close_time(REF_SYMBOL, "1d")
            cur_1w_close = get_last_closed_close_time(REF_SYMBOL, "1w")

            # init
            if ST.last_1d_close_time is None:
                ST.last_1d_close_time = cur_1d_close
                start_new_day()   # birinchi ishga tushganda 4h high larni yuklaydi
            if ST.last_1w_close_time is None:
                ST.last_1w_close_time = cur_1w_close
                start_new_week()  # birinchi ishga tushganda 1d high larni yuklaydi

            # new daily close
            if cur_1d_close != ST.last_1d_close_time:
                ST.last_1d_close_time = cur_1d_close
                start_new_day()

            # new weekly close
            if cur_1w_close != ST.last_1w_close_time:
                ST.last_1w_close_time = cur_1w_close
                start_new_week()

            # 2) batch bilan high'larni yuklash
            load_batch_1d_highs(uni)
            load_batch_4h_highs(uni)

            # 3) bitta snapshot price bilan signal tekshirish
            prices = fetch_price_map()
            check_signals(prices)

        except Exception as e:
            print("[ERROR]", e)

        time.sleep(LOOP_SLEEP_SEC)

if __name__ == "__main__":
    main()
