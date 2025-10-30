from flask import Flask, jsonify, Response, request
import yfinance as yf
from datetime import timezone, datetime
import json
import os
import time
from typing import Tuple, Optional
import logging
logging.basicConfig(level=logging.INFO)


app = Flask(__name__)

PORTFOLIO_FILE = os.path.join(os.path.dirname(__file__), "portfolios.json")

DEMO_PORTFOLIOS = {
    "portfolios": [
        {
            "name": "Long-Term (USD)",
            "currency": "USD",
            "holdings": [
                { "ticker": "QQQ",  "shares": 10,  "avg_cost": 420.0 },
                { "ticker": "NVDA", "shares": 2,   "avg_cost": 950.0 }
            ]
        },
        {
            "name": "TFSA (CAD)",
            "currency": "CAD",
            "holdings": [
                { "ticker": "HHIS.TO", "shares": 100, "avg_cost": 22.10 }
            ]
        }
    ]
}

def load_portfolios():
    try:
        logging.info(f"Looking for portfolios at: {PORTFOLIO_FILE}")
        if not os.path.exists(PORTFOLIO_FILE):
            logging.warning("portfolios.json not found. Using DEMO_PORTFOLIOS.")
            return DEMO_PORTFOLIOS
        with open(PORTFOLIO_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "portfolios" not in data:
            logging.warning("portfolios.json invalid shape. Using DEMO_PORTFOLIOS.")
            return DEMO_PORTFOLIOS
        if not data["portfolios"]:
            logging.warning("portfolios.json has no portfolios. Using DEMO_PORTFOLIOS.")
            return DEMO_PORTFOLIOS
        logging.info(f"Loaded {len(data['portfolios'])} portfolios.")
        return data
    except Exception as e:
        logging.exception(f"Failed to load portfolios.json: {e}")
        return DEMO_PORTFOLIOS


# ---- Quote cache (simple TTL) ----
_CACHE = {}
_CACHE_TTL = 10  # seconds

def _cache_get(ticker: str):
    now = time.time()
    c = _CACHE.get(ticker)
    if c and (now - c["t"]) < _CACHE_TTL:
        return c
    return None

def _cache_put(ticker: str, price: Optional[float], interval: Optional[str], iso: Optional[str]):
    _CACHE[ticker] = {"p": price, "i": interval, "ts": iso, "t": time.time()}

def _hist_try(ticker: str, period: str, interval: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    hist = yf.Ticker(ticker).history(period=period, interval=interval)
    if hist is not None and not hist.empty:
        last = hist.iloc[-1]
        price = float(last["Close"])
        ts = last.name
        ts_utc = ts.tz_convert(timezone.utc) if hasattr(ts, "tz_convert") else ts.tz_localize(timezone.utc)
        return price, interval, ts_utc.isoformat()
    return None, None, None

def get_price(ticker: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    # cache
    c = _cache_get(ticker)
    if c:
        return c["p"], c["i"], c["ts"]

    # Try multiple sources, most granular first
    for period, interval in [
        ("1d", "1m"),
        ("5d", "1h"),
        ("1mo", "1d"),
    ]:
        p, i, ts = _hist_try(ticker, period, interval)
        if p is not None:
            _cache_put(ticker, p, i, ts)
            return p, i, ts

    # Last resort: fast_info
    try:
        info = yf.Ticker(ticker).fast_info
        p = float(info.last_price) if getattr(info, "last_price", None) is not None else None
        if p is not None:
            ts = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
            _cache_put(ticker, p, "fast_info", ts)
            return p, "fast_info", ts
    except Exception:
        pass

    _cache_put(ticker, None, None, None)
    return None, None, None

@app.get("/api/quote")
def api_quote():
    try:
        tickers_arg = request.args.get("tickers")
        if tickers_arg:
            tickers = [t.strip() for t in tickers_arg.split(",") if t.strip()]
        else:
            data = load_portfolios() or {}
            tickers = []
            for pf in data.get("portfolios", []) or []:
                for h in pf.get("holdings", []) or []:
                    t = (h or {}).get("ticker")
                    if t and t not in tickers:
                        tickers.append(t)

        # If no tickers, return empty list (not a 503)
        if not tickers:
            return jsonify([]), 200

        out = []
        for t in tickers:
            p, i, ts = get_price(t)
            if p is None:
                out.append({"ticker": t, "error": "No data"})
            else:
                out.append({"ticker": t, "price": p, "interval": i, "time_utc": ts})

        return jsonify(out), 200

    except Exception as e:
        return jsonify({"error": "api_quote_failed", "detail": str(e)}), 500


@app.get("/api/portfolios")
def api_portfolios():
    try:
        data = load_portfolios() or {}
        portfolios = data.get("portfolios") or []

        # If nothing configured, return an empty array (valid JSON)
        if not portfolios:
            return jsonify([]), 200

        # Collect all tickers to prefetch (ignore blanks)
        all_tickers = []
        for pf in portfolios:
            for h in pf.get("holdings", []) or []:
                t = (h or {}).get("ticker")
                if t and t not in all_tickers:
                    all_tickers.append(t)

        # Prefetch quotes (never crash)
        quotes = {}
        for t in all_tickers:
            p, i, ts = get_price(t)
            quotes[t] = {"price": p, "interval": i, "time_utc": ts}

        response_payload = []
        for pf in portfolios:
            currency = pf.get("currency", "USD")
            holdings_out = []
            total_cost = 0.0
            total_value = 0.0

            for h in pf.get("holdings", []) or []:
                h = h or {}
                ticker = h.get("ticker")
                shares = float(h.get("shares", 0) or 0)
                avg_cost = float(h.get("avg_cost", 0) or 0)
                q = quotes.get(ticker, {})
                price = q.get("price")

                position_cost = shares * avg_cost
                position_value = (shares * price) if (price is not None) else None

                if position_value is not None:
                    pl = position_value - position_cost
                    pl_pct = (pl / position_cost * 100.0) if position_cost > 0 else None
                    total_cost += position_cost
                    total_value += position_value
                else:
                    pl = None
                    pl_pct = None

                holdings_out.append({
                    "ticker": ticker,
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "price": price,
                    "market_value": position_value,
                    "pl": pl,
                    "pl_pct": pl_pct,
                    "interval": q.get("interval"),
                    "time_utc": q.get("time_utc")
                })

            if total_cost == 0 and total_value == 0:
                pf_total_pl = 0.0
                pf_total_pl_pct = None
            else:
                pf_total_pl = total_value - total_cost
                pf_total_pl_pct = (pf_total_pl / total_cost * 100.0) if total_cost > 0 else None

            response_payload.append({
                "name": pf.get("name", "Portfolio"),
                "currency": currency,
                "holdings": holdings_out,
                "totals": {
                    "cost": total_cost,
                    "value": total_value,
                    "pl": pf_total_pl,
                    "pl_pct": pf_total_pl_pct
                },
                "last_updated_utc": datetime.utcnow().isoformat() + "Z"
            })

        # Always return a valid response
        return jsonify(response_payload), 200

    except Exception as e:
        # Never return None — surface the error in JSON so the UI can show it
        return jsonify({"error": "api_portfolios_failed", "detail": str(e)}), 500


# ---------- UI ----------
@app.get("/")
def index():
    html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Portfolios</title>
<style>
  :root{--bg:#0b1020;--card:#121936;--txt:#e9eefc;--muted:#9fb0ff;--green:#28c76f;--red:#ff4d4f}
  body{font-family:Arial,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);margin:0;padding:30px}
  h1{margin:0 0 20px;font-size:28px}
  .wrap{display:flex;flex-direction:column;gap:24px;max-width:1200px;margin:0 auto}
  .pf-card{background:var(--card);border-radius:16px;padding:18px 18px 8px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
  .pf-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:10px}
  .pf-name{font-weight:800;font-size:18px}
  .pf-cur{color:var(--muted);font-size:12px;text-transform:uppercase}
  table{width:100%;border-collapse:collapse}
  th,td{padding:10px 8px;font-size:14px;text-align:right}
  th{color:var(--muted);font-weight:600;border-bottom:1px solid rgba(255,255,255,0.08)}
  td:first-child, th:first-child{text-align:left}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:700}
  .up{color:var(--green)}
  .down{color:var(--red)}
  .tot{margin-top:8px;display:flex;justify-content:flex-end;gap:16px;color:var(--muted);font-size:14px}
  .stamp{color:var(--muted);font-size:12px;margin-top:6px}
  .grid{display:flex;flex-wrap:wrap;gap:16px;margin-top:8px}
  .mini{background:var(--card);border-radius:12px;padding:12px 16px;min-width:180px}
  .mini .big{font-size:28px;font-weight:800;margin-top:2px}
</style>
</head>
<body>
  <div class="wrap">
    <h1>📊 My Portfolios</h1>
    <div id="portfolios"></div>

    <h1>📈 Quick Quotes</h1>
    <div class="grid" id="cards"></div>
  </div>

<script>
function fmt(n, currency) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD", maximumFractionDigits: 2 }).format(n);
}
function fmtPct(n){
  if (n === null || n === undefined || isNaN(n)) return "—";
  return (n>=0? "+":"") + n.toFixed(2) + "%";
}
function signClass(n){ return (n===null||n===undefined||isNaN(n)) ? "" : (n>=0 ? "up" : "down"); }

async function loadPortfolios(){
  const res = await fetch('/api/portfolios');
  if(!res.ok) return;
  const data = await res.json();
  const host = document.getElementById('portfolios');
  host.innerHTML = '';
  for(const pf of data){
    const div = document.createElement('div');
    div.className = 'pf-card';
    const rows = pf.holdings.map(h => `
      <tr>
        <td>${h.ticker}</td>
        <td>${h.shares}</td>
        <td>${fmt(h.avg_cost, pf.currency)}</td>
        <td>${h.price!==null && h.price!==undefined ? fmt(h.price, pf.currency) : "—"}</td>
        <td>${h.market_value!==null && h.market_value!==undefined ? fmt(h.market_value, pf.currency) : "—"}</td>
        <td class="${signClass(h.pl)}">${h.pl!==null && h.pl!==undefined ? fmt(h.pl, pf.currency) : "—"}</td>
        <td class="${signClass(h.pl_pct)}">${fmtPct(h.pl_pct)}</td>
      </tr>
    `).join('');
    const tot = pf.totals || {};
    div.innerHTML = `
      <div class="pf-head">
        <div>
          <div class="pf-name">${pf.name}</div>
          <div class="pf-cur">Currency: ${pf.currency}</div>
        </div>
        <div class="grid">
          <div class="mini">
            <div>Portfolio Value</div>
            <div class="big">${fmt(tot.value, pf.currency)}</div>
          </div>
          <div class="mini">
            <div>Total P/L</div>
            <div class="big ${signClass(tot.pl)}">${fmt(tot.pl, pf.currency)}</div>
          </div>
          <div class="mini">
            <div>Return</div>
            <div class="big ${signClass(tot.pl_pct)}">${fmtPct(tot.pl_pct)}</div>
          </div>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Ticker</th><th>Shares</th><th>Avg Cost</th><th>Last Price</th>
            <th>Market Value</th><th>P/L</th><th>P/L%</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="stamp">Last updated: ${new Date(pf.last_updated_utc).toLocaleString()}</div>
    `;
    host.appendChild(div);
  }
}

async function loadQuotes(){
  // Build a quick quote panel using all tickers found in portfolios
  const res = await fetch('/api/quote');
  if(!res.ok) return;
  const data = await res.json();
  const grid = document.getElementById('cards');
  grid.innerHTML = '';
  for(const d of data){
    const card = document.createElement('div');
    card.className = 'mini';
    card.innerHTML = `
      <div style="color:#9fb0ff">${d.ticker}</div>
      <div class="big">${(d.price!=null)? d.price.toFixed(2) : "—"}</div>
      <div style="color:#9fb0ff;font-size:12px">Updated: ${d.time_utc? new Date(d.time_utc).toLocaleString() : "—"}</div>
    `;
    grid.appendChild(card);
  }
}

async function refreshAll(){
  await loadPortfolios();
  await loadQuotes();
}
refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")

if __name__ == "__main__":
    # Bind to all interfaces so other devices on your LAN can view it
    app.run(host="0.0.0.0", port=3000, debug=False)
