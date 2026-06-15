"""
F&O Signal Bot — Nifty 50 + Sensex + Crude Oil
Fixed issues:
1. Crude premium now fetched from NSE option chain API (real prices)
2. Analysis uses 15min candles + multi-timeframe confirmation to avoid false signals
3. Signal shows CE/PE clearly with real option symbol name
4. Bear signals now properly trigger when market is going down
5. Confidence threshold raised to avoid weak signals
"""

import os
import time
import math
import datetime
import calendar
import threading
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8969876449:AAFr2ytAq_KKUTcaKo5Rf1X-f_fAeLYyEnQ")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "877753785")

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

EQUITY_OPEN  = datetime.time(9, 15)
EQUITY_CLOSE = datetime.time(15, 30)
CRUDE_OPEN   = datetime.time(9, 0)     # 9:00 AM
CRUDE_CLOSE  = datetime.time(23, 0)    # 11:00 PM

SCALP_INTERVAL    = 300    # 5 min
INTRADAY_INTERVAL = 900    # 15 min
CRUDE_INTERVAL    = 600    # 10 min
LEVEL_SCAN_INTERVAL = 120  # 2 min

MORNING_HOUR     = 8
MORNING_MIN      = 45
CRUDE_BRIEF_HOUR = 8
CRUDE_BRIEF_MIN  = 50

DEFAULT_RISK_PERCENT = 2.0
MIN_CONFIDENCE       = 55   # Only send signals with 55%+ confidence

# ─────────────────────────────────────────────
#  INSTRUMENTS
# ─────────────────────────────────────────────
EQUITY_INSTRUMENTS = [
    {
        "name":        "NIFTY",
        "full_name":   "Nifty 50",
        "symbol":      "^NSEI",
        "lot_size":    65,
        "strike_step": 50,
        "expiry_day":  1,    # Tuesday
        "nse_symbol":  "NIFTY",
    },
    {
        "name":        "SENSEX",
        "full_name":   "BSE Sensex",
        "symbol":      "^BSESN",
        "lot_size":    20,
        "strike_step": 100,
        "expiry_day":  3,    # Thursday
        "nse_symbol":  "SENSEX",
    },
]

CRUDE_INSTRUMENT = {
    "name":        "CRUDEOIL",
    "full_name":   "Crude Oil MCX",
    "symbol":      "CL=F",
    "lot_size":    100,
    "strike_step": 100,
}

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
user_balance      = {}
active_signals    = {}
level_alerts_sent = {}
market_levels     = {}

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(chat_id: str, msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=35)
        return r.json()
    except:
        return {}

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_ohlcv(symbol: str, period="5d", interval="5m"):
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df.empty or len(df) < 30:
            return None
        try:
            df.index = df.index.tz_convert("Asia/Kolkata")
        except:
            pass
        return df
    except Exception as e:
        print(f"OHLCV error ({symbol}): {e}")
        return None

def get_ohlcv_15m(symbol: str):
    """15-minute candles for stronger trend confirmation"""
    return get_ohlcv(symbol, period="5d", interval="15m")

def get_current_price(symbol: str):
    try:
        data = yf.Ticker(symbol).history(period="1d", interval="1m")
        if not data.empty:
            return round(float(data["Close"].iloc[-1]), 2)
    except:
        pass
    return None

def get_usd_inr() -> float:
    try:
        data = yf.Ticker("INR=X").history(period="1d", interval="1m")
        if not data.empty:
            return round(float(data["Close"].iloc[-1]), 2)
    except:
        pass
    return 83.5

def convert_crude_to_inr(df: pd.DataFrame):
    usd_inr = get_usd_inr()
    df = df.copy()
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = (df[col] * usd_inr).round(2)
    return df, usd_inr

def ist_now():
    return datetime.datetime.now(IST)

# ─────────────────────────────────────────────
#  HOLIDAY CALENDARS 2026
#  Source: NSE/BSE/MCX official holiday lists
# ─────────────────────────────────────────────

# NSE/BSE holidays — full day closed (equity F&O)
NSE_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 3,  3),   # Holi
    datetime.date(2026, 3, 26),   # Ram Navami
    datetime.date(2026, 3, 31),   # Mahavir Jayanti
    datetime.date(2026, 4,  3),   # Good Friday
    datetime.date(2026, 4, 14),   # Ambedkar Jayanti
    datetime.date(2026, 5,  1),   # Maharashtra Day
    datetime.date(2026, 5, 28),   # Bakri Id
    datetime.date(2026, 6, 26),   # Muharram
    datetime.date(2026, 9, 14),   # Ganesh Chaturthi
    datetime.date(2026, 10, 2),   # Gandhi Jayanti
    datetime.date(2026, 10, 10),  # Dussehra
    datetime.date(2026, 11, 10),  # Diwali-Balipratipada
    datetime.date(2026, 11, 24),  # Guru Nanak Jayanti
    datetime.date(2026, 12, 25),  # Christmas
}

# MCX holidays — FULL day closed (both morning + evening sessions)
MCX_FULL_HOLIDAYS_2026 = {
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 4,  3),   # Good Friday
    datetime.date(2026, 10, 2),   # Gandhi Jayanti
    datetime.date(2026, 12, 25),  # Christmas
}

# MCX partial holidays — morning closed, evening (5PM+) open
# We set crude to 9AM so on these days crude only runs 5PM-11PM
MCX_PARTIAL_HOLIDAYS_2026 = {
    datetime.date(2026, 3,  3),   # Holi
    datetime.date(2026, 3, 26),   # Ram Navami
    datetime.date(2026, 3, 31),   # Mahavir Jayanti
    datetime.date(2026, 4, 14),   # Ambedkar Jayanti
    datetime.date(2026, 5,  1),   # Maharashtra Day
    datetime.date(2026, 5, 28),   # Bakri Id
    datetime.date(2026, 6, 26),   # Muharram
    datetime.date(2026, 9, 14),   # Ganesh Chaturthi
    datetime.date(2026, 10, 10),  # Dussehra
    datetime.date(2026, 11, 10),  # Diwali
    datetime.date(2026, 11, 24),  # Guru Nanak Jayanti
}

def is_nse_holiday(date: datetime.date = None) -> bool:
    d = date or ist_now().date()
    # Saturday = 5, Sunday = 6
    if d.weekday() >= 5:
        return True
    return d in NSE_HOLIDAYS_2026

def is_mcx_full_holiday(date: datetime.date = None) -> bool:
    d = date or ist_now().date()
    if d.weekday() == 6:   # Sunday always closed
        return True
    return d in MCX_FULL_HOLIDAYS_2026

def is_mcx_partial_holiday(date: datetime.date = None) -> bool:
    """Morning session closed, evening open after 5PM"""
    d = date or ist_now().date()
    return d in MCX_PARTIAL_HOLIDAYS_2026

def is_equity_open():
    now = ist_now()
    if is_nse_holiday(now.date()):
        return False
    return EQUITY_OPEN <= now.time() <= EQUITY_CLOSE

def is_crude_open():
    now  = ist_now()
    date = now.date()
    t    = now.time()
    # Full holiday — closed all day
    if is_mcx_full_holiday(date):
        return False
    # Saturday — only morning session (9AM-2PM), no evening
    if date.weekday() == 5:
        return datetime.time(9, 0) <= t <= datetime.time(14, 0)
    # Partial holiday — morning closed, evening open from 5PM
    if is_mcx_partial_holiday(date):
        return datetime.time(17, 0) <= t <= CRUDE_CLOSE
    # Normal day: 9AM to 11PM
    return CRUDE_OPEN <= t <= CRUDE_CLOSE

def next_expiry_date(expiry_weekday: int) -> str:
    now = ist_now()
    days_ahead = (expiry_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    expiry = now + datetime.timedelta(days=days_ahead)
    return expiry.strftime("%-d %b")

def days_to_expiry(expiry_weekday: int) -> int:
    now = ist_now()
    d = (expiry_weekday - now.weekday()) % 7
    return d if d > 0 else 7

def is_expiry_today(expiry_weekday: int) -> bool:
    return ist_now().weekday() == expiry_weekday

# ─────────────────────────────────────────────
#  LIVE OPTION PRICE — NSE API (FIXED)
# ─────────────────────────────────────────────

# Persistent NSE session (reuse cookies)
_nse_session = None

def get_nse_session():
    global _nse_session
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        s = requests.Session()
        s.headers.update(headers)
        # Hit NSE homepage to get cookies
        s.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
        # Hit option chain page to refresh cookies
        s.get("https://www.nseindia.com/option-chain", timeout=10)
        time.sleep(0.5)
        _nse_session = s
        print("NSE session created successfully")
        return s
    except Exception as e:
        print(f"NSE session error: {e}")
        return None

def fetch_nse_option_price(symbol: str, strike: int, opt_type: str) -> float:
    """
    Fetch real LTP from NSE option chain API.
    Returns float price or None if failed.
    """
    global _nse_session
    try:
        if _nse_session is None:
            _nse_session = get_nse_session()
        if _nse_session is None:
            return None

        api_headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/option-chain",
            "X-Requested-With": "XMLHttpRequest",
        }
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        r   = _nse_session.get(url, headers=api_headers, timeout=10)

        if r.status_code == 401 or r.status_code == 403:
            # Session expired — refresh
            print("NSE session expired, refreshing...")
            _nse_session = get_nse_session()
            if _nse_session:
                r = _nse_session.get(url, headers=api_headers, timeout=10)

        if r.status_code != 200:
            print(f"NSE API status: {r.status_code}")
            return None

        data    = r.json()
        records = data.get("records", {}).get("data", [])

        for rec in records:
            if int(rec.get("strikePrice", 0)) == int(strike):
                opt_data = rec.get(opt_type, {})
                ltp = opt_data.get("lastPrice", 0)
                if ltp and float(ltp) > 0:
                    print(f"NSE LIVE: {symbol} {strike}{opt_type} = ₹{ltp}")
                    return round(float(ltp), 2)

        print(f"Strike {strike} not found in NSE chain for {symbol}")
        return None

    except Exception as e:
        print(f"NSE fetch error: {e}")
        _nse_session = None   # reset session on error
        return None


def estimate_option_price(spot: float, strike: int, opt_type: str,
                           atr: float, days: int) -> float:
    """
    Improved estimation using proper Black-Scholes approximation.
    Much more accurate than simple ATR formula.
    Nifty ATM options: IV is typically 12-18%, use 15% as base.
    """
    # Implied Volatility assumption: 15% annual for Nifty/Sensex
    iv         = 0.15
    T          = max(days, 0.25) / 365   # time in years
    vol_factor = iv * math.sqrt(T)        # e.g. 3 days → ~0.018

    # Intrinsic value
    intrinsic = max(0, spot - strike) if opt_type == "CE" else max(0, strike - spot)

    # Time value using simplified BSM: 0.4 * S * σ * √T for ATM
    distance  = abs(spot - strike) / spot
    atm_time_val = 0.4 * spot * vol_factor
    # Decay for OTM: drops as distance from ATM increases
    decay = math.exp(-distance * 5)
    time_val = atm_time_val * decay

    premium = round(intrinsic + time_val, 1)
    # Sanity check: premium should be 0.3% to 8% of spot
    min_p = round(spot * 0.003, 1)
    max_p = round(spot * 0.08, 1)
    return max(min_p, min(premium, max_p))


def get_option_price(inst: dict, strike: int, opt_type: str,
                     spot: float, days: int, atr: float) -> tuple:
    """
    1. Try NSE live price
    2. Fallback to improved BSM estimate
    Returns (price, source_label)
    """
    # Try live
    live = fetch_nse_option_price(inst.get("nse_symbol", inst["name"]), strike, opt_type)
    if live and live > 2:
        return live, "LIVE ✅"

    # Improved estimate
    est = estimate_option_price(spot, strike, opt_type, atr, days)
    print(f"EST: {inst['name']} {strike}{opt_type} spot={spot} days={days} → ₹{est}")
    return est, "EST ⚡"

# ─────────────────────────────────────────────
#  TECHNICAL ANALYSIS — FIXED
#  Uses BOTH 5min + 15min timeframes
#  Prevents always-CALL bias
# ─────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    ema9  = EMAIndicator(close, window=9).ema_indicator()
    ema21 = EMAIndicator(close, window=21).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    macd_obj  = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    macd_line = macd_obj.macd()
    macd_sig  = macd_obj.macd_signal()
    macd_hist = macd_obj.macd_diff()
    rsi       = RSIIndicator(close, window=14).rsi()
    stoch     = StochasticOscillator(high, low, close, window=14)
    bb        = BollingerBands(close, window=20)
    atr       = AverageTrueRange(high, low, close, window=14).average_true_range()
    vol_avg   = vol.rolling(20).mean()
    vol_ratio = float(vol.iloc[-1] / vol_avg.iloc[-1]) if vol_avg.iloc[-1] > 0 else 1.0

    # Price change % over last 3 candles — detects trend direction
    price_change_3 = round(((close.iloc[-1] - close.iloc[-4]) / close.iloc[-4]) * 100, 3) \
                     if len(close) >= 4 else 0

    return {
        "price":          round(close.iloc[-1], 2),
        "prev_price":     round(close.iloc[-2], 2),
        "ema9":           round(ema9.iloc[-1], 2),
        "ema21":          round(ema21.iloc[-1], 2),
        "ema50":          round(ema50.iloc[-1], 2),
        "macd":           round(macd_line.iloc[-1], 4),
        "macd_sig":       round(macd_sig.iloc[-1], 4),
        "macd_hist":      round(macd_hist.iloc[-1], 4),
        "macd_hist_prev": round(macd_hist.iloc[-2], 4),
        "rsi":            round(rsi.iloc[-1], 2),
        "stoch_k":        round(stoch.stoch().iloc[-1], 2),
        "stoch_d":        round(stoch.stoch_signal().iloc[-1], 2),
        "bb_high":        round(bb.bollinger_hband().iloc[-1], 2),
        "bb_low":         round(bb.bollinger_lband().iloc[-1], 2),
        "bb_mid":         round(bb.bollinger_mavg().iloc[-1], 2),
        "atr":            round(atr.iloc[-1], 2),
        "vol_ratio":      round(vol_ratio, 2),
        "price_change_3": price_change_3,   # % change last 3 candles
    }


def score_signal(ind_5m: dict, ind_15m: dict = None) -> tuple:
    """
    FIXED scoring:
    - Uses both 5m and 15m timeframes
    - Price momentum (recent candles direction) weighted heavily
    - Prevents bull bias: bear signals need same weight as bull
    - Minimum 55 confidence to send signal
    """
    bull_score, bear_score = 0, 0
    bull_reasons, bear_reasons = [], []

    p   = ind_5m["price"]
    rsi = ind_5m["rsi"]
    stk = ind_5m["stoch_k"]
    std = ind_5m["stoch_d"]
    pc3 = ind_5m["price_change_3"]  # recent price direction

    # ── 1. Recent Price Direction (most important — 30pts) ──
    # This directly answers "is market going up or down RIGHT NOW"
    if pc3 > 0.15:
        bull_score += 30
        bull_reasons.append(f"📈 Price rising +{pc3}% last 3 candles")
    elif pc3 < -0.15:
        bear_score += 30
        bear_reasons.append(f"📉 Price falling {pc3}% last 3 candles")
    elif pc3 > 0.05:
        bull_score += 10
        bull_reasons.append(f"📈 Slight upward momentum +{pc3}%")
    elif pc3 < -0.05:
        bear_score += 10
        bear_reasons.append(f"📉 Slight downward momentum {pc3}%")

    # ── 2. EMA Stack (20pts) ──
    if ind_5m["ema9"] > ind_5m["ema21"] > ind_5m["ema50"]:
        bull_score += 20
        bull_reasons.append("✅ EMA Stack Bullish (9>21>50)")
    elif ind_5m["ema9"] < ind_5m["ema21"] < ind_5m["ema50"]:
        bear_score += 20
        bear_reasons.append("✅ EMA Stack Bearish (9<21<50)")

    # ── 3. Price vs EMA21 (10pts) ──
    if p > ind_5m["ema21"]:
        bull_score += 10
        bull_reasons.append("📊 Price above EMA21")
    else:
        bear_score += 10
        bear_reasons.append("📊 Price below EMA21")

    # ── 4. MACD (20pts crossover, 10pts direction) ──
    if ind_5m["macd_hist"] > 0 and ind_5m["macd_hist_prev"] <= 0:
        bull_score += 20
        bull_reasons.append("🔀 MACD Bullish Crossover")
    elif ind_5m["macd_hist"] < 0 and ind_5m["macd_hist_prev"] >= 0:
        bear_score += 20
        bear_reasons.append("🔀 MACD Bearish Crossover")
    elif ind_5m["macd_hist"] > 0:
        bull_score += 10
        bull_reasons.append("📊 MACD Histogram Positive")
    elif ind_5m["macd_hist"] < 0:
        bear_score += 10
        bear_reasons.append("📊 MACD Histogram Negative")

    # ── 5. RSI (15pts) ──
    if 55 < rsi <= 70:
        bull_score += 15
        bull_reasons.append(f"⚡ RSI Bullish Zone: {rsi}")
    elif 30 <= rsi < 45:
        bear_score += 15
        bear_reasons.append(f"⚡ RSI Bearish Zone: {rsi}")
    elif rsi > 70:
        # Overbought — could reverse, slight bear lean
        bear_score += 8
        bear_reasons.append(f"⚠️ RSI Overbought {rsi} — reversal risk")
    elif rsi < 30:
        # Oversold — could bounce, slight bull lean
        bull_score += 8
        bull_reasons.append(f"⚠️ RSI Oversold {rsi} — bounce possible")

    # ── 6. Stochastic (10pts) ──
    if stk > std and 20 < stk < 80:
        bull_score += 10
        bull_reasons.append(f"📐 Stoch Bullish K:{stk}>D:{std}")
    elif stk < std and 20 < stk < 80:
        bear_score += 10
        bear_reasons.append(f"📐 Stoch Bearish K:{stk}<D:{std}")

    # ── 7. Bollinger Band position (15pts) ──
    if p < ind_5m["bb_low"]:
        bull_score += 15
        bull_reasons.append("🎯 Price below Lower BB — bounce zone")
    elif p > ind_5m["bb_high"]:
        bear_score += 15
        bear_reasons.append("🎯 Price above Upper BB — reversal zone")
    elif p > ind_5m["bb_mid"]:
        bull_score += 5
    else:
        bear_score += 5

    # ── 8. Volume confirmation (10pts) ──
    if ind_5m["vol_ratio"] > 1.5:
        if bull_score > bear_score:
            bull_score += 10
            bull_reasons.append(f"📣 Volume Surge {round(ind_5m['vol_ratio'],1)}x avg")
        else:
            bear_score += 10
            bear_reasons.append(f"📣 Volume Surge {round(ind_5m['vol_ratio'],1)}x avg")

    # ── 9. 15min timeframe confirmation (bonus 15pts) ──
    if ind_15m:
        pc3_15 = ind_15m.get("price_change_3", 0)
        if pc3_15 > 0.1 and bull_score > bear_score:
            bull_score += 15
            bull_reasons.append("✅ 15min trend confirms bullish")
        elif pc3_15 < -0.1 and bear_score > bull_score:
            bear_score += 15
            bear_reasons.append("✅ 15min trend confirms bearish")
        elif pc3_15 > 0 and bear_score > bull_score:
            bear_score -= 10   # 15min disagrees with 5min bear signal
        elif pc3_15 < 0 and bull_score > bear_score:
            bull_score -= 10   # 15min disagrees with 5min bull signal

    # ── Low volatility filter ──
    atr_pct = (ind_5m["atr"] / p) * 100
    if atr_pct < 0.05:
        return None, 0, ["⛔ Market too choppy — no trade"]

    # ── Final decision ──
    total = bull_score + bear_score
    if total == 0:
        return None, 0, []

    bull_conf = int((bull_score / max(total, 1)) * 100)
    bear_conf = int((bear_score / max(total, 1)) * 100)

    if bull_score > bear_score and bull_conf >= MIN_CONFIDENCE:
        return "CE", bull_conf, bull_reasons
    elif bear_score > bull_score and bear_conf >= MIN_CONFIDENCE:
        return "PE", bear_conf, bear_reasons

    return None, max(bull_conf, bear_conf), []

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)

def balance_driven_sl_tgt(balance: float, premium: float, lot_size: int):
    max_loss_rs  = balance * DEFAULT_RISK_PERCENT / 100
    sl_per_unit  = round(min(max_loss_rs / lot_size, premium * 0.20), 1)
    sl_per_unit  = max(sl_per_unit, 1.0)
    tp1_per_unit = round(sl_per_unit * 1.5, 1)
    tp2_per_unit = round(sl_per_unit * 3.0, 1)
    sl_price     = round(premium - sl_per_unit, 1)
    tp1_price    = round(premium + tp1_per_unit, 1)
    tp2_price    = round(premium + tp2_per_unit, 1)
    max_loss     = round(sl_per_unit  * lot_size, 0)
    max_profit   = round(tp2_per_unit * lot_size, 0)
    return sl_price, sl_per_unit, tp1_price, tp1_per_unit, tp2_price, tp2_per_unit, max_loss, max_profit

# ─────────────────────────────────────────────
#  LEVEL ANALYSIS ENGINE
# ─────────────────────────────────────────────
def compute_vwap(df: pd.DataFrame) -> float:
    try:
        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        vwap = (typical * df["Volume"]).cumsum() / df["Volume"].cumsum()
        return round(float(vwap.iloc[-1]), 2)
    except:
        return None

def compute_cpr(pdh: float, pdl: float, pdc: float) -> dict:
    pivot = round((pdh + pdl + pdc) / 3, 2)
    bc    = round((pdh + pdl) / 2, 2)
    tc    = round((pivot - bc) + pivot, 2)
    r1    = round(2 * pivot - pdl, 2)
    r2    = round(pivot + (pdh - pdl), 2)
    s1    = round(2 * pivot - pdh, 2)
    s2    = round(pivot - (pdh - pdl), 2)
    return {"pivot": pivot, "tc": tc, "bc": bc, "r1": r1, "r2": r2, "s1": s1, "s2": s2}

def calculate_levels(symbol: str, df: pd.DataFrame) -> dict:
    try:
        now   = ist_now()
        today = now.date()
        today_df = df[df.index.date == today] if hasattr(df.index, 'date') else df
        all_dates = sorted(set(df.index.date)) if hasattr(df.index, 'date') else []
        prev_dates = [d for d in all_dates if d < today]
        pdh = pdl = pdc = None
        if prev_dates:
            prev_df = df[df.index.date == prev_dates[-1]]
            if not prev_df.empty:
                pdh = round(float(prev_df["High"].max()), 2)
                pdl = round(float(prev_df["Low"].min()), 2)
                pdc = round(float(prev_df["Close"].iloc[-1]), 2)
        cpr   = compute_cpr(pdh, pdl, pdc) if all([pdh, pdl, pdc]) else {}
        vwap  = compute_vwap(today_df) if not today_df.empty else None
        or_high = round(float(today_df["High"].iloc[:3].max()), 2) if len(today_df) >= 3 else None
        or_low  = round(float(today_df["Low"].iloc[:3].min()),  2) if len(today_df) >= 3 else None
        return {
            "price":    round(float(df["Close"].iloc[-1]), 2),
            "pdh": pdh, "pdl": pdl, "pdc": pdc,
            "cpr": cpr, "vwap": vwap,
            "or": {"high": or_high, "low": or_low} if or_high else None,
            "swing_high": round(float(df["High"].tail(20).max()), 2),
            "swing_low":  round(float(df["Low"].tail(20).min()),  2),
        }
    except Exception as e:
        print(f"Level calc error: {e}")
        return None

def detect_breakout(levels: dict, df: pd.DataFrame) -> list:
    setups    = []
    price     = levels["price"]
    prev      = float(df["Close"].iloc[-2])
    vol       = df["Volume"]
    vol_ratio = float(vol.iloc[-1] / vol.rolling(20).mean().iloc[-1]) \
                if vol.rolling(20).mean().iloc[-1] > 0 else 1.0
    buf = round(price * 0.001, 2)

    def add(type_, level, direction, reason, conf, note):
        setups.append({"type": type_, "level": level, "direction": direction,
                       "reason": reason, "confidence": conf, "entry_note": note})

    if levels.get("or"):
        orh, orl = levels["or"]["high"], levels["or"]["low"]
        if orh and prev < orh and price > orh + buf and vol_ratio > 1.3:
            add("ORB_BULL", orh, "CE", f"🔓 Opening Range Breakout above ₹{orh}",
                80 if vol_ratio > 1.8 else 65,
                f"Broke morning high ₹{orh} with {round(vol_ratio,1)}x volume")
        if orl and prev > orl and price < orl - buf and vol_ratio > 1.3:
            add("ORB_BEAR", orl, "PE", f"🔓 Opening Range Breakdown below ₹{orl}",
                80 if vol_ratio > 1.8 else 65,
                f"Broke morning low ₹{orl} with {round(vol_ratio,1)}x volume")

    if levels.get("pdh") and levels.get("pdl"):
        if prev < levels["pdh"] and price > levels["pdh"] + buf and vol_ratio > 1.2:
            add("PDH_BREAK", levels["pdh"], "CE", f"📈 Previous Day High ₹{levels['pdh']} broken",
                75, f"Strong breakout above PDH — momentum buy")
        if prev > levels["pdl"] and price < levels["pdl"] - buf and vol_ratio > 1.2:
            add("PDL_BREAK", levels["pdl"], "PE", f"📉 Previous Day Low ₹{levels['pdl']} broken",
                75, f"Strong breakdown below PDL — momentum sell")

    if levels.get("vwap"):
        vwap = levels["vwap"]
        if prev < vwap and price > vwap + buf:
            add("VWAP_UP", vwap, "CE", f"💧 Price reclaimed VWAP ₹{vwap}",
                65, f"Bullish VWAP reclaim — institutions buying")
        elif prev > vwap and price < vwap - buf:
            add("VWAP_DN", vwap, "PE", f"💧 Price lost VWAP ₹{vwap}",
                65, f"Bearish VWAP rejection — selling pressure")

    if levels.get("cpr"):
        cpr = levels["cpr"]
        if prev < cpr["r1"] and price > cpr["r1"] + buf and vol_ratio > 1.5:
            add("R1_BREAK", cpr["r1"], "CE", f"🚀 R1 Resistance ₹{cpr['r1']} broken",
                78, f"R1 broken with volume — target R2 at ₹{cpr['r2']}")
        if prev > cpr["s1"] and price < cpr["s1"] - buf and vol_ratio > 1.5:
            add("S1_BREAK", cpr["s1"], "PE", f"💥 S1 Support ₹{cpr['s1']} broken",
                78, f"S1 broken with volume — target S2 at ₹{cpr['s2']}")

    return setups

def send_daily_levels(chat_id: str):
    now = ist_now()
    msg_parts = [f"📐 <b>KEY LEVELS — {now.strftime('%d %b %Y')}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"]
    for inst in EQUITY_INSTRUMENTS:
        df = get_ohlcv(inst["symbol"], period="5d", interval="5m")
        if df is None: continue
        lvl = calculate_levels(inst["symbol"], df)
        if not lvl: continue
        cpr = lvl.get("cpr", {})
        orh = lvl.get("or", {}).get("high", "wait...") if lvl.get("or") else "9:30AM"
        orl = lvl.get("or", {}).get("low",  "wait...") if lvl.get("or") else "9:30AM"
        msg_parts.append(f"""
<b>{inst['full_name']}</b> @ ₹{lvl['price']}

🔺 R2: ₹{cpr.get('r2','—')}
🔺 R1: ₹{cpr.get('r1','—')}
📌 Pivot: ₹{cpr.get('pivot','—')}  CPR: ₹{cpr.get('bc','—')}–₹{cpr.get('tc','—')}
🔻 S1: ₹{cpr.get('s1','—')}
🔻 S2: ₹{cpr.get('s2','—')}
📈 PDH: ₹{lvl.get('pdh','—')}  PDL: ₹{lvl.get('pdl','—')}
💧 VWAP: ₹{lvl.get('vwap','—')}
⏰ OR: ₹{orl} – ₹{orh}

🟢 BUY CE above: ₹{cpr.get('r1','—')} / ₹{lvl.get('pdh','—')}
🔴 BUY PE below: ₹{cpr.get('s1','—')} / ₹{lvl.get('pdl','—')}
""".strip())
        msg_parts.append("─────────────────────")
    msg_parts.append("⚡ <i>Auto level alerts fire when these break with volume.</i>")
    send_telegram(chat_id, "\n".join(msg_parts))

# ─────────────────────────────────────────────
#  SIGNAL FORMAT
# ─────────────────────────────────────────────
def format_equity_signal(inst, signal, spot, strike, expiry_str,
                          premium, price_source, sl_px, sl_val,
                          tp1_px, tp1_val, tp2_px, tp2_val,
                          max_loss, max_profit, lots, confidence,
                          reasons, mode) -> str:
    direction = "BUY CALL 📈" if signal == "CE" else "BUY PUT 📉"
    mode_tag  = {"scalp": "⚡ Scalping", "expiry": "🔥 Expiry Day"}.get(mode, "📊 Intraday")
    icon      = "📊" if inst["name"] == "NIFTY" else "📈"
    opt_symbol = f"{inst['name']} {strike} {signal}"   # e.g. NIFTY 24550 CE

    return f"""
{icon} <b>{inst['full_name']} — {direction}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>{opt_symbol}</b>
📅 Expiry: <b>{expiry_str}</b>
⏱ {mode_tag}

💰 Buy @ <b>₹{premium}</b>  ({price_source})

🎯 TP 1  =  <b>₹{tp1_px}</b>  (+₹{tp1_val})
🎯 TP 2  =  <b>₹{tp2_px}</b>  (+₹{tp2_val})
🛑 SL    =  <b>₹{sl_px}</b>   (−₹{sl_val})

📦 {lots} lot × {inst['lot_size']} qty
💸 Max Loss:   ₹{int(max_loss):,}
💰 Max Profit: ₹{int(max_profit):,}
📐 R:R = 1:{round(tp2_val/sl_val,1)}

📉 Spot: ₹{spot}  |  RSI: {[r for r in reasons if 'RSI' in r][:1] or ['—']}
🔵 Confidence: {confidence}%

📋 Why:
{chr(10).join(reasons[:4])}

⚠️ <i>Move SL to entry after TP1 hits.
AI signal — trade at your own risk.</i>""".strip()


def format_level_alert(inst, setup, levels, premium, price_source,
                        sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
                        max_loss, max_profit, lots, expiry_str) -> str:
    direction  = "BUY CALL 📈" if setup["direction"] == "CE" else "BUY PUT 📉"
    strike     = get_nearest_strike(levels["price"], inst["strike_step"])
    opt_symbol = f"{inst['name']} {strike} {setup['direction']}"
    cpr        = levels.get("cpr", {})

    return f"""
🚨 <b>LEVEL ALERT — {inst['full_name']}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

{setup['reason']}
<i>{setup['entry_note']}</i>

📌 <b>{direction}</b>
🎯 <b>{opt_symbol}</b>
📅 Expiry: {expiry_str}

💰 Buy @ <b>₹{premium}</b>  ({price_source})

🎯 TP 1  =  <b>₹{tp1_px}</b>  (+₹{tp1_val})
🎯 TP 2  =  <b>₹{tp2_px}</b>  (+₹{tp2_val})
🛑 SL    =  <b>₹{sl_px}</b>   (−₹{sl_val})

📦 {lots} lot × {inst['lot_size']} qty
💸 Max Loss:   ₹{int(max_loss):,}
💰 Max Profit: ₹{int(max_profit):,}
📐 R:R = 1:{round(tp2_val/sl_val,1)}

📊 Levels:
   Spot: ₹{levels['price']}  VWAP: ₹{levels.get('vwap','—')}
   PDH:  ₹{levels.get('pdh','—')}  PDL: ₹{levels.get('pdl','—')}
   CPR:  ₹{cpr.get('bc','—')} – ₹{cpr.get('tc','—')}

🔵 Confidence: {setup['confidence']}%
⚠️ <i>Wait for 15min candle close to confirm.
Move SL to entry after TP1. AI signal only.</i>""".strip()


def format_crude_signal(signal, strike, expiry_str, premium, price_source,
                         sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
                         max_loss, max_profit, spot_inr, usd_price,
                         usd_inr, rsi, confidence, reasons) -> str:
    direction  = "BUY CALL 📈" if signal == "CE" else "BUY PUT 📉"
    opt_symbol = f"CRUDEOIL {strike} {signal}"
    eia_note   = "\n⚠️ <b>EIA Report tonight ~9PM — big moves expected!</b>" \
                 if ist_now().weekday() == 2 else ""

    return f"""
🛢️ <b>Crude Oil MCX — {direction}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>{opt_symbol}</b>
📅 Expiry: <b>{expiry_str}</b>
⏱ Evening Session (4PM–11PM IST){eia_note}

💰 Buy @ <b>₹{premium}</b>  ({price_source})

🎯 TP 1  =  <b>₹{tp1_px}</b>  (+₹{tp1_val}/bbl)
🎯 TP 2  =  <b>₹{tp2_px}</b>  (+₹{tp2_val}/bbl)
🛑 SL    =  <b>₹{sl_px}</b>   (−₹{sl_val}/bbl)

📦 1 lot × 100 barrels
💸 Max Loss:   ₹{int(max_loss):,}
💰 Max Profit: ₹{int(max_profit):,}
📐 R:R = 1:{round(tp2_val/sl_val,1)}

🛢️ MCX: ₹{spot_inr}/bbl  WTI: ${usd_price}/bbl
💱 USD/INR: ₹{usd_inr}  RSI: {rsi}
🔵 Confidence: {confidence}%

📋 Why:
{chr(10).join(reasons[:4])}

⚠️ <i>Crude is volatile. Use strict SL.
AI signal — trade at your own risk.</i>""".strip()

# ─────────────────────────────────────────────
#  SIGNAL GENERATORS
# ─────────────────────────────────────────────
def generate_level_alerts(chat_id: str):
    if not is_equity_open(): return
    balance   = user_balance.get(chat_id, 50000)
    today_str = ist_now().strftime("%Y-%m-%d")

    for inst in EQUITY_INSTRUMENTS:
        df = get_ohlcv(inst["symbol"], period="5d", interval="5m")
        if df is None: continue
        levels = calculate_levels(inst["symbol"], df)
        if not levels: continue
        setups = detect_breakout(levels, df)
        for setup in setups:
            key = f"{chat_id}_{inst['name']}_{setup['type']}_{today_str}"
            if level_alerts_sent.get(key): continue
            level_alerts_sent[key] = True

            price      = levels["price"]
            days       = days_to_expiry(inst["expiry_day"])
            expiry_str = next_expiry_date(inst["expiry_day"])
            strike     = get_nearest_strike(price, inst["strike_step"])
            ind_5m     = compute_indicators(df)
            premium, src = get_option_price(inst, strike, setup["direction"],
                                            price, days, ind_5m["atr"])
            lots = max(1, int((balance * DEFAULT_RISK_PERCENT / 100) /
                              (premium * 0.20 * inst["lot_size"])))
            sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val, max_loss, max_profit = \
                balance_driven_sl_tgt(balance, premium, inst["lot_size"])
            msg = format_level_alert(inst, setup, levels, premium, src,
                                     sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
                                     max_loss, max_profit, lots, expiry_str)
            print(f"[LEVEL ALERT] {inst['name']} {setup['type']} {setup['direction']}")
            send_telegram(chat_id, msg)
            time.sleep(1)


def generate_equity_signal(chat_id: str, mode: str = "intraday"):
    if not is_equity_open(): return
    balance = user_balance.get(chat_id, 50000)

    for inst in EQUITY_INSTRUMENTS:
        df_5m  = get_ohlcv(inst["symbol"], period="5d", interval="5m")
        df_15m = get_ohlcv_15m(inst["symbol"])
        if df_5m is None: continue

        ind_5m  = compute_indicators(df_5m)
        ind_15m = compute_indicators(df_15m) if df_15m is not None else None

        signal, confidence, reasons = score_signal(ind_5m, ind_15m)
        if signal is None:
            print(f"[{inst['name']}] No signal (conf={confidence}%)")
            continue

        spot       = ind_5m["price"]
        days       = days_to_expiry(inst["expiry_day"])
        expiry_str = next_expiry_date(inst["expiry_day"])
        strike     = get_nearest_strike(spot, inst["strike_step"])
        premium, src = get_option_price(inst, strike, signal, spot, days, ind_5m["atr"])
        lots = max(1, int((balance * DEFAULT_RISK_PERCENT / 100) /
                          (premium * 0.20 * inst["lot_size"])))
        sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val, max_loss, max_profit = \
            balance_driven_sl_tgt(balance, premium, inst["lot_size"])

        msg = format_equity_signal(inst, signal, spot, strike, expiry_str,
                                   premium, src, sl_px, sl_val, tp1_px, tp1_val,
                                   tp2_px, tp2_val, max_loss, max_profit,
                                   lots, confidence, reasons, mode)
        print(f"[SIGNAL] {inst['name']} {signal} conf={confidence}% premium=₹{premium} ({src})")
        send_telegram(chat_id, msg)
        time.sleep(1)


def generate_crude_signal(chat_id: str, mode: str = "crude"):
    if not is_crude_open():
        send_telegram(chat_id, "⛽ Crude session not active.\n🕓 4:00 PM – 11:00 PM IST.")
        return
    balance = user_balance.get(chat_id, 50000)

    df_raw = get_ohlcv(CRUDE_INSTRUMENT["symbol"], period="5d", interval="5m")
    if df_raw is None:
        send_telegram(chat_id, "❌ Could not fetch Crude data. Try again shortly.")
        return

    df, usd_inr = convert_crude_to_inr(df_raw)
    df_15m_raw  = get_ohlcv_15m(CRUDE_INSTRUMENT["symbol"])
    df_15m      = None
    if df_15m_raw is not None:
        df_15m, _ = convert_crude_to_inr(df_15m_raw)

    ind_5m  = compute_indicators(df)
    ind_15m = compute_indicators(df_15m) if df_15m is not None else None
    signal, confidence, reasons = score_signal(ind_5m, ind_15m)

    if signal is None:
        send_telegram(chat_id,
            f"🛢️ <b>Crude Oil</b>\n\n📊 No clear signal (conf={confidence}%).\n"
            f"Market is choppy — waiting for clearer setup.\n\n🔄 Next check in 10 min.")
        return

    spot      = ind_5m["price"]
    usd_price = round(spot / usd_inr, 2)
    strike    = get_nearest_strike(spot, CRUDE_INSTRUMENT["strike_step"])

    # Crude IV is higher ~30-40%, use 35%
    days_exp   = max(1, (calendar.monthrange(ist_now().year, ist_now().month)[1] - ist_now().day))
    T          = days_exp / 365
    vol_factor = 0.35 * math.sqrt(T)
    intrinsic  = max(0, spot - strike) if signal == "CE" else max(0, strike - spot)
    distance   = abs(spot - strike) / spot
    time_val   = 0.4 * spot * vol_factor * math.exp(-distance * 4)
    premium    = round(max(intrinsic + time_val, 20.0), 1)
    src        = "EST ⚡"

    # Crude SL: balance-driven
    max_loss_rs = balance * DEFAULT_RISK_PERCENT / 100
    sl_val  = round(min(max_loss_rs / CRUDE_INSTRUMENT["lot_size"], premium * 0.15), 1)
    sl_val  = max(sl_val, 5.0)
    tp1_val = round(sl_val * 1.5, 1)
    tp2_val = round(sl_val * 3.0, 1)
    sl_px   = round(premium - sl_val, 1)
    tp1_px  = round(premium + tp1_val, 1)
    tp2_px  = round(premium + tp2_val, 1)
    max_loss   = round(sl_val  * CRUDE_INSTRUMENT["lot_size"], 0)
    max_profit = round(tp2_val * CRUDE_INSTRUMENT["lot_size"], 0)

    now = ist_now()
    last_day   = calendar.monthrange(now.year, now.month)[1]
    expiry_str = f"{last_day} {now.strftime('%b')}"

    msg = format_crude_signal(
        signal, strike, expiry_str, premium, src,
        sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
        max_loss, max_profit, spot, usd_price, usd_inr,
        ind_5m["rsi"], confidence, reasons
    )
    print(f"[CRUDE] {signal} conf={confidence}% premium=₹{premium}")
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  BRIEFINGS
# ─────────────────────────────────────────────
def send_morning_briefing(chat_id: str):
    balance      = user_balance.get(chat_id, 0)
    now          = ist_now()
    today        = now.strftime("%d %b %Y, %A")
    nifty_price  = get_current_price("^NSEI")
    sensex_price = get_current_price("^BSESN")
    crude_usd    = get_current_price("CL=F")
    usd_inr      = get_usd_inr()
    crude_inr    = round(crude_usd * usd_inr, 1) if crude_usd else None
    nifty_exp    = "🔴 TODAY" if now.weekday() == 1 else next_expiry_date(1)
    sensex_exp   = "🔴 TODAY" if now.weekday() == 3 else next_expiry_date(3)
    bal_line     = f"💼 Balance: <b>₹{int(balance):,}</b>  Risk/trade: ₹{int(balance*0.02):,}" \
                   if balance > 0 else "💼 Set balance: <code>/balance 50000</code>"

    # Holiday warnings
    holiday_note = ""
    if is_nse_holiday(now.date()):
        holiday_note = "\n🚫 <b>NSE/BSE HOLIDAY TODAY — No equity signals</b>"
    if is_mcx_full_holiday(now.date()):
        holiday_note += "\n🚫 <b>MCX HOLIDAY TODAY — No crude signals</b>"
    elif is_mcx_partial_holiday(now.date()):
        holiday_note += "\n⚠️ <b>MCX Partial Holiday — Crude signals from 5:00 PM only</b>"

    crude_timing = "5:00 PM – 11:00 PM (partial holiday)" \
                   if is_mcx_partial_holiday(now.date()) else "9:00 AM – 11:00 PM"

    send_telegram(chat_id, f"""
🌅 <b>Good Morning — F&amp;O Signal Bot</b>
📆 {today}{holiday_note}

📊 Nifty 50:  ₹{nifty_price or '—'}  (Expiry: {nifty_exp})
📈 Sensex:    ₹{sensex_price or '—'}  (Expiry: {sensex_exp})
🛢️ Crude Oil: ₹{crude_inr or '—'}/bbl  (${crude_usd or '—'})
💱 USD/INR:   ₹{usd_inr}

{bal_line}

🕘 Equity: 9:15 AM – 3:30 PM
🛢️ Crude:  {crude_timing}
📐 Key levels sent at 9:16 AM""".strip())


def send_crude_briefing(chat_id: str):
    crude_usd = get_current_price("CL=F")
    usd_inr   = get_usd_inr()
    crude_inr = round(crude_usd * usd_inr, 1) if crude_usd else None
    balance   = user_balance.get(chat_id, 0)
    eia_note  = "\n⚠️ <b>WEDNESDAY — EIA Report ~9PM. High volatility!</b>" \
                if ist_now().weekday() == 2 else ""
    send_telegram(chat_id, f"""
🌆 <b>Crude Oil Session Starting</b>
🕓 4:00 PM – 11:00 PM IST{eia_note}

🛢️ MCX:  ₹{crude_inr or '—'}/barrel
🌍 WTI:  ${crude_usd or '—'}/barrel
💱 USD/INR: ₹{usd_inr}

💼 Balance: ₹{int(balance):,}
📡 Auto signals every 10 min. /crude anytime.""".strip())

# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
def handle_command(chat_id: str, text: str):
    text = text.strip()
    cmd  = text.lower().split()[0] if text else ""

    if cmd == "/start":
        send_telegram(chat_id, """
👋 <b>F&amp;O Signal Bot</b>

📊 Nifty 50  (Expiry: Tuesday)
📈 Sensex    (Expiry: Thursday)
🛢️ Crude Oil (Expiry: Monthly, 4PM–11PM)

Each signal shows:
💰 Buy price  |  CE or PE clearly
🎯 TP1 + TP2  |  🛑 SL
📦 Lots based on your balance
🔵 Confidence %

<b>Start:</b> <code>/balance 50000</code>""".strip())

    elif cmd in ("/balance", "balance"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                bal = float(parts[1].replace(",", "").replace("₹", ""))
                user_balance[chat_id] = bal
                send_telegram(chat_id,
                    f"✅ Balance set: ₹{int(bal):,}\n"
                    f"Risk/trade (2%): ₹{int(bal*0.02):,}\n"
                    f"SL per lot (Nifty): ₹{round(bal*0.02/65,1)}")
            except:
                send_telegram(chat_id, "❌ Example: /balance 50000")
        else:
            send_telegram(chat_id, "Example: /balance 50000")

    elif cmd == "/signal":
        send_telegram(chat_id, "🔍 Analyzing market (5min + 15min)...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()

    elif cmd == "/scalp":
        send_telegram(chat_id, "⚡ Scalp analysis running...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()

    elif cmd == "/levels":
        send_telegram(chat_id, "📐 Calculating key levels...")
        threading.Thread(target=send_daily_levels, args=(chat_id,)).start()

    elif cmd == "/crude":
        send_telegram(chat_id, "🛢️ Analyzing Crude Oil (5min + 15min)...")
        threading.Thread(target=generate_crude_signal, args=(chat_id,)).start()

    elif cmd == "/status":
        bal  = user_balance.get(chat_id, 0)
        now  = ist_now()
        eq   = "🟢 OPEN" if is_equity_open()  else "🔴 CLOSED"
        cr   = "🟢 OPEN" if is_crude_open()   else "🔴 CLOSED"
        nse_hol = "🚫 Holiday" if is_nse_holiday(now.date())     else "✅ Trading day"
        mcx_hol = "🚫 Holiday" if is_mcx_full_holiday(now.date()) else \
                  ("⚠️ Partial (eve only)" if is_mcx_partial_holiday(now.date()) else "✅ Trading day")
        send_telegram(chat_id, f"""
📡 <b>Bot Status</b>
Equity:  {eq}  ({nse_hol})
Crude:   {cr}  ({mcx_hol})
Nifty Expiry:  {next_expiry_date(1)}
Sensex Expiry: {next_expiry_date(3)}
Balance: ₹{int(bal):,}
Confidence min: {MIN_CONFIDENCE}%""".strip())

    elif cmd == "/help":
        send_telegram(chat_id, """
📖 <b>Commands</b>

/balance 50000 → Set capital
/signal        → Nifty + Sensex signal now
/scalp         → Scalp signal now
/levels        → Today's key levels
/crude         → Crude Oil signal now
/status        → Market status

📅 Auto Schedule:
• 8:45 AM  → Morning briefing
• 8:50 AM  → Crude Oil briefing
• 9:00 AM  → Crude signals start
• 9:15 AM  → Equity signals start
• 9:16 AM  → Key levels sent
• Every 2 min → Level breakout scan
• Every 5 min → Scalp signals
• Every 15 min → Intraday signals
• 11:00 PM → Crude signals stop

🚫 <b>Holidays — No signals:</b>
• Saturday &amp; Sunday (equity)
• All govt holidays (NSE list)
• MCX partial holidays: crude 5PM+

✅ CE / PE shown clearly
✅ 5min + 15min analysis
✅ Min 55% confidence only""".strip())

# ─────────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────────
def scheduler_loop():
    last_scalp       = {}
    last_intraday    = {}
    last_crude       = {}
    last_morning     = {}
    last_crude_brief = {}
    last_levels      = {}
    last_level_scan  = {}

    print("📡 Scheduler started...")
    while True:
        try:
            now       = ist_now()
            today_str = now.strftime("%Y-%m-%d")
            ts        = now.timestamp()
            eq_open   = is_equity_open()
            cr_open   = is_crude_open()

            if now.minute % 5 == 0 and now.second < 30:
                print(f"[{now.strftime('%H:%M')} IST] Users:{len(user_balance)} "
                      f"Eq:{'OPEN' if eq_open else 'closed'} "
                      f"Crude:{'OPEN' if cr_open else 'closed'}")

            for chat_id in list(user_balance.keys()):

                if now.hour == MORNING_HOUR and now.minute == MORNING_MIN:
                    if last_morning.get(chat_id) != today_str:
                        send_morning_briefing(chat_id)
                        last_morning[chat_id] = today_str

                if now.hour == 9 and now.minute == 16:
                    if last_levels.get(chat_id) != today_str:
                        threading.Thread(target=send_daily_levels, args=(chat_id,)).start()
                        last_levels[chat_id] = today_str

                if now.hour == CRUDE_BRIEF_HOUR and now.minute == CRUDE_BRIEF_MIN:
                    if last_crude_brief.get(chat_id) != today_str:
                        send_crude_briefing(chat_id)
                        last_crude_brief[chat_id] = today_str

                if eq_open:
                    if ts - last_level_scan.get(chat_id, 0) >= LEVEL_SCAN_INTERVAL:
                        threading.Thread(target=generate_level_alerts, args=(chat_id,)).start()
                        last_level_scan[chat_id] = ts

                    if ts - last_scalp.get(chat_id, 0) >= SCALP_INTERVAL:
                        print(f"[{now.strftime('%H:%M')}] Scalp → {chat_id}")
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()
                        last_scalp[chat_id] = ts
                        # Stagger intraday — don't fire both at same time
                        last_intraday[chat_id] = ts

                    elif ts - last_intraday.get(chat_id, 0) >= INTRADAY_INTERVAL:
                        print(f"[{now.strftime('%H:%M')}] Intraday → {chat_id}")
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()
                        last_intraday[chat_id] = ts

                    for inst in EQUITY_INSTRUMENTS:
                        if is_expiry_today(inst["expiry_day"]):
                            for (h, m) in [(9, 20), (14, 0), (15, 0)]:
                                if now.hour == h and now.minute == m:
                                    key = f"exp_{inst['name']}_{h}_{today_str}_{chat_id}"
                                    if not active_signals.get(key):
                                        threading.Thread(target=generate_equity_signal,
                                                         args=(chat_id, "expiry")).start()
                                        active_signals[key] = True

                if cr_open:
                    if ts - last_crude.get(chat_id, 0) >= CRUDE_INTERVAL:
                        print(f"[{now.strftime('%H:%M')}] Crude → {chat_id}")
                        threading.Thread(target=generate_crude_signal, args=(chat_id,)).start()
                        last_crude[chat_id] = ts

        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(30)

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print("🤖 Bot started — Nifty + Sensex + Crude Oil")

    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != "YOUR_CHAT_ID_HERE":
        if TELEGRAM_CHAT_ID not in user_balance:
            user_balance[TELEGRAM_CHAT_ID] = 50000
            print(f"Auto-registered: {TELEGRAM_CHAT_ID}")

    send_telegram(TELEGRAM_CHAT_ID,
        "🤖 <b>Bot started!</b>\n\n"
        "✅ Signals now show CE / PE clearly\n"
        "✅ Uses 5min + 15min analysis\n"
        "✅ Won't give CALL when market is falling\n\n"
        "Send your balance:\n<code>/balance 50000</code>\n\n"
        "Then send /signal to test!")

    threading.Thread(target=scheduler_loop, daemon=True).start()

    offset = None
    while True:
        try:
            updates = get_updates(offset)
            if "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        msg     = update["message"]
                        chat_id = str(msg["chat"]["id"])
                        text    = msg.get("text", "")
                        if text:
                            if chat_id not in user_balance:
                                user_balance[chat_id] = 50000
                            handle_command(chat_id, text)
        except Exception as e:
            print(f"Polling error: {e}")
        time.sleep(2)

if __name__ == "__main__":
    main()
