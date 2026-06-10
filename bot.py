"""
Nifty/Sensex F&O Signal Bot
Sends BUY CALL / BUY PUT signals with Target & SL to Telegram
Based on your balance, intraday + scalping + expiry strategies
"""

import os
import json
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
#  CONFIG  (edit these)
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8969876449:AAFr2ytAq_KKUTcaKo5Rf1X-f_fAeLYyEnQ")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "877753785")

# Market hours (IST)
MARKET_OPEN   = datetime.time(9, 15)
MARKET_CLOSE  = datetime.time(15, 30)

# How often to run analysis (seconds)
SCALP_INTERVAL    = 300   # 5 min  – scalping signals
INTRADAY_INTERVAL = 900   # 15 min – intraday signals
MORNING_HOUR      = 8
MORNING_MIN       = 45

# Risk Management defaults (overridden by user balance)
DEFAULT_RISK_PERCENT = 2.0   # Risk 2% of capital per trade
SL_PERCENT           = 25    # Stop Loss: 25% of option premium
TARGET_PERCENT       = 50    # Target:    50% of option premium (2:1 R:R via underlying)

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
user_balance   = {}   # chat_id -> float
active_signals = {}   # track sent signals

# ─────────────────────────────────────────────
#  TELEGRAM HELPERS
# ─────────────────────────────────────────────
def send_telegram(chat_id: str, msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram send error: {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 30, "offset": offset}
    try:
        r = requests.get(url, params=params, timeout=35)
        return r.json()
    except Exception as e:
        print(f"Get updates error: {e}")
        return {}

# ─────────────────────────────────────────────
#  MARKET DATA
# ─────────────────────────────────────────────
def get_nifty_data(period="1d", interval="5m"):
    """Fetch Nifty 50 OHLCV data via yfinance"""
    try:
        ticker = yf.Ticker("^NSEI")
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            return None
        df.index = df.index.tz_convert("Asia/Kolkata")
        return df
    except Exception as e:
        print(f"Data fetch error (Nifty): {e}")
        return None

def get_banknifty_data(period="1d", interval="5m"):
    """Fetch BankNifty OHLCV data"""
    try:
        ticker = yf.Ticker("^NSEBANK")
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            return None
        df.index = df.index.tz_convert("Asia/Kolkata")
        return df
    except Exception as e:
        print(f"Data fetch error (BankNifty): {e}")
        return None

def get_current_price(symbol="^NSEI"):
    try:
        t = yf.Ticker(symbol)
        data = t.history(period="1d", interval="1m")
        if not data.empty:
            return round(float(data["Close"].iloc[-1]), 2)
    except:
        pass
    return None

def is_market_open():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    t   = now.time()
    wd  = now.weekday()
    return wd < 5 and MARKET_OPEN <= t <= MARKET_CLOSE

def is_expiry_thursday():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    return now.weekday() == 3   # Thursday

# ─────────────────────────────────────────────
#  TECHNICAL ANALYSIS ENGINE
# ─────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    # Trend
    ema9  = EMAIndicator(close, window=9).ema_indicator()
    ema21 = EMAIndicator(close, window=21).ema_indicator()
    ema50 = EMAIndicator(close, window=50).ema_indicator()
    macd_obj = MACD(close)
    macd_line = macd_obj.macd()
    macd_sig  = macd_obj.macd_signal()
    macd_hist = macd_obj.macd_diff()

    # Momentum
    rsi   = RSIIndicator(close, window=14).rsi()
    stoch = StochasticOscillator(high, low, close, window=14)
    stoch_k = stoch.stoch()
    stoch_d = stoch.stoch_signal()

    # Volatility
    bb    = BollingerBands(close, window=20)
    bb_h  = bb.bollinger_hband()
    bb_l  = bb.bollinger_lband()
    bb_m  = bb.bollinger_mavg()
    atr   = AverageTrueRange(high, low, close, window=14).average_true_range()

    # Volume surge
    vol_avg = vol.rolling(20).mean()
    vol_ratio = vol.iloc[-1] / vol_avg.iloc[-1] if vol_avg.iloc[-1] > 0 else 1

    return {
        "price":      round(close.iloc[-1], 2),
        "ema9":       round(ema9.iloc[-1], 2),
        "ema21":      round(ema21.iloc[-1], 2),
        "ema50":      round(ema50.iloc[-1], 2),
        "macd":       round(macd_line.iloc[-1], 4),
        "macd_sig":   round(macd_sig.iloc[-1], 4),
        "macd_hist":  round(macd_hist.iloc[-1], 4),
        "macd_hist_prev": round(macd_hist.iloc[-2], 4),
        "rsi":        round(rsi.iloc[-1], 2),
        "stoch_k":    round(stoch_k.iloc[-1], 2),
        "stoch_d":    round(stoch_d.iloc[-1], 2),
        "bb_high":    round(bb_h.iloc[-1], 2),
        "bb_low":     round(bb_l.iloc[-1], 2),
        "bb_mid":     round(bb_m.iloc[-1], 2),
        "atr":        round(atr.iloc[-1], 2),
        "vol_ratio":  round(vol_ratio, 2),
        "candle_high": round(high.iloc[-1], 2),
        "candle_low":  round(low.iloc[-1], 2),
        "prev_high":   round(high.iloc[-2], 2),
        "prev_low":    round(low.iloc[-2], 2),
    }

def score_signal(ind: dict) -> tuple:
    """
    Multi-factor scoring system.
    Returns: (signal, score, reasons)
    signal = 'CALL' | 'PUT' | None
    score  = 0-100 (confidence)
    """
    bull_score = 0
    bear_score = 0
    bull_reasons = []
    bear_reasons = []

    p     = ind["price"]
    rsi   = ind["rsi"]
    stk   = ind["stoch_k"]
    std   = ind["stoch_d"]

    # ── EMA Alignment ──
    if ind["ema9"] > ind["ema21"] > ind["ema50"]:
        bull_score += 20
        bull_reasons.append("✅ EMA Stack Bullish (9>21>50)")
    elif ind["ema9"] < ind["ema21"] < ind["ema50"]:
        bear_score += 20
        bear_reasons.append("✅ EMA Stack Bearish (9<21<50)")

    # ── Price vs EMA ──
    if p > ind["ema21"]:
        bull_score += 10
        bull_reasons.append("📈 Price above EMA21")
    else:
        bear_score += 10
        bear_reasons.append("📉 Price below EMA21")

    # ── MACD Crossover ──
    if ind["macd_hist"] > 0 and ind["macd_hist_prev"] <= 0:
        bull_score += 25
        bull_reasons.append("🔀 MACD Bullish Crossover")
    elif ind["macd_hist"] < 0 and ind["macd_hist_prev"] >= 0:
        bear_score += 25
        bear_reasons.append("🔀 MACD Bearish Crossover")
    elif ind["macd"] > ind["macd_sig"]:
        bull_score += 10
        bull_reasons.append("📊 MACD above Signal line")
    else:
        bear_score += 10
        bear_reasons.append("📊 MACD below Signal line")

    # ── RSI ──
    if 40 < rsi < 60:
        pass  # neutral
    elif rsi > 60:
        bull_score += 15
        bull_reasons.append(f"⚡ RSI Strong: {rsi}")
    elif rsi < 40:
        bear_score += 15
        bear_reasons.append(f"⚡ RSI Weak: {rsi}")

    if rsi > 70:
        bull_score -= 10   # overbought warning
        bull_reasons.append("⚠️ RSI Overbought – reduced confidence")
    elif rsi < 30:
        bear_score -= 10
        bear_reasons.append("⚠️ RSI Oversold – reduced confidence")

    # ── Stochastic ──
    if stk > std and stk < 80:
        bull_score += 10
        bull_reasons.append(f"📐 Stoch Bullish K:{stk} > D:{std}")
    elif stk < std and stk > 20:
        bear_score += 10
        bear_reasons.append(f"📐 Stoch Bearish K:{stk} < D:{std}")

    # ── Bollinger Band ──
    if p < ind["bb_low"]:
        bull_score += 15
        bull_reasons.append("🎯 Price at Lower BB – Bounce expected")
    elif p > ind["bb_high"]:
        bear_score += 15
        bear_reasons.append("🎯 Price at Upper BB – Reversal expected")

    # ── Volume Surge ──
    if ind["vol_ratio"] > 1.5:
        if bull_score > bear_score:
            bull_score += 10
            bull_reasons.append(f"📣 Volume Surge {ind['vol_ratio']}x avg")
        else:
            bear_score += 10
            bear_reasons.append(f"📣 Volume Surge {ind['vol_ratio']}x avg")

    # ── ATR (volatility filter – avoid choppy) ──
    atr_pct = (ind["atr"] / p) * 100
    if atr_pct < 0.1:
        return None, 0, ["⛔ Low volatility – skip trade"]

    # ── Decision ──
    if bull_score > bear_score and bull_score >= 45:
        return "CALL", min(bull_score, 100), bull_reasons
    elif bear_score > bull_score and bear_score >= 45:
        return "PUT", min(bear_score, 100), bear_reasons
    else:
        return None, max(bull_score, bear_score), []

# ─────────────────────────────────────────────
#  OPTION STRIKE CALCULATION
# ─────────────────────────────────────────────
def get_nearest_strike(price: float, is_nifty=True) -> int:
    """Round to nearest Nifty (50) or BankNifty (100) strike"""
    step = 50 if is_nifty else 100
    return int(round(price / step) * step)

def estimate_option_premium(spot: float, strike: float, atr: float, signal: str) -> float:
    """
    Simple premium estimate using intrinsic + time value.
    For ATM options: premium ≈ 0.5 * ATR * sqrt(days_to_expiry)
    """
    days = days_to_expiry()
    time_val = atr * math.sqrt(max(days, 0.5)) * 0.4
    intrinsic = max(0, spot - strike) if signal == "CALL" else max(0, strike - spot)
    return round(intrinsic + time_val, 1)

def days_to_expiry() -> int:
    """Days to next Thursday expiry"""
    now = datetime.datetime.now()
    days_ahead = (3 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return days_ahead

def calculate_quantity(balance: float, premium: float) -> int:
    """
    Calculate how many lots to buy based on balance.
    Nifty lot = 25, BankNifty lot = 15
    Risk 2% of capital
    """
    risk_amount  = balance * (DEFAULT_RISK_PERCENT / 100)
    sl_amount    = premium * (SL_PERCENT / 100)
    if sl_amount <= 0:
        return 1
    lots = int(risk_amount / (sl_amount * 25))
    return max(1, lots)

# ─────────────────────────────────────────────
#  SIGNAL GENERATOR
# ─────────────────────────────────────────────
def generate_signal(chat_id: str, mode: str = "intraday"):
    """Generate and send a signal to user"""
    if not is_market_open():
        return

    balance = user_balance.get(chat_id, 50000)

    for name, symbol, is_nifty in [("NIFTY 50", "^NSEI", True), ("BANK NIFTY", "^NSEBANK", False)]:
        df = get_nifty_data() if is_nifty else get_banknifty_data()
        if df is None or len(df) < 50:
            continue

        ind    = compute_indicators(df)
        signal, confidence, reasons = score_signal(ind)

        if signal is None:
            continue

        spot   = ind["price"]
        strike = get_nearest_strike(spot, is_nifty)
        atr    = ind["atr"]

        # ATM strike for call/put
        call_strike = strike
        put_strike  = strike

        premium = estimate_option_premium(spot, strike, atr, signal)

        # Target & SL on PREMIUM
        sl_val     = round(premium * (SL_PERCENT / 100), 1)
        target_val = round(premium * (TARGET_PERCENT / 100), 1)
        sl_price   = round(premium - sl_val, 1)
        tgt_price  = round(premium + target_val, 1)

        # Qty based on balance
        lots = calculate_quantity(balance, premium)
        lot_size  = 25 if is_nifty else 15
        max_loss  = round(sl_val * lots * lot_size, 0)
        max_profit = round(target_val * lots * lot_size, 0)

        # Expiry label
        exp_label = "🔴 EXPIRY DAY" if is_expiry_thursday() else f"Expiry in {days_to_expiry()} days"

        # Mode emoji
        mode_icon = "⚡ SCALP" if mode == "scalp" else ("🔥 EXPIRY" if mode == "expiry" else "📊 INTRADAY")

        # Build message
        signal_emoji = "🟢 BUY CALL 📈" if signal == "CALL" else "🔴 BUY PUT 📉"
        strike_label = f"{call_strike}CE" if signal == "CALL" else f"{put_strike}PE"

        msg = f"""
╔══════════════════════════╗
   {mode_icon} SIGNAL  |  {name}
╚══════════════════════════╝

{signal_emoji}
🎯 Strike: <b>{name} {strike_label}</b>
⏰ Mode: {mode.upper()}  |  {exp_label}

💰 <b>Est. Premium:</b> ₹{premium}
🛑 <b>Stop Loss:</b>   ₹{sl_price}  (−₹{sl_val})
✅ <b>Target:</b>      ₹{tgt_price}  (+₹{target_val})

📦 <b>Lots:</b> {lots} lot(s) × {lot_size} qty
   Max Loss:   ₹{int(max_loss):,}
   Max Profit: ₹{int(max_profit):,}
   Risk/Reward: 1:{round(target_val/sl_val,1)}

📉 Spot: ₹{spot}   ATR: {atr}
🔵 Confidence: {confidence}%

📋 <b>Reasons:</b>
{chr(10).join(reasons[:5])}

⚠️ <i>This is AI-generated analysis only.
Trade at your own risk. F&amp;O involves
substantial risk of capital loss.</i>
"""
        send_telegram(chat_id, msg.strip())
        time.sleep(1)

# ─────────────────────────────────────────────
#  MORNING BRIEFING
# ─────────────────────────────────────────────
def send_morning_briefing(chat_id: str):
    balance = user_balance.get(chat_id, 0)
    today   = datetime.datetime.now().strftime("%d %b %Y, %A")
    expiry  = "🔴 TODAY IS EXPIRY DAY!" if is_expiry_thursday() else f"📅 Next expiry: {days_to_expiry()} days"

    nifty_price = get_current_price("^NSEI")
    bn_price    = get_current_price("^NSEBANK")

    bal_line = f"💼 Your Balance: <b>₹{int(balance):,}</b>" if balance > 0 else \
               "💼 Balance not set.\nSend: <code>BALANCE 50000</code>"

    risk_amt = round(balance * DEFAULT_RISK_PERCENT / 100) if balance > 0 else "-"

    msg = f"""
🌅 <b>GOOD MORNING — F&amp;O SIGNAL BOT</b>
📆 {today}

{expiry}

📊 <b>Pre-Market Levels:</b>
   Nifty 50:    ₹{nifty_price or 'fetching...'}
   Bank Nifty:  ₹{bn_price or 'fetching...'}

{bal_line}
⚠️  Risk per trade: ₹{risk_amt} ({DEFAULT_RISK_PERCENT}%)

🕘 Market opens at 9:15 AM
📡 Signals will start automatically.

<b>Commands:</b>
/balance 50000  → set your capital
/signal         → get signal now
/status         → bot status
/help           → all commands
"""
    send_telegram(chat_id, msg.strip())

# ─────────────────────────────────────────────
#  COMMAND HANDLER
# ─────────────────────────────────────────────
def handle_command(chat_id: str, text: str):
    text = text.strip()

    if text.lower() == "/start":
        send_telegram(chat_id, """
👋 <b>Welcome to Nifty F&O Signal Bot!</b>

I will send you BUY CALL / BUY PUT signals for:
• 📊 Nifty 50
• 🏦 Bank Nifty

Signals include:
✅ Which option to buy (strike + CE/PE)
🎯 Target price
🛑 Stop Loss
📦 How many lots (based on your balance)
📈 Confidence score with reasons

<b>To get started:</b>
Send your trading capital:
<code>/balance 50000</code>

Then signals will auto-start at 9:15 AM every day!
""".strip())

    elif text.lower().startswith("/balance") or text.lower().startswith("balance"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                bal = float(parts[1].replace(",", ""))
                user_balance[chat_id] = bal
                send_telegram(chat_id, f"✅ Balance set to ₹{int(bal):,}\n\nRisk per trade (2%): ₹{int(bal*0.02):,}\nSignals will be sized accordingly.")
            except:
                send_telegram(chat_id, "❌ Invalid amount. Example: /balance 50000")
        else:
            send_telegram(chat_id, "Send your balance like: /balance 50000")

    elif text.lower() == "/signal":
        send_telegram(chat_id, "🔍 Analyzing market... please wait.")
        threading.Thread(target=generate_signal, args=(chat_id, "intraday")).start()

    elif text.lower() == "/scalp":
        send_telegram(chat_id, "⚡ Running scalp analysis...")
        threading.Thread(target=generate_signal, args=(chat_id, "scalp")).start()

    elif text.lower() == "/status":
        bal = user_balance.get(chat_id, 0)
        market = "🟢 OPEN" if is_market_open() else "🔴 CLOSED"
        expiry = "🔴 EXPIRY TODAY" if is_expiry_thursday() else f"In {days_to_expiry()} days"
        send_telegram(chat_id, f"""
📡 <b>Bot Status</b>
Market: {market}
Expiry: {expiry}
Your Balance: ₹{int(bal):,}
Auto Signals: Every 5 min (scalp), 15 min (intraday)
""".strip())

    elif text.lower() == "/help":
        send_telegram(chat_id, """
📖 <b>Commands</b>

/balance 50000  → Set your capital
/signal         → Get intraday signal now
/scalp          → Get scalp signal now
/status         → Check bot status
/help           → This menu

💡 <b>Every morning at 8:45 AM</b> you'll get a briefing.
📡 <b>Auto signals</b> start at 9:15 AM.

⚠️ Always use Stop Loss. Never risk more than 2% per trade.
""".strip())

# ─────────────────────────────────────────────
#  SCHEDULER  (runs in background thread)
# ─────────────────────────────────────────────
def scheduler_loop():
    last_scalp    = {}
    last_intraday = {}
    last_morning  = {}

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
            today_str = now.strftime("%Y-%m-%d")

            # Morning briefing
            for chat_id in list(user_balance.keys()):
                if now.hour == MORNING_HOUR and now.minute == MORNING_MIN:
                    if last_morning.get(chat_id) != today_str:
                        send_morning_briefing(chat_id)
                        last_morning[chat_id] = today_str

            if is_market_open():
                ts = now.timestamp()

                for chat_id in list(user_balance.keys()):
                    # Scalp signals every 5 min
                    if ts - last_scalp.get(chat_id, 0) >= SCALP_INTERVAL:
                        threading.Thread(target=generate_signal, args=(chat_id, "scalp")).start()
                        last_scalp[chat_id] = ts

                    # Intraday signals every 15 min
                    if ts - last_intraday.get(chat_id, 0) >= INTRADAY_INTERVAL:
                        threading.Thread(target=generate_signal, args=(chat_id, "intraday")).start()
                        last_intraday[chat_id] = ts

                    # Expiry day special signals at 9:20 and 14:00
                    if is_expiry_thursday():
                        if (now.hour == 9 and now.minute == 20) or \
                           (now.hour == 14 and now.minute == 0):
                            mode_key = f"expiry_{now.hour}_{today_str}"
                            if active_signals.get(f"{chat_id}_{mode_key}") != True:
                                threading.Thread(target=generate_signal, args=(chat_id, "expiry")).start()
                                active_signals[f"{chat_id}_{mode_key}"] = True

        except Exception as e:
            print(f"Scheduler error: {e}")

        time.sleep(60)

# ─────────────────────────────────────────────
#  MAIN POLLING LOOP
# ─────────────────────────────────────────────
def main():
    print("🤖 Nifty F&O Signal Bot started...")
    send_telegram(TELEGRAM_CHAT_ID, "🤖 Bot started! Send /start to begin.")

    # Start scheduler in background
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

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
