import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime

# =========================
# CONFIGURACIÓN
# =========================
SYMBOL = "BTCUSDT"
INTERVAL = "15m"       # 5m, 15m, 1h, 4h, 1d
LIMIT = 300

TELEGRAM_BOT_TOKEN = "8613736992:AAGimBmP6SvwOEefCz80kbMpFa11ILcxVF8"
TELEGRAM_CHAT_ID = "6001125969"

CHECK_EVERY_SECONDS = 60

ZLEN = 70
ATR_LEN = 14
ATR_MULT = 1.6
RSI_LEN = 14
RR_MIN = 2.0


# =========================
# DATA BINANCE
# =========================
def get_klines(symbol, interval, limit=300):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    data = requests.get(url, params=params, timeout=10).json()

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)

    return df


# =========================
# INDICADORES
# =========================
def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(df, length=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(length).mean()


def zero_lag_ema(close, length):
    lag = int((length - 1) / 2)
    adjusted = close + (close - close.shift(lag))
    return ema(adjusted, length)


# =========================
# SOPORTE / RESISTENCIA
# =========================
def recent_support_resistance(df, lookback=30):
    recent = df.iloc[-lookback:]
    support = recent["low"].min()
    resistance = recent["high"].max()
    return support, resistance


# =========================
# TELEGRAM
# =========================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    response = requests.post(url, json=payload, timeout=10)

    print("Respuesta Telegram:", response.text)

# =========================
# MOTOR DE SEÑALES
# =========================
def analyze():
    df = get_klines(SYMBOL, INTERVAL, LIMIT)

    df["zlema"] = zero_lag_ema(df["close"], ZLEN)
    df["atr"] = atr(df, ATR_LEN)
    df["rsi"] = rsi(df["close"], RSI_LEN)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = last["close"]
    zlema = last["zlema"]
    atr_val = last["atr"]
    rsi_val = last["rsi"]

    upper_band = zlema + atr_val * ATR_MULT
    lower_band = zlema - atr_val * ATR_MULT

    support, resistance = recent_support_resistance(df)

    bullish = close > upper_band and rsi_val > 50
    bearish = close < lower_band and rsi_val < 50

    near_resistance = close < resistance and ((resistance - close) / close) < 0.006
    near_support = close > support and ((close - support) / close) < 0.006

    buy_signal = bullish and not near_resistance and prev["close"] <= prev["zlema"]
    sell_signal = bearish and not near_support and prev["close"] >= prev["zlema"]

    if buy_signal:
        entry = close
        sl = min(support, close - atr_val * ATR_MULT)
        risk = entry - sl

        tp1 = entry + risk
        tp2 = entry + risk * RR_MIN
        tp3 = entry + risk * 3

        return {
            "type": "BUY",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rsi": rsi_val,
            "reason": "Zero Lag bullish + RSI > 50 + soporte cercano"
        }

    if sell_signal:
        entry = close
        sl = max(resistance, close + atr_val * ATR_MULT)
        risk = sl - entry

        tp1 = entry - risk
        tp2 = entry - risk * RR_MIN
        tp3 = entry - risk * 3

        return {
            "type": "SELL",
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "rsi": rsi_val,
            "reason": "Zero Lag bearish + RSI < 50 + resistencia cercana"
        }

    return None


# =========================
# LOOP PRINCIPAL
# =========================
def main():
    print("Bot iniciado...")
    send_telegram("✅ Bot conectado correctamente")

    last_signal = None

    while True:
        try:
            signal = analyze()

            if signal:
                signal_key = f"{signal['type']}_{round(signal['entry'], 2)}"

                if signal_key != last_signal:
                    msg = f"""
🚨 Señal {SYMBOL}
TF: {INTERVAL}

Tipo: {signal['type']}
Entrada: {signal['entry']:.2f}
SL: {signal['sl']:.2f}

TP1: {signal['tp1']:.2f}
TP2: {signal['tp2']:.2f}
TP3: {signal['tp3']:.2f}

R/R: 1:{RR_MIN}
RSI: {signal['rsi']:.2f}

Confirmación:
{signal['reason']}

Fecha/Hora:
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
                    send_telegram(msg)
                    print(msg)

                    last_signal = signal_key
                else:
                    print("Señal repetida, no enviada.")

            else:
                print("Sin señal...")

        except Exception as e:
            print("Error:", e)

        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()