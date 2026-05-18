"""
Hotstuff Live Trading Chart
Serves a real-time candlestick chart with fill markers via local web server.

Usage:
  python3 -m bot.chart
  python3 -m bot.chart BTC-PERP
  Then open http://localhost:8765 in your browser
"""
import sys
import os
import time
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from eth_account import Account
from hotstuff import InfoClient
from hotstuff.methods.info.market import ChartParams
from hotstuff.methods.info.account import FillsParams

SYMBOL_IDS = {
    'BTC-PERP': '1',
    'ETH-PERP': '2',
    'SOL-PERP': '3',
    'HYPE-PERP': '7',
}

PORT = 8765
market = sys.argv[1] if len(sys.argv) > 1 else 'BTC-PERP'
symbol_id = SYMBOL_IDS.get(market, '1')
address = Account.from_key(os.environ['HOTSTUFF_PRIVATE_KEY']).address
info = InfoClient(is_testnet=False)

_cache = {'candles': [], 'fills': [], 'updated': 0}
_lock = threading.Lock()


def fetch_data():
    now = int(time.time())
    try:
        candles = info.chart(ChartParams(
            symbol=symbol_id, resolution='5',
            from_=now - 86400, to=now, chart_type='mark'
        ))
        candle_data = [
            {'t': c.time, 'o': c.open, 'h': c.high, 'l': c.low, 'c': c.close, 'v': c.volume}
            for c in candles
        ]
    except Exception as e:
        print(f"Candle fetch error: {e}")
        candle_data = _cache['candles']

    try:
        fills_resp = info.fills(FillsParams(user=address, limit=500))
        fill_data = []
        for f in fills_resp.entries:
            d = vars(f) if not isinstance(f, dict) else f
            if d.get('instrument', '') != market:
                continue
            ts = d.get('block_timestamp', 0)
            if isinstance(ts, str):
                ts = int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1000)
            fill_data.append({
                't':         ts,
                'price':     float(d.get('price', 0)),
                'side':      d.get('side', ''),
                'direction': d.get('direction', ''),
                'size':      float(d.get('size', 0)),
                'pnl':       float(d.get('closed_pnl', 0)),
                'fee':       float(d.get('fee', 0)),
                'cloid':     str(d.get('cloid', ''))[:20],
            })
    except Exception as e:
        print(f"Fill fetch error: {e}")
        fill_data = _cache['fills']

    with _lock:
        _cache['candles'] = candle_data
        _cache['fills'] = fill_data
        _cache['updated'] = int(time.time() * 1000)


def refresh_loop():
    while True:
        fetch_data()
        time.sleep(10)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hotstuff Live Chart</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap');

  :root {
    --bg: #0a0c0f;
    --surface: #111318;
    --border: #1e2330;
    --text: #c8d0e0;
    --dim: #4a5568;
    --green: #00d4a0;
    --red: #ff4466;
    --cyan: #00b8d9;
    --yellow: #f6c90e;
    --accent: #7c5cbf;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .market-badge {
    font-size: 18px;
    font-weight: 600;
    color: var(--cyan);
    letter-spacing: 0.05em;
  }

  .price-display {
    font-size: 22px;
    font-weight: 500;
    color: #fff;
    transition: color 0.3s;
  }

  .price-change {
    font-size: 13px;
    padding: 2px 8px;
    border-radius: 3px;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 20px;
    font-size: 12px;
    color: var(--dim);
  }

  .stat-block { text-align: right; }
  .stat-label { color: var(--dim); font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; }
  .stat-value { color: var(--text); font-size: 13px; font-weight: 500; margin-top: 1px; }

  .live-dot {
    width: 7px; height: 7px;
    background: var(--green);
    border-radius: 50%;
    animation: pulse 2s infinite;
    flex-shrink: 0;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(0,212,160,0.4); }
    50% { opacity: 0.7; box-shadow: 0 0 0 5px rgba(0,212,160,0); }
  }

  #chart {
    flex: 1;
    min-height: 0;
  }

  .stats-bar {
    display: flex;
    gap: 24px;
    padding: 8px 20px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-size: 11px;
    flex-shrink: 0;
  }

  .stat-item { display: flex; gap: 6px; align-items: center; }
  .stat-item .lbl { color: var(--dim); }
  .stat-item .val { font-weight: 500; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--text); }

  select {
    background: var(--border);
    color: var(--text);
    border: 1px solid var(--dim);
    padding: 4px 8px;
    border-radius: 3px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    cursor: pointer;
  }

  .updated { color: var(--dim); font-size: 11px; }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="live-dot"></div>
    <span class="market-badge" id="marketLabel">BTC-PERP</span>
    <span class="price-display" id="lastPrice">—</span>
    <span class="price-change" id="priceChange"></span>
  </div>
  <div class="header-right">
    <div class="stat-block">
      <div class="stat-label">Total Fills</div>
      <div class="stat-value" id="totalFills">—</div>
    </div>
    <div class="stat-block">
      <div class="stat-label">Session PnL</div>
      <div class="stat-value" id="sessionPnl">—</div>
    </div>
    <div class="stat-block">
      <div class="stat-label">Total Fees</div>
      <div class="stat-value" id="totalFees">—</div>
    </div>
    <select id="resSelect" onchange="changeRes(this.value)">
      <option value="5">5m</option>
      <option value="15">15m</option>
      <option value="60">1h</option>
      <option value="240">4h</option>
    </select>
    <span class="updated" id="updatedAt">—</span>
  </div>
</header>

<div id="chart"></div>

<div class="stats-bar" id="statsBar"></div>

<script>
const MARKET = document.title.split(' ')[0];
let currentRes = '5';
let chartData = null;

const dirColors = {
  openLong:    '#00d4a0',
  closeLong:   '#00a878',
  flipToLong:  '#00b8d9',
  openShort:   '#ff4466',
  closeShort:  '#cc3355',
  flipToShort: '#ff6688',
};

const layout = {
  paper_bgcolor: '#0a0c0f',
  plot_bgcolor:  '#0a0c0f',
  margin: { t: 10, b: 40, l: 60, r: 60 },
  xaxis: {
    type: 'date',
    gridcolor: '#1e2330',
    tickcolor: '#1e2330',
    tickfont: { color: '#4a5568', family: 'IBM Plex Mono', size: 11 },
    rangeslider: { visible: false },
    showgrid: true,
  },
  yaxis: {
    gridcolor: '#1e2330',
    tickcolor: '#1e2330',
    tickfont: { color: '#4a5568', family: 'IBM Plex Mono', size: 11 },
    side: 'right',
    showgrid: true,
  },
  legend: {
    font: { color: '#c8d0e0', family: 'IBM Plex Mono', size: 11 },
    bgcolor: 'rgba(17,19,24,0.8)',
    bordercolor: '#1e2330',
    borderwidth: 1,
  },
  hoverlabel: {
    bgcolor: '#111318',
    bordercolor: '#1e2330',
    font: { family: 'IBM Plex Mono', size: 11, color: '#c8d0e0' },
  },
};

const config = {
  displayModeBar: false,
  responsive: true,
};

function buildChart(data) {
  const candles = data.candles;
  const fills   = data.fills;

  const times  = candles.map(c => new Date(c.t));
  const opens  = candles.map(c => c.o);
  const highs  = candles.map(c => c.h);
  const lows   = candles.map(c => c.l);
  const closes = candles.map(c => c.c);

  const lastClose = closes[closes.length - 1];
  const prevClose = closes[closes.length - 2] || lastClose;
  const chg = ((lastClose - prevClose) / prevClose * 100).toFixed(2);
  document.getElementById('lastPrice').textContent =
    lastClose.toLocaleString('en-US', { minimumFractionDigits: 1 });
  const chgEl = document.getElementById('priceChange');
  chgEl.textContent = `${chg > 0 ? '+' : ''}${chg}%`;
  chgEl.style.background = chg >= 0 ? 'rgba(0,212,160,0.15)' : 'rgba(255,68,102,0.15)';
  chgEl.style.color = chg >= 0 ? '#00d4a0' : '#ff4466';

  const candleTrace = {
    type: 'candlestick',
    x: times, open: opens, high: highs, low: lows, close: closes,
    name: 'Price',
    increasing: { line: { color: '#00d4a0', width: 1 }, fillcolor: 'rgba(0,212,160,0.3)' },
    decreasing: { line: { color: '#ff4466', width: 1 }, fillcolor: 'rgba(255,68,102,0.3)' },
    hoverinfo: 'x+y',
    whiskerwidth: 0.3,
  };

  // Group fills by direction
  const fillGroups = {};
  let totalPnl = 0, totalFees = 0;
  for (const f of fills) {
    if (!fillGroups[f.direction]) fillGroups[f.direction] = [];
    fillGroups[f.direction].push(f);
    totalPnl  += f.pnl;
    totalFees += f.fee;
  }

  document.getElementById('totalFills').textContent = fills.length;
  const pnlEl = document.getElementById('sessionPnl');
  pnlEl.textContent = `$${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(4)}`;
  pnlEl.style.color = totalPnl >= 0 ? '#00d4a0' : '#ff4466';
  document.getElementById('totalFees').textContent = `$${totalFees.toFixed(4)}`;

  const fillTraces = Object.entries(fillGroups).map(([dir, arr]) => {
    const isBuy = dir.includes('Long');
    return {
      type: 'scatter',
      mode: 'markers',
      name: dir,
      x: arr.map(f => new Date(f.t)),
      y: arr.map(f => f.price),
      marker: {
        symbol: isBuy ? 'triangle-up' : 'triangle-down',
        size: 10,
        color: dirColors[dir] || '#888',
        line: { color: '#0a0c0f', width: 1 },
      },
      customdata: arr.map(f => [f.size, f.pnl, f.fee, f.cloid]),
      hovertemplate:
        `<b>${dir}</b><br>` +
        `Price: %{y:,.4f}<br>` +
        `Size: %{customdata[0]}<br>` +
        `PnL: $%{customdata[1]:+.4f}<br>` +
        `Fee: $%{customdata[2]:.4f}<br>` +
        `<extra></extra>`,
    };
  });

  // Stats bar
  const statsEl = document.getElementById('statsBar');
  const dirStats = Object.entries(fillGroups).map(([dir, arr]) => {
    const pnl = arr.reduce((s, f) => s + f.pnl + f.fee, 0);
    const cls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'neu';
    return `<div class="stat-item">
      <span class="lbl" style="color:${dirColors[dir]||'#888'}">${dir}</span>
      <span class="val ${cls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(3)}</span>
      <span class="lbl">(${arr.length})</span>
    </div>`;
  }).join('');
  statsEl.innerHTML = dirStats;

  const traces = [candleTrace, ...fillTraces];

  if (!chartData) {
    Plotly.newPlot('chart', traces, layout, config);
    chartData = true;
  } else {
    Plotly.react('chart', traces, layout, config);
  }

  document.getElementById('updatedAt').textContent =
    'updated ' + new Date().toLocaleTimeString();
}

async function fetchAndUpdate() {
  try {
    const resp = await fetch('/data?res=' + currentRes);
    const data = await resp.json();
    buildChart(data);
  } catch(e) {
    console.error('Fetch error:', e);
  }
}

function changeRes(val) {
  currentRes = val;
  chartData = null;
  fetchAndUpdate();
}

fetchAndUpdate();
setInterval(fetchAndUpdate, 10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML.encode())

        elif self.path.startswith('/data'):
            with _lock:
                payload = json.dumps({
                    'candles': _cache['candles'],
                    'fills':   _cache['fills'],
                    'updated': _cache['updated'],
                })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(payload.encode())

        else:
            self.send_response(404)
            self.end_headers()


def main():
    print(f"Fetching initial data for {market}...")
    fetch_data()
    print(f"Chart server running at http://localhost:{PORT}")
    print(f"Open in your browser — updates every 10s")

    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()

    server = HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChart server stopped.")


if __name__ == '__main__':
    main()
