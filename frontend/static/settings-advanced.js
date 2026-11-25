// Settings → Advanced: how long history is kept, log lines per check, forgetting old AI servers.
function advancedSection() {
  return {
    a: null, servers: [], error: '', customDays: false, confirmForget: null,
    dayChoices: [7, 30, 90, 180, 365],
    lineChoices: [100, 200, 300, 500, 1000],
    async load() {
      try {
        this.a = await api('api/advanced');
        this.customDays = !this.dayChoices.includes(this.a.retention_days);
        this.servers = (await api('api/advanced/servers')).servers;
      } catch (e) { this.error = e.message; }
    },
    withCurrent(list, value) { return [...new Set([...list, value])].sort((a, b) => a - b); },
    async save(change) {
      this.error = '';
      try { this.a = await api('api/advanced', {method: 'PUT', body: change}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.error = e.message; await this.load(); }
    },
    pickDays(v) {
      if (v === 'custom') { this.customDays = true; return; }
      this.customDays = false;
      this.save({retention_days: Number(v)});
    },
    async forget(s) {
      this.error = '';
      try {
        await api('api/advanced/servers/' + s.id, {method: 'DELETE'});
        this.confirmForget = null;
        this.$dispatch('toast', 'Forgotten.');
        await this.load();
      } catch (e) { this.error = e.message; }
    },
    seen(t) { return t ? new Date(t * 1000).toLocaleDateString() : 'never'; },
  };
}

// Settings → Advanced → AI instructions: Default (read-only) plus Prompt 1, 2, 3…, one in use.
function promptsEditor() {
  return {
    open: false, understood: false, data: null, sel: 0, form: {name: '', text: ''}, dirty: false,
    error: '', busy: false, results: null, picked: {}, poll: null, confirmUse: null,
    async load() {
      try { this.data = await api('api/prompts'); } catch (e) { this.error = e.message; }
    },
    get current() { return this.data && this.data.prompts.find(p => p.id === this.sel); },
    get activeLabel() {
      if (!this.data || !this.data.active) return 'Default';
      const p = this.data.prompts.find(x => x.id === this.data.active);
      return p ? p.label : 'Default';
    },
    begin() { this.open = true; this.select(this.data.active || 0); },
    select(id) {
      if (this.dirty && !confirm('You have unsaved changes to this prompt. Leave them?')) return;
      this.sel = id; this.dirty = false; this.error = ''; this.results = null; this.picked = {};
      const p = this.current;
      this.form = p ? {name: p.name, text: p.text} : {name: '', text: this.data.default};
      if (p) this.loadResults();
    },
    async create() {
      this.error = '';
      try {
        const p = await api('api/prompts', {method: 'POST', body: {}});
        await this.load(); this.dirty = false; this.select(p.id);
      } catch (e) { this.error = e.message; }
    },
    async save() {
      this.error = ''; this.busy = true;
      try {
        await api('api/prompts/' + this.sel, {method: 'PUT', body: this.form});
        await this.load(); this.dirty = false; this.$dispatch('toast', 'Saved.'); this.loadResults();
      } catch (e) { this.error = e.message; }
      this.busy = false;
    },
    async remove() {
      if (!confirm('Delete ' + this.current.label + ' and its test results? This can\'t be undone.')) return;
      this.error = '';
      try {
        this.data = await api('api/prompts/' + this.sel, {method: 'DELETE'});
        this.dirty = false; this.select(this.data.active || 0);
      } catch (e) { this.error = e.message; }
    },
    async use(id) {
      this.error = '';
      try {
        this.data = await api('api/prompts/active', {method: 'PUT', body: {id}});
        this.confirmUse = null;
        this.$dispatch('toast', id ? 'Checks now use this prompt.' : 'Checks now use the Default.');
      } catch (e) { this.error = e.message; }
    },
    async loadResults() {
      if (!this.sel) return;
      try {
        this.results = await api('api/prompts/' + this.sel + '/results');
        const waiting = this.results.models.some(m => m.pending);
        clearTimeout(this.poll);
        if (waiting) this.poll = setTimeout(() => this.loadResults(), 3000);
      } catch (e) { this.error = e.message; }
    },
    pickAll(on) { for (const m of this.results.models) this.picked[m.name] = on; },
    get pickedNames() { return Object.keys(this.picked).filter(n => this.picked[n]); },
    async runTest() {
      this.error = '';
      if (this.dirty) { this.error = 'Save your changes first, so the test uses them.'; return; }
      try {
        await api('api/prompts/' + this.sel + '/test', {method: 'POST', body: {names: this.pickedNames}});
        this.picked = {}; this.$dispatch('toast', 'Testing started.');
        setTimeout(() => this.loadResults(), 500);
      } catch (e) { this.error = e.message; }
    },
    score(r) {
      if (!r) return '—';
      if (r.status === 'done') return Math.round(r.score) + (r.sec_per_check ? ' · ' + r.sec_per_check.toFixed(1) + ' s' : '');
      if (r.status === 'too_slow') return 'Too slow';
      return 'Failed';
    },
    diff(m) {
      if (!m.this || !m.default || m.this.status !== 'done' || m.default.status !== 'done') return '';
      const d = Math.round(m.this.score - m.default.score);
      return d === 0 ? 'same' : (d > 0 ? '+' + d : String(d));
    },
  };
}

// Settings → Backup
function backupSection() {
  return {
    b: null, error: '', busy: false, restoring: null, picked: {}, result: null, confirmDelete: null, uploading: false,
    everyChoices: [[6, 'every 6 hours'], [12, 'every 12 hours'], [24, 'once a day'], [48, 'every 2 days'],
                   [72, 'every 3 days'], [168, 'once a week']],
    keepChoices: [3, 7, 14, 30],
    kindLabel: {auto: 'Automatic', manual: 'Made by you', 'before-restore': 'Saved before a restore', uploaded: 'Uploaded'},
    async load() { try { this.b = await api('api/backup'); } catch (e) { this.error = e.message; } },
    withCurrent(list, value) { return [...new Set([...list, value])].sort((a, b) => a - b); },
    async save(change) {
      this.error = '';
      try { this.b = await api('api/backup', {method: 'PUT', body: change}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.error = e.message; await this.load(); }
    },
    async now() {
      this.error = ''; this.busy = true;
      try { await api('api/backup/now', {method: 'POST'}); await this.load(); this.$dispatch('toast', 'Backup saved.'); }
      catch (e) { this.error = e.message; }
      this.busy = false;
    },
    async remove(f) {
      this.error = '';
      try { await api('api/backup/files/' + encodeURIComponent(f.name), {method: 'DELETE'}); this.confirmDelete = null; await this.load(); }
      catch (e) { this.error = e.message; }
    },
    async upload(file) {
      if (!file) return;
      this.error = ''; this.uploading = true;
      try {
        const r = await fetch('api/backup/upload', {method: 'POST', body: file, headers: {'Content-Type': 'application/zip'}});
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || 'That file couldn\'t be uploaded.');
        await this.load();
        await this.startRestore({name: data.name});
      } catch (e) { this.error = e.message; }
      this.uploading = false;
    },
    async startRestore(f) {
      this.error = ''; this.result = null;
      try {
        this.restoring = await api('api/backup/files/' + encodeURIComponent(f.name) + '/contents');
        this.picked = {};
        for (const g of Object.keys(this.restoring.groups)) this.picked[g] = true;
      } catch (e) { this.error = e.message; }
    },
    get pickedGroups() { return Object.keys(this.picked).filter(g => this.picked[g]); },
    async doRestore() {
      this.error = ''; this.busy = true;
      try {
        this.result = await api('api/backup/files/' + encodeURIComponent(this.restoring.name) + '/restore',
                                {method: 'POST', body: {groups: this.pickedGroups}});
        this.restoring = null; await this.load();
      } catch (e) { this.error = e.message; }
      this.busy = false;
    },
    size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; },
    when(t) { return new Date(t * 1000).toLocaleString(); },
  };
}
