import os
import time
import threading
import requests
import pandas as pd
from datetime import datetime
from flask import Flask, render_template_string, jsonify

# ============================================================
# CONFIGURACION
# ============================================================
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
LIMIT = 500

# Parametros iguales al Pine Script AlgoAlpha
ZL_LENGTH = 70
BAND_MULT = 1.2

MTF_TIMEFRAMES = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1D": "1d",
}

# En Render usa Environment Variables:
# TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6001125969")

CHECK_EVERY_SECONDS = 60
last_sent_signal = None

app = Flask(__name__)

data_actual = {
    "symbol": SYMBOL,
    "interval": INTERVAL,
    "source": "Binance",
    "price": "-",
    "signal": "WAIT",
    "raw_signal": "WAIT",
    "trend": "NEUTRAL",
    "trend_value": 0,
    "zlema": "-",
    "upper": "-",
    "lower": "-",
    "volatility": "-",
    "entry_type": "NONE",
    "event_time": None,
    "entry": "-",
    "sl": "-",
    "tp1": "-",
    "tp2": "-",
    "tp3": "-",
    "score": 50,
    "updated": "-",
    "error": "",
    "prices": [],
    "zlema_series": [],
    "upper_series": [],
    "lower_series": [],
    "mtf": {"5m": "WAIT", "15m": "WAIT", "1h": "WAIT", "4h": "WAIT", "1D": "WAIT"},
}


def safe_request_json(url, params=None, timeout=15, retries=4, sleep_seconds=2):
    last_error = None
    for _ in range(retries):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_error = e
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Request failed after retries: {last_error}")


def get_klines(symbol=SYMBOL, interval=INTERVAL, limit=LIMIT):
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    # Render estaba recibiendo error 451 usando Binance Spot.
    # Esta version usa SOLO Binance Futures USDT-M.
    base_urls = [
        "https://fapi.binance.com"
    ]

    last_error = None
    raw = None

    for base in base_urls:
        try:
            url = f"{base}/fapi/v1/klines"
            raw = safe_request_json(url, params=params)
            break
        except Exception as e:
            last_error = e
            print(f"Error Binance Futures endpoint {base}:", e)

    if raw is None:
        raise RuntimeError(f"No se pudo obtener data de Binance Futures: {last_error}")

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df

def atr(df, length):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # TradingView ta.atr usa RMA/Wilder.
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def calc_zero_lag(df):
    df = df.copy()
    length = ZL_LENGTH
    lag = int((length - 1) / 2)

    src = df["close"]
    adjusted_src = src + (src - src.shift(lag))

    # zlema = ta.ema(src + (src - src[lag]), length)
    df["zlema"] = adjusted_src.ewm(span=length, adjust=False).mean()

    # volatility = highest(atr(length), length*3) * mult
    df["atr"] = atr(df, length)
    df["volatility"] = df["atr"].rolling(length * 3).max() * BAND_MULT
    df["upper"] = df["zlema"] + df["volatility"]
    df["lower"] = df["zlema"] - df["volatility"]

    trend = []
    current_trend = 0

    for i in range(len(df)):
        if i == 0 or pd.isna(df["zlema"].iloc[i]) or pd.isna(df["volatility"].iloc[i]):
            trend.append(current_trend)
            continue

        prev_close = df["close"].iloc[i - 1]
        now_close = df["close"].iloc[i]
        prev_upper = df["upper"].iloc[i - 1]
        now_upper = df["upper"].iloc[i]
        prev_lower = df["lower"].iloc[i - 1]
        now_lower = df["lower"].iloc[i]

        # if ta.crossover(close, zlema + volatility) trend := 1
        if prev_close <= prev_upper and now_close > now_upper:
            current_trend = 1

        # if ta.crossunder(close, zlema - volatility) trend := -1
        if prev_close >= prev_lower and now_close < now_lower:
            current_trend = -1

        trend.append(current_trend)

    df["trend"] = trend

    # Flechas grandes
    df["bullish_trend_signal"] = (df["trend"].shift(1) <= 0) & (df["trend"] > 0)
    df["bearish_trend_signal"] = (df["trend"].shift(1) >= 0) & (df["trend"] < 0)

    # Flechas pequenas de entrada
    df["bullish_entry_signal"] = (
        (df["close"].shift(1) <= df["zlema"].shift(1)) &
        (df["close"] > df["zlema"]) &
        (df["trend"] == 1) &
        (df["trend"].shift(1) == 1)
    )

    df["bearish_entry_signal"] = (
        (df["close"].shift(1) >= df["zlema"].shift(1)) &
        (df["close"] < df["zlema"]) &
        (df["trend"] == -1) &
        (df["trend"].shift(1) == -1)
    )

    return df


def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "PEGA_AQUI_TU_TOKEN_REAL":
        print("Telegram no configurado")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}

    try:
        response = requests.post(url, json=payload, timeout=10)
        print("Telegram:", response.text)
    except Exception as e:
        print("Error Telegram:", e)


def get_mtf_status():
    result = {}
    for label, interval in MTF_TIMEFRAMES.items():
        try:
            df_tf = get_klines(interval=interval, limit=LIMIT)
            df_tf = calc_zero_lag(df_tf)
            trend_val = int(df_tf["trend"].iloc[-1])
            result[label] = "Bullish" if trend_val == 1 else "Bearish" if trend_val == -1 else "WAIT"
        except Exception as e:
            print(f"Error MTF {label}:", e)
            result[label] = "ERROR"
    return result


def analyze():
    df = get_klines()
    df = calc_zero_lag(df)
    last = df.iloc[-1]

    price = float(last["close"])
    zlema = float(last["zlema"]) if pd.notna(last["zlema"]) else price
    upper = float(last["upper"]) if pd.notna(last["upper"]) else price
    lower = float(last["lower"]) if pd.notna(last["lower"]) else price
    volatility = float(last["volatility"]) if pd.notna(last["volatility"]) else 0

    trend_value = int(last["trend"])
    trend = "BULLISH" if trend_value == 1 else "BEARISH" if trend_value == -1 else "NEUTRAL"

    # NUEVA LOGICA: revisa ultimas 5 velas para no perder senales.
    recent = df.tail(5)
    raw_signal = "WAIT"
    entry_type = "NONE"
    event_time = None

    for idx, row in recent.iterrows():
        if bool(row["bullish_trend_signal"]):
            raw_signal = "BUY"
            entry_type = "ZERO LAG TREND BUY"
            event_time = int(row["close_time"])
        elif bool(row["bearish_trend_signal"]):
            raw_signal = "SELL"
            entry_type = "ZERO LAG TREND SELL"
            event_time = int(row["close_time"])
        elif bool(row["bullish_entry_signal"]):
            raw_signal = "BUY"
            entry_type = "ZERO LAG ENTRY BUY"
            event_time = int(row["close_time"])
        elif bool(row["bearish_entry_signal"]):
            raw_signal = "SELL"
            entry_type = "ZERO LAG ENTRY SELL"
            event_time = int(row["close_time"])

    display_signal = raw_signal
    if raw_signal == "WAIT":
        if trend_value == 1:
            display_signal = "BULLISH"
        elif trend_value == -1:
            display_signal = "BEARISH"

    mtf = get_mtf_status()
    bullish_count = sum(1 for x in mtf.values() if x == "Bullish")
    bearish_count = sum(1 for x in mtf.values() if x == "Bearish")

    if trend_value == 1:
        score = 50 + bullish_count * 10
    elif trend_value == -1:
        score = 50 + bearish_count * 10
    else:
        score = 50
    score = max(0, min(100, score))

    entry = price
    if raw_signal == "BUY" or trend_value == 1:
        sl = lower
        risk = max(entry - sl, volatility, price * 0.002)
        tp1 = entry + risk
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3
    elif raw_signal == "SELL" or trend_value == -1:
        sl = upper
        risk = max(sl - entry, volatility, price * 0.002)
        tp1 = entry - risk
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3
    else:
        sl = "-"
        tp1 = "-"
        tp2 = "-"
        tp3 = "-"

    tail = df.tail(80)

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "source": "Binance",
        "price": round(price, 2),
        "signal": display_signal,
        "raw_signal": raw_signal,
        "trend": trend,
        "trend_value": trend_value,
        "zlema": round(zlema, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "volatility": round(volatility, 2),
        "entry_type": entry_type,
        "event_time": event_time,
        "entry": round(entry, 2),
        "sl": round(sl, 2) if sl != "-" else "-",
        "tp1": round(tp1, 2) if tp1 != "-" else "-",
        "tp2": round(tp2, 2) if tp2 != "-" else "-",
        "tp3": round(tp3, 2) if tp3 != "-" else "-",
        "score": score,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": "",
        "prices": [round(x, 2) for x in tail["close"].tolist()],
        "zlema_series": [round(x, 2) if pd.notna(x) else None for x in tail["zlema"].tolist()],
        "upper_series": [round(x, 2) if pd.notna(x) else None for x in tail["upper"].tolist()],
        "lower_series": [round(x, 2) if pd.notna(x) else None for x in tail["lower"].tolist()],
        "mtf": mtf,
    }


def update_signal():
    global data_actual, last_sent_signal

    while True:
        try:
            data_actual = analyze()
            print(
                "Estado:", data_actual["signal"], data_actual["price"],
                "| Evento:", data_actual["entry_type"],
                "| Event time:", data_actual["event_time"]
            )

            current_event = data_actual["raw_signal"]
            if current_event in ["BUY", "SELL"]:
                event_key = f"{current_event}_{data_actual['entry_type']}_{data_actual['event_time']}"

                if event_key != last_sent_signal:
                    mtf_lines = "\n".join([f"{k}: {v}" for k, v in data_actual["mtf"].items()])
                    msg = f"""
🚨 ZERO LAG SIGNAL {data_actual['symbol']}
Fuente: Binance
TF principal: {data_actual['interval']}

Evento: {data_actual['entry_type']}
Tipo: {current_event}

Precio: {data_actual['price']}
Entrada: {data_actual['entry']}
SL: {data_actual['sl']}
TP1: {data_actual['tp1']}
TP2: {data_actual['tp2']}
TP3: {data_actual['tp3']}

Zero Lag: {data_actual['zlema']}
Upper: {data_actual['upper']}
Lower: {data_actual['lower']}
Score MTF: {data_actual['score']}%

MTF:
{mtf_lines}

Fecha/Hora:
{data_actual['updated']}
"""
                    send_telegram(msg)
                    last_sent_signal = event_key

        except Exception as e:
            err = str(e)
            print("Error update_signal:", err)
            data_actual["error"] = err
            data_actual["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(CHECK_EVERY_SECONDS)


HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>BTC Zero Lag Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800;900&display=swap');
:root{--green:#00ffbb;--red:#ff1100;--blue:#38bdf8;--yellow:#facc15;--txt:#eef6ff;--muted:#9fb0c7;--panel:rgba(8,12,24,.76);--stroke:rgba(255,255,255,.15)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:'Plus Jakarta Sans',system-ui,sans-serif;color:var(--txt);background:linear-gradient(180deg,rgba(145,78,112,.92),rgba(48,43,78,.88) 32%,rgba(8,20,35,1) 100%),radial-gradient(circle at 20% 8%,rgba(56,189,248,.25),transparent 28%),radial-gradient(circle at 85% 8%,rgba(168,85,247,.22),transparent 30%);padding:30px}.shell{max-width:1520px;margin:auto;border-radius:32px;padding:24px;background:var(--panel);border:1px solid rgba(255,255,255,.18);box-shadow:0 40px 100px rgba(0,0,0,.55);backdrop-filter:blur(22px)}.top{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:18px}.brand{display:flex;align-items:center;gap:14px}.logo{width:46px;height:46px;border-radius:50%;background:conic-gradient(var(--green),var(--blue),#8b5cf6,#f59e0b,var(--green));box-shadow:0 0 28px rgba(56,189,248,.65)}h1{margin:0;font-size:29px;font-weight:900;letter-spacing:-1px}.status{padding:13px 18px;border-radius:999px;background:rgba(255,255,255,.09);color:#bfdbfe;font-weight:600}.grid{display:grid;grid-template-columns:1fr 1.55fr 1fr;gap:16px}.card{background:linear-gradient(180deg,rgba(255,255,255,.095),rgba(255,255,255,.045));border:1px solid var(--stroke);border-radius:22px;padding:20px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 18px 40px rgba(0,0,0,.28)}.card h2,.card h3{margin:0 0 8px;font-weight:850;letter-spacing:-.4px}.sub{color:var(--muted);font-size:13px}.signalbox{text-align:center;min-height:500px;display:flex;flex-direction:column;justify-content:center}.signal{font-size:76px;line-height:1;font-weight:950;letter-spacing:4px;margin:10px 0 20px}.BUY,.BULLISH{color:var(--green);text-shadow:0 0 35px rgba(0,255,187,.75)}.SELL,.BEARISH{color:var(--red);text-shadow:0 0 35px rgba(255,17,0,.75)}.WAIT,.NEUTRAL{color:var(--yellow);text-shadow:0 0 35px rgba(250,204,21,.55)}.gauge{height:250px;position:relative}.gauge canvas{position:absolute;inset:0}.gcenter{position:absolute;left:0;right:0;bottom:35px;text-align:center}.gcenter b{font-size:56px;font-weight:950}.gcenter small{display:block;color:var(--muted)}.linebox{height:330px}.mtf{margin-top:14px;display:grid;gap:8px}.mtfrow{display:grid;grid-template-columns:80px 1fr;border:1px solid rgba(255,255,255,.13);border-radius:12px;overflow:hidden}.mtfrow span{padding:10px 12px;background:rgba(255,255,255,.05)}.mtfrow b{padding:10px 12px;text-align:center}.mtfrow .Bullish{background:rgba(0,255,187,.25);color:var(--green)}.mtfrow .Bearish{background:rgba(255,17,0,.25);color:var(--red)}.mtfrow .WAIT{background:rgba(250,204,21,.18);color:var(--yellow)}.mtfrow .ERROR{background:rgba(255,255,255,.12);color:#cbd5e1}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}.stat{border-radius:16px;background:rgba(255,255,255,.06);padding:16px;text-align:center}.stat small{display:block;color:var(--muted);font-size:12px}.stat b{display:block;font-size:23px;margin-top:7px}.lower{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:16px}.big{font-size:28px;font-weight:900;margin-top:10px}.row{display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,.09);padding:11px 0}.note{margin-top:14px;padding:13px;border-radius:14px;background:rgba(56,189,248,.12);border:1px solid rgba(56,189,248,.18);color:#dbeafe;font-size:13px;line-height:1.5}@media(max-width:1100px){body{padding:14px}.top,.grid,.lower,.stats{grid-template-columns:1fr;display:grid}.signal{font-size:50px}}
</style>
</head>
<body>
<div class="shell">
  <div class="top"><div class="brand"><div class="logo"></div><div><h1>BTC Zero Lag Signals</h1><div class="sub">Replica Python del Pine Script AlgoAlpha · Binance · {{d.interval}}</div></div></div><div class="status">{{d.source}} · {{d.updated}}</div></div>
  <div class="grid">
    <div class="card"><h2>MTF Trend Table</h2><div class="sub">Mismo concepto de la tabla del indicador TradingView.</div><div class="mtf">{% for tf, val in d.mtf.items() %}<div class="mtfrow"><span>{{tf}}</span><b class="{{val}}">{{val}}</b></div>{% endfor %}</div><div class="note">Evento actual: <b>{{d.entry_type}}</b><br>Event time: <b>{{d.event_time}}</b><br>Trend base: <b class="{{d.trend}}">{{d.trend}}</b></div></div>
    <div class="card signalbox"><div class="sub">Señal actual Zero Lag</div><div class="signal {{d.signal}}">{{d.signal}}</div><div class="gauge"><canvas id="gaugeChart"></canvas><div class="gcenter"><b>{{d.score}}%</b><small>Confluencia MTF</small></div></div><div class="stats"><div class="stat"><small>Precio</small><b>{{d.price}}</b></div><div class="stat"><small>ZLEMA</small><b>{{d.zlema}}</b></div><div class="stat"><small>Volatilidad</small><b>{{d.volatility}}</b></div></div></div>
    <div class="card"><h2>Zero Lag Bands</h2><div class="sub">Close + ZLEMA + bandas de volatilidad</div><div class="linebox"><canvas id="lineChart"></canvas></div></div>
  </div>
  <div class="lower"><div class="card"><h3>Trade Plan</h3><div class="big {{d.signal}}">{{d.entry}}</div><div class="sub">Entrada estimada</div><div class="note">La entrada se genera por evento real Zero Lag, no por score.</div></div><div class="card"><h3>Bandas</h3><div class="row"><span>Upper</span><b>{{d.upper}}</b></div><div class="row"><span>ZLEMA</span><b>{{d.zlema}}</b></div><div class="row"><span>Lower</span><b>{{d.lower}}</b></div></div><div class="card"><h3>Risk / Targets</h3><div class="row"><span>SL</span><b>{{d.sl}}</b></div><div class="row"><span>TP1</span><b>{{d.tp1}}</b></div><div class="row"><span>TP2</span><b>{{d.tp2}}</b></div><div class="row"><span>TP3</span><b>{{d.tp3}}</b></div></div></div>
</div>
<script>
const prices={{ d.prices | tojson }};const zlema={{ d.zlema_series | tojson }};const upper={{ d.upper_series | tojson }};const lower={{ d.lower_series | tojson }};const score={{ d.score }};Chart.defaults.color="#9fb0c7";Chart.defaults.font.family="Plus Jakarta Sans";
new Chart(document.getElementById("lineChart"),{type:"line",data:{labels:prices.map((_,i)=>i+1),datasets:[{label:"Close",data:prices,borderColor:"#38bdf8",backgroundColor:"rgba(56,189,248,.12)",fill:false,tension:.35,pointRadius:0,borderWidth:2},{label:"ZLEMA",data:zlema,borderColor:"#ffffff",fill:false,tension:.35,pointRadius:0,borderWidth:2},{label:"Upper",data:upper,borderColor:"#ff1100",fill:false,tension:.35,pointRadius:0,borderWidth:1},{label:"Lower",data:lower,borderColor:"#00ffbb",fill:false,tension:.35,pointRadius:0,borderWidth:1}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{boxWidth:10}}},scales:{x:{display:false},y:{grid:{color:"rgba(255,255,255,.07)"}}}}});
new Chart(document.getElementById("gaugeChart"),{type:"doughnut",data:{datasets:[{data:[score,100-score],backgroundColor:[score>=70?"#00ffbb":score>=50?"#facc15":"#ff1100","rgba(255,255,255,.08)"],borderWidth:0,circumference:220,rotation:250,cutout:"73%"}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{enabled:false}}}});
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML, d=data_actual)


@app.route("/api")
def api():
    return jsonify(data_actual)


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "updated": data_actual.get("updated"),
        "error": data_actual.get("error", ""),
        "price": data_actual.get("price"),
        "signal": data_actual.get("signal"),
    })


def run_bot():
    while True:
        try:
            update_signal()
        except Exception as e:
            print("ERROR BOT:", e)
            time.sleep(10)


if __name__ == "__main__":
    print("VERSION ZERO LAG FINAL CARGADA")
    t = threading.Thread(target=run_bot)
    t.daemon = True
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
