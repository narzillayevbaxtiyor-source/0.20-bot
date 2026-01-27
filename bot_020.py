import os
import time
import json
import asyncio
from typing import Dict, List, Optional

import requests
from telegram import Bot
from telegram.ext import Application

STATE_FILE = "state_020.json"

# ====== ENV ======
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
GROUP_CHAT_ID = int((os.getenv("GROUP_CHAT_ID") or "0").strip())

# ====== SETTINGS ======
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "30"))          # har 30 soniyada tekshiradi
THRESHOLD_PCT = float(os.getenv("THRESHOLD_PCT", "0.20"))        # 0.20% qolgan bo'lsa chiqaradi
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))              # bir tickerni 30 daq qayta yubormaslik
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "2000000"))     # USDT volume filter (2M)
ONLY_USDT = os.getenv("ONLY_USDT", "1") == "1"
PAIR_SUFFIX = os.getenv("PAIR_SUFFIX", "USDT")                   # default USDT

# Binance endpoints (fallback)
BINANCE_ENDPOINTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
]

# ====== HELPERS ======
def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_sent_ts": {}}  # symbol -> epoch seconds

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

def is_bad_symbol(sym: str) -> bool:
    # Leveraged tokenlar va shunga o'xshashlarni chiqarib tashlaymiz
    bad = ["UP" + PAIR_SUFFIX, "DOWN" + PAIR_SUFFIX, "BULL" + PAIR_SUFFIX, "BEAR" + PAIR_SUFFIX]
    return any(sym.endswith(x) for x in bad)

def distance_to_24h_high_pct(last_price: float, high_price: float) -> float:
    # (high - last) / high * 100
    if high_price <= 0:
        return 999.0
    return (high_price - last_price) / high_price * 100.0

def get_candidates_020() -> List[str]:
    """
    Topadi: USDT spot juftliklari ichida:
    dist_pct = (highPrice - lastPrice)/highPrice*100 <= THRESHOLD_PCT
    va quoteVolume >= MIN_QUOTE_VOL
    """
    data = binance_get("/api/v3/ticker/24hr")
    candidates = []

    for t in data:
        sym = t.get("symbol", "")
        if not sym:
            continue

        if ONLY_USDT:
            if not sym.endswith(PAIR_SUFFIX):
                continue

        if is_bad_symbol(sym):
            continue

        try:
            last_price = float(t["lastPrice"])
            high_price = float(t["highPrice"])
            quote_vol = float(t.get("quoteVolume", "0") or "0")
        except Exception:
            continue

        if last_price <= 0 or high_price <= 0:
            continue

        if quote_vol < MIN_QUOTE_VOL:
            continue

        dist_pct = distance_to_24h_high_pct(last_price, high_price)
        if 0 <= dist_pct <= THRESHOLD_PCT:
            candidates.append((sym, dist_pct))

    # eng yaqinlari (dist_pct kichik) tepaga
    candidates.sort(key=lambda x: x[1])
    return [s for s, _ in candidates[:20]]  # spamni cheklash

async def scan_and_post(app: Application):
    # ENV tekshiruv
    if not BOT_TOKEN or GROUP_CHAT_ID == 0:
        return

    state = load_state()
    last_sent_ts: Dict[str, float] = state.get("last_sent_ts", {})
    now = time.time()

    try:
        symbols = get_candidates_020()
    except Exception:
        return

    bot: Bot = app.bot

    for sym in symbols:
        last = float(last_sent_ts.get(sym, 0))
        if now - last < COOLDOWN_MIN * 60:
            continue

        try:
            # Guruhga faqat ticker yuboriladi
            await bot.send_message(chat_id=GROUP_CHAT_ID, text=sym)
            last_sent_ts[sym] = now
        except Exception:
            # Telegram xato bo'lsa ham bot yiqilmasin
            pass

    state["last_sent_ts"] = last_sent_ts
    save_state(state)

# ====== MAIN (PTB v20+) ======
async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    async def job(context):
        await scan_and_post(application)

    # Har SCAN_EVERY_SEC da ishlaydi
    application.job_queue.run_repeating(job, interval=SCAN_EVERY_SEC, first=5)

    await application.initialize()
    await application.start()

    # Bot doimiy ishlashi uchun "abadiy kutadi"
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
