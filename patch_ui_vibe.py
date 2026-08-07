"""Patch DockLlama UI to address vibe-coded app complaints.
Changes:
1. Font upgrade: IBM Plex Sans + JetBrains Mono
2. Typography hierarchy: 3-tier type scale
3. Flatten nested card backgrounds
4. Priority-based card treatment (unhealthy emphasis, healthy compact)
5. Spacing system audit (8px grid)
6. Dashboard summary banner
"""

CSS_PATH = "/home/o51r15/scripts/dockmon/frontend/common.css"
INDEX_PATH = "/home/o51r15/scripts/dockmon/frontend/index.html"

# ── 1. COMMON.CSS OVERHAUL ──────────────────────────────────────────

with open(CSS_PATH) as f:
    css = f.read()

# Replace the entire CSS with the improved version
new_css = """/* DockLlama Shared Styles */
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #0f1117; --surface: #1a1d27; --surface2: #242834;
  --border: #2e3345; --text: #e4e6ed; --text-dim: #8b8fa3;
  --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6;
  --orange: #f97316;
  /* Spacing scale (8px base) */
  --sp-xs: 4px; --sp-sm: 8px; --sp-md: 16px; --sp-lg: 24px; --sp-xl: 32px; --sp-2xl: 48px;
  /* Font families */
  --font-body: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
}
:root.light {
  --bg: #f5f6fa; --surface: #ffffff; --surface2: #eef0f5;
  --border: #d1d5db; --text: #1a1d27; --text-dim: #6b7280;
  --green: #16a34a; --yellow: #ca8a04; --red: #dc2626; --blue: #2563eb;
  --orange: #ea580c;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:var(--font-body); font-size:14px; line-height:1.5; }

/* ── Typography Hierarchy ── */
.page-title { font-size:1.5rem; font-weight:700; letter-spacing:-0.02em; }
.section-title { font-size:1.15rem; font-weight:600; margin-bottom:var(--sp-md); }
.mono { font-family:var(--font-mono); }

/* Header */
.header { padding:var(--sp-md) var(--sp-lg); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
.header h1 { font-size:1.4rem; font-weight:600; }
.header h1 span { color:var(--blue); }
.header-right { display:flex; gap:var(--sp-md); align-items:center; }

/* Nav */
nav { padding:var(--sp-sm) var(--sp-lg); border-bottom:1px solid var(--border); display:flex; gap:var(--sp-sm); flex-wrap:wrap; }
nav a { color:var(--text-dim); text-decoration:none; padding:var(--sp-xs) var(--sp-sm); border-radius:6px; font-size:0.85rem; font-weight:500; transition:all 0.15s; }
nav a.active { color:var(--text); background:var(--surface2); font-weight:600; }
nav a:hover { color:var(--text); background:var(--surface2); }

/* Layout */
.layout { display:flex; min-height:calc(100vh - 110px); }
.sidebar { width:200px; border-right:1px solid var(--border); padding:var(--sp-md) 0; flex-shrink:0; }
.sidebar a { display:block; padding:var(--sp-sm) var(--sp-lg); color:var(--text-dim); text-decoration:none; font-size:0.85rem; cursor:pointer; font-weight:500; transition:all 0.15s; }
.sidebar a:hover { color:var(--text); background:var(--surface); }
.sidebar a.active { color:var(--text); background:var(--surface2); border-right:2px solid var(--blue); font-weight:600; }

/* Badges */
.badge { padding:0.25rem 0.6rem; border-radius:4px; font-size:0.75rem; font-weight:600; font-family:var(--font-mono); letter-spacing:0.04em; }
.badge-dry { background:#eab30822; color:var(--yellow); }
.badge-live { background:#22c55e22; color:var(--green); }

/* Buttons */
.btn { padding:var(--sp-xs) var(--sp-sm); border-radius:5px; border:1px solid var(--border); background:var(--surface2); color:var(--text); font-size:0.8rem; font-family:var(--font-body); font-weight:500; cursor:pointer; transition:all 0.15s; }
.btn:hover { background:var(--border); }
.btn:disabled { opacity:0.4; cursor:not-allowed; }
.btn-blue { border-color:var(--blue); color:var(--blue); }
.btn-blue:hover { background:var(--blue); color:white; }
.btn-primary { background:var(--blue); border-color:var(--blue); color:#fff; font-size:0.85rem; padding:var(--sp-sm) var(--sp-md); border-radius:6px; font-weight:600; }
.btn-primary:hover { opacity:0.9; }
.btn-primary:disabled { opacity:0.4; cursor:not-allowed; }
.btn-eval { border-color:var(--blue); color:var(--blue); }

/* Forms */
select, input[type='text'], input[type='number'], input[type='url'] {
  background:var(--surface); border:1px solid var(--border); color:var(--text);
  padding:var(--sp-sm) 0.75rem; border-radius:6px; font-size:0.85rem; font-family:var(--font-body);
}
select:focus, input:focus { outline:none; border-color:var(--blue); }
textarea { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:var(--sp-sm); border-radius:6px; font-size:0.85rem; font-family:var(--font-mono); width:100%; resize:vertical; }

/* Status dots */
.status-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.dot-green { background:var(--green); box-shadow:0 0 6px var(--green); }
.dot-yellow { background:var(--yellow); box-shadow:0 0 6px var(--yellow); }
.dot-red { background:var(--red); box-shadow:0 0 6px var(--red); }
.dot-gray { background:var(--text-dim); }
.dot-orange { background:var(--orange); box-shadow:0 0 6px var(--orange); }
.hc-dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-left:4px; }
.hc-dot-green { background:var(--green); box-shadow:0 0 4px var(--green); }
.hc-dot-yellow { background:var(--yellow); box-shadow:0 0 4px var(--yellow); }
.hc-dot-red { background:var(--red); box-shadow:0 0 4px var(--red); }

/* Confidence — use mono for data values */
.confidence { font-weight:600; font-family:var(--font-mono); }
.conf-high { color:var(--green); }
.conf-mid { color:var(--yellow); }
.conf-low { color:var(--red); }

/* Loading */
.loading { text-align:center; padding:var(--sp-2xl); color:var(--text-dim); }

/* Toast */
.toast { position:fixed; bottom:var(--sp-xl); left:50%; transform:translateX(-50%); padding:var(--sp-sm) var(--sp-md); border-radius:8px; font-size:0.85rem; z-index:200; animation:fadeup 0.3s; }
.toast-success { background:var(--green); color:white; }
.toast-error { background:var(--red); color:white; }
@keyframes fadeup { from { opacity:0; transform:translateX(-50%) translateY(10px); } to { opacity:1; transform:translateX(-50%) translateY(0); } }

/* Blackout banner */
.blackout-banner { background:rgba(234,179,8,0.15); border:1px solid var(--yellow); border-radius:8px; padding:var(--sp-sm) var(--sp-md); margin-bottom:var(--sp-md); display:flex; align-items:center; gap:var(--sp-sm); font-size:0.85rem; color:var(--yellow); }
.blackout-banner .bo-icon { font-size:1.1rem; }

/* Offline banner */
.offline-banner { background:rgba(239,68,68,0.15); border:1px solid var(--red); border-radius:8px; padding:var(--sp-sm) var(--sp-md); margin:var(--sp-sm) var(--sp-lg) 0; display:flex; align-items:center; gap:var(--sp-sm); font-size:0.85rem; color:var(--red); }

/* Theme toggle */
.theme-toggle { background:none; border:1px solid var(--border); color:var(--text-dim); padding:0.3rem 0.5rem; border-radius:6px; cursor:pointer; font-size:1rem; line-height:1; }
.theme-toggle:hover { color:var(--text); background:var(--surface2); }

/* Tables */
table { width:100%; border-collapse:collapse; font-size:0.8rem; }
th { text-align:left; padding:var(--sp-sm); color:var(--text-dim); border-bottom:1px solid var(--border); font-weight:500; }
td { padding:var(--sp-sm); border-bottom:1px solid var(--border); }

/* Pagination */
.pagination { display:flex; gap:var(--sp-sm); align-items:center; margin-top:var(--sp-md); justify-content:center; }
.pagination button { padding:0.3rem 0.7rem; border-radius:4px; border:1px solid var(--border); background:var(--surface2); color:var(--text); font-size:0.8rem; cursor:pointer; }
.pagination button:hover { background:var(--border); }
.pagination button:disabled { opacity:0.3; cursor:not-allowed; }
.pagination span { font-size:0.8rem; color:var(--text-dim); }

/* Content sections — used in settings/insights */
.content-section { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:var(--sp-lg); margin-bottom:var(--sp-lg); }
.content-section h2 { font-size:1.15rem; font-weight:600; margin-bottom:var(--sp-xs); }
.content-section p.desc { color:var(--text-dim); font-size:0.85rem; margin-bottom:var(--sp-lg); line-height:1.6; }

/* Mobile */
@media (max-width: 768px) {
  .header { padding:var(--sp-md); flex-wrap:wrap; }
  .header h1 { font-size:1.2rem; }
  nav { padding:var(--sp-sm) var(--sp-md); gap:var(--sp-xs); overflow-x:auto; }
  nav a { font-size:0.8rem; padding:0.3rem 0.6rem; white-space:nowrap; }
  .layout { flex-direction:column; }
  .sidebar { width:100%; border-right:none; border-bottom:1px solid var(--border); display:flex; overflow-x:auto; padding:var(--sp-sm); }
  .sidebar a { padding:var(--sp-xs) var(--sp-sm); white-space:nowrap; border-right:none !important; }
  .sidebar a.active { border-bottom:2px solid var(--blue); border-right:none !important; }
}
@media (max-width: 480px) {
  .header { padding:var(--sp-sm) var(--sp-md); }
  .fleet-bar { padding:0 var(--sp-md); flex-direction:column; }
  .grid { padding:var(--sp-md) !important; grid-template-columns:1fr !important; }
}
"""

with open(CSS_PATH, "w") as f:
    f.write(new_css)

print("✅ common.css updated")

# ── 2. INDEX.HTML OVERHAUL ──────────────────────────────────────────

with open(INDEX_PATH) as f:
    html = f.read()

# 2a. Replace the <style> block with improved version
old_style_start = "<style>"
old_style_end = "</style>"
style_start = html.index(old_style_start)
style_end = html.index(old_style_end, style_start) + len(old_style_end)

new_style = """<style>
/* Dashboard grid */
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:var(--sp-md); padding:var(--sp-lg); }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:var(--sp-lg); transition:border-color 0.2s; }
.card:hover { border-color:var(--text-dim); }
.card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:var(--sp-sm); }
.card-name { font-weight:600; font-size:1rem; font-family:var(--font-mono); }
.card-image { color:var(--text-dim); font-size:0.7rem; font-family:var(--font-mono); margin-bottom:var(--sp-sm); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* Flattened eval info — no nested card background */
.card-eval { padding:var(--sp-sm) 0 0 0; font-size:0.8rem; line-height:1.6; border-top:1px solid var(--border); }
.card-eval .label { color:var(--text-dim); font-size:0.75rem; }
.card-meta { display:flex; justify-content:space-between; margin-top:var(--sp-xs); font-size:0.7rem; color:var(--text-dim); font-family:var(--font-mono); }

/* Priority card accents — unhealthy containers get visual emphasis */
.card-unhealthy { border-left:3px solid var(--red); }
.card-degraded { border-left:3px solid var(--yellow); }
.card-critical { border-left:3px solid var(--red); background:rgba(239,68,68,0.04); }

/* Summary banner */
.summary-banner { display:flex; align-items:center; gap:var(--sp-lg); padding:var(--sp-md) var(--sp-lg); margin:0; background:var(--surface); border-bottom:1px solid var(--border); }
.summary-stat { display:flex; align-items:center; gap:var(--sp-sm); font-size:0.85rem; }
.summary-stat .count { font-family:var(--font-mono); font-weight:700; font-size:1.1rem; }
.summary-stat .count-alert { color:var(--red); }
.summary-stat .count-warn { color:var(--yellow); }
.summary-stat .count-ok { color:var(--green); }
.summary-stat .stat-label { color:var(--text-dim); }
.summary-eval-all { margin-left:auto; }

/* Fleet bar */
.fleet-bar { display:flex; gap:var(--sp-md); padding:var(--sp-md) var(--sp-lg); flex-wrap:wrap; align-items:center; }
.fleet-stat { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:var(--sp-sm) var(--sp-md); display:flex; align-items:center; gap:var(--sp-sm); }
.fleet-stat .fs-value { font-size:1.1rem; font-weight:700; font-family:var(--font-mono); }
.fleet-stat .fs-label { font-size:0.7rem; color:var(--text-dim); }
.fleet-stat .fs-icon { font-size:1rem; }

/* Sparklines */
.card-sparklines { display:flex; gap:0.75rem; margin-top:var(--sp-sm); min-height:40px; }
.sparkline-wrap { flex:1; }
.sparkline-label { font-size:0.65rem; color:var(--text-dim); margin-bottom:0.15rem; }
.sparkline-value { font-size:0.65rem; color:var(--text); font-weight:600; font-family:var(--font-mono); float:right; }
.sparkline svg { width:100%; height:24px; }
.sparkline path { fill:none; stroke-width:1.5; stroke-linecap:round; stroke-linejoin:round; }
.spark-cpu { stroke:#3b82f6; }
.spark-mem { stroke:#8b5cf6; }
.spark-area-cpu { fill:rgba(59,130,246,0.1); stroke:none; }
.spark-area-mem { fill:rgba(139,92,246,0.1); stroke:none; }

/* Detail drawer */
.detail-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100; }
.detail-drawer { position:fixed; top:0; right:0; bottom:0; width:min(600px,90vw); background:var(--bg); border-left:1px solid var(--border); z-index:101; overflow-y:auto; padding:var(--sp-lg); }
.detail-drawer h2 { margin:0 0 var(--sp-xs); font-size:1.25rem; font-family:var(--font-mono); }
.detail-drawer .close-btn { position:absolute; top:var(--sp-md); right:var(--sp-md); background:none; border:none; color:var(--text-dim); font-size:1.5rem; cursor:pointer; }
.chart-container { position:relative; height:200px; margin-bottom:var(--sp-md); }
.chart-container canvas { width:100%!important; }
.range-selector { display:flex; gap:var(--sp-sm); margin-bottom:var(--sp-md); }
.range-btn { background:var(--surface); border:1px solid var(--border); color:var(--text-dim); padding:var(--sp-xs) 0.75rem; border-radius:4px; cursor:pointer; font-size:0.75rem; font-family:var(--font-mono); }
.range-btn.active { background:var(--blue); color:white; border-color:var(--blue); }
.card-actions { margin-top:var(--sp-sm); display:flex; gap:var(--sp-sm); }
</style>"""

html = html[:style_start] + new_style + html[style_end:]

# 2b. Replace the fleet-bar + evaluate-all section with summary banner
old_fleet_section = """<div class="fleet-bar" x-show="!loading && fleetData">
  <div class="fleet-stat">
    <span class="fs-icon">&#x1F4BB;</span>
    <div><div class="fs-value" x-text="fleetData?.container_count || 0"></div><div class="fs-label">Containers</div></div>
  </div>
  <div class="fleet-stat">
    <span class="fs-icon">&#x26A1;</span>
    <div><div class="fs-value" x-text="(fleetData?.total_cpu || 0) + '%'"></div><div class="fs-label">Total CPU</div></div>
  </div>
  <div class="fleet-stat">
    <span class="fs-icon">&#x1F4BE;</span>
    <div><div class="fs-value" x-text="formatMB(fleetData?.total_mem_mb || 0)"></div><div class="fs-label">Total RAM</div></div>
  </div>
</div>
<div style="padding:0.5rem 2rem;" x-show="!loading">
  <button class="btn btn-eval" @click="evalAll()" :disabled="evaluatingAll">
    <span x-text="evaluatingAll ? 'Evaluating All...' : 'Evaluate All'"></span>
  </button>
</div>"""

new_fleet_section = """<!-- Summary banner — workflow entry point -->
<div class="summary-banner" x-show="!loading" x-cloak>
  <div class="summary-stat" x-show="unhealthyCount > 0">
    <span class="count count-alert" x-text="unhealthyCount"></span>
    <span class="stat-label">need attention</span>
  </div>
  <div class="summary-stat" x-show="degradedCount > 0">
    <span class="count count-warn" x-text="degradedCount"></span>
    <span class="stat-label">degraded</span>
  </div>
  <div class="summary-stat">
    <span class="count count-ok" x-text="healthyCount"></span>
    <span class="stat-label">healthy</span>
  </div>
  <div class="summary-stat" style="margin-left:var(--sp-md);padding-left:var(--sp-md);border-left:1px solid var(--border);">
    <span class="count" style="color:var(--text)" x-text="fleetData?.container_count || containers.length"></span>
    <span class="stat-label">total</span>
  </div>
  <div class="summary-stat" x-show="fleetData">
    <span class="fs-value mono" x-text="(fleetData?.total_cpu || 0) + '%'"></span>
    <span class="stat-label">CPU</span>
  </div>
  <div class="summary-stat" x-show="fleetData">
    <span class="fs-value mono" x-text="formatMB(fleetData?.total_mem_mb || 0)"></span>
    <span class="stat-label">RAM</span>
  </div>
  <div class="summary-eval-all">
    <button class="btn btn-eval" @click="evalAll()" :disabled="evaluatingAll">
      <span x-text="evaluatingAll ? 'Evaluating...' : 'Evaluate All'"></span>
    </button>
  </div>
</div>"""

html = html.replace(old_fleet_section, new_fleet_section)

# 2c. Add priority card classes based on status
old_card_div = '<div class="card" @click="openDetail(c)" style="cursor:pointer;">'
new_card_div = '<div class="card" :class="cardPriorityClass(c)" @click="openDetail(c)" style="cursor:pointer;">'
html = html.replace(old_card_div, new_card_div)

# 2d. Remove nested card-eval background — it's now flat (handled by CSS above)
# The card-eval class no longer has background:var(--surface2), just border-top

# 2e. Add computed properties for summary banner counts and card priority class
old_return_start = """    async init() {"""
new_return_start = """    // Summary banner computed counts
    get healthyCount() {
      return this.containers.filter(c => c.running && c.last_evaluation?.ai_status === 'healthy').length;
    },
    get unhealthyCount() {
      return this.containers.filter(c => c.running && c.last_evaluation && ['unhealthy','critical'].includes(c.last_evaluation.ai_status)).length;
    },
    get degradedCount() {
      return this.containers.filter(c => c.running && c.last_evaluation?.ai_status === 'degraded').length;
    },
    cardPriorityClass(c) {
      if (!c.last_evaluation) return '';
      const s = c.last_evaluation.ai_status;
      if (s === 'critical') return 'card-critical';
      if (s === 'unhealthy') return 'card-unhealthy';
      if (s === 'degraded') return 'card-degraded';
      return '';
    },

    async init() {"""

html = html.replace(old_return_start, new_return_start)

with open(INDEX_PATH, "w") as f:
    f.write(html)

print("✅ index.html updated")
print("Done! All 6 vibe-code fixes applied.")
