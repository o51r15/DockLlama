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

    async load() {
      await this.loadStatus();
      if (this.status.connected) this.loadModels(); else this.discover();
      this.pollPulls();
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
          await this.loadStatus(); await this.loadModels();
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
  };
}
