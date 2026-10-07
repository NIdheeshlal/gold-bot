"""
Gold MACD Zero Cross -> Telegram alert bot
Rules (same as the TradingView indicator "Gold MACD Zero Cross [15m]"):
  - XAU/USD 15 minute candles, MACD (12, 26, 9)
  - BUY  when the MACD line closes above 0
  - SELL when the MACD line closes below 0
  - Stop = 1 x ATR(14), Target = 2 x ATR(14), spread $0.50 added to entry
  - One trade at a time (new signals are ignored while a trade is open)
Runs every 15 minutes (GitHub Actions). Keeps its memory in state.json.
Not financial advice. Demo account first.
"""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

TD_KEY = os.environ.get("TD_API_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
SPREAD = float(os.environ.get("SPREAD", "0.50"))
SL_MULT, TP_MULT = 1.0, 2.0
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DUBAI = timezone(timedelta(hours=4))


def http_get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_bars():
    q = urllib.parse.urlencode({"symbol": "XAU/USD", "interval": "15min", "outputsize": 400,
                                "timezone": "UTC", "order": "ASC", "apikey": TD_KEY})
    data = http_get("https://api.twelvedata.com/time_series?" + q)
    if data.get("status") != "ok":
        raise RuntimeError(f"Twelve Data error: {data}")
    bars = []
    for v in data["values"]:
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        bars.append(dict(t=t, o=float(v["open"]), h=float(v["high"]), l=float(v["low"]), c=float(v["close"])))
    bars = [b for b in bars if b["h"] - b["l"] > 0.3]          # drop dead weekend quotes
    now = datetime.now(timezone.utc)
    return [b for b in bars if b["t"] + timedelta(minutes=15) <= now]   # completed candles only


def ema(vals, n):
    a, out = 2 / (n + 1), []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else a * v + (1 - a) * out[-1])
    return out


def rma(vals, n):  # Wilder smoothing, same as TradingView ta.atr
    out = []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else (v + (n - 1) * out[-1]) / n)
    return out


def indicators(bars):
    c = [b["c"] for b in bars]
    macd = [f - s for f, s in zip(ema(c, 12), ema(c, 26))]
    tr = [bars[0]["h"] - bars[0]["l"]] + [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
                                         for p, b in zip(bars, bars[1:])]
    return macd, rma(tr, 14)


def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[telegram not configured]\n" + text); return
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=30)


def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"last_bar": None, "trade": None}


def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2)


def dubai(t): return t.astimezone(DUBAI).strftime("%d %b %H:%M")


def main():
    st = load_state()
    bars = fetch_bars()
    if len(bars) < 60:
        print("not enough data"); return
    macd, atr = indicators(bars)
    last_seen = datetime.fromisoformat(st["last_bar"]) if st.get("last_bar") else bars[-2]["t"]
    new_idx = [i for i, b in enumerate(bars) if b["t"] > last_seen and i >= 30]
    for i in new_idx:
        b = bars[i]
        tr = st.get("trade")
        # 1) manage open trade (SL first if both hit in one candle, like the indicator)
        if tr:
            s = tr["side"]
            hit_sl = b["l"] <= tr["sl"] if s == 1 else b["h"] >= tr["sl"]
            hit_tp = b["h"] >= tr["tp"] if s == 1 else b["l"] <= tr["tp"]
            if hit_sl or hit_tp:
                res = -abs(tr["entry"] - tr["sl"]) if hit_sl else abs(tr["tp"] - tr["entry"])
                send(("❌ SL hit" if hit_sl else "✅ TP hit") + f"  ({'BUY' if s == 1 else 'SELL'} from {tr['entry']:.2f})\n"
                     f"Result: {'+' if res > 0 else '-'}${abs(res):.2f} per 1 oz\nTime: {dubai(b['t'] + timedelta(minutes=15))} Dubai")
                st["trade"] = None
                tr = None
        # 2) new signal on this candle close
        if tr is None:
            up = macd[i - 1] <= 0 < macd[i]
            dn = macd[i - 1] >= 0 > macd[i]
            if up or dn:
                s = 1 if up else -1
                entry = b["c"] + s * SPREAD
                sl, tp = entry - s * SL_MULT * atr[i], entry + s * TP_MULT * atr[i]
                st["trade"] = dict(side=s, entry=round(entry, 2), sl=round(sl, 2), tp=round(tp, 2), t=b["t"].isoformat())
                send(("🟢 GOLD BUY" if s == 1 else "🔴 GOLD SELL") + "  (15m MACD zero cross)\n"
                     f"Candle closed: {dubai(b['t'] + timedelta(minutes=15))} Dubai\n"
                     f"Entry ≈ {entry:.2f}\nSL: {sl:.2f}  (-${SL_MULT * atr[i]:.2f})\nTP: {tp:.2f}  (+${TP_MULT * atr[i]:.2f})\n"
                     f"MACD: {macd[i]:.2f}\nCheck your chart, then trade on DEMO. Not financial advice.")
        st["last_bar"] = b["t"].isoformat()
    if not new_idx:
        st["last_bar"] = bars[-1]["t"].isoformat()
    save_state(st)
    print(f"ok  last candle {dubai(bars[-1]['t'])} Dubai  MACD {macd[-1]:.2f}  ATR {atr[-1]:.2f}  open trade: {st['trade']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        send("✅ Gold bot connected. You will get BUY / SELL alerts here.")
    else:
        main()
