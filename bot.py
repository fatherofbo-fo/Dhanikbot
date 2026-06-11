"""
F&O Signal Bot — Nifty 50 + Sensex + Crude Oil
Signal format: clean card style with TP1, TP2, SL
"""

import os
import time
import math
import datetime
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
CRUDE_OPEN   = datetime.time(16, 0)
CRUDE_CLOSE  = datetime.time(23, 0)

SCALP_INTERVAL    = 300    # 5 min
INTRADAY_INTERVAL = 900    # 15 min
CRUDE_INTERVAL    = 600    # 10 min

MORNING_HOUR     = 8
MORNING_MIN      = 45
CRUDE_BRIEF_HOUR = 15
CRUDE_BRIEF_MIN  = 45

DEFAULT_RISK_PERCENT = 2.0

# ─────────────────────────────────────────────
#  INSTRUMENTS  (BankNifty removed, Sensex added)
# ─────────────────────────────────────────────
EQUITY_INSTRUMENTS = [
    {
        "name":        "NIFTY",
        "full_name":   "Nifty 50",
        "symbol":      "^NSEI",
        "lot_size":    65,
        "strike_step": 50,
        "expiry_day":  1,    # Tuesday (NSE changed from Thursday to Tuesday, Sep 2025)
    },
    {
        "name":        "SENSEX",
        "full_name":   "BSE Sensex",
        "symbol":      "^BSESN",
        "lot_size":    20,
        "strike_step": 100,
        "expiry_day":  3,    # Thursday (BSE Sensex weekly expiry)
    },
]

CRUDE_INSTRUMENT = {
    "name":        "CRUDEOIL",
    "full_name":   "Crude Oil MCX",
    "symbol":      "CL=F",
    "lot_size":    100,  # MCX Crude lot = 100 barrels
    "strike_step": 100,
}

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
user_balance   = {}
active_signals = {}

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
def get_ohlcv(symbol: str, period="2d", interval="5m"):
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

def is_equity_open():
    now = ist_now()
    return now.weekday() < 5 and EQUITY_OPEN <= now.time() <= EQUITY_CLOSE

def is_crude_open():
    now = ist_now()
    return now.weekday() < 6 and CRUDE_OPEN <= now.time() <= CRUDE_CLOSE

def next_expiry_date(expiry_weekday: int) -> str:
    """Return next expiry date string e.g. '13 Jun'"""
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
#  TECHNICAL ANALYSIS
# ─────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    ema9  = EMAIndicator(close, window=9).ema_indicator()
    ema21 = EMAIndicator(close, window=21).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    macd_obj  = MACD(close)
    macd_line = macd_obj.macd()
    macd_sig  = macd_obj.macd_signal()
    macd_hist = macd_obj.macd_diff()
    rsi       = RSIIndicator(close, window=14).rsi()
    stoch     = StochasticOscillator(high, low, close, window=14)
    bb        = BollingerBands(close, window=20)
    atr       = AverageTrueRange(high, low, close, window=14).average_true_range()
    vol_avg   = vol.rolling(20).mean()
    vol_ratio = (vol.iloc[-1] / vol_avg.iloc[-1]) if vol_avg.iloc[-1] > 0 else 1.0

    return {
        "price":          round(close.iloc[-1], 2),
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
        "atr":            round(atr.iloc[-1], 2),
        "vol_ratio":      round(vol_ratio, 2),
    }

def score_signal(ind: dict) -> tuple:
    bull_score, bear_score = 0, 0
    bull_reasons, bear_reasons = [], []
    p, rsi, stk, std = ind["price"], ind["rsi"], ind["stoch_k"], ind["stoch_d"]

    if ind["ema9"] > ind["ema21"] > ind["ema50"]:
        bull_score += 20; bull_reasons.append("✅ EMA Stack Bullish (9>21>50)")
    elif ind["ema9"] < ind["ema21"] < ind["ema50"]:
        bear_score += 20; bear_reasons.append("✅ EMA Stack Bearish (9<21<50)")

    if p > ind["ema21"]:
        bull_score += 10; bull_reasons.append("📈 Price above EMA21")
    else:
        bear_score += 10; bear_reasons.append("📉 Price below EMA21")

    if ind["macd_hist"] > 0 and ind["macd_hist_prev"] <= 0:
        bull_score += 25; bull_reasons.append("🔀 MACD Bullish Crossover")
    elif ind["macd_hist"] < 0 and ind["macd_hist_prev"] >= 0:
        bear_score += 25; bear_reasons.append("🔀 MACD Bearish Crossover")
    elif ind["macd"] > ind["macd_sig"]:
        bull_score += 10; bull_reasons.append("📊 MACD above Signal line")
    else:
        bear_score += 10; bear_reasons.append("📊 MACD below Signal line")

    if rsi > 60:
        bull_score += 15; bull_reasons.append(f"⚡ RSI Strong: {rsi}")
    elif rsi < 40:
        bear_score += 15; bear_reasons.append(f"⚡ RSI Weak: {rsi}")
    if rsi > 70:
        bull_score -= 10; bull_reasons.append("⚠️ RSI Overbought – caution")
    elif rsi < 30:
        bear_score -= 10; bear_reasons.append("⚠️ RSI Oversold – caution")

    if stk > std and stk < 80:
        bull_score += 10; bull_reasons.append(f"📐 Stoch Bullish K:{stk} > D:{std}")
    elif stk < std and stk > 20:
        bear_score += 10; bear_reasons.append(f"📐 Stoch Bearish K:{stk} < D:{std}")

    if p < ind["bb_low"]:
        bull_score += 15; bull_reasons.append("🎯 Price at Lower BB – Bounce")
    elif p > ind["bb_high"]:
        bear_score += 15; bear_reasons.append("🎯 Price at Upper BB – Reversal")

    if ind["vol_ratio"] > 1.5:
        if bull_score > bear_score:
            bull_score += 10; bull_reasons.append(f"📣 Volume Surge {ind['vol_ratio']}x avg")
        else:
            bear_score += 10; bear_reasons.append(f"📣 Volume Surge {ind['vol_ratio']}x avg")

    if (ind["atr"] / p) * 100 < 0.08:
        return None, 0, ["⛔ Low volatility – skip trade"]

    if bull_score > bear_score and bull_score >= 45:
        return "CALL", min(bull_score, 100), bull_reasons
    elif bear_score > bull_score and bear_score >= 45:
        return "PUT",  min(bear_score, 100), bear_reasons
    return None, max(bull_score, bear_score), []

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)

def estimate_equity_premium(spot: float, strike: float, atr: float,
                             signal: str, days: int) -> float:
    time_val  = atr * math.sqrt(max(days, 0.5)) * 0.4
    intrinsic = max(0, spot - strike) if signal == "CALL" else max(0, strike - spot)
    return round(intrinsic + time_val, 1)

def balance_driven_sl_tgt(balance: float, premium: float, lot_size: int):
    """
    Calculate SL & two Targets from balance.
    Max loss = 2% of balance.
    TP1 = 1:1.5 R:R
    TP2 = 1:3   R:R
    """
    max_loss_rs = balance * DEFAULT_RISK_PERCENT / 100      # e.g. ₹1,000
    sl_per_unit = round(max_loss_rs / lot_size, 1)          # per share/barrel
    sl_per_unit = min(sl_per_unit, round(premium * 0.20, 1))# cap at 20% of premium
    sl_per_unit = max(sl_per_unit, 1.0)

    tp1_per_unit = round(sl_per_unit * 1.5, 1)
    tp2_per_unit = round(sl_per_unit * 3.0, 1)

    sl_price  = round(premium - sl_per_unit, 1)
    tp1_price = round(premium + tp1_per_unit, 1)
    tp2_price = round(premium + tp2_per_unit, 1)

    max_loss   = round(sl_per_unit  * lot_size, 0)
    max_profit = round(tp2_per_unit * lot_size, 0)

    return sl_price, sl_per_unit, tp1_price, tp1_per_unit, tp2_price, tp2_per_unit, max_loss, max_profit

def fetch_live_crude_premium(strike: int, option_type: str, spot_inr: float) -> float:
    try:
        now = ist_now()
        month_map = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                     7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
        exp_str = f"{month_map[now.month]}{str(now.year)[2:]}"
        symbol  = f"CRUDEOIL{exp_str}{strike}{option_type}"
        r = requests.get(f"https://api.mcxindia.com/api/option-chain?symbol={symbol}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code == 200:
            ltp = r.json().get("ltp") or r.json().get("LTP")
            if ltp and float(ltp) > 0:
                return round(float(ltp), 2)
    except:
        pass
    # Fallback: realistic estimate ~2.5% of spot
    atm    = round(spot_inr / 100) * 100
    dist   = abs(strike - atm)
    prem   = spot_inr * 0.025 * math.exp(-dist / (spot_inr * 0.03))
    days   = max(3, (ist_now().weekday() - 3) % 7)
    return round(prem * math.sqrt(days / 30), 1)

# ─────────────────────────────────────────────
#  SIGNAL FORMAT  (clean card style)
# ─────────────────────────────────────────────
def format_equity_signal(inst: dict, signal: str, spot: float,
                          strike: int, expiry_str: str, premium: float,
                          sl_px, sl_val, tp1_px, tp1_val,
                          tp2_px, tp2_val, max_loss, max_profit,
                          lots: int, confidence: int, reasons: list,
                          mode: str) -> str:

    opt_type  = "CE" if signal == "CALL" else "PE"
    direction = "BUY CALL 📈" if signal == "CALL" else "BUY PUT 📉"
    mode_tag  = {"scalp": "⚡ Scalping", "expiry": "🔥 Expiry Day"}.get(mode, "📊 Intraday")
    icon      = "📊" if inst["name"] == "NIFTY" else "📈"

    return f"""
{icon} <b>{inst['full_name']} — {direction}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>{inst['name']} {strike}{opt_type}</b>
📅 Expiry: <b>{expiry_str}</b>
⏱ Mode: {mode_tag}

💰 Buy @ <b>₹{premium}</b>

🎯 TP 1  =  <b>₹{tp1_px}</b>  (+₹{tp1_val})
🎯 TP 2  =  <b>₹{tp2_px}</b>  (+₹{tp2_val})
🛑 SL    =  <b>₹{sl_px}</b>   (−₹{sl_val})

📦 Lots: {lots} × {inst['lot_size']} qty
💸 Max Loss:   ₹{int(max_loss):,}
💰 Max Profit: ₹{int(max_profit):,}  (at TP2)
📐 R:R = 1:{round(tp2_val/sl_val, 1)}

📉 Spot: ₹{spot}  |  RSI: {reasons[0].split(': ')[-1] if 'RSI' in reasons[0] else 'N/A'}
🔵 Confidence: {confidence}%

📋 Analysis:
{chr(10).join(reasons[:4])}

⚠️ <i>Exit at TP1 if unsure. Move SL to entry after TP1.
AI signal only — trade at your own risk.</i>
""".strip()


def format_crude_signal(signal: str, strike: int, expiry_str: str,
                         premium: float, sl_px, sl_val, tp1_px, tp1_val,
                         tp2_px, tp2_val, max_loss, max_profit,
                         spot_inr: float, usd_price: float, usd_inr: float,
                         rsi: float, confidence: int, reasons: list) -> str:

    opt_type  = "CE" if signal == "CALL" else "PE"
    direction = "BUY CALL 📈" if signal == "CALL" else "BUY PUT 📉"

    eia_note = "\n⚠️ <b>EIA Data tonight ~9PM — expect big moves!</b>" \
               if ist_now().weekday() == 2 else ""

    return f"""
🛢️ <b>Crude Oil MCX — {direction}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>CRUDEOIL {strike}{opt_type}</b>
📅 Expiry: <b>{expiry_str}</b>
⏱ Evening Session (4PM–11PM IST){eia_note}

💰 Buy @ <b>₹{premium}</b>

🎯 TP 1  =  <b>₹{tp1_px}</b>  (+₹{tp1_val}/barrel)
🎯 TP 2  =  <b>₹{tp2_px}</b>  (+₹{tp2_val}/barrel)
🛑 SL    =  <b>₹{sl_px}</b>   (−₹{sl_val}/barrel)

📦 1 lot × 100 barrels
💸 Max Loss:   ₹{int(max_loss):,}
💰 Max Profit: ₹{int(max_profit):,}  (at TP2)
📐 R:R = 1:{round(tp2_val/sl_val, 1)}

🛢️ MCX: ₹{spot_inr}/bbl  |  WTI: ${usd_price}/bbl
💱 USD/INR: ₹{usd_inr}  |  RSI: {rsi}
🔵 Confidence: {confidence}%

📋 Analysis:
{chr(10).join(reasons[:4])}

⚠️ <i>Crude is volatile. Exit at TP1 if unsure.
AI signal only — trade at your own risk.</i>
""".strip()

# ─────────────────────────────────────────────
#  SIGNAL GENERATORS
# ─────────────────────────────────────────────
def generate_equity_signal(chat_id: str, mode: str = "intraday"):
    if not is_equity_open():
        return
    balance = user_balance.get(chat_id, 50000)

    for inst in EQUITY_INSTRUMENTS:
        df = get_ohlcv(inst["symbol"])
        if df is None:
            continue
        ind = compute_indicators(df)
        signal, confidence, reasons = score_signal(ind)
        if signal is None:
            continue

        spot       = ind["price"]
        days       = days_to_expiry(inst["expiry_day"])
        expiry_str = next_expiry_date(inst["expiry_day"])
        strike     = get_nearest_strike(spot, inst["strike_step"])
        premium    = estimate_equity_premium(spot, strike, ind["atr"], signal, days)
        lots       = max(1, int((balance * DEFAULT_RISK_PERCENT / 100) /
                                (premium * 0.20 * inst["lot_size"])))

        sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val, max_loss, max_profit = \
            balance_driven_sl_tgt(balance, premium, inst["lot_size"])

        msg = format_equity_signal(
            inst, signal, spot, strike, expiry_str, premium,
            sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
            max_loss, max_profit, lots, confidence, reasons, mode
        )
        send_telegram(chat_id, msg)
        time.sleep(1)


def generate_crude_signal(chat_id: str, mode: str = "crude"):
    if not is_crude_open():
        send_telegram(chat_id, "⛽ Crude session not active.\n🕓 Crude signals: 4:00 PM – 11:00 PM IST.")
        return

    balance = user_balance.get(chat_id, 50000)
    inst    = CRUDE_INSTRUMENT

    df_raw = get_ohlcv(inst["symbol"], period="5d", interval="5m")
    if df_raw is None:
        send_telegram(chat_id, "❌ Could not fetch Crude data. Try again shortly.")
        return

    df, usd_inr = convert_crude_to_inr(df_raw)
    ind = compute_indicators(df)
    signal, confidence, reasons = score_signal(ind)

    if signal is None:
        send_telegram(chat_id, f"🛢️ <b>Crude Oil</b>\n\n📊 No clear signal right now.\nMarket consolidating.\nConfidence: {confidence}%\n\n🔄 Next check in 10 minutes.")
        return

    spot      = ind["price"]
    usd_price = round(spot / usd_inr, 2)
    strike    = get_nearest_strike(spot, inst["strike_step"])
    opt_type  = "CE" if signal == "CALL" else "PE"
    premium   = fetch_live_crude_premium(strike, opt_type, spot)

    # Crude SL: balance-driven, capped at 15% of premium
    max_loss_rs  = balance * DEFAULT_RISK_PERCENT / 100
    sl_val       = round(min(max_loss_rs / inst["lot_size"], premium * 0.15), 1)
    tp1_val      = round(sl_val * 1.5, 1)
    tp2_val      = round(sl_val * 3.0, 1)
    sl_px        = round(premium - sl_val, 1)
    tp1_px       = round(premium + tp1_val, 1)
    tp2_px       = round(premium + tp2_val, 1)
    max_loss     = round(sl_val  * inst["lot_size"], 0)
    max_profit   = round(tp2_val * inst["lot_size"], 0)

    # Crude expiry: last day of current month
    now = ist_now()
    import calendar
    last_day = calendar.monthrange(now.year, now.month)[1]
    expiry_str = f"{last_day} {now.strftime('%b')}"

    msg = format_crude_signal(
        signal, strike, expiry_str, premium,
        sl_px, sl_val, tp1_px, tp1_val, tp2_px, tp2_val,
        max_loss, max_profit, spot, usd_price, usd_inr,
        ind["rsi"], confidence, reasons
    )
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  MORNING BRIEFING
# ─────────────────────────────────────────────
def send_morning_briefing(chat_id: str):
    balance      = user_balance.get(chat_id, 0)
    today        = ist_now().strftime("%d %b %Y, %A")
    nifty_price  = get_current_price("^NSEI")
    sensex_price = get_current_price("^BSESN")
    crude_usd    = get_current_price("CL=F")
    usd_inr      = get_usd_inr()
    crude_inr    = round(crude_usd * usd_inr, 1) if crude_usd else None

    nifty_expiry  = "🔴 TODAY" if ist_now().weekday() == 1 else next_expiry_date(1)
    sensex_expiry = "🔴 TODAY" if ist_now().weekday() == 3 else next_expiry_date(3)

    bal_line = f"💼 Balance: <b>₹{int(balance):,}</b>  |  Risk/trade: ₹{int(balance*0.02):,}" \
               if balance > 0 else "💼 Set balance: <code>/balance 50000</code>"

    msg = f"""
🌅 <b>Good Morning — F&amp;O Signal Bot</b>
📆 {today}

📊 <b>Pre-Market Levels:</b>
   Nifty 50:  ₹{nifty_price or '—'}   (Expiry: {nifty_expiry})
   Sensex:    ₹{sensex_price or '—'}   (Expiry: {sensex_expiry})
   Crude Oil: ₹{crude_inr or '—'}/bbl  (${crude_usd or '—'})
   USD/INR:   ₹{usd_inr}

{bal_line}

🕘 Equity signals:  9:15 AM – 3:30 PM
🛢️ Crude signals:  4:00 PM – 11:00 PM

/signal → get signal now
/crude  → get crude signal now""".strip()
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  CRUDE BRIEFING
# ─────────────────────────────────────────────
def send_crude_briefing(chat_id: str):
    crude_usd = get_current_price("CL=F")
    usd_inr   = get_usd_inr()
    crude_inr = round(crude_usd * usd_inr, 1) if crude_usd else None
    balance   = user_balance.get(chat_id, 0)
    bal_line  = f"💼 Balance: ₹{int(balance):,}" if balance > 0 else "Set balance: /balance 50000"
    eia_note  = "\n⚠️ <b>WEDNESDAY — EIA Report ~9PM. High volatility tonight!</b>" \
                if ist_now().weekday() == 2 else ""

    msg = f"""
🌆 <b>Crude Oil Session Starting</b>
🕓 4:00 PM – 11:00 PM IST{eia_note}

🛢️ MCX:  ₹{crude_inr or '—'}/barrel
🌍 WTI:  ${crude_usd or '—'}/barrel
💱 USD/INR: ₹{usd_inr}

{bal_line}
📡 Auto signals every 10 min. Use /crude anytime.""".strip()
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
def handle_command(chat_id: str, text: str):
    text = text.strip()
    cmd  = text.lower().split()[0] if text else ""

    if cmd == "/start":
        send_telegram(chat_id, """
👋 <b>F&amp;O Signal Bot</b>

Signals for:
📊 Nifty 50    → every Thursday expiry
📈 Sensex      → every Friday expiry
🛢️ Crude Oil  → monthly expiry (4PM–11PM)

Each signal:
💰 Buy price  🎯 TP1  🎯 TP2  🛑 SL

<b>Start:</b> <code>/balance 50000</code>""".strip())

    elif cmd in ("/balance", "balance"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                bal = float(parts[1].replace(",", ""))
                user_balance[chat_id] = bal
                send_telegram(chat_id, f"✅ Balance: ₹{int(bal):,}\nRisk/trade (2%): ₹{int(bal*0.02):,}")
            except:
                send_telegram(chat_id, "❌ Example: /balance 50000")
        else:
            send_telegram(chat_id, "Example: /balance 50000")

    elif cmd == "/signal":
        send_telegram(chat_id, "🔍 Analyzing Nifty & Sensex...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()

    elif cmd == "/scalp":
        send_telegram(chat_id, "⚡ Running scalp analysis...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()

    elif cmd == "/crude":
        send_telegram(chat_id, "🛢️ Fetching live Crude data...")
        threading.Thread(target=generate_crude_signal, args=(chat_id,)).start()

    elif cmd == "/status":
        bal       = user_balance.get(chat_id, 0)
        eq_status = "🟢 OPEN" if is_equity_open() else "🔴 CLOSED"
        cr_status = "🟢 OPEN" if is_crude_open()  else "🔴 CLOSED"
        send_telegram(chat_id, f"""
📡 <b>Bot Status</b>
Equity:  {eq_status}
Crude:   {cr_status}
Balance: ₹{int(bal):,}

Next Nifty Expiry:  {next_expiry_date(3)}
Next Sensex Expiry: {next_expiry_date(4)}""".strip())

    elif cmd == "/help":
        send_telegram(chat_id, """
📖 <b>Commands</b>

/balance 50000 → Set capital
/signal        → Nifty + Sensex signal now
/scalp         → Scalp signal now
/crude         → Crude Oil signal now
/status        → Market status
/help          → This menu

📅 Auto Schedule:
• 8:45 AM  → Morning briefing
• 9:15 AM  → Equity signals (every 5/15 min)
• 3:45 PM  → Crude briefing
• 4:00 PM  → Crude signals (every 10 min)""".strip())

# ─────────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────────
def scheduler_loop():
    last_scalp       = {}
    last_intraday    = {}
    last_crude       = {}
    last_morning     = {}
    last_crude_brief = {}

    while True:
        try:
            now       = ist_now()
            today_str = now.strftime("%Y-%m-%d")
            ts        = now.timestamp()

            for chat_id in list(user_balance.keys()):

                if now.hour == MORNING_HOUR and now.minute == MORNING_MIN:
                    if last_morning.get(chat_id) != today_str:
                        send_morning_briefing(chat_id)
                        last_morning[chat_id] = today_str

                if now.hour == CRUDE_BRIEF_HOUR and now.minute == CRUDE_BRIEF_MIN:
                    if last_crude_brief.get(chat_id) != today_str:
                        send_crude_briefing(chat_id)
                        last_crude_brief[chat_id] = today_str

                if is_equity_open():
                    if ts - last_scalp.get(chat_id, 0) >= SCALP_INTERVAL:
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()
                        last_scalp[chat_id] = ts
                    if ts - last_intraday.get(chat_id, 0) >= INTRADAY_INTERVAL:
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()
                        last_intraday[chat_id] = ts

                    # Expiry day special signals
                    for inst in EQUITY_INSTRUMENTS:
                        if is_expiry_today(inst["expiry_day"]):
                            for (h, m) in [(9, 20), (14, 0), (15, 0)]:
                                if now.hour == h and now.minute == m:
                                    key = f"expiry_{inst['name']}_{h}_{today_str}_{chat_id}"
                                    if not active_signals.get(key):
                                        threading.Thread(target=generate_equity_signal, args=(chat_id, "expiry")).start()
                                        active_signals[key] = True

                if is_crude_open():
                    if ts - last_crude.get(chat_id, 0) >= CRUDE_INTERVAL:
                        threading.Thread(target=generate_crude_signal, args=(chat_id,)).start()
                        last_crude[chat_id] = ts

        except Exception as e:
            print(f"Scheduler error: {e}")

        time.sleep(60)

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print("🤖 Bot started — Nifty + Sensex + Crude Oil")
    send_telegram(TELEGRAM_CHAT_ID, "🤖 Bot started!\n\n📊 Nifty 50 + Sensex + 🛢️ Crude Oil\nSend /start to begin.")
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
                            handle_command(chat_id, text)
        except Exception as e:
            print(f"Polling error: {e}")
        time.sleep(2)

if __name__ == "__main__":
    main()
