import os
import time
import json
import asyncio
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List, Tuple

import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

STATE_FILE = "state_3m.json"

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_DM_CHAT_ID = int((os.getenv("ADMIN_DM_CHAT_ID") or "0").strip())

# Muboh botni aniq ajratish:
# 1) Username orqali (masalan: "my_muboh_bot") — @ belgisiz yozing
MUBOH_BOT_USERNAME = (os.getenv("MUBOH_BOT_USERNAME") or "").strip().lstrip("@")

# 2) Yoki bot user_id orqali (eng ishonchli)
MUBOH_BOT_ID = int((os.getenv("MUBOH_BOT_ID") or "0").strip() or "0")

# ---- 3m settings
PAIR_SUFFIX = os.getenv("PAIR_SUFFIX", "USDT")  # LTC -> LTCUSDT
INTERVAL = "3m"
KLINE_LIMIT = int(os.getenv("KLINE_LIMIT", "200"))
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "20"))  # 3m ichida tez-tez tekshiradi
SYMBOL_COOLDOWN_MIN = int(os.getenv("SYMBOL_COOLDOWN_MIN", "60"))  # muboh bo'lgandan keyin qayta trigger qilmaslik
MIN_PULLBACK_CANDLES = int(os.getenv("MIN_PULLBACK_CANDLES", "1"))  # kamida 1 pullback candle
LOOKBACK_FOR_HIGH = int(os.getenv("LOOKBACK_FOR_HIGH", "20"))       # make-a-high aniqlash oynasi

BINANCE_ENDPOINTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
]

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"symbols": {}}

def save_state(state: Dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def binance_get(path: str, params: Optional[dict] = None, timeout: int = 10):
    last_err = None
    for base in BINANCE_ENDPOINTS:
        try:
            r = requests.get(base + path, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Binance request failed on all endpoints: {last_err}")

def get_last_price(symbol: str) -> float:
    j = binance_get("/api/v3/ticker/price", params={"symbol": symbol})
    return float(j["price"])

def get_klines(symbol: str, interval: str, limit: int) -> List[List]:
    return binance_get("/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})

def parse_ticker_from_muboh_text(text: str) -> Optional[str]:
    """
    Sizning formatga mos:
    1) "LTC - Litecoin" (1-qatorda)
    2) "Litecoin (LTC)"
    """
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    import re

    # 1) 1-qatordan: TICKER - Name
    m = re.match(r"^([A-Z0-9]{2,10})\s*-\s*.+$", lines[0])
    if m:
        return m.group(1)

    # 2) Qavs ichidan (LTC)
    m2 = re.search(r"\(([A-Z0-9]{2,10})\)", text)
    if m2:
        return m2.group(1)

    # 3) fallback: birinchi katta harfli token
    m3 = re.search(r"\b([A-Z0-9]{2,10})\b", text)
    if m3:
        token = m3.group(1)
        # keraksiz so'zlarni chiqarib tashlash
        if token not in {"MUBOH", "USDT", "USD", "BTC", "ETH", "RSI", "MACD"}:
            return token

    return None

def is_muboh_message(update: Update) -> bool:
    msg = update.effective_message
    if not msg or not msg.text:
        return False
    txt = msg.text.upper()
    if "MUBOH" not in txt:
        return False

    u = msg.from_user
    if not u:
        return False

    if MUBOH_BOT_ID and u.id == MUBOH_BOT_ID:
        return True

    if MUBOH_BOT_USERNAME and (u.username or "").lower() == MUBOH_BOT_USERNAME.lower():
        return True

    # Agar ikkalasi ham berilmagan bo'lsa, xavfsizlik uchun false:
    return False

@dataclass
class SymbolState:
    symbol: str
    active: bool = False          # muboh bo'lib navbatga kirgan
    last_muboh_ts: float = 0.0
    bought: bool = False
    buy_level: float = 0.0        # pullback candle high
    trail_low: float = 0.0        # oxirgi yopilgan candle low (sell uchun)
    last_signal_ts: float = 0.0

def find_setup_levels(kl: List[List]) -> Optional[Tuple[float, float]]:
    """
    BUY setup:
    - "make a high": so'nggi LOOKBACK_FOR_HIGH candle ichida eng katta high bo'lgan joy topiladi
    - undan keyin kamida MIN_PULLBACK_CANDLES ta pullback (bearish yoki pastroq close) bo'lsin
    - "pullback candle": oxirgi yopilgan candle (eng oxirgi closed candle) pullback hisoblanadi
    - BUY level = pullback candle HIGH
    Return: (buy_level, pullback_low_ref)
    """
    if len(kl) < LOOKBACK_FOR_HIGH + 5:
        return None

    # Klines: [openTime, open, high, low, close, volume, closeTime, ...]
    highs = [float(x[2]) for x in kl]
    lows  = [float(x[3]) for x in kl]
    opens = [float(x[1]) for x in kl]
    closes= [float(x[4]) for x in kl]

    # Oxirgi yopilgan candle indexi:
    last_closed = len(kl) - 2

    # Make-a-high: so'nggi LOOKBACK_FOR_HIGH oynada max high topamiz (last_closed dan oldin)
    start = max(0, last_closed - LOOKBACK_FOR_HIGH)
    window_highs = highs[start:last_closed+1]
    if not window_highs:
        return None
    max_high = max(window_highs)
    idx_high = start + window_highs.index(max_high)

    # High dan keyin pullback bo'lishi shart: idx_high < last_closed
    if idx_high >= last_closed - MIN_PULLBACK_CANDLES:
        return None

    # Pullback candlelar: idx_high+1 dan last_closed gacha kamida MIN_PULLBACK_CANDLES ta
    pullback_count = 0
    for i in range(idx_high + 1, last_closed + 1):
        # pullbackni soddalashtirib: bearish candle yoki close < prev close
        prev_close = closes[i-1] if i-1 >= 0 else closes[i]
        if closes[i] < opens[i] or closes[i] < prev_close:
            pullback_count += 1

    if pullback_count < MIN_PULLBACK_CANDLES:
        return None

    # BUY trigger candle = oxirgi yopilgan candle (pullbackdagi oxirgi yopilgan sham)
    buy_level = highs[last_closed]
    pullback_low_ref = lows[last_closed]
    return (buy_level, pullback_low_ref)

async def run_analysis_and_maybe_signal(app: Application, st: SymbolState) -> SymbolState:
    now = time.time()

    # klines olib ko'ramiz
    try:
        kl = get_klines(st.symbol, INTERVAL, KLINE_LIMIT)
        last_price = get_last_price(st.symbol)
    except Exception:
        return st

    # Oxirgi yopilgan candle low (sell uchun trail)
    try:
        last_closed_low = float(kl[-2][3])
    except Exception:
        return st

    # BUY topish (faqat bought=false bo'lsa)
    if not st.bought:
        setup = find_setup_levels(kl)
        if setup:
            buy_level, _ = setup
            st.buy_level = buy_level

            # BUY: narx pullback candle HIGH ni kesib o'tsa
            if last_price > buy_level:
                st.bought = True
                st.trail_low = last_closed_low
                st.last_signal_ts = now

                await app.bot.send_message(
                    chat_id=ADMIN_DM_CHAT_ID,
                    text=f"✅ BUY {st.symbol}\n"
                         f"Breakout: price {last_price:.6f} > pullback_high {buy_level:.6f}\n"
                         f"TF: {INTERVAL}"
                )
        return st

    # SELL (faqat BUY'dan keyin)
    # Trail doim oxirgi yopilgan candle low ga yangilanadi
    st.trail_low = last_closed_low

    if last_price < st.trail_low:
        st.bought = False
        st.buy_level = 0.0
        st.last_signal_ts = now

        await app.bot.send_message(
            chat_id=ADMIN_DM_CHAT_ID,
            text=f"🟥 SELL {st.symbol}\n"
                 f"Breakdown: price {last_price:.6f} < last_closed_low {st.trail_low:.6f}\n"
                 f"TF: {INTERVAL}"
        )
    return st

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # faqat muboh botdan kelgan "MUBOH" postlarni ushlaymiz
    if not is_muboh_message(update):
        return

    text = (update.effective_message.text or "").strip()
    ticker = parse_ticker_from_muboh_text(text)
    if not ticker:
        return

    symbol = f"{ticker}{PAIR_SUFFIX}"
    state = load_state()
    symbols: Dict[str, Dict] = state.get("symbols", {})
    now = time.time()

    # cooldown (muboh qayta kelib qolsa)
    existing = symbols.get(symbol)
    if existing:
        last_muboh_ts = float(existing.get("last_muboh_ts", 0))
        if now - last_muboh_ts < SYMBOL_COOLDOWN_MIN * 60:
            return

    st = SymbolState(symbol=symbol, active=True, last_muboh_ts=now)
    symbols[symbol] = asdict(st)
    state["symbols"] = symbols
    save_state(state)

    # Admin DMga "qabul qildim" deb ham yuborib qo'yamiz (xohlasangiz o'chirasiz)
    await context.bot.send_message(
        chat_id=ADMIN_DM_CHAT_ID,
        text=f"🟢 MUBOH detected: {symbol}\n3m analiz navbatga qo'shildi."
    )

async def periodic_scan(app: Application):
    if ADMIN_DM_CHAT_ID == 0:
        return

    state = load_state()
    symbols: Dict[str, Dict] = state.get("symbols", {})
    if not symbols:
        return

    changed = False
    for sym, raw in list(symbols.items()):
        try:
            st = SymbolState(**raw)
        except Exception:
            continue

        # faqat active symbol
        if not st.active:
            continue

        # analiz
        new_st = await run_analysis_and_maybe_signal(app, st)
        symbols[sym] = asdict(new_st)
        changed = True

    if changed:
        state["symbols"] = symbols
        save_state(state)

async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_message))

    async def job(_):
        await periodic_scan(application)

    application.job_queue.run_repeating(job, interval=SCAN_EVERY_SEC, first=8)

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await application.updater.idle()

if __name__ == "__main__":
    asyncio.run(main())
