import os
import time
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ======================
# ENV
# ======================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TOP_N = int(os.getenv("TOP_N") or "50")

SCAN_PRICE_SEC = float(os.getenv("SCAN_PRICE_SEC") or "5")           # price poll
REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")       # top gainers refresh
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "60")  # kline refresh

NEAR_PCT = float(os.getenv("NEAR_PCT") or "0.02")  # 2% near daily high

STATE_FILE = os.getenv("STATE_FILE") or "state_weekly_daily_buy.json"
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yo'q")

# ======================
# DATA
# ======================
@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int

def now_ms() -> int:
    return int(time.time() * 1000)

# ======================
# STATE
# ======================
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "symbols": {},
        "top_symbols": [],
        "last_top_refresh_ms": 0,
    }

def save_state(st: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def sym_state(st: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    s = st["symbols"].get(symbol)
    if not s:
        s = {
            "last_1w_closed_open": None,
            "last_1d_closed_open": None,

            "last_1d_high": None,         # last CLOSED 1D high

            # Weekly arm -> Daily near-high BUY (1x)
            "armed_weekly": None,         # weekly open_time that armed
            "buy_done_weekly": None,      # same weekly open_time when buy already fired
        }
        st["symbols"][symbol] = s
    return s

# ======================
# HTTP / TG
# ======================
async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        r.raise_for_status()
        return await r.json()

async def tg_send_text(session: aiohttp.ClientSession, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
        await r.text()

# ======================
# BINANCE
# ======================
async def get_top_gainers(session: aiohttp.ClientSession, top_n: int) -> List[str]:
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    data = await http_get_json(session, url)

    usdt = []
    for x in data:
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym.endswith("BUSDUSDT") or sym.endswith("USDCUSDT"):
            continue
        if "UPUSDT" in sym or "DOWNUSDT" in sym or "BULLUSDT" in sym or "BEARUSDT" in sym:
            continue
        try:
            pct = float(x.get("priceChangePercent", "0") or "0")
        except Exception:
            continue
        usdt.append((sym, pct))

    usdt.sort(key=lambda t: t[1], reverse=True)
    return [s for s, _ in usdt[:top_n]]

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int) -> List[Candle]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    raw = await http_get_json(session, url, params={"symbol": symbol, "interval": interval, "limit": str(limit)})
    out: List[Candle] = []
    for k in raw:
        out.append(Candle(
            open_time=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            close_time=int(k[6]),
        ))
    return out

async def get_price_map(session: aiohttp.ClientSession, symbols: List[str]) -> Dict[str, float]:
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    data = await http_get_json(session, url)
    wanted = set(symbols)
    mp: Dict[str, float] = {}
    for x in data:
        sym = x.get("symbol")
        if sym in wanted:
            mp[sym] = float(x["price"])
    return mp

def last_closed(candles: List[Candle]) -> Candle:
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

# ======================
# CORE LOGIC
# ======================
async def refresh_weekly_daily(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str) -> None:
    ss = sym_state(st, symbol)

    # weekly
    w = await get_klines(session, symbol, "1w", 3)
    w_closed = last_closed(w)
    if ss["last_1w_closed_open"] != w_closed.open_time:
        ss["last_1w_closed_open"] = w_closed.open_time

        # ✅ Haftalik yopilganda "arm" qilamiz va buy_done ni reset qilamiz
        ss["armed_weekly"] = w_closed.open_time
        ss["buy_done_weekly"] = None

        await tg_send_text(session, f"🗓 WEEKLY CLOSED | {symbol} | close={w_closed.close}")

    # daily
    d = await get_klines(session, symbol, "1d", 3)
    d_closed = last_closed(d)
    if ss["last_1d_closed_open"] != d_closed.open_time:
        ss["last_1d_closed_open"] = d_closed.open_time
        ss["last_1d_high"] = d_closed.high

        await tg_send_text(session, f"📅 DAILY CLOSED | {symbol} | high={d_closed.high} | close={d_closed.close}")

async def handle_signals(session: aiohttp.ClientSession, st: Dict[str, Any], symbol: str, price: float) -> None:
    ss = sym_state(st, symbol)

    weekly_closed_open = ss.get("last_1w_closed_open")
    armed_weekly = ss.get("armed_weekly")
    buy_done_weekly = ss.get("buy_done_weekly")

    last_1d_high = ss.get("last_1d_high")
    if not weekly_closed_open or not armed_weekly or not last_1d_high:
        return

    # ✅ Asosiy shart:
    # haftalik yopilgandan keyin (armed_weekly == weekly_closed_open),
    # faqat 1 marta: price daily_high ga 2% qolsa BUY.
    if armed_weekly == weekly_closed_open and buy_done_weekly != weekly_closed_open:
        near_level = last_1d_high * (1.0 - NEAR_PCT)  # 2% below daily high
        if price >= near_level and price < last_1d_high:
            ss["buy_done_weekly"] = weekly_closed_open

            dist_pct = (last_1d_high - price) / last_1d_high * 100.0
            await tg_send_text(
                session,
                f"🟦 BUY (weekly->daily 1x)\n"
                f"{symbol}\n"
                f"price={price}\n"
                f"daily_high={last_1d_high}\n"
                f"remaining_to_high={dist_pct:.2f}% (target 2%)"
            )

# ======================
# LOOPS
# ======================
async def loop_refresh_top(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        try:
            syms = await get_top_gainers(session, TOP_N)
            st["top_symbols"] = syms
            st["last_top_refresh_ms"] = now_ms()

            # ✅ Top 50 update xabari Telegramga bormaydi (faqat log)
            print(f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)}")

            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_weekly_daily(session, st, symbol)
            except Exception as e:
                print("kline refresh error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

async def loop_prices(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue
        try:
            price_map = await get_price_map(session, syms)
            for symbol, price in price_map.items():
                try:
                    await handle_signals(session, st, symbol, price)
                except Exception as e:
                    print("signal error", symbol, e)
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Price loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(SCAN_PRICE_SEC)

async def main():
    st = load_state()

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=50, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(session, "🚀 Bot started: Weekly close -> (1x) BUY when price is within 2% of last CLOSED Daily HIGH")

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st)),
            asyncio.create_task(loop_prices(session, st)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
