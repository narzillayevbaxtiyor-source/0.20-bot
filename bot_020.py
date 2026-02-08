import os
import time
import json
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
from dotenv import load_dotenv

# chart
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

load_dotenv()

# ======================
# ENV
# ======================
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

TOP_N = int(os.getenv("TOP_N") or "50")

SCAN_PRICE_SEC = float(os.getenv("SCAN_PRICE_SEC") or "5")           # price poll
REFRESH_TOP_SEC = float(os.getenv("REFRESH_TOP_SEC") or "120")       # top gainers refresh
REFRESH_KLINES_SEC = float(os.getenv("REFRESH_KLINES_SEC") or "60")  # kline refresh (cached)

NEAR_PCT = float(os.getenv("NEAR_PCT") or "0.01")  # 1% near max
CHART_CANDLES = int(os.getenv("CHART_CANDLES") or "120")

STATE_FILE = os.getenv("STATE_FILE") or "state_top50_signals.json"

# Binance endpoints (public)
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://data-api.binance.vision").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID yo'q")

# ======================
# DATA STRUCTS
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
        "symbols": {},  # per symbol state
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
            "last_1d_closed_open": None,
            "last_1w_closed_open": None,
            "last_4h_closed_open": None,
            "last_15m_closed_open": None,

            "last_4h_high": None,   # last CLOSED 4h high
            "last_15m_low": None,   # last CLOSED 15m low

            "near_sent_for_4h": None,    # 4h open_time
            "break_sent_for_4h": None,   # 4h open_time

            "upmove_active": False,
            "upmove_started_4h": None,   # 4h open_time that triggered break

            # old
            "sell_sent_15m": None,

            # ✅ NEW: sell only once after each break
            "sell_once_done_for_break": None,  # stores upmove_started_4h when SELL already fired
        }
        st["symbols"][symbol] = s
    return s

# ======================
# HTTP HELPERS
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

async def tg_send_photo(session: aiohttp.ClientSession, caption: str, image_path: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        form = aiohttp.FormData()
        form.add_field("chat_id", TELEGRAM_CHAT_ID)
        form.add_field("caption", caption)
        form.add_field("photo", f, filename=os.path.basename(image_path), content_type="image/png")
        async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
            await r.text()

# ======================
# BINANCE DATA
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
        except:
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

# ======================
# CHART RENDER
# ======================
def render_candles_png(
    symbol: str,
    interval: str,
    candles: List[Candle],
    lines: List[Tuple[str, float]],
    pivot: Optional[Tuple[str, float]] = None,   # ("PIVOT_RES", price)
) -> str:
    # Candle spacing
    step = 2.2
    xs = [i * step for i in range(len(candles))]

    o = [c.open for c in candles]
    h = [c.high for c in candles]
    l = [c.low for c in candles]
    cl = [c.close for c in candles]

    fig = plt.figure(figsize=(10, 5))
    ax = fig.add_subplot(111)
    ax.set_title(f"{symbol} | {interval}")

    wick_lw = 1.1
    body_lw = 6.5

    for i in range(len(candles)):
        ax.vlines(xs[i], l[i], h[i], linewidth=wick_lw)
        body_low = min(o[i], cl[i])
        body_high = max(o[i], cl[i])
        ax.vlines(xs[i], body_low, body_high, linewidth=body_lw)

    for label, y in lines:
        ax.hlines(y, xs[0], xs[-1], linestyles="dashed", linewidth=1.4)
        ax.text(xs[0], y, f" {label}:{y:.6f}", va="bottom")

    # ✅ Pivot resistance marker (near the max da 1D chart uchun)
    if pivot is not None:
        p_label, p_y = pivot
        ax.hlines(p_y, xs[0], xs[-1], linestyles="dashed", linewidth=2.0)
        ax.text(xs[0], p_y, f" {p_label}:{p_y:.6f}", va="bottom")
        # dot at right side for visibility
        ax.scatter([xs[-2] if len(xs) >= 2 else xs[-1]], [p_y], s=40)

    # last CLOSED candle centered
    if len(xs) >= 2:
        center = xs[-2]
    else:
        center = xs[-1]

    half = max(10, len(xs) // 2) * step
    left = max(0.0, center - half)
    right = center + half
    ax.set_xlim(left, right)

    ax.grid(True, linewidth=0.3)
    ax.set_xticks([])

    out_path = f"/tmp/{symbol}_{interval}_{int(time.time())}.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=180)
    plt.close(fig)
    return out_path

# ======================
# LOGIC
# ======================
def last_closed(candles: List[Candle]) -> Candle:
    if len(candles) < 2:
        raise ValueError("Not enough candles")
    return candles[-2]

def find_last_green_then_red_pivot(d1: List[Candle]) -> Optional[float]:
    """
    1D candles list includes forming candle at end; we search among CLOSED candles.
    Find last pattern: green candle then next candle red.
    Return pivot price = CLOSE of the RED candle (yopilgan nuqta).
    """
    if len(d1) < 4:
        return None
    # closed range: up to -2
    closed = d1[:-1]
    # scan backwards on indices (need i-1 and i)
    for i in range(len(closed) - 1, 0, -1):
        prev = closed[i - 1]
        cur = closed[i]
        prev_green = prev.close > prev.open
        cur_red = cur.close < cur.open
        if prev_green and cur_red:
            return cur.close
    return None

async def refresh_symbol_klines_cached(
    session: aiohttp.ClientSession,
    st: Dict[str, Any],
    symbol: str,
    cache: Dict[str, Dict[str, Any]],
) -> None:
    for interval, limit in [("1w", 3), ("1d", 3), ("4h", 5), ("15m", 5)]:
        entry = cache.setdefault(symbol, {}).get(interval)
        tnow = now_ms()
        if entry and (tnow - entry["t"] < int(REFRESH_KLINES_SEC * 1000)):
            continue

        candles = await get_klines(session, symbol, interval, limit)
        cache.setdefault(symbol, {})[interval] = {"t": tnow, "candles": candles}

        ss = sym_state(st, symbol)
        closed = last_closed(candles)

        if interval == "1w":
            if ss["last_1w_closed_open"] != closed.open_time:
                ss["last_1w_closed_open"] = closed.open_time
                await tg_send_text(session, f"🗓 WEEKLY CLOSED | {symbol} | close={closed.close}")
        elif interval == "1d":
            if ss["last_1d_closed_open"] != closed.open_time:
                ss["last_1d_closed_open"] = closed.open_time
                await tg_send_text(session, f"📅 DAILY CLOSED | {symbol} | close={closed.close}")
        elif interval == "4h":
            if ss["last_4h_closed_open"] != closed.open_time:
                ss["last_4h_closed_open"] = closed.open_time
                ss["last_4h_high"] = closed.high
                ss["near_sent_for_4h"] = None
                ss["break_sent_for_4h"] = None
        elif interval == "15m":
            if ss["last_15m_closed_open"] != closed.open_time:
                ss["last_15m_closed_open"] = closed.open_time
                ss["last_15m_low"] = closed.low

async def handle_signals(
    session: aiohttp.ClientSession,
    st: Dict[str, Any],
    symbol: str,
    price: float,
    cache: Dict[str, Dict[str, Any]],
) -> None:
    ss = sym_state(st, symbol)

    last4h_high = ss.get("last_4h_high")
    last4h_open = ss.get("last_4h_closed_open")
    daily_closed_open = ss.get("last_1d_closed_open")

    if not last4h_high or not last4h_open or not daily_closed_open:
        return

    # 1) near the max  (✅ now shows 1D chart + pivot resistance)
    near_level = last4h_high * (1.0 - NEAR_PCT)
    if price >= near_level and price < last4h_high:
        if ss.get("near_sent_for_4h") != last4h_open:
            ss["near_sent_for_4h"] = last4h_open

            # 1D candles for pivot + chart
            d1 = await get_klines(session, symbol, "1d", min(200, max(CHART_CANDLES, 80)))
            pivot_price = find_last_green_then_red_pivot(d1)
            view1d = d1[-CHART_CANDLES:] if len(d1) > CHART_CANDLES else d1

            img = render_candles_png(
                symbol,
                "1d",
                view1d,
                lines=[
                    ("PRICE", price),
                    ("4H_MAX", last4h_high),
                ],
                pivot=("PIVOT_RES", pivot_price) if pivot_price else None
            )

            cap = f"🟨 near the max | {symbol}\nprice={price}\n4h_max={last4h_high}"
            if pivot_price:
                cap += f"\nres(pivot)={pivot_price}"
            await tg_send_photo(session, cap, img)

    # 2) break the max (starts upmove + resets "sell once" latch)
    if price >= last4h_high:
        if ss.get("break_sent_for_4h") != last4h_open:
            ss["break_sent_for_4h"] = last4h_open
            ss["upmove_active"] = True
            ss["upmove_started_4h"] = last4h_open

            # ✅ reset sell-once latch for this break
            ss["sell_once_done_for_break"] = None

            candles = await get_klines(session, symbol, "4h", min(CHART_CANDLES, 200))
            view = candles[-CHART_CANDLES:] if len(candles) > CHART_CANDLES else candles
            img = render_candles_png(symbol, "4h", view, [
                ("PRICE", price),
                ("4H_MAX", last4h_high),
            ])
            await tg_send_photo(session, f"🟩 price break the max | {symbol}\nprice={price}\n4h_max={last4h_high}", img)

    # 3) SELL: only ONCE after each break
    if ss.get("upmove_active"):
        # if already sold for this break -> do nothing
        if ss.get("sell_once_done_for_break") == ss.get("upmove_started_4h"):
            return

        last15m_low = ss.get("last_15m_low")
        last15m_open = ss.get("last_15m_closed_open")
        if last15m_low and last15m_open:
            if price < last15m_low:
                # ✅ fire SELL only once per break
                ss["sell_once_done_for_break"] = ss.get("upmove_started_4h")

                candles15 = await get_klines(session, symbol, "15m", min(CHART_CANDLES, 200))
                view15 = candles15[-CHART_CANDLES:] if len(candles15) > CHART_CANDLES else candles15
                img = render_candles_png(symbol, "15m", view15, [
                    ("PRICE", price),
                    ("15M_LOW", last15m_low),
                ])
                await tg_send_photo(
                    session,
                    f"🟥 SELL (once) | {symbol}\nprice={price}\nlast_closed_15m_low={last15m_low}",
                    img,
                )

# ======================
# MAIN LOOPS
# ======================
async def loop_refresh_top(session: aiohttp.ClientSession, st: Dict[str, Any]) -> None:
    while True:
        try:
            syms = await get_top_gainers(session, TOP_N)
            st["top_symbols"] = syms
            st["last_top_refresh_ms"] = now_ms()
            await tg_send_text(session, f"✅ Top {TOP_N} gainers updated. Tracking: {len(syms)} symbols")
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Top refresh error: {type(e).__name__}: {e}")
        await asyncio.sleep(REFRESH_TOP_SEC)

async def loop_refresh_klines(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(2)
            continue
        for symbol in syms:
            try:
                await refresh_symbol_klines_cached(session, st, symbol, cache)
            except Exception as e:
                print("kline refresh error", symbol, e)
        save_state(st)
        await asyncio.sleep(REFRESH_KLINES_SEC)

async def loop_prices(session: aiohttp.ClientSession, st: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> None:
    while True:
        syms = st.get("top_symbols") or []
        if not syms:
            await asyncio.sleep(1)
            continue
        try:
            price_map = await get_price_map(session, syms)
            for symbol, price in price_map.items():
                try:
                    await handle_signals(session, st, symbol, price, cache)
                except Exception as e:
                    print("signal error", symbol, e)
            save_state(st)
        except Exception as e:
            await tg_send_text(session, f"⚠️ Price loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(SCAN_PRICE_SEC)

async def main():
    st = load_state()
    cache: Dict[str, Dict[str, Any]] = {}

    timeout = aiohttp.ClientTimeout(total=20)
    connector = aiohttp.TCPConnector(limit=50, ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        await tg_send_text(session, "🚀 Bot started: Top50 + 4H near/break + 15m SELL once + 1D pivot resistance on near")

        tasks = [
            asyncio.create_task(loop_refresh_top(session, st)),
            asyncio.create_task(loop_refresh_klines(session, st, cache)),
            asyncio.create_task(loop_prices(session, st, cache)),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
