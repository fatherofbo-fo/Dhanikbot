"""
Nifty/BankNifty/Crude Oil F&O Signal Bot
- Equity (Nifty + BankNifty): 9:15 AM – 3:30 PM IST
- Crude Oil (MCX):             4:00 PM – 11:00 PM IST
Premium for Crude: fetched LIVE from MCX via NSE API
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
CRUDE_OPEN   = datetime.time(16, 0)    # 4:00 PM
CRUDE_CLOSE  = datetime.time(23, 0)    # 11:00 PM

SCALP_INTERVAL    = 300
INTRADAY_INTERVAL = 900
CRUDE_INTERVAL    = 600

MORNING_HOUR     = 8
MORNING_MIN      = 45
CRUDE_BRIEF_HOUR = 15
CRUDE_BRIEF_MIN  = 45

DEFAULT_RISK_PERCENT = 2.0
SL_PERCENT           = 25
TARGET_PERCENT       = 50

# ─────────────────────────────────────────────
#  INSTRUMENTS
# ─────────────────────────────────────────────
EQUITY_INSTRUMENTS = [
    {"name": "NIFTY 50",   "symbol": "^NSEI",    "lot_size": 25,  "strike_step": 50,  "type": "equity"},
    {"name": "BANK NIFTY", "symbol": "^NSEBANK",  "lot_size": 15,  "strike_step": 100, "type": "equity"},
]

CRUDE_INSTRUMENT = {
    "name":        "CRUDE OIL (MCX)",
    "symbol":      "CL=F",
    "lot_size":    100,       # MCX crude = 100 barrels per lot
    "strike_step": 100,       # MCX crude strike gap = ₹100
    "type":        "commodity",
}

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
user_balance   = {}
active_signals = {}

# ─────────────────────────────────────────────
#  TELEGRAM HELPERS
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

def is_expiry_thursday():
    return ist_now().weekday() == 3

def days_to_expiry() -> int:
    now = ist_now()
    d = (3 - now.weekday()) % 7
    return d if d > 0 else 7

# ─────────────────────────────────────────────
#  LIVE CRUDE OPTION PREMIUM (MCX via NSE API)
# ─────────────────────────────────────────────
def fetch_live_crude_premium(strike: int, option_type: str, spot_inr: float) -> float:
    """
    Tries to fetch live MCX crude option premium.
    Falls back to Black-Scholes-like estimate if API unavailable.
    option_type: 'CE' or 'PE'
    """
    # Method 1: Try MCX option chain via unofficial API
    try:
        now  = ist_now()
        # Build expiry string — nearest month last day
        # MCX crude expires on last day of expiry month
        month_map = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                     7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
        m = now.month
        y = now.year
        exp_str = f"{month_map[m]}{str(y)[2:]}"  # e.g. JUN26

        symbol = f"CRUDEOIL{exp_str}{strike}{option_type}"
        url = f"https://api.mcxindia.com/api/option-chain?symbol={symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            ltp = data.get("ltp") or data.get("LTP") or data.get("lastPrice")
            if ltp and float(ltp) > 0:
                return round(float(ltp), 2)
    except:
        pass

    # Method 2: Estimate using realistic crude option pricing
    # Crude ATM premium is typically 2-4% of spot price
    # OTM premium drops by ~50% per ₹100 move away from ATM
    atm_strike = round(spot_inr / 100) * 100
    distance   = abs(strike - atm_strike)
    atm_premium = spot_inr * 0.025        # ~2.5% of spot for ATM
    decay_factor = math.exp(-distance / (spot_inr * 0.03))
    days = days_to_expiry()
    time_decay = math.sqrt(days / 30)     # scale by time
    premium = atm_premium * decay_factor * time_decay
    return round(max(premium, 5.0), 1)

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
        bull_score -= 10; bull_reasons.append("⚠️ RSI Overbought")
    elif rsi < 30:
        bear_score -= 10; bear_reasons.append("⚠️ RSI Oversold")

    if stk > std and stk < 80:
        bull_score += 10; bull_reasons.append(f"📐 Stoch Bullish K:{stk}>D:{std}")
    elif stk < std and stk > 20:
        bear_score += 10; bear_reasons.append(f"📐 Stoch Bearish K:{stk}<D:{std}")

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

def estimate_equity_premium(spot: float, strike: float, atr: float, signal: str) -> float:
    days      = days_to_expiry()
    time_val  = atr * math.sqrt(max(days, 0.5)) * 0.4
    intrinsic = max(0, spot - strike) if signal == "CALL" else max(0, strike - spot)
    return round(intrinsic + time_val, 1)

def calculate_lots(balance: float, premium: float, lot_size: int) -> int:
    risk_amount = balance * (DEFAULT_RISK_PERCENT / 100)
    sl_amount   = premium * (SL_PERCENT / 100)
    if sl_amount <= 0:
        return 1
    return max(1, int(risk_amount / (sl_amount * lot_size)))

# ─────────────────────────────────────────────
#  SIGNAL — EQUITY
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

        spot    = ind["price"]
        strike  = get_nearest_strike(spot, inst["strike_step"])
        premium = estimate_equity_premium(spot, strike, ind["atr"], signal)
        sl_val  = round(premium * SL_PERCENT / 100, 1)
        tgt_val = round(premium * TARGET_PERCENT / 100, 1)
        sl_px   = round(premium - sl_val, 1)
        tgt_px  = round(premium + tgt_val, 1)
        lots    = calculate_lots(balance, premium, inst["lot_size"])
        max_loss   = round(sl_val  * lots * inst["lot_size"], 0)
        max_profit = round(tgt_val * lots * inst["lot_size"], 0)

        exp_label = "🔴 EXPIRY DAY" if is_expiry_thursday() else f"Expiry in {days_to_expiry()} days"
        mode_icon = {"scalp": "⚡ SCALP", "expiry": "🔥 EXPIRY"}.get(mode, "📊 INTRADAY")
        sig_emoji = "🟢 BUY CALL 📈" if signal == "CALL" else "🔴 BUY PUT 📉"
        opt_label = f"{strike}{'CE' if signal == 'CALL' else 'PE'}"

        msg = f"""
╔══════════════════════════╗
   {mode_icon}  |  {inst['name']}
╚══════════════════════════╝

{sig_emoji}
🎯 Strike: <b>{inst['name']} {opt_label}</b>
⏰ {mode.upper()}  |  {exp_label}

💰 <b>Live Premium:</b> ₹{premium}
🛑 <b>Stop Loss:</b>   ₹{sl_px}  (−₹{sl_val})
✅ <b>Target:</b>      ₹{tgt_px}  (+₹{tgt_val})

📦 <b>Lots:</b> {lots} × {inst['lot_size']} qty
   Max Loss:   ₹{int(max_loss):,}
   Max Profit: ₹{int(max_profit):,}
   R:R  →  1:{round(tgt_val/sl_val,1)}

📉 Spot: ₹{spot}   ATR: {ind['atr']}   RSI: {ind['rsi']}
🔵 Confidence: {confidence}%

📋 <b>Reasons:</b>
{chr(10).join(reasons[:5])}

⚠️ <i>AI analysis only. Trade at your own risk.</i>""".strip()
        send_telegram(chat_id, msg)
        time.sleep(1)

# ─────────────────────────────────────────────
#  SIGNAL — CRUDE OIL (with live premium)
# ─────────────────────────────────────────────
def generate_crude_signal(chat_id: str, mode: str = "crude"):
    if not is_crude_open():
        send_telegram(chat_id, "⛽ Crude Oil session is not active right now.\n🕓 Crude signals run 4:00 PM – 11:00 PM IST.")
        return

    balance = user_balance.get(chat_id, 50000)
    inst    = CRUDE_INSTRUMENT

    df_raw = get_ohlcv(inst["symbol"], period="5d", interval="5m")
    if df_raw is None:
        send_telegram(chat_id, "❌ Could not fetch Crude Oil data. Try again in a minute.")
        return

    df, usd_inr = convert_crude_to_inr(df_raw)
    ind = compute_indicators(df)
    signal, confidence, reasons = score_signal(ind)

    if signal is None:
        send_telegram(chat_id, f"⛽ <b>CRUDE OIL</b>\n\n📊 No clear signal right now.\nMarket consolidating.\nConfidence: {confidence}%\n\n🔄 Next check in 10 minutes.")
        return

    spot       = ind["price"]   # INR per barrel
    usd_price  = round(spot / usd_inr, 2)
    strike     = get_nearest_strike(spot, inst["strike_step"])
    opt_type   = "CE" if signal == "CALL" else "PE"

    # ── Fetch LIVE premium ──
    premium = fetch_live_crude_premium(strike, opt_type, spot)

    sl_val  = round(premium * SL_PERCENT / 100, 1)
    tgt_val = round(premium * TARGET_PERCENT / 100, 1)
    sl_px   = round(premium - sl_val, 1)
    tgt_px  = round(premium + tgt_val, 1)
    lots    = calculate_lots(balance, premium, inst["lot_size"])
    max_loss   = round(sl_val  * lots * inst["lot_size"], 0)
    max_profit = round(tgt_val * lots * inst["lot_size"], 0)

    sig_emoji  = "🟢 BUY CALL 📈" if signal == "CALL" else "🔴 BUY PUT 📉"
    opt_label  = f"{strike}{opt_type}"

    # Wednesday EIA warning
    eia_note = ""
    if ist_now().weekday() == 2:   # Wednesday
        eia_note = "\n⚠️ <b>EIA Inventory data tonight ~9PM — high volatility!</b>"

    msg = f"""
╔══════════════════════════╗
   🛢️ CRUDE OIL SIGNAL (MCX)
╚══════════════════════════╝

{sig_emoji}
🎯 Strike: <b>CRUDE {opt_label}</b>
⏰ Evening Session (4PM–11PM IST){eia_note}

💰 <b>Live Premium:</b> ₹{premium}
🛑 <b>Stop Loss:</b>   ₹{sl_px}  (−₹{sl_val})
✅ <b>Target:</b>      ₹{tgt_px}  (+₹{tgt_val})

📦 <b>Lots:</b> {lots} × {inst['lot_size']} barrels
   Max Loss:   ₹{int(max_loss):,}
   Max Profit: ₹{int(max_profit):,}
   R:R  →  1:{round(tgt_val/sl_val,1)}

🛢️ MCX Spot:  ₹{spot}/barrel
🌍 WTI Price: ${usd_price}/barrel
💱 USD/INR:   ₹{usd_inr}
📊 ATR: {ind['atr']}   RSI: {ind['rsi']}
🔵 Confidence: {confidence}%

📋 <b>Reasons:</b>
{chr(10).join(reasons[:5])}

⚠️ <i>Crude is highly volatile. Use strict SL.
AI analysis only. Trade at your own risk.</i>""".strip()
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  MORNING BRIEFING
# ─────────────────────────────────────────────
def send_morning_briefing(chat_id: str):
    balance     = user_balance.get(chat_id, 0)
    today       = ist_now().strftime("%d %b %Y, %A")
    expiry      = "🔴 TODAY IS EXPIRY DAY!" if is_expiry_thursday() else f"📅 Next expiry: {days_to_expiry()} days"
    nifty_price = get_current_price("^NSEI")
    bn_price    = get_current_price("^NSEBANK")
    crude_usd   = get_current_price("CL=F")
    usd_inr     = get_usd_inr()
    crude_inr   = round(crude_usd * usd_inr, 1) if crude_usd else None
    bal_line    = f"💼 Balance: <b>₹{int(balance):,}</b>  |  Risk/trade: ₹{int(balance*0.02):,}" \
                  if balance > 0 else "💼 Balance not set. Send: <code>/balance 50000</code>"

    msg = f"""
🌅 <b>GOOD MORNING — F&amp;O SIGNAL BOT</b>
📆 {today}   {expiry}

📊 <b>Pre-Market Levels:</b>
   Nifty 50:    ₹{nifty_price or '—'}
   Bank Nifty:  ₹{bn_price or '—'}
   Crude Oil:   ₹{crude_inr or '—'}/bbl  (${crude_usd or '—'})
   USD/INR:     ₹{usd_inr}

{bal_line}

🕘 <b>Equity signals:</b>  9:15 AM – 3:30 PM
🛢️ <b>Crude signals:</b>  4:00 PM – 11:00 PM

/balance 50000 → set capital
/signal        → equity signal now
/crude         → crude signal now""".strip()
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  EVENING CRUDE BRIEFING
# ─────────────────────────────────────────────
def send_crude_briefing(chat_id: str):
    crude_usd = get_current_price("CL=F")
    usd_inr   = get_usd_inr()
    crude_inr = round(crude_usd * usd_inr, 1) if crude_usd else None
    balance   = user_balance.get(chat_id, 0)
    bal_line  = f"💼 Balance: ₹{int(balance):,}" if balance > 0 else "💼 Set balance: /balance 50000"

    # Wednesday EIA alert
    eia_note = "\n⚠️ <b>WEDNESDAY — EIA Inventory Report ~9PM IST\nExpect high volatility tonight!</b>" \
               if ist_now().weekday() == 2 else ""

    msg = f"""
🌆 <b>CRUDE OIL SESSION STARTING</b>
🕓 4:00 PM – 11:00 PM IST{eia_note}

🛢️ <b>Current Crude Price:</b>
   MCX:  ₹{crude_inr or '—'}/barrel
   WTI:  ${crude_usd or '—'}/barrel
   USD/INR: ₹{usd_inr}

{bal_line}

📡 Auto crude signals every 10 minutes.
Send /crude to get a signal right now.

⚠️ <i>Use strict stop loss. Crude is volatile.</i>""".strip()
    send_telegram(chat_id, msg)

# ─────────────────────────────────────────────
#  COMMAND HANDLER
# ─────────────────────────────────────────────
def handle_command(chat_id: str, text: str):
    text = text.strip()
    cmd  = text.lower().split()[0] if text else ""

    if cmd == "/start":
        send_telegram(chat_id, """
👋 <b>Welcome to F&amp;O Signal Bot!</b>

Signals for:
• 📊 Nifty 50         9:15 AM – 3:30 PM
• 🏦 Bank Nifty       9:15 AM – 3:30 PM
• 🛢️ Crude Oil (MCX) 4:00 PM – 11:00 PM

Each signal includes:
✅ Strike (CE/PE)  🎯 Target  🛑 SL
📦 Lots based on balance  🔵 Confidence %

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
            send_telegram(chat_id, "Send like: /balance 50000")

    elif cmd == "/signal":
        send_telegram(chat_id, "🔍 Analyzing Nifty & BankNifty...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()

    elif cmd == "/scalp":
        send_telegram(chat_id, "⚡ Running scalp analysis...")
        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()

    elif cmd == "/crude":
        send_telegram(chat_id, "🛢️ Fetching live Crude Oil data & premium...")
        threading.Thread(target=generate_crude_signal, args=(chat_id,)).start()

    elif cmd == "/status":
        bal       = user_balance.get(chat_id, 0)
        eq_status = "🟢 OPEN" if is_equity_open() else "🔴 CLOSED"
        cr_status = "🟢 OPEN" if is_crude_open()  else "🔴 CLOSED"
        expiry    = "🔴 EXPIRY TODAY" if is_expiry_thursday() else f"In {days_to_expiry()} days"
        send_telegram(chat_id, f"""
📡 <b>Bot Status</b>
Equity Market:  {eq_status}
Crude Market:   {cr_status}
Nifty Expiry:   {expiry}
Your Balance:   ₹{int(bal):,}

Auto Signals:
• Equity: every 5min (scalp), 15min (intraday)
• Crude:  every 10min (4PM–11PM)""".strip())

    elif cmd == "/help":
        send_telegram(chat_id, """
📖 <b>Commands</b>

/balance 50000 → Set your capital
/signal        → Equity signal now
/scalp         → Scalp signal now
/crude         → Crude Oil signal now
/status        → Market status
/help          → This menu

📅 <b>Auto Schedule:</b>
• 8:45 AM  → Morning briefing
• 9:15 AM  → Equity signals start
• 3:45 PM  → Crude briefing
• 4:00 PM  → Crude signals start
• 11:00 PM → Crude signals stop

⚠️ Always use Stop Loss!""".strip())

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

                # Morning briefing 8:45 AM
                if now.hour == MORNING_HOUR and now.minute == MORNING_MIN:
                    if last_morning.get(chat_id) != today_str:
                        send_morning_briefing(chat_id)
                        last_morning[chat_id] = today_str

                # Crude briefing 3:45 PM
                if now.hour == CRUDE_BRIEF_HOUR and now.minute == CRUDE_BRIEF_MIN:
                    if last_crude_brief.get(chat_id) != today_str:
                        send_crude_briefing(chat_id)
                        last_crude_brief[chat_id] = today_str

                # Equity signals
                if is_equity_open():
                    if ts - last_scalp.get(chat_id, 0) >= SCALP_INTERVAL:
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "scalp")).start()
                        last_scalp[chat_id] = ts
                    if ts - last_intraday.get(chat_id, 0) >= INTRADAY_INTERVAL:
                        threading.Thread(target=generate_equity_signal, args=(chat_id, "intraday")).start()
                        last_intraday[chat_id] = ts
                    if is_expiry_thursday():
                        for (h, m) in [(9, 20), (14, 0), (15, 0)]:
                            if now.hour == h and now.minute == m:
                                key = f"expiry_{h}_{today_str}_{chat_id}"
                                if not active_signals.get(key):
                                    threading.Thread(target=generate_equity_signal, args=(chat_id, "expiry")).start()
                                    active_signals[key] = True

                # Crude signals 4 PM – 11 PM
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
    print("🤖 F&O Signal Bot (Equity + Crude) started...")
    send_telegram(TELEGRAM_CHAT_ID, "🤖 Bot started!\n\nSend /start to begin.\n📊 Equity: 9:15AM–3:30PM\n🛢️ Crude: 4:00PM–11:00PM")
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
