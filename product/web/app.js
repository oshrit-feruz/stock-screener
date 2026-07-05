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

// ── Onboarding ────────────────────────────────────────────────────────────────

function checkMode() {
  if (localStorage.getItem('onboarding_complete') === 'true') {
    var mode = localStorage.getItem('user_mode') || 'fresh';
    if (mode === 'portfolio') {        // backward compat: old value → new name
      mode = 'existing';
      localStorage.setItem('user_mode', 'existing');
    }
    _userMode = mode;
    updateTabBar();
    activateDefaultTab();
  } else {
    showOnboarding();
  }
}

function showOnboarding() {
  var el = document.getElementById('onboarding');
  if (el) el.style.display = 'flex';
  showOnboardingScreen(1);
}

function hideOnboarding() {
  var el = document.getElementById('onboarding');
  if (el) el.style.display = 'none';
}

function showOnboardingScreen(n) {
  [1, 2, 3].forEach(function (i) {
    var s = document.getElementById('ob-screen-' + i);
    if (s) s.style.display = (i === n) ? '' : 'none';
  });
}

function obNext(toScreen) { showOnboardingScreen(toScreen); }
function obBack(toScreen)  { showOnboardingScreen(toScreen); }

function completeOnboarding(mode) {
  var sel = document.querySelector('input[name="time_horizon"]:checked');
  var th  = sel ? sel.value : '6_to_12m';
  localStorage.setItem('user_time_horizon', th);
  localStorage.setItem('user_mode', mode);
  localStorage.setItem('onboarding_complete', 'true');
  hideOnboarding();
  _userMode = mode;
  updateTabBar();
  activateDefaultTab();
}

var _MODE_LABELS = {
  fresh:    'Fresh Start — see recovery signals',
  existing: 'Existing Stocks — monitor my holdings',
  portfolio: 'Existing Stocks — monitor my holdings',  // backward compat
  both:     'Both — signals + portfolio monitoring'
};

function updateTabBar() {
  var isExisting  = (_userMode === 'existing' || _userMode === 'portfolio');
  var showSignals   = (_userMode === 'fresh' || _userMode === 'both');
  var showPositions = (_userMode === 'fresh');    // positions tab only for fresh mode
  var showPortfolio = (isExisting || _userMode === 'both');
  var showAlerts    = (isExisting || _userMode === 'both');
  // simulator + settings always visible

  function setVis(tab, show) {
    var btn = document.querySelector('[data-tab="' + tab + '"]');
    if (btn) btn.style.display = show ? '' : 'none';
  }
  setVis('signals',   showSignals);
  setVis('positions', showPositions);
  setVis('portfolio', showPortfolio);
  setVis('alerts',    showAlerts);
}

function showLoadingBanner(msg) {
  var banner = document.getElementById('loading-banner');
  var title  = document.getElementById('loading-banner-title');
  if (banner) banner.style.display = 'block';
  if (title && msg) title.textContent = msg;
}

function hideLoadingBanner() {
  var banner = document.getElementById('loading-banner');
  if (banner) banner.style.display = 'none';
}

function checkServerAndLoad() {
  showLoadingBanner('Starting up… (~30 seconds on first visit)');
  fetch('/api/health', { signal: AbortSignal.timeout(60000) })
    .then(function (res) {
      if (res.ok) {
        hideLoadingBanner();
        var isExisting = (_userMode === 'existing' || _userMode === 'portfolio');
        if (isExisting) { loadPortfolio(); } else { loadSignals(); }
      } else {
        showLoadingBanner('Server is starting up. Please wait…');
        setTimeout(checkServerAndLoad, 5000);
      }
    })
    .catch(function () {
      showLoadingBanner('Server is starting up. Please wait…');
      setTimeout(checkServerAndLoad, 5000);
    });
}

function activateDefaultTab() {
  var isExisting = (_userMode === 'existing' || _userMode === 'portfolio');
  if (isExisting) {
    switchTab('portfolio');
  } else {
    switchTab('signals');
  }
  checkServerAndLoad();
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
  if (tab === 'beta')      loadBeta();
  if (tab === 'settings')  loadSettings();
  var simBtn = document.getElementById('sim-run-btn');
  if (tab !== 'simulator' && simBtn) simBtn.disabled = false;
}

// ── Signals ────────────────────────────────────────────────────────────────────
function loadSignals() {
  var ctr = document.getElementById('signals-container');
  if (_sigCache && Date.now() - _sigCacheTs < CACHE_TTL) {
    renderSignals(_sigCache);
    return;
  }
  ctr.innerHTML = '<div class="loading">Scanning the Top-100 universe&hellip; (may take up to 60 s on first visit)</div>';
  fetch('/api/screener')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.warming) {
        // Server still warming up — retry in 15 seconds
        ctr.innerHTML = '<div class="loading">&#9203; Server is warming up&hellip; checking again in 15 s</div>';
        setTimeout(loadSignals, 15000);
        return;
      }
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
      '  across the Top-100 point-in-time universe.</p>',
      '</div>'
    ].join('\n');
    return;
  }

  ctr.innerHTML = data.buy_signals.map(sigCardHTML).join('');
  maybeShowFirstSignalTooltip();
}

function timeHorizonBannerHTML() {
  var th = localStorage.getItem('user_time_horizon') || '6_to_12m';
  if (th === 'under_3m' || th === '3_to_6m') {
    return '<div class="th-warning-banner">'
      + '&#9888;&#65039; Your time horizon is under 6 months. '
      + 'This signal\'s edge is strongest at 12 months. '
      + 'At 3&ndash;6 months: avg +6&ndash;12%. Proceed with extra caution.'
      + '</div>';
  }
  if (th === 'over_12m') {
    return '<div class="th-info-banner">'
      + '&#8505;&#65039; You plan to hold beyond 12 months. '
      + 'The signal has no validated edge past 252 days. '
      + 'You may hold longer at your own discretion.'
      + '</div>';
  }
  return '';
}

function maybeShowFirstSignalTooltip() {
  if (localStorage.getItem('first_signal_seen') === 'true') return;
  var el = document.getElementById('first-signal-tooltip');
  if (el) el.style.display = 'flex';
}

function dismissFirstSignalTooltip() {
  localStorage.setItem('first_signal_seen', 'true');
  var el = document.getElementById('first-signal-tooltip');
  if (el) el.style.display = 'none';
}

function sigCardHTML(s) {
  var pct = Math.round((s.composite_score || 0) * 100);
  var thBanner = timeHorizonBannerHTML();
  return [
    '<div class="card" id="card-' + s.ticker + '">',
    thBanner,
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

// ── Beta tracking ────────────────────────────────────────────────────────────────
function loadBeta() {
  var ctr = document.getElementById('beta-container');
  ctr.innerHTML = '<div class="loading">Loading beta tracking&hellip;</div>';
  fetch('/api/beta/dashboard')
    .then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Failed to load beta tracking'); });
      return r.json();
    })
    .then(function (data) { renderBeta(data); })
    .catch(function () {
      ctr.innerHTML = '<div class="err-box">Failed to load beta tracking.</div>';
    });
}

function renderBeta(data) {
  var ctr     = document.getElementById('beta-container');
  var summary = (data && data.summary) || {};
  var open    = (data && data.open_positions) || [];
  var closed  = (data && data.closed_positions) || [];

  if ((summary.total_opened || 0) === 0) {
    ctr.innerHTML = [
      '<div class="empty">',
      '  <div class="em-h">Beta tracking hasn’t started yet.</div>',
      '  <p>No positions have been opened yet.<br>',
      '  Once a BUY signal is tracked, it appears here with<br>',
      '  live return vs SPY and money-market over the same period.</p>',
      '</div>'
    ].join('\n');
    return;
  }

  var html = [betaSummaryHTML(data, summary, open.length, closed.length)];
  if (open.length) {
    html.push('<div class="section-title" style="margin-top:18px;">Open positions</div>');
    html.push(open.map(betaOpenCardHTML).join(''));
  }
  if (closed.length) {
    html.push('<div class="section-title" style="margin-top:18px;">Closed positions</div>');
    html.push(closed.map(betaClosedCardHTML).join(''));
  }
  ctr.innerHTML = html.join('\n');
}

function betaSummaryHTML(data, s, nOpen, nClosed) {
  var since = data.beta_start ? ' &middot; since ' + data.beta_start : '';
  var rows = [
    '<div class="card">',
    '  <div class="pos-header">',
    '    <div class="pos-ticker">Beta summary</div>',
    '    <div style="font-size:12px;color:var(--muted);">' + (s.total_opened || 0) + ' opened' + since + '</div>',
    '  </div>',
    '  <div class="pos-meta" style="margin-top:8px;"><b>' + nOpen + '</b> open &nbsp;&middot;&nbsp; <b>' + nClosed + '</b> closed</div>'
  ];
  var agg = s.closed_aggregate;
  if (agg) {
    rows.push('  <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;">');
    rows.push('    <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">Closed aggregate over the same periods</div>');
    rows.push(betaCmpRow('Strategy', fmtRet(agg.strategy_return_pct)));
    rows.push(betaCmpRow('SPY', fmtRet(agg.spy_return_pct)));
    rows.push(betaCmpRow('Money-market', fmtRet(agg.mm_return_pct)));
    rows.push('  </div>');
  } else {
    rows.push('  <div style="font-size:12px;color:var(--muted);margin-top:10px;">Aggregate vs SPY / money-market appears once a position closes.</div>');
  }
  rows.push('</div>');
  return rows.join('\n');
}

function betaCmpRow(label, ret) {
  return '<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;">' +
         '<span style="font-size:13px;color:var(--muted);">' + label + '</span>' +
         '<span class="ret-badge ' + ret.cls + '" style="font-size:14px;">' + ret.str + '</span>' +
         '</div>';
}

// Side-by-side "vs SPY / vs money-market" block shared by open + closed cards.
function betaVsBlock(spy, mm, vsSpy, vsMm) {
  return [
    '  <div style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px;">',
    '    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">',
    '      <div><div style="font-size:11px;color:var(--muted);">vs SPY</div>',
    '        <div class="ret-badge ' + vsSpy.cls + '" style="font-size:14px;">' + vsSpy.str + '</div></div>',
    '      <div><div style="font-size:11px;color:var(--muted);">vs Money-mkt</div>',
    '        <div class="ret-badge ' + vsMm.cls + '" style="font-size:14px;">' + vsMm.str + '</div></div>',
    '    </div>',
    '    <div style="margin-top:6px;font-size:11px;color:var(--muted);">Same period &mdash; SPY ' +
         '<span style="color:var(--text);font-family:monospace;">' + spy.str + '</span> &middot; MM ' +
         '<span style="color:var(--text);font-family:monospace;">' + mm.str + '</span></div>',
    '  </div>'
  ].join('\n');
}

function betaOpenCardHTML(p) {
  var r    = fmtRet(p.return_pct);
  var prog = Math.min(100, Math.round(((p.days_held || 0) / 252) * 100));
  return [
    '<div class="card">',
    '  <div class="pos-header">',
    '    <div class="pos-ticker">' + escHtml(p.ticker) + '</div>',
    '    <div class="ret-badge ' + r.cls + '">' + r.str + '</div>',
    '  </div>',
    '  <div class="pos-meta">Entry <b>$' + fmt(p.entry_price, 2) + '</b> on <b>' + p.entry_date + '</b></div>',
    '  <div style="margin-top:10px;" class="bar-row">',
    '    <div class="bar-track" style="height:6px;"><div class="bar-fill bar-fill-blue" style="width:' + prog + '%;height:6px;"></div></div>',
    '  </div>',
    '  <div class="prog-label">Day ' + (p.days_held || 0) + ' of 252 &mdash; ' + (p.days_remaining || 0) + ' days remaining</div>',
    betaVsBlock(fmtRet(p.spy_return_pct), fmtRet(p.mm_return_pct), fmtRet(p.vs_spy_pct), fmtRet(p.vs_mm_pct)),
    '</div>'
  ].join('\n');
}

function betaClosedCardHTML(p) {
  var r = fmtRet(p.return_pct);
  return [
    '<div class="card">',
    '  <div class="pos-header">',
    '    <div class="pos-ticker">' + escHtml(p.ticker) +
       ' <span style="font-size:11px;color:var(--muted);font-weight:400;">closed</span></div>',
    '    <div class="ret-badge ' + r.cls + '">' + r.str + '</div>',
    '  </div>',
    '  <div class="pos-meta"><b>' + p.entry_date + '</b> &rarr; <b>' + p.exit_date + '</b>' +
       ' &nbsp;&middot;&nbsp; ' + (p.days_held || 0) + ' days held</div>',
    betaVsBlock(fmtRet(p.spy_return_pct), fmtRet(p.mm_return_pct), fmtRet(p.vs_spy_pct), fmtRet(p.vs_mm_pct)),
    '</div>'
  ].join('\n');
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

// ── Simulator ─────────────────────────────────────────────────────────────────
var _simResultA = null;
var _simResultB = null;
var _taxMode = localStorage.getItem('tax_mode') || 'none';

function setTaxMode(val) {
  _taxMode = val;
  localStorage.setItem('tax_mode', val);
  if (_simResultA) document.getElementById('sim-results-A').innerHTML = renderSimResults(_simResultA, 'A');
  if (_simResultB) document.getElementById('sim-results-B').innerHTML = renderSimResults(_simResultB, 'B');
  if (_simResultA && _simResultB) renderComparison();
}

function computeIsraelTax(data) {
  var trades   = data.trades || [];
  var s        = data.summary;
  var params   = data.params;
  var INITIAL  = 100000;

  // Only tax closed trades (exclude open_at_end)
  var yearlyGains  = {};
  var yearlyLosses = {};
  trades.forEach(function (t) {
    if (t.exit_reason === 'open_at_end') return;
    var year = (t.exit_date || '').substring(0, 4);
    if (!year) return;
    var pnl = t.pnl_usd || 0;
    if (!yearlyGains[year])  yearlyGains[year]  = 0;
    if (!yearlyLosses[year]) yearlyLosses[year] = 0;
    if (pnl > 0) yearlyGains[year]  += pnl;
    else         yearlyLosses[year] += pnl;
  });

  var totalTax = 0;
  Object.keys(yearlyGains).forEach(function (year) {
    var net = Math.max(0, (yearlyGains[year] || 0) + (yearlyLosses[year] || 0));
    totalTax += net * 0.25;
  });

  var finalPortfolio    = s.final_portfolio;
  var afterTaxPortfolio = finalPortfolio - totalTax;
  var afterTaxReturnPct = (afterTaxPortfolio / INITIAL - 1) * 100;

  var years = Math.max(1, (new Date(params.end_date) - new Date(params.start_date)) / (365.25 * 86400000));
  var afterTaxCagr = (Math.pow(Math.max(0.01, afterTaxPortfolio / INITIAL), 1 / years) - 1) * 100;

  return {
    totalTax:         Math.round(totalTax),
    afterTaxPortfolio: Math.round(afterTaxPortfolio),
    afterTaxReturnPct: afterTaxReturnPct,
    afterTaxCagr:      afterTaxCagr,
  };
}

function toggleTpInput(inputId, checkbox) {
  var el = document.getElementById(inputId);
  if (el) el.disabled = !checkbox.checked;
}

function _getSimParams(suffix) {
  suffix = suffix || '';
  var etName    = 'entry_threshold' + (suffix ? '_' + suffix : '');
  var emName    = 'exit_mode'       + (suffix ? '_' + suffix : '');
  var tpEnId    = 'tp-enable'  + (suffix ? '-' + suffix : '');
  var tpValId   = 'tp-pct'     + (suffix ? '-' + suffix : '');
  var slEnId    = 'sl-enable'  + (suffix ? '-' + suffix : '');
  var slValId   = 'sl-pct'     + (suffix ? '-' + suffix : '');
  var tsEnId    = 'ts-enable'  + (suffix ? '-' + suffix : '');
  var tsValId   = 'ts-pct'     + (suffix ? '-' + suffix : '');
  var etEl      = document.querySelector('input[name="' + etName + '"]:checked');
  var emEl      = document.querySelector('input[name="' + emName + '"]:checked');
  var tpEnabled = (document.getElementById(tpEnId) || {}).checked;
  var tpVal     = tpEnabled ? parseFloat((document.getElementById(tpValId) || {}).value || 30) : 0;
  var slEnabled = (document.getElementById(slEnId) || {}).checked;
  var slVal     = slEnabled ? parseFloat((document.getElementById(slValId) || {}).value || 20) : 0;
  var tsEnabled = (document.getElementById(tsEnId) || {}).checked;
  var tsVal     = tsEnabled ? parseFloat((document.getElementById(tsValId) || {}).value || 25) : 0;
  var et        = etEl ? parseFloat(etEl.value) : 0.80;
  var em        = emEl ? emEl.value : '252d_only';
  var ps        = parseFloat((document.getElementById('pos-size-slider') || {}).value || 10);
  var sd        = (document.getElementById('sim-start-date') || {}).value || '2018-01-01';
  var ed        = (document.getElementById('sim-end-date')   || {}).value || '2026-06-12';
  var exv       = parseFloat((document.getElementById('exit-thresh-val') || {}).value || 0.40);
  return {
    entry_threshold:   et,
    exit_threshold:    exv,
    exit_mode:         em,
    take_profit_pct:   tpVal,
    stop_loss_pct:      slVal,
    trailing_stop_pct:  tsVal,
    position_size_pct:  ps,
    max_positions:     10,
    start_date:        sd,
    end_date:          ed,
  };
}

function runSimulation(scenario) {
  scenario = scenario || 'A';
  var suffix = scenario === 'B' ? 'B' : '';
  var params = _getSimParams(suffix);

  // Validate
  if (params.exit_mode !== '252d_only' && params.exit_threshold >= params.entry_threshold) {
    showToast('Exit threshold must be lower than entry threshold'); return;
  }
  if (new Date(params.end_date) <= new Date(params.start_date)) {
    showToast('End date must be after start date'); return;
  }
  if (params.start_date < '2010-01-01') {
    showToast('Start date cannot be before 2010 — our universe has reliable data from 2010 onwards'); return;
  }

  var btn = document.getElementById('sim-run-btn');
  var rContainer = document.getElementById('sim-results-' + scenario);
  if (!rContainer) return;

  if (btn && scenario === 'A') btn.disabled = true;
  rContainer.innerHTML = [
    '<div class="card" style="text-align:center;padding:28px 20px;">',
    '  <div style="font-size:28px;margin-bottom:10px;">&#8987;</div>',
    '  <div style="font-size:15px;font-weight:600;margin-bottom:6px;">Running simulation&hellip;</div>',
    '  <div style="font-size:13px;color:var(--muted);">Scanning the Top-100 universe across ' +
         Math.round((new Date(params.end_date) - new Date(params.start_date)) / (365.25 * 86400000)) +
         ' years of data.</div>',
    '  <div style="font-size:12px;color:var(--muted);margin-top:6px;">This takes 10&ndash;30 seconds on first run.</div>',
    '</div>'
  ].join('\n');

  fetch('/api/backtest', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(params),
  })
  .then(function (r) {
    if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Simulation failed'); });
    return r.json();
  })
  .then(function (data) {
    if (scenario === 'A') {
      _simResultA = data;
    } else {
      _simResultB = data;
    }
    rContainer.innerHTML = renderSimResults(data, scenario);
    if (btn && scenario === 'A') btn.disabled = false;
    // Show add-scenario button after first result
    var addBtn = document.getElementById('sim-add-scenario');
    if (addBtn && scenario === 'A') addBtn.style.display = '';
    // Show comparison if both results exist
    if (_simResultA && _simResultB) renderComparison();
  })
  .catch(function (err) {
    rContainer.innerHTML = '<div class="err-box">&#9888;&#65039; ' + escHtml(err.message) + '</div>';
    if (btn && scenario === 'A') btn.disabled = false;
  });
}

function addScenario() {
  var scB = document.getElementById('sim-scenario-B');
  var addBtn = document.getElementById('sim-add-scenario');
  if (scB) scB.style.display = '';
  if (addBtn) addBtn.style.display = 'none';
}

function renderSimResults(data, scenario) {
  if (!data || !data.params || !data.summary) {
    return '<div class="err-box">&#9888;&#65039; Unexpected result format — try running the simulation again.</div>';
  }
  var s = data.summary;
  var params = data.params;
  scenario = scenario || 'A';

  var exitLabel = {
    '252d_only':        'Hold 12 months',
    'threshold_or_252d': 'Threshold or 12m',
    'threshold_only':   'Threshold exit',
  }[params.exit_mode] || params.exit_mode;
  if (params.take_profit_pct && params.take_profit_pct > 0) {
    exitLabel += ' · TP +' + fmt(params.take_profit_pct, 0) + '%';
  }
  if (params.stop_loss_pct && params.stop_loss_pct > 0) {
    exitLabel += ' · SL −' + fmt(params.stop_loss_pct, 0) + '%';
  }
  if (params.trailing_stop_pct && params.trailing_stop_pct > 0) {
    exitLabel += ' · TS −' + fmt(params.trailing_stop_pct, 0) + '% from peak';
  }

  var hasOpen   = s.final_portfolio !== s.final_portfolio_realized;
  var beatLabel = hasOpen
    ? (s.beat_spy === true  ? '&#9989; Beat S&P 500 (incl. open)'  :
       s.beat_spy === false ? '&#10060; Underperformed S&P 500 (incl. open)' : '')
    : (s.beat_spy === true  ? '&#9989; Beat S&P 500'  :
       s.beat_spy === false ? '&#10060; Underperformed S&P 500' : '');
  var realizedBeatHtml = (hasOpen && s.beat_spy_realized !== undefined && s.beat_spy_realized !== s.beat_spy)
    ? (s.beat_spy_realized === true
        ? ' &nbsp;<span class="beat-badge beat-yes" style="font-size:11px;">&#9989; Realized also beats</span>'
        : ' &nbsp;<span class="beat-badge beat-no"  style="font-size:11px;">&#10060; Realized does not beat</span>')
    : '';
  var beatHtml = beatLabel
    ? '<span class="beat-badge ' + (s.beat_spy ? 'beat-yes' : 'beat-no') + '">' + beatLabel + '</span>' + realizedBeatHtml
    : '';

  var cmpGrid = '';
  if (data.spy_comparison.final_spy) {
    var hasOpen    = s.final_portfolio !== s.final_portfolio_realized;
    var spyCls     = s.spy_total_return_pct >= 0 ? 'ret-pos' : 'ret-neg';
    var botRetCls  = s.total_return_pct    >= 0 ? 'ret-pos' : 'ret-neg';
    var realRetCls = (s.total_return_realized_pct >= 0) ? 'ret-pos' : 'ret-neg';

    // Tax column
    var taxColHtml = '';
    if (_taxMode === 'israel_25') {
      var taxData = computeIsraelTax(data);
      var atCls = taxData.afterTaxReturnPct >= 0 ? 'ret-pos' : 'ret-neg';
      taxColHtml = [
        '  <div class="cmp-card" style="border:1px solid var(--accent);border-radius:8px;">',
        '    <div class="cmp-label">After Tax (IL 25%)</div>',
        '    <div class="cmp-val">$' + fmtK(taxData.afterTaxPortfolio) + '</div>',
        '    <div class="cmp-sub ' + atCls + '">' + (taxData.afterTaxReturnPct >= 0 ? '+' : '') + fmt(taxData.afterTaxReturnPct, 1) + '%</div>',
        '    <div class="cmp-sub">' + fmt(taxData.afterTaxCagr, 1) + '% / yr</div>',
        '  </div>',
      ].join('\n');
    }

    var numCols = (hasOpen ? 3 : 2) + (_taxMode === 'israel_25' ? 1 : 0);
    var gridClass = numCols >= 4 ? 'sim-cmp-4col' : numCols === 3 ? 'sim-cmp-3col' : '';
    cmpGrid = [
      '<div class="sim-cmp-grid ' + gridClass + '" style="margin-top:12px;">',
      '  <div class="cmp-card">',
      '    <div class="cmp-label">Bot (incl. open)</div>',
      '    <div class="cmp-val">$' + fmtK(s.final_portfolio) + '</div>',
      '    <div class="cmp-sub ' + botRetCls + '">' + (s.total_return_pct >= 0 ? '+' : '') + fmt(s.total_return_pct, 1) + '%</div>',
      '    <div class="cmp-sub">' + fmt(s.cagr, 1) + '% / yr</div>',
      '  </div>',
      (hasOpen ? [
        '  <div class="cmp-card">',
        '    <div class="cmp-label">Bot (realized)</div>',
        '    <div class="cmp-val">$' + fmtK(s.final_portfolio_realized) + '</div>',
        '    <div class="cmp-sub ' + realRetCls + '">' + (s.total_return_realized_pct >= 0 ? '+' : '') + fmt(s.total_return_realized_pct, 1) + '%</div>',
        '    <div class="cmp-sub">' + fmt(s.cagr_realized, 1) + '% / yr</div>',
        '  </div>',
      ].join('\n') : ''),
      taxColHtml,
      '  <div class="cmp-card">',
      '    <div class="cmp-label">S&P 500 (SPY)</div>',
      '    <div class="cmp-val">$' + fmtK(data.spy_comparison.final_spy) + '</div>',
      '    <div class="cmp-sub ' + spyCls + '">' + (s.spy_total_return_pct >= 0 ? '+' : '') + fmt(s.spy_total_return_pct, 1) + '%</div>',
      '    <div class="cmp-sub">' + fmt(s.spy_cagr, 1) + '% / yr</div>',
      '  </div>',
      '</div>',
    ].join('\n');
  }

  var yearRows = (data.yearly || []).map(function (y) {
    var pRet = y.portfolio_return;
    var sRet = y.spy_return;
    var beat  = (pRet !== null && sRet !== null) ? (pRet >= sRet ? '&#9989;' : '&#10060;') : '';
    var pCls  = (pRet !== null && pRet >= 0) ? 'ret-pos' : 'ret-neg';
    return [
      '<div class="yr-row">',
      '  <span class="yr-year">' + y.year + '</span>',
      '  <span class="yr-bot ' + pCls + '">' + (pRet !== null ? (pRet >= 0 ? '+' : '') + fmt(pRet, 1) + '%' : '—') + '</span>',
      '  <span class="yr-spy">' + (sRet !== null ? (sRet >= 0 ? '+' : '') + fmt(sRet, 1) + '%' : '—') + '</span>',
      '  <span class="yr-tick">' + beat + '</span>',
      '</div>',
    ].join('\n');
  }).join('\n');

  // Split open vs closed trades
  var allTrades    = data.trades || [];
  var endDate      = new Date(params.end_date);
  var today        = new Date();
  var isRecentEnd  = (today - endDate) / (1000 * 60 * 60 * 24) <= 60;
  var openTrades   = allTrades.filter(function (t) { return t.exit_reason === 'open_at_end'; });
  var closedTrades = allTrades.filter(function (t) { return t.exit_reason !== 'open_at_end'; });

  // Open positions section (only when sim end is near today)
  var openSection = '';
  if (isRecentEnd && openTrades.length > 0) {
    openSection = [
      '<div class="section-title">Currently Open Positions (' + openTrades.length + ')</div>',
      '<div class="card">',
      openTrades.map(function (t) { return tradeRowHTML(t, true); }).join('\n'),
      '<div class="disclaimer" style="margin-top:8px;">Unrealized returns as of ' + escHtml(params.end_date) + '. Not closed yet.</div>',
      '</div>',
    ].join('\n');
  } else if (openTrades.length > 0) {
    // Sim ended in the past — show open_at_end trades inline with a note
    closedTrades = allTrades; // treat all as closed with labels
  }

  // Closed trades section — show first 12, toggle for rest
  var SHOW_FIRST   = 12;
  var closedHtml   = closedTrades.slice(0, SHOW_FIRST).map(function (t) {
    return tradeRowHTML(t, !isRecentEnd && t.exit_reason === 'open_at_end');
  }).join('\n');
  var moreBtn      = '';
  var hiddenHtml   = '';
  if (closedTrades.length > SHOW_FIRST) {
    hiddenHtml = '<div id="sim-all-trades-' + scenario + '" style="display:none;">' +
      closedTrades.slice(SHOW_FIRST).map(function (t) {
        return tradeRowHTML(t, !isRecentEnd && t.exit_reason === 'open_at_end');
      }).join('\n') + '</div>';
    moreBtn = '<button class="btn btn-ghost btn-sm" style="margin-top:10px;" ' +
      'onclick="toggleAllTrades(\'' + scenario + '\')">Show all ' + closedTrades.length + ' trades &#9660;</button>';
  }

  var closedTitle = isRecentEnd
    ? 'Closed Trades (' + closedTrades.length + ')'
    : 'All Trades (' + allTrades.length + (openTrades.length > 0 ? ', ' + openTrades.length + ' open at end' : '') + ')';

  var taxHtml = '';
  if (_taxMode === 'israel_25') {
    var tax = computeIsraelTax(data);
    var preTaxRet  = s.total_return_pct;
    var preTaxCagr = s.cagr;
    var atCls = tax.afterTaxReturnPct >= 0 ? 'ret-pos' : 'ret-neg';
    taxHtml = [
      '<div class="section-title">Israel Tax Estimate (25%)</div>',
      '<div class="card">',
      detailRow('Pre-tax return',    (preTaxRet  >= 0 ? '+' : '') + fmt(preTaxRet,  1) + '%'),
      detailRow('Pre-tax CAGR',      fmt(preTaxCagr, 1) + '% / yr'),
      detailRow('Tax paid (est.)',   '&minus;$' + fmtK(tax.totalTax)),
      detailRow('After-tax portfolio', '$' + fmtK(tax.afterTaxPortfolio)),
      '<div class="about-row">',
      '  <span>After-tax return</span>',
      '  <span class="about-val ' + atCls + '" style="font-family:monospace;">' +
          (tax.afterTaxReturnPct >= 0 ? '+' : '') + fmt(tax.afterTaxReturnPct, 1) + '%</span>',
      '</div>',
      detailRow('After-tax CAGR',    fmt(tax.afterTaxCagr, 1) + '% / yr'),
      '<div class="disclaimer" style="margin-top:12px;font-size:11px;">',
      '  &#9888;&#65039; Tax estimate only. Based on 25% Israel capital gains rate with annual loss offset. ',
      '  Does not account for currency gains, inflation adjustment (infl. linkage), or personal tax bracket. ',
      '  Consult a tax advisor.',
      '</div>',
      '</div>',
    ].join('\n');
  }

  return [
    '<div class="section-title" style="margin-top:16px;">Results &mdash; Entry &ge;' + params.entry_threshold + ' | ' + exitLabel + '</div>',

    cmpGrid,
    '<div style="margin:8px 0;">' + beatHtml + '</div>',

    '<div class="card">',
    '  <div class="subsection" style="margin-top:0;">Details</div>',
    detailRow('Signals fired',    s.n_signals),
    detailRow('Trades opened',    s.n_trades),
    detailRow('Avg hold',         fmt(s.avg_hold_days, 0) + ' days'),
    detailRow('Win rate',         fmt(s.pct_positive, 0) + '%'),
    detailRow('Avg return/trade', (s.mean_return_pct >= 0 ? '+' : '') + fmt(s.mean_return_pct, 1) + '%'),
    detailRow('Time in market',   fmt(s.pct_time_invested, 0) + '%'),
    detailRow('Avg capital util', fmt(s.avg_capital_utilization, 0) + '%'),
    (s.n_missed_capital > 0 ? detailRow('Missed (no capital)', s.n_missed_capital + ' signal' + (s.n_missed_capital === 1 ? '' : 's')) : ''),
    (s.pct_stop_loss     > 0 ? detailRow('Stopped out (SL)',       fmt(s.pct_stop_loss, 0)     + '%') : ''),
    (s.pct_trailing_stop > 0 ? detailRow('Trailing stop hit',      fmt(s.pct_trailing_stop, 0) + '%') : ''),
    (s.pct_take_profit > 0 ? detailRow('Exited via TP',    fmt(s.pct_take_profit, 0) + '%') : ''),
    detailRow('Max drawdown',     fmt(s.max_drawdown_pct, 1) + '%'),
    detailRow('Sharpe ratio',     fmt(s.sharpe, 2)),
    (s.best_year  ? detailRow('Best year',  s.best_year.year  + '  ' + (s.best_year.return_pct  >= 0 ? '+' : '') + s.best_year.return_pct  + '%') : ''),
    (s.worst_year ? detailRow('Worst year', s.worst_year.year + '  ' + (s.worst_year.return_pct >= 0 ? '+' : '') + s.worst_year.return_pct + '%') : ''),
    '</div>',

    '<div class="section-title">Year by Year</div>',
    '<div class="card">',
    '  <div class="yr-row" style="border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:2px;">',
    '    <span class="yr-year" style="color:var(--muted);">Year</span>',
    '    <span class="yr-bot"  style="color:var(--muted);">Bot</span>',
    '    <span class="yr-spy"  style="color:var(--muted);">SPY</span>',
    '    <span class="yr-tick"></span>',
    '  </div>',
    yearRows,
    '</div>',

    openSection,

    '<div class="section-title">' + closedTitle + '</div>',
    '<div class="card">',
    closedHtml,
    hiddenHtml,
    moreBtn,
    '</div>',

    taxHtml,

    (function() {
      var missed = data.missed_capital || [];
      if (!missed.length) return '';
      var rows = missed.map(function(m) {
        return [
          '<div class="trade-row">',
          '  <div class="tr-main">',
          '    <span class="tr-ticker">' + escHtml(m.ticker) + '</span>',
          '    <span style="font-size:11px;color:var(--amber);">no capital</span>',
          '  </div>',
          '  <div class="tr-sub">',
          '    <span class="tr-dates">' + escHtml(m.date) + ' &middot; score ' + fmt(m.composite, 2) +
               ' &middot; needed $' + fmtK(m.needed) + ', had $' + fmtK(m.cash_avail) + '</span>',
          '  </div>',
          '</div>',
        ].join('\n');
      }).join('\n');
      return [
        '<div class="section-title">Missed Signals — No Capital (' + missed.length + ')</div>',
        '<div class="card">',
        rows,
        '<div class="disclaimer" style="margin-top:8px;">Signal fired but portfolio was fully allocated. Position not opened.</div>',
        '</div>',
      ].join('\n');
    })(),

    '<div class="sim-disclaimer" style="margin-top:12px;">',
    '  &#9888;&#65039; Historical simulation on the same data used to build the signal. ',
    '  Results are optimistically biased. Past performance does not guarantee future results. ',
    '  Not investment advice.',
    '</div>',
  ].join('\n');
}

function detailRow(label, val) {
  return [
    '<div class="about-row">',
    '  <span>' + label + '</span>',
    '  <span class="about-val" style="color:var(--text);font-family:monospace;">' + escHtml(String(val)) + '</span>',
    '</div>'
  ].join('\n');
}

function tradeRowHTML(t, isOpen) {
  var cls    = t.return_pct >= 0 ? 'ret-pos' : 'ret-neg';
  var retStr = (t.return_pct >= 0 ? '+' : '') + fmt(t.return_pct, 1) + '%';
  var retLabel = retStr + (isOpen ? ' <span style="font-size:10px;font-weight:400;color:var(--muted);">unrealized</span>' : '');

  var entryPx = '$' + fmt(t.entry_price, 2);
  var exitPx  = '$' + fmt(t.exit_price,  2);
  var pricesStr = entryPx + ' &rarr; ' + exitPx + (isOpen ? ' <span style="color:var(--muted);">(now)</span>' : '');

  var entryMo = (t.entry_date || '').substring(0, 10);
  var exitMo  = (t.exit_date  || '').substring(0, 10);
  var datesStr;
  if (isOpen) {
    datesStr = 'entered ' + entryMo + ' &middot; ' + t.hold_days + 'd held';
  } else {
    var reasonTag = '';
    if (t.exit_reason === 'take_profit')    reasonTag = ' &middot; TP &#9650;';
    else if (t.exit_reason === 'trailing_stop') reasonTag = ' &middot; TS &#9660;';
    else if (t.exit_reason === 'stop_loss')     reasonTag = ' &middot; SL &#9660;';
    else if (t.exit_reason === 'threshold')     reasonTag = ' &middot; signal exit';
    datesStr = entryMo + ' &rarr; ' + exitMo + ' &middot; ' + t.hold_days + 'd' + reasonTag;
  }

  return [
    '<div class="trade-row' + (isOpen ? ' tr-open-pos' : '') + '">',
    '  <div class="tr-main">',
    '    <span class="tr-ticker">' + t.ticker + '</span>',
    (isOpen ? '    <span class="open-badge">OPEN</span>' : ''),
    '    <span class="tr-ret ' + cls + '">' + retLabel + '</span>',
    '  </div>',
    '  <div class="tr-sub">',
    '    <span class="tr-prices">' + pricesStr + '</span>',
    '    <span class="tr-dates">' + datesStr + '</span>',
    '  </div>',
    '</div>'
  ].join('\n');
}

function toggleAllTrades(scenario) {
  var el = document.getElementById('sim-all-trades-' + scenario);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

function renderComparison() {
  var cmp = document.getElementById('sim-compare');
  if (!cmp || !_simResultA || !_simResultB) return;
  var a = _simResultA.summary;
  var b = _simResultB.summary;
  var pa = _simResultA.params;
  var pb = _simResultB.params;

  cmp.style.display = '';
  cmp.innerHTML = [
    '<div class="section-title">Side-by-Side Comparison</div>',
    '<div class="sim-cmp-grid">',
    '  <div class="cmp-card">',
    '    <div class="cmp-label">Scenario A &mdash; Entry ' + pa.entry_threshold + '</div>',
    '    <div class="cmp-val">$' + fmtK(a.final_portfolio) + '</div>',
    '    <div class="cmp-sub ret-pos">+' + fmt(a.total_return_pct, 1) + '%</div>',
    '    <div class="cmp-sub">' + fmt(a.cagr, 1) + '% / yr</div>',
    '    <div class="cmp-sub">' + a.n_trades + ' trades &nbsp;&middot;&nbsp; ' + fmt(a.pct_positive, 0) + '% wins</div>',
    '  </div>',
    '  <div class="cmp-card">',
    '    <div class="cmp-label">Scenario B &mdash; Entry ' + pb.entry_threshold + '</div>',
    '    <div class="cmp-val">$' + fmtK(b.final_portfolio) + '</div>',
    '    <div class="cmp-sub ret-pos">+' + fmt(b.total_return_pct, 1) + '%</div>',
    '    <div class="cmp-sub">' + fmt(b.cagr, 1) + '% / yr</div>',
    '    <div class="cmp-sub">' + b.n_trades + ' trades &nbsp;&middot;&nbsp; ' + fmt(b.pct_positive, 0) + '% wins</div>',
    '  </div>',
    '</div>',
    (_simResultA.spy_comparison.final_spy
      ? '<div style="text-align:center;margin-top:10px;font-size:12px;color:var(--muted);">SPY: $' +
        fmtK(_simResultA.spy_comparison.final_spy) + ' (+' +
        fmt(_simResultA.summary.spy_total_return_pct, 1) + '%)</div>'
      : ''),
  ].join('\n');
}

function fmtK(n) {
  if (n === null || n === undefined) return '—';
  var v = Math.round(Number(n));
  if (v >= 1000000) return (v / 1000000).toFixed(2) + 'M';
  if (v >= 1000)    return (v / 1000).toFixed(0) + 'K';
  return String(v);
}

// ── Settings ──────────────────────────────────────────────────────────────────

function loadSettings() {
  var hEl = document.getElementById('settings-horizon');
  var mEl = document.getElementById('settings-mode');
  var th  = localStorage.getItem('user_time_horizon') || '6_to_12m';
  var m   = localStorage.getItem('user_mode') || 'fresh';
  if (m === 'portfolio') m = 'existing';
  if (hEl) hEl.value = th;
  if (mEl) mEl.value = m;

  // Show/hide portfolio section based on mode
  var isExisting = (m === 'existing' || m === 'both');
  var sec = document.getElementById('settings-portfolio-section');
  if (sec) {
    sec.style.display = isExisting ? '' : 'none';
    if (isExisting) loadSettingsPortfolio();
  }
}

function loadSettingsPortfolio() {
  var ctr = document.getElementById('settings-portfolio-list');
  if (!ctr) return;
  fetch('/api/portfolio')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var holdings = data.holdings || [];
      if (!holdings.length) {
        ctr.innerHTML = '<div style="font-size:13px;color:var(--muted);padding:4px 0;">No holdings yet. Add them in the Portfolio tab.</div>';
        return;
      }
      ctr.innerHTML = holdings.map(function (h) {
        var retStr = h.current_return_pct !== null && h.current_return_pct !== undefined
          ? ' <span style="font-family:monospace;font-size:12px;color:' + (h.current_return_pct >= 0 ? 'var(--green)' : 'var(--red)') + ';">'
            + (h.current_return_pct >= 0 ? '+' : '') + h.current_return_pct.toFixed(1) + '%</span>'
          : '';
        return '<div class="about-row" style="align-items:center;">'
          + '<span style="font-weight:600;">' + escHtml(h.ticker) + retStr + '</span>'
          + '<button class="btn btn-danger btn-sm" onclick="settingsRemoveHolding(\'' + escHtml(h.ticker) + '\')">Remove</button>'
          + '</div>';
      }).join('');
    })
    .catch(function () {
      if (ctr) ctr.innerHTML = '<div style="font-size:12px;color:var(--muted);">Could not load portfolio.</div>';
    });
}

function settingsRemoveHolding(ticker) {
  if (!confirm('Remove ' + ticker + ' from portfolio?')) return;
  _portfolio = _portfolio.filter(function (h) { return h.ticker !== ticker; });
  // If _portfolio is empty (not loaded yet), fetch first then remove
  fetch('/api/portfolio')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var remaining = (data.holdings || []).filter(function (h) { return h.ticker !== ticker; });
      return fetch('/api/portfolio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ holdings: remaining }),
      });
    })
    .then(function () {
      loadSettingsPortfolio();
      showToast(ticker + ' removed');
    })
    .catch(function () { showToast('Failed to remove'); });
}

function saveTimeHorizon(val) {
  localStorage.setItem('user_time_horizon', val);
  showToast('Time horizon updated');
}

function saveMode(val) {
  localStorage.setItem('user_mode', val);
  _userMode = val;
  updateTabBar();
  showToast('Mode updated');
  // Show/hide portfolio section in settings
  var isExisting = (val === 'existing' || val === 'both');
  var sec = document.getElementById('settings-portfolio-section');
  if (sec) {
    sec.style.display = isExisting ? '' : 'none';
    if (isExisting) loadSettingsPortfolio();
  }
}

function resetOnboarding() {
  if (!confirm('Restart onboarding?\nThis will clear your preferences.')) return;
  localStorage.removeItem('onboarding_complete');
  localStorage.removeItem('user_mode');
  localStorage.removeItem('user_time_horizon');
  localStorage.removeItem('first_signal_seen');
  location.reload();
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  var phTicker = document.getElementById('ph-ticker');
  if (phTicker) {
    phTicker.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') addHolding();
    });
  }

  // Restore tax mode radio from localStorage
  var savedTax = localStorage.getItem('tax_mode') || 'none';
  var taxRadio = document.querySelector('input[name="tax_mode"][value="' + savedTax + '"]');
  if (taxRadio) taxRadio.checked = true;
});
