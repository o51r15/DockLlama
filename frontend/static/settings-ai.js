// Settings → AI section (Alpine component). Uses api() from settings.js.
function aiSection() {
  return {
    status: {connected: false},
    changing: false, address: '', busy: false, problem: null, helpTab: 'Windows',
    found: [], discovering: false,
    confirm: null, pendingAddress: '',
    models: [], suggestion: null, downloads: [], modelsProblem: '', loadingModels: false,
    pulls: {}, pullTimer: null,
    adv: null,
    tests: {busy: false, active: null, scheduled: null, watched: null, results: {}}, testsTimer: null,
    plan: null, watching: null, hist: null, autoTest: true,

    async load() {
      await this.loadStatus();
      if (this.status.connected) { this.loadModels(); this.pollTests(); } else this.discover();
      this.pollPulls();
      try { this.autoTest = (await api('api/ai/auto-test')).enabled; } catch (e) { /* keeps the default */ }
    },
    async saveAutoTest() {
      try { await api('api/ai/auto-test', {method: 'PUT', body: {enabled: this.autoTest}}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.modelsProblem = e.message; }
    },

    async loadStatus() {
      try { this.status = await api('api/ai/status'); } catch (e) { this.problem = {message: e.message}; }
    },

    async discover() {
      this.discovering = true; this.found = [];
      try { this.found = (await api('api/ai/discover')).found; } catch (e) { /* manual entry still works */ }
      this.discovering = false;
      if (!this.found.length && !this.status.connected && !this.problem) {
        this.problem = {kind: 'none_found', message: "DockLlama couldn't find Ollama on its own. Type the address of the machine running it."};
        this.helpTab = 'Windows';
      }
    },

    async connect(address, decision = null, serverId = null) {
      this.busy = true; this.problem = null;
      try {
        const r = await api('api/ai/connect', {method: 'POST', body: {address, decision, server_id: serverId}});
        if (r.needs_confirm) { this.confirm = r.needs_confirm; this.pendingAddress = address; }
        else if (!r.ok) { this.problem = r.problem; this.confirm = null; }
        else {
          this.confirm = null; this.changing = false; this.address = '';
          await this.loadStatus(); await this.loadModels(); this.pollTests();
          this.$dispatch('toast', r.restored ? 'Connected. Settings and results for this server are back.' : 'Connected.');
        }
      } catch (e) { this.problem = {message: e.message}; }
      this.busy = false;
    },

    async renameServer() {
      const name = window.prompt('Name for this Ollama server', this.status.name);
      if (!name) return;
      try { await api('api/ai/server-name', {method: 'PUT', body: {name}}); this.status.name = name.trim(); }
      catch (e) { this.problem = {message: e.message}; }
    },

    async saveHardware() {
      try { await api('api/ai/hardware', {method: 'PUT', body: {hardware: this.status.hardware}}); await this.loadModels(); }
      catch (e) { this.modelsProblem = e.message; }
    },

    async loadModels() {
      this.loadingModels = true;
      try {
        const d = await api('api/ai/models');
        this.models = d.models; this.suggestion = d.suggestion; this.downloads = d.downloads;
        this.status.model = d.model;
        this.modelsProblem = d.problem ? d.problem.message + ' Showing the models DockLlama saw last time.' : '';
      } catch (e) { this.modelsProblem = e.message; }
      this.loadingModels = false;
    },

    async useModel(name) {
      try { await api('api/ai/model', {method: 'PUT', body: {name}}); await this.loadModels(); }
      catch (e) { this.modelsProblem = e.message; }
    },

    // --- downloads ---
    async download(name) {
      try { const p = await api('api/ai/pull', {method: 'POST', body: {name}}); this.pulls = {...this.pulls, [name]: p}; this.pollPulls(); }
      catch (e) { this.modelsProblem = e.message; }
    },
    anyPullRunning() { return Object.values(this.pulls).some(p => !p.done); },
    pullPercent(name) { const p = this.pulls[name]; return p && p.total ? Math.round(100 * p.completed / p.total) : (p && p.done ? 100 : 2); },
    pullText(name) {
      const p = this.pulls[name]; if (!p) return '';
      if (p.error) return p.error;
      if (p.done) return 'Downloaded.';
      if (p.total) return `${(p.completed / 1e9).toFixed(1)} of ${(p.total / 1e9).toFixed(1)} GB · ${this.pullPercent(name)}%`;
      return p.status;
    },
    async pollPulls() {
      clearTimeout(this.pullTimer);
      try {
        const list = await api('api/ai/pulls');
        const next = {}; list.forEach(p => { next[p.name] = p; });
        const finished = Object.values(next).some(p => p.done && this.pulls[p.name] && !this.pulls[p.name].done);
        this.pulls = next;
        if (finished) this.loadModels();
      } catch (e) { /* try again next tick */ }
      if (this.anyPullRunning()) this.pullTimer = setTimeout(() => this.pollPulls(), 1000);
    },

    // --- Advanced ---
    recThink() {
      const o = this.adv.options;
      if (o.thinking === 'levels') return o.think_levels.includes('off') ? 'off' : (o.think_levels.includes('low') ? 'low' : o.think_levels[0]);
      return o.thinking === 'toggle' ? 'false' : null;
    },
    recPredict(think) { return (think && think !== 'false' && think !== 'off') ? 2000 : 500; },
    fmtK(n) { return n >= 1024 ? Math.round(n / 1024) + 'K' : String(n); },
    ctxValue() { return this.adv.options.context_steps[this.adv.form.ctxIndex]; },

    async openAdvanced(name) {
      try {
        const d = await api('api/ai/advanced?name=' + encodeURIComponent(name));
        const o = d.options, s = d.settings;
        const ctx = s.num_ctx || o.context_recommended;
        let idx = o.context_steps.indexOf(ctx); if (idx < 0) idx = o.context_steps.indexOf(o.context_recommended);
        const think = s.think === undefined ? null : (s.think === false && o.thinking === 'levels' ? 'off' : String(s.think));
        this.adv = {name, options: o, warnings: d.warnings, error: '', saved: false, form: {
          think: null, ctxIndex: Math.max(0, idx),
          keep_alive: typeof s.keep_alive === 'string' ? s.keep_alive : '',
          num_predict: s.num_predict || null, temperature: s.temperature ?? 0.1,
          timeout: s.timeout || 0, retry: s.retry ?? true, schema_format: s.schema_format ?? true,
        }};
        this.adv.form.think = think ?? this.recThink();
        if (!this.adv.form.num_predict) this.adv.form.num_predict = this.recPredict(this.adv.form.think);
      } catch (e) { this.modelsProblem = e.message; }
    },

    advPayload() {
      const f = this.adv.form, o = this.adv.options, out = {};
      const rec = this.recThink();
      if (o.thinking !== 'none' && f.think !== rec) out.think = o.thinking === 'toggle' ? f.think === 'true' : (f.think === 'off' ? false : f.think);
      if (this.ctxValue() !== o.context_recommended) out.num_ctx = this.ctxValue();
      if (f.keep_alive) out.keep_alive = f.keep_alive;
      if (f.num_predict !== this.recPredict(f.think)) out.num_predict = f.num_predict;
      if (Math.abs(f.temperature - 0.1) > 1e-9) out.temperature = f.temperature;
      if (f.timeout) out.timeout = Math.max(30, f.timeout);
      if (!f.retry) out.retry = false;
      if (!f.schema_format) out.schema_format = false;
      return out;
    },

    async testWithSettings() {
      await this.saveAdvanced();
      if (this.adv.error) return;
      const name = this.adv.name; this.adv = null; this.startTest(name);
    },

    async saveAdvanced() {
      this.adv.error = ''; this.adv.saved = false;
      try {
        const d = await api('api/ai/advanced?name=' + encodeURIComponent(this.adv.name), {method: 'PUT', body: this.advPayload()});
        this.adv.warnings = d.warnings; this.adv.saved = true; this.loadModels();
      } catch (e) { this.adv.error = e.message; }
    },

    async resetAdvanced() {
      try {
        await api('api/ai/advanced?name=' + encodeURIComponent(this.adv.name), {method: 'DELETE'});
        const name = this.adv.name; await this.openAdvanced(name); this.adv.saved = true; this.loadModels();
      } catch (e) { this.adv.error = e.message; }
    },

    // --- model tests ---
    async pollTests() {
      clearTimeout(this.testsTimer);
      const wasActive = !!(this.tests.active);
      try { this.tests = await api('api/ai/tests'); } catch (e) { /* try again next tick */ }
      const active = !!(this.tests.active);
      if (wasActive && !active) this.loadModels();  // a run just finished: refresh results and suggestion
      if (this.watching && this.tests.watched && this.tests.watched.run_id === this.watching) {
        if (this.tests.watched.status === 'done') { this.loadModels(); this.dismissBannerFor(this.watching); }
      }
      this.testsTimer = setTimeout(() => this.pollTests(), active ? 1500 : 15000);
    },
    resultFor(name) { return this.tests.results[name] || null; },
    liveFor(name) {
      const a = this.tests.active; if (!a) return null;
      return a.models.find(m => m.name === name && (m.status === 'running' || m.status === 'queued')) || null;
    },
    fitText(fit) { return {gpu: 'on the graphics card', partial: 'partly on the CPU', cpu: 'on the CPU'}[fit] || ''; },
    resultShort(r) {
      if (!r) return '';
      if (r.status === 'too_slow') return 'Too slow here';
      if (r.status === 'failed') return 'Test failed';
      return Math.round(r.score) + '/100 here' + (r.sec_per_check ? ' · ' + r.sec_per_check.toFixed(1) + ' s per check' : '');
    },
    fitShort(fit) { return {gpu: 'On GPU', partial: 'Partly on CPU', cpu: 'CPU only'}[fit] || ''; },
    resultText(r) {
      if (!r) return '';
      if (r.status === 'too_slow') return 'Too slow here';
      if (r.status === 'failed') return 'Test failed';
      let s = 'Scored ' + Math.round(r.score) + ' here';
      if (r.sec_per_check) s += ' · ' + r.sec_per_check.toFixed(1) + ' s per check';
      if (this.fitText(r.fit)) s += ' · ' + this.fitText(r.fit);
      return s;
    },
    liveText(l) {
      if (l.status === 'queued') return 'Waiting to test';
      if (l.phase === 'waiting') return 'Waiting for a check round to finish…';
      if (l.phase === 'loading') return 'Loading the model…';
      if (l.phase === 'retrying') return 'Trying a slow question again…';
      return 'Testing: question ' + Math.min(l.scenario + 1, l.of) + ' of ' + l.of;
    },
    fmtDuration(s) {
      if (s < 90) return 'about a minute';
      if (s < 3600) return 'about ' + Math.round(s / 60) + ' minutes';
      const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
      return 'about ' + h + ' h' + (m ? ' ' + m + ' min' : '');
    },
    fmtClock(ts) { return new Date(ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); },
    fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : ''; },
    activeLine() {
      const a = this.tests.active; if (!a) return '';
      const cur = a.models.find(m => m.status === 'running');
      let s = a.status === 'paused' ? 'Paused' : (cur ? 'Testing ' + cur.name : 'Starting');
      if (cur && cur.of) s += ' (question ' + Math.min(cur.scenario + 1, cur.of) + ' of ' + cur.of + ')';
      if (a.total > 1) s += ' · ' + a.finished + ' of ' + a.total + ' models done';
      if (a.status !== 'paused' && a.remaining_s) s += ' · ' + this.fmtDuration(a.remaining_s) + ' left';
      return s;
    },
    runPercent(r) {
      if (!r || !r.total) return 0;
      const cur = r.models.find(m => m.status === 'running');
      const part = cur && cur.of ? cur.scenario / cur.of : 0;
      return Math.round(100 * (r.finished + part) / r.total);
    },
    async startTest(name) {
      try { await api('api/ai/tests', {method: 'POST', body: {names: [name], kind: 'one'}}); this.pollTests(); }
      catch (e) { this.modelsProblem = e.message; }
    },
    async runAction(runId, action) {
      try { await api('api/ai/tests/' + runId + '/' + action, {method: 'POST'}); }
      catch (e) { this.modelsProblem = e.message; }
      this.pollTests();
    },

    // "Test all": pick models, see the estimate, choose how to run it.
    async openPlan() {
      try {
        const p = await api('api/ai/tests/plan');
        this.plan = {...p, error: ''};
      } catch (e) { this.modelsProblem = e.message; }
    },
    planTotal() { return this.plan ? this.plan.models.filter(m => m.ticked).reduce((a, m) => a + m.estimate_s, 0) : 0; },
    planCount() { return this.plan ? this.plan.models.filter(m => m.ticked).length : 0; },
    async startPlan(how) {
      const names = this.plan.models.filter(m => m.ticked).map(m => m.name);
      if (!names.length) { this.plan.error = 'Tick at least one model.'; return; }
      try {
        const r = await api('api/ai/tests', {method: 'POST', body: {names, kind: 'all',
          mode: how === 'watch' ? 'watch' : 'background', tonight: how === 'tonight'}});
        this.plan = null;
        if (how === 'watch') this.watching = r.run_id;
        else this.$dispatch('toast', how === 'tonight' ? 'Model testing will start tonight.'
                                                     : 'Testing in the background. You can keep using DockLlama.');
        this.pollTests();
      } catch (e) { this.plan.error = e.message; }
    },

    // Wait-and-watch screen
    watchedRun() { const w = this.tests.watched; return w && w.run_id === this.watching ? w : null; },
    watchRanked() {
      const w = this.watchedRun(); if (!w) return [];
      // The server's pick first (it prefers the faster of near-equal scores), then by score.
      return [...w.models].sort((a, b) => (b.name === w.suggested) - (a.name === w.suggested)
        || (b.status === 'done') - (a.status === 'done') || (b.score ?? -1) - (a.score ?? -1));
    },
    async closeWatch() {
      const w = this.watchedRun(), id = this.watching;
      this.watching = null;
      if (w && ['running', 'paused'].includes(w.status)) {
        await this.runAction(id, 'background');
        this.$dispatch('toast', 'Testing carries on in the background. You\'ll see the results on your next page.');
      }
    },
    async dismissBannerFor(runId) {
      try { await api('api/ai/banner/dismiss', {method: 'POST', body: {run_id: runId}}); } catch (e) { /* harmless */ }
    },
    async watchUse(name) { await this.useModel(name); this.$dispatch('toast', 'Now using ' + name + '.'); },

    async openHistory(name) {
      try { this.hist = await api('api/ai/tests/history?name=' + encodeURIComponent(name)); this.hist.open = null; }
      catch (e) { this.modelsProblem = e.message; }
    },
    settingsText(s) {
      const keys = Object.keys(s || {});
      if (!keys.length) return 'recommended settings';
      return keys.map(k => k === 'num_ctx' ? 'context ' + this.fmtK(s[k]) : k === 'think' ? 'thinking ' + s[k] : k + ' ' + s[k]).join(', ');
    },
  };
}
