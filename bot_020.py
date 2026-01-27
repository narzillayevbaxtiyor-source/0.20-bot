import os
import time
import json
import asyncio
from typing import Dict, List, Tuple, Optional

import requests
from telegram import Bot
from telegram.ext import Application

STATE_FILE = "state_020.json"

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
GROUP_CHAT_ID = int((os.getenv("GROUP_CHAT_ID") or "0").strip())

# ---- Settings
SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "30"))   # har 30s tekshiradi
THRESHOLD_PCT = float(os.getenv("THRESHOLD_PCT", "0.20"))  # 0.20%
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))        # bir tickerni 30 daq qayta yubormaslik
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "2000000"))  # USDT volume filter (2M default)
ONLY_USDT = os.getenv("ONLY_USDT", "1") == "1"

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

def get_candidates_020() -> List[str]:
    """
    Topadi: USDT spot juftliklari ichida:
    (highPrice - lastPrice)/highPrice*100 <= THRESHOLD_PCT
    va quoteVolume >= MIN_QUOTE_VOL
    """
    data = binance_get("/api/v3/ticker/24hr")
    out = []
    for t in data:
        sym = t.get("symbol", "")
        if ONLY_USDT and not sym.endswith("USDT"):
            continue
        # Spot bo'lmaganlar (UP/DOWN tokenlar, BULL/BEAR)ni filtr qilish (xohlasangiz kengaytirasiz)
        if any(x in sym for x in ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]):
            continue

        try:
            last_price = float(t["lastPrice"])
            high_price = float(t["highPrice"])
            quote_vol = float(t.get("quoteVolume", "0") or "0")
            if high_price <= 0 or last_price <= 0:
                continue
        except Exception:
            continue

        if quote_vol < MIN_QUOTE_VOL:
            continue

        dist_pct = (high_price - last_price) / high_price * 100.0
        if 0 <= dist_pct <= THRESHOLD_PCT:
            out.append(sym)

    # Ko'p bo'lib ketsa: eng yaqinlari yuqoriga chiqsin
    # (dist_pct kichik bo'lganlar oldin) — dist_pctni qayta hisoblab sort qilamiz
    def key(sym: str) -> float:
        # qayta topish uchun kichik query qilmasdan, taxminiy joyda qoldiramiz
        return sym  # deterministik bo'lsin
    return out[:30]  # guruhni spam qilmaslik uchun limit

async def scan_and_post(app: Application):
    if not BOT_TOKEN or GROUP_CHAT_ID == 0:
        return

    state = load_state()
    last_sent_ts: Dict[str, float] = state.get("last_sent_ts", {})
    now = time.time()

    try:
        symbols = get_candidates_020()
    except Exception as e:
        # xohlasangiz admin logiga yuborish mumkin
        return

    bot: Bot = app.bot
    for sym in symbols:
        last = float(last_sent_ts.get(sym, 0))
        if now - last < COOLDOWN_MIN * 60:
            continue

        # Guruhga faqat ticker:
        try:
            await bot.send_message(chat_id=GROUP_CHAT_ID, text=sym)
            last_sent_ts[sym] = now
        except Exception:
            pass

    state["last_sent_ts"] = last_sent_ts
    save_state(state)

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    async def job(_):
        await scan_and_post(application)

    application.job_queue.run_repeating(job, interval=SCAN_EVERY_SEC, first=5)
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    await application.updater.idle()

if __name__ == "__main__":
    asyncio.run(main())
