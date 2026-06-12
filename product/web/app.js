'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
var _sigCache   = null;
var _sigCacheTs = 0;
var CACHE_TTL   = 60 * 60 * 1000; // 1 hour

var _userMode  = null;
var _portfolio = []; // [{ticker, entry_price, alert_up_pct, alert_down_pct}]

// ── Utilities ──────────────────────────────────────────────────────────────────
function showToast(msg, ms) {
  ms = ms || 2500;
  var el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._timer);
  el._timer = setTimeout(function () { el.classList.add('hidden'); }, ms);
}

function fmt(n, decimals) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(decimals !== undefined ? decimals : 2);
}

function fmtRet(pct) {
  if (pct === null || pct === undefined) return { str: 'Unavailable', cls: 'ret-neu' };
  var s   = (pct >= 0 ? '+' : '') + fmt(pct, 1) + '%';
  var cls = pct >= 0 ? 'ret-pos' : 'ret-neg';
  return { str: s, cls: cls };
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Mode / Onboarding ─────────────────────────────────────────────────────────
function checkMode() {
  var mode = localStorage.getItem('user_mode');
  if (!mode) {
    showOnboarding();
    return;
  }
  _userMode = mode;
  hideOnboarding();
  updateTabBar();
  activateDefaultTab();
}

function showOnboarding() {
  var el = document.getElementById('onboarding');
  if (el) el.style.display = 'flex';
}

function hideOnboarding() {
  var el = document.getElementById('onboarding');
  if (el) el.style.display = 'none';
}

function setMode(mode) {
  _userMode = mode;
  localStorage.setItem('user_mode', mode);
  hideOnboarding();
  updateTabBar();
  activateDefaultTab();
}

var _MODE_LABELS = {
  fresh:     'Fresh Start — see recovery signals',
  portfolio: 'Portfolio — monitor my holdings',
  both:      'Both — signals + portfolio monitoring'
};

function updateTabBar() {
  var showSignals   = (_userMode === 'fresh'     || _userMode === 'both');
  var showPositions = (_userMode === 'fresh'     || _userMode === 'both');
  var showPortfolio = (_userMode === 'portfolio' || _userMode === 'both');
  var showAlerts    = (_userMode === 'portfolio' || _userMode === 'both');

  function setVis(tab, show) {
    var btn = document.querySelector('[data-tab="' + tab + '"]');
    if (btn) btn.style.display = show ? '' : 'none';
  }
  setVis('signals',   showSignals);
  setVis('positions', showPositions);
  setVis('portfolio', showPortfolio);
  setVis('alerts',    showAlerts);
  // Settings always visible

  var lbl = document.getElementById('current-mode-label');
  if (lbl) lbl.textContent = 'Mode: ' + (_MODE_LABELS[_userMode] || _userMode);
}

function activateDefaultTab() {
  if (_userMode === 'fresh' || _userMode === 'both') {
    switchTab('signals');
    loadSignals();
  } else {
    switchTab('portfolio');
    loadPortfolio();
  }
}

// ── Tab switching ──────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-pane').forEach(function (el) { el.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function (el)  { el.classList.remove('active'); });
  var pane = document.getElementById('tab-' + tab);
  if (pane) pane.classList.add('active');
  var btn = document.querySelector('[data-tab="' + tab + '"]');
  if (btn) btn.classList.add('active');

  if (tab === 'positions') loadPositions();
  if (tab === 'portfolio') loadPortfolio();
  if (tab === 'alerts')    loadPortfolioAlerts();
}

// ── Signals ────────────────────────────────────────────────────────────────────
function loadSignals() {
  var ctr = document.getElementById('signals-container');
  if (_sigCache && Date.now() - _sigCacheTs < CACHE_TTL) {
    renderSignals(_sigCache);
    return;
  }
  ctr.innerHTML = '<div class="loading">Scanning 50 tickers&hellip; (10&ndash;20 s first run)</div>';
  fetch('/api/screener')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      _sigCache   = data;
      _sigCacheTs = Date.now();
      document.getElementById('last-updated').textContent = 'Last updated: ' + data.as_of;
      renderSignals(data);
    })
    .catch(function () {
      ctr.innerHTML = '<div class="err-box">Could not load signals. Is the server running?</div>';
    });
}

function renderSignals(data) {
  var ctr = document.getElementById('signals-container');
  document.getElementById('last-updated').textContent = 'Last updated: ' + data.as_of;

  if (!data.buy_signals || data.buy_signals.length === 0) {
    ctr.innerHTML = [
      '<div class="empty">',
      '  <div class="em-h">No setups today.</div>',
      '  <p>The signal is selective.<br>',
      '  That is a feature, not a bug.<br><br>',
      '  It fires when price, momentum,<br>',
      '  volume, and quality all align.<br>',
      '  Typically 2&ndash;5 signals per month<br>',
      '  across 50 large-cap tickers.</p>',
      '</div>'
    ].join('\n');
    return;
  }

  ctr.innerHTML = data.buy_signals.map(sigCardHTML).join('');
}

function sigCardHTML(s) {
  var pct = Math.round((s.composite_score || 0) * 100);
  return [
    '<div class="card" id="card-' + s.ticker + '">',
    '  <div class="sig-ticker">',
    '    <span>&#128315; ' + s.ticker + '</span>',
    '    <span class="buy-badge">BUY</span>',
    '  </div>',
    '  <div class="sig-dd">Down ' + fmt(s.drawdown_pct, 1) + '% from 52-week high</div>',
    '  <div class="sig-score-row bar-row" style="margin-top:10px;">',
    '    <span class="bar-label">Signal strength</span>',
    '    <div class="bar-track"><div class="bar-fill bar-fill-blue" style="width:' + pct + '%"></div></div>',
    '    <span class="bar-val">' + fmt(s.composite_score, 2) + '</span>',
    '  </div>',
    '  <div class="card-actions">',
    '    <button class="btn btn-ghost" id="why-btn-' + s.ticker + '" onclick="toggleDetail(\'' + s.ticker + '\')">Why now?</button>',
    '    <button class="btn btn-primary" onclick="toggleTrackForm(\'' + s.ticker + '\',' + s.price + ')">Track</button>',
    '  </div>',
    '  <div class="track-form" id="tf-' + s.ticker + '">',
    '    <label>Entry $</label>',
    '    <input type="number" id="tp-' + s.ticker + '" value="' + s.price + '" step="0.01" min="0.01">',
    '    <button class="btn btn-primary btn-sm" onclick="trackPosition(\'' + s.ticker + '\')">Confirm</button>',
    '    <button class="btn btn-ghost btn-sm" onclick="toggleTrackForm(\'' + s.ticker + '\',' + s.price + ')">Cancel</button>',
    '  </div>',
    '  <div class="sig-detail" id="det-' + s.ticker + '">',
    sigDetailHTML(s),
    '  </div>',
    '</div>'
  ].join('\n');
}

function sigDetailHTML(s) {
  return [
    '<div class="subsection">Price info</div>',
    '<div class="detail-row"><span class="dl">Current price</span><span class="dv">$' + fmt(s.price, 2) + '</span></div>',
    '<div class="detail-row"><span class="dl">52-week high</span><span class="dv">$' + fmt(s.high_52w, 2) + '</span></div>',
    '<div class="detail-row"><span class="dl">Drawdown</span><span class="dv" style="color:var(--red)">-' + fmt(s.drawdown_pct, 1) + '% &#8595;</span></div>',

    '<div class="subsection">Quality checks</div>',
    '<div class="check">&#9989; Revenue positive</div>',
    '<div class="check">&#9989; Margin positive</div>',
    '<div class="check">&#9989; Debt manageable (D/E &lt; 3)</div>',

    '<div class="subsection">Signal components</div>',
    compRowHTML('Dip score', s.dip_score),
    compRowHTML('Momentum', s.momentum_score),
    compRowHTML('Volume', s.volume_score),

    '<div class="subsection">Historical edge</div>',
    '<div class="edge-box">',
    '  <div class="edge-row"><span>Avg 12m return (BUY)</span><span class="ev-green">+49.2%</span></div>',
    '  <div class="edge-row"><span>Avg 12m return (random)</span><span class="ev-muted">+22.3%</span></div>',
    '  <div class="edge-row"><span>Edge</span><span class="ev-green">+26.9 pp</span></div>',
    '  <div class="edge-row"><span>Sample</span><span class="ev-muted">2,382 signals / 7 years</span></div>',
    '</div>',

    '<div class="warn-text">',
    '  &#9888;&#65039; Expect another 10&ndash;15% drop before recovery.<br>',
    '  Median drawdown before recovery: &minus;15%.<br>',
    '  Hold 12 months (~252 trading days). No stop-loss.<br>',
    '  The edge requires holding through the drawdown.',
    '</div>',

    '<div class="subsection">Similar historical entries</div>',
    '<div class="case">AVGO Mar 2020: down 48% &#8594; <span class="case-ret">+89.9%</span> in 3 months</div>',
    '<div class="case">TSLA May 2023: down 49% &#8594; <span class="case-ret">+61.5%</span> in 3 months</div>',
    '<div class="case">CRM  Dec 2022: down 50% &#8594; <span class="case-ret">+53.0%</span> in 3 months</div>',

    '<div class="disclaimer">',
    '  &#9888;&#65039; Not a recommendation. You decide.<br>',
    '  This is a statistical pattern, not a guarantee.<br>',
    '  1 in 4 similar entries ends negative at 12 months.<br>',
    '  Past performance does not guarantee future results.',
    '</div>',
    '<button class="btn btn-primary" style="margin-top:12px;width:100%;" onclick="toggleTrackForm(\'' + s.ticker + '\',' + s.price + ')">Track this position</button>'
  ].join('\n');
}

function compRowHTML(label, score) {
  var v   = (score !== null && score !== undefined) ? score : 0;
  var pct = Math.round(v * 100);
  return [
    '<div class="comp-row">',
    '  <span class="comp-label">' + label + '</span>',
    '  <div class="bar-track"><div class="bar-fill bar-fill-blue" style="width:' + pct + '%"></div></div>',
    '  <span class="bar-val">' + fmt(v, 2) + '</span>',
    '</div>'
  ].join('\n');
}

function toggleDetail(ticker) {
  var det = document.getElementById('det-' + ticker);
  var btn = document.getElementById('why-btn-' + ticker);
  if (!det) return;
  var isOpen = det.classList.toggle('open');
  if (btn) btn.textContent = isOpen ? 'Hide' : 'Why now?';
}

function toggleTrackForm(ticker, price) {
  var form = document.getElementById('tf-' + ticker);
  if (!form) return;
  form.classList.toggle('open');
}

function trackPosition(ticker) {
  var inp   = document.getElementById('tp-' + ticker);
  var price = parseFloat(inp ? inp.value : '0');
  if (!price || price <= 0) { showToast('Enter a valid price'); return; }
  var today = new Date().toISOString().split('T')[0];
  fetch('/api/positions/open', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ ticker: ticker, entry_price: price, entry_date: today })
  })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (res.success) {
        showToast(ticker + ' added to positions');
        switchTab('positions');
      } else {
        showToast('Failed to track. Try again.');
      }
    })
    .catch(function () { showToast('Failed to track. Try again.'); });
}

// ── Positions ──────────────────────────────────────────────────────────────────
function loadPositions() {
  var ctr = document.getElementById('positions-container');
  ctr.innerHTML = '<div class="loading">Loading positions&hellip;</div>';
  fetch('/api/positions')
    .then(function (r) { return r.json(); })
    .then(function (data) { renderPositions(data.positions || []); })
    .catch(function () {
      ctr.innerHTML = '<div class="err-box">Failed to load positions.</div>';
    });
}

function renderPositions(positions) {
  var ctr = document.getElementById('positions-container');
  if (!positions || positions.length === 0) {
    ctr.innerHTML = [
      '<div class="empty">',
      '  <div class="em-h">No open positions.</div>',
      '  <p>When you tap "Track" on a BUY signal,<br>',
      '  it appears here with live return tracking<br>',
      '  and a 252-day countdown.</p>',
      '</div>'
    ].join('\n');
    return;
  }
  ctr.innerHTML = positions.map(posCardHTML).join('');
}

function posCardHTML(p) {
  var r    = fmtRet(p.current_return_pct);
  var prog = Math.min(100, Math.round((p.days_held / 252) * 100));
  var cur  = p.current_price ? '$' + fmt(p.current_price, 2) : 'Price unavailable';
  var exp  = p.expected_return_pct !== null ? (p.expected_return_pct >= 0 ? '+' : '') + fmt(p.expected_return_pct, 1) + '%' : '—';

  return [
    '<div class="card">',
    '  <div class="pos-header">',
    '    <div class="pos-ticker">' + p.ticker + '</div>',
    '    <div class="ret-badge ' + r.cls + '">' + r.str + '</div>',
    '  </div>',
    '  <div class="pos-meta">',
    '    Entry <b>$' + fmt(p.entry_price, 2) + '</b> on <b>' + p.entry_date + '</b>',
    '    &nbsp;&middot;&nbsp; Now <b>' + cur + '</b>',
    '  </div>',
    '  <div style="margin-top:10px;" class="bar-row">',
    '    <div class="bar-track" style="height:6px;"><div class="bar-fill bar-fill-blue" style="width:' + prog + '%;height:6px;"></div></div>',
    '  </div>',
    '  <div class="prog-label">Day ' + p.days_held + ' of 252 &mdash; ' + p.days_remaining + ' days remaining</div>',
    '  <div style="font-size:11px;color:var(--muted);margin-top:3px;">Historical avg at day ' + p.days_held + ': <span style="font-family:monospace;color:var(--text)">' + exp + '</span></div>',
    p.context_message ? '  <div class="context-msg">&ldquo;' + escHtml(p.context_message) + '&rdquo;</div>' : '',
    '  <div class="card-actions" style="margin-top:12px;">',
    '    <button class="btn btn-danger btn-sm" onclick="closePosition(\'' + p.ticker + '\')">Close position</button>',
    '  </div>',
    '  <div class="disclaimer">&#9888;&#65039; Not advice. You control the exit decision. Past performance does not guarantee future results.</div>',
    '</div>'
  ].join('\n');
}

function closePosition(ticker) {
  if (!confirm('Close ' + ticker + '?\nThis will record your final return.')) return;
  fetch('/api/positions/close', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ ticker: ticker })
  })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (res.success) {
        var retStr = res.final_return_pct !== null
          ? ' Final: ' + (res.final_return_pct >= 0 ? '+' : '') + fmt(res.final_return_pct, 1) + '%'
          : '';
        showToast(ticker + ' closed.' + retStr);
        loadPositions();
      } else {
        showToast('Failed to close position.');
      }
    })
    .catch(function () { showToast('Failed to close position.'); });
}

// ── Portfolio ──────────────────────────────────────────────────────────────────
function loadPortfolio() {
  var ctr = document.getElementById('portfolio-container');
  if (ctr) ctr.innerHTML = '<div class="loading">Loading&hellip;</div>';
  fetch('/api/portfolio')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      _portfolio = (data.holdings || []).map(function (h) {
        return {
          ticker:        h.ticker,
          entry_price:   h.entry_price,
          alert_up_pct:  h.alert_up_pct,
          alert_down_pct: h.alert_down_pct,
        };
      });
      renderPortfolio(data.holdings || []);
    })
    .catch(function () {
      if (ctr) ctr.innerHTML = '<div class="err-box">Failed to load portfolio.</div>';
    });
}

function renderPortfolio(holdings) {
  var ctr = document.getElementById('portfolio-container');
  if (!ctr) return;
  if (!holdings || holdings.length === 0) {
    ctr.innerHTML = [
      '<div class="empty">',
      '  <div class="em-h">No holdings yet.</div>',
      '  <p>Add stocks you own below.<br><br>',
      '  You\'ll get alerts when they hit<br>',
      '  your price targets or trigger<br>',
      '  a recovery signal.</p>',
      '</div>'
    ].join('\n');
    return;
  }
  ctr.innerHTML = holdings.map(portfolioCardHTML).join('');
}

function portfolioCardHTML(h) {
  var r   = fmtRet(h.current_return_pct);
  var cur = h.current_price ? '$' + fmt(h.current_price, 2) : 'unavailable';
  var ent = h.entry_price   ? '$' + fmt(h.entry_price, 2)   : 'no entry set';
  return [
    '<div class="card">',
    '  <div class="ph-header">',
    '    <div class="ph-ticker">' + h.ticker + '</div>',
    '    <div class="ret-badge ' + r.cls + '">' + r.str + '</div>',
    '  </div>',
    '  <div class="ph-meta">',
    '    Entry <b>' + ent + '</b> &nbsp;&middot;&nbsp; Now <b>' + cur + '</b>',
    '  </div>',
    '  <div class="ph-alerts-row">',
    '    Alert up: <b>+' + fmt(h.alert_up_pct || 20, 0) + '%</b>',
    '    &nbsp;&middot;&nbsp; Alert down: <b>-' + fmt(h.alert_down_pct || 10, 0) + '%</b>',
    '  </div>',
    '  <div class="card-actions" style="margin-top:10px;">',
    '    <button class="btn btn-danger btn-sm" onclick="removeHolding(\'' + h.ticker + '\')">Remove</button>',
    '  </div>',
    '</div>'
  ].join('\n');
}

function addHolding() {
  var tickerEl = document.getElementById('ph-ticker');
  var ticker   = (tickerEl.value || '').trim().toUpperCase().replace(/[^A-Z]/g, '');
  if (!ticker) { showToast('Enter a ticker'); return; }

  var already = _portfolio.some(function (h) { return h.ticker === ticker; });
  if (already) { showToast(ticker + ' already in portfolio'); return; }

  var priceVal   = document.getElementById('ph-price').value;
  var entryPrice = priceVal ? parseFloat(priceVal) : null;
  var alertUp    = parseFloat(document.getElementById('ph-alert-up').value)   || 20;
  var alertDown  = parseFloat(document.getElementById('ph-alert-down').value) || 10;

  _portfolio = _portfolio.concat([{
    ticker:         ticker,
    entry_price:    entryPrice,
    alert_up_pct:   alertUp,
    alert_down_pct: alertDown,
  }]);

  tickerEl.value = '';
  document.getElementById('ph-price').value = '';
  savePortfolio();
}

function removeHolding(ticker) {
  if (!confirm('Remove ' + ticker + ' from portfolio?')) return;
  _portfolio = _portfolio.filter(function (h) { return h.ticker !== ticker; });
  savePortfolioQuiet();
}

function savePortfolio() {
  fetch('/api/portfolio', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ holdings: _portfolio }),
  })
    .then(function (r) { return r.json(); })
    .then(function () { loadPortfolio(); showToast('Portfolio saved'); })
    .catch(function () { showToast('Failed to save portfolio'); });
}

function savePortfolioQuiet() {
  fetch('/api/portfolio', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ holdings: _portfolio }),
  })
    .then(function (r) { return r.json(); })
    .then(function () { loadPortfolio(); })
    .catch(function () { showToast('Failed to save portfolio'); });
}

// ── Portfolio Alerts ───────────────────────────────────────────────────────────
function loadPortfolioAlerts() {
  var ctr = document.getElementById('alerts-container');
  if (ctr) ctr.innerHTML = '<div class="loading">Checking alerts&hellip;</div>';
  fetch('/api/portfolio/alerts')
    .then(function (r) { return r.json(); })
    .then(function (data) { renderPortfolioAlerts(data.alerts || []); })
    .catch(function () {
      if (ctr) ctr.innerHTML = '<div class="err-box">Failed to load alerts.</div>';
    });
}

function renderPortfolioAlerts(alerts) {
  var ctr = document.getElementById('alerts-container');
  if (!ctr) return;
  if (!alerts || alerts.length === 0) {
    ctr.innerHTML = [
      '<div class="empty">',
      '  <div class="em-h">No alerts right now.</div>',
      '  <p>We check your holdings for price targets,<br>',
      '  recovery signals, and recent news.<br><br>',
      '  All clear for today.</p>',
      '</div>'
    ].join('\n');
    return;
  }
  ctr.innerHTML = alerts.map(alertCardHTML).join('');
}

var _ALERT_LABELS = {
  PRICE_TARGET_UP:       'Price Target UP',
  PRICE_TARGET_DOWN:     'Price Target DOWN',
  SIGNAL_ON_HELD_TICKER: 'Recovery Signal',
  NEWS:                  'News',
};
var _ALERT_CLS = {
  PRICE_TARGET_UP:       'at-up',
  PRICE_TARGET_DOWN:     'at-down',
  SIGNAL_ON_HELD_TICKER: 'at-signal',
  NEWS:                  'at-news',
};

function alertCardHTML(a) {
  var id       = 'ab-' + a.ticker + '-' + a.type;
  var label    = _ALERT_LABELS[a.type] || a.type;
  var cls      = _ALERT_CLS[a.type]   || '';
  var newsLink = (a.type === 'NEWS' && a.url)
    ? '<a href="' + escHtml(a.url) + '" target="_blank" rel="noopener noreferrer" style="color:var(--blue);font-size:12px;display:block;margin-top:6px;">Read article &#8599;</a>'
    : '';
  return [
    '<div class="alert-card">',
    '  <span class="alert-type-badge ' + cls + '">' + label + '</span>',
    '  <div class="alert-headline">' + escHtml(a.headline || '') + '</div>',
    newsLink,
    '  <button class="btn btn-ghost btn-sm" style="margin-top:10px;" onclick="toggleAlertBody(\'' + id + '\', this)">Details</button>',
    '  <div class="alert-body" id="' + id + '">' + escHtml(a.body || '') + '</div>',
    '  <div class="disclaimer" style="margin-top:6px;">&#9888;&#65039; Informational only. Not investment advice.</div>',
    '</div>'
  ].join('\n');
}

function toggleAlertBody(id, btn) {
  var el = document.getElementById(id);
  if (!el) return;
  var open = el.classList.toggle('open');
  if (btn) btn.textContent = open ? 'Hide' : 'Details';
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  var phTicker = document.getElementById('ph-ticker');
  if (phTicker) {
    phTicker.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') addHolding();
    });
  }
});
