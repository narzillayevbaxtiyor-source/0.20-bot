import os
import time
import requests
from dataclasses import dataclass
from typing import Optional, Tuple

# =========================
# ENV
# =========================
BINANCE_BASE = os.getenv("BINANCE_BASE", "https://data-api.binance.vision").strip()

SYMBOL = (os.getenv("SYMBOL", "BTCUSDT") or "").strip().upper()

TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

POLL_SEC = float(os.getenv("POLL_SEC", "8"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo'q")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_CHAT_ID env yo'q")
if not SYMBOL.endswith("USDT"):
    raise RuntimeError("SYMBOL USDT bilan tugashi kerak, masalan BTCUSDT")

SESSION = requests.Session()

# =========================
# HELPERS
# =========================
def fetch_json(url: str, params=None):
    r = SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int = 2):
    return fetch_json(f"{BINANCE_BASE}/api/v3/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })

def fetch_price(symbol: str) -> float:
    d = fetch_json(f"{BINANCE_BASE}/api/v3/ticker/price", {"symbol": symbol})
    return float(d["price"])

def kline_to_ohlc(k) -> Tuple[int, float, float, float, float, int]:
    # openTime, open, high, low, close, closeTime
    return int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), int(k[6])

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = SESSION.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=25)
    if r.status_code != 200:
        print("[TG SEND ERROR]", r.status_code, r.text)

# =========================
# STATE
# =========================
@dataclass
class WatchState:
    # Candle close tracking
    last_week_close_time: Optional[int] = None
    last_day_close_time: Optional[int] = None

    # Active watch levels (armed after higher timeframe close)
    watch_1d_high: Optional[float] = None
    watch_4h_high: Optional[float] = None

    # Prevent duplicate alerts for same armed level
    sent_1d_break: bool = False
    sent_4h_break: bool = False

ST = WatchState()

# =========================
# CORE
# =========================
def update_weekly_trigger(symbol: str):
    """Agar yangi 1w sham yopilgan bo'lsa, 1d high watch ni yangilaydi."""
    # 2 ta kline: [-2] last closed, [-1] current
    wk = fetch_klines(symbol, "1w", 2)
    last_closed = kline_to_ohlc(wk[-2])
    _, _, _, _, _, wk_close_time = last_closed

    if ST.last_week_close_time is None:
        ST.last_week_close_time = wk_close_time
        return  # birinchi ishga tushganda signal aralashtirmaymiz

    if wk_close_time != ST.last_week_close_time:
        # yangi haftalik sham yopildi
        ST.last_week_close_time = wk_close_time

        # endi oxirgi yopilgan 1D sham HIGH ni kuzatamiz
        d = fetch_klines(symbol, "1d", 2)
        d_last = kline_to_ohlc(d[-2])
        _, _, d_high, _, _, _ = d_last

        ST.watch_1d_high = d_high
        ST.sent_1d_break = False  # yangi watch -> qayta ruxsat

def update_daily_trigger(symbol: str):
    """Agar yangi 1d sham yopilgan bo'lsa, 4h high watch ni yangilaydi."""
    d = fetch_klines(symbol, "1d", 2)
    d_last = kline_to_ohlc(d[-2])
    _, _, _, _, _, d_close_time = d_last

    if ST.last_day_close_time is None:
        ST.last_day_close_time = d_close_time
        return

    if d_close_time != ST.last_day_close_time:
        # yangi kunlik sham yopildi
        ST.last_day_close_time = d_close_time

        # endi oxirgi yopilgan 4H sham HIGH ni kuzatamiz
        h4 = fetch_klines(symbol, "4h", 2)
        h4_last = kline_to_ohlc(h4[-2])
        _, _, h4_high, _, _, _ = h4_last

        ST.watch_4h_high = h4_high
        ST.sent_4h_break = False  # yangi watch -> qayta ruxsat

def check_breaks(symbol: str):
    price = fetch_price(symbol)

    # 1D high break (armed after weekly close)
    if ST.watch_1d_high is not None and not ST.sent_1d_break:
        if price > ST.watch_1d_high:
            ST.sent_1d_break = True
            tg_send(f"📈 <b>{symbol}</b>\n<b>1d high break</b>")

    # 4H high break (armed after daily close)
    if ST.watch_4h_high is not None and not ST.sent_4h_break:
        if price > ST.watch_4h_high:
            ST.sent_4h_break = True
            tg_send(f"📈 <b>{symbol}</b>\n<b>4h high break</b>")

def main():
    tg_send(f"✅ High-break bot start: <b>{SYMBOL}</b>")

    while True:
        try:
            # 1) close events
            update_weekly_trigger(SYMBOL)
            update_daily_trigger(SYMBOL)

            # 2) price breaks
            check_breaks(SYMBOL)

        except Exception as e:
            print("[ERROR]", e)

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
