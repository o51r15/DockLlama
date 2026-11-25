// Settings page logic (Alpine component).
async function api(path, options = {}) {
  const opts = {...options};
  if (opts.body !== undefined) {
    opts.headers = {'Content-Type': 'application/json', ...(opts.headers || {})};
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = 'login'; throw new Error('signed out'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || 'Something went wrong. Try again.');
  return data;
}

function settingsPage() {
  return {
    sections: [
      {id: 'ai', label: 'AI'},
      {id: 'machines', label: 'Machines'},
      {id: 'containers', label: 'Containers'},
      {id: 'fixing', label: 'Checking and fixing'},
      {id: 'notifications', label: 'Notifications'},
      {id: 'account', label: 'Account'},
      {id: 'backup', label: 'Backup'},
      {id: 'advanced', label: 'Advanced'},
    ],
    section: 'containers',
    loginRequired: false,
    machineList: [],
    containers: [],
    containersError: '',
    loadingContainers: false,
    search: '',
    toast: '',
    pw: {current: '', next: '', again: '', error: '', done: false, busy: false},

    async init() {
      const hash = window.location.hash.slice(1);
      if (this.sections.some(s => s.id === hash)) this.section = hash;
      window.addEventListener('hashchange', () => {
        const h = window.location.hash.slice(1);
        if (this.sections.some(s => s.id === h)) this.section = h;
      });
      const status = await api('api/auth/status');
      this.loginRequired = status.login_required;
      await this.loadMachines();
      this.loadContainers();
    },

    async loadMachines() {
      try { this.machineList = await api('api/machines'); } catch (e) { this.showToast(e.message); }
    },

    async loadContainers() {
      this.loadingContainers = true;
      try {
        const ms = this.machineList.length ? this.machineList : await api('api/machines');
        const all = [], errors = [];
        for (const m of ms) {
          const d = await api('api/machines/' + m.id + '/containers');
          d.containers.forEach(c => { c.machine = m.name; c.machine_id = m.id; });
          all.push(...d.containers);
          if (d.error) errors.push((ms.length > 1 ? m.name + ': ' : '') + d.error);
        }
        this.containers = all;
        this.containersError = errors.join(' ');
      } catch (e) {
        this.containersError = e.message;
      }
      this.loadingContainers = false;
    },
    multiMachine() { return new Set(this.containers.map(c => c.machine_id)).size > 1; },

    visible() {
      const q = this.search.trim().toLowerCase();
      return this.containers.filter(c => !q || c.name.toLowerCase().includes(q) ||
        (c.image || '').toLowerCase().includes(q) || (c.compose_project || '').toLowerCase().includes(q));
    },

    groups() {
      const map = new Map();
      for (const c of this.visible()) {
        let key = c.gone ? 'No longer exists' : (c.compose_project || 'Not part of a compose project');
        if (this.multiMachine()) key = c.machine + ' · ' + key;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(c);
      }
      return [...map.entries()].map(([name, items]) => ({name, items}));
    },

    liveCount() { return this.containers.filter(c => !c.gone).length; },
    watchedCount() { return this.containers.filter(c => !c.gone && c.monitored).length; },

    dotClass(c) {
      if (c.gone) return 'grey';
      if (c.state === 'running') return c.health === 'unhealthy' ? 'bad' : (c.health === 'starting' ? 'warn' : 'ok');
      if (c.state === 'restarting' || c.state === 'dead' || c.oom_killed) return 'bad';
      if (c.state === 'exited' && c.exit_code) return 'bad';
      return 'grey';
    },

    async toggle(c, on) {
      const before = c.monitored;
      c.monitored = on;
      try {
        await api(`api/containers/${c.db_id}`, {method: 'PATCH', body: {monitored: on}});
      } catch (e) {
        c.monitored = before;
        this.showToast(e.message);
      }
    },

    async setAll(on) {
      const targets = this.visible().filter(c => !c.gone && c.db_id !== null);
      if (!targets.length) return;
      try {
        await api('api/containers/monitor', {method: 'POST', body: {db_ids: targets.map(c => c.db_id), monitored: on}});
        targets.forEach(c => { c.monitored = on; });
      } catch (e) { this.showToast(e.message); }
    },

    async forget(c) {
      try {
        await api(`api/containers/${c.db_id}`, {method: 'DELETE'});
        this.containers = this.containers.filter(x => x !== c);
      } catch (e) { this.showToast(e.message); }
    },

    async changePassword() {
      const p = this.pw;
      p.error = ''; p.done = false;
      if (p.next.length < 8) { p.error = 'Use at least 8 characters.'; return; }
      if (p.next !== p.again) { p.error = "The two passwords don't match."; return; }
      p.busy = true;
      try {
        await api('api/auth/password', {method: 'POST', body: {current_password: p.current, new_password: p.next}});
        p.current = p.next = p.again = '';
        p.done = true;
        this.loginRequired = true;
      } catch (e) { p.error = e.message; }
      p.busy = false;
    },

    showToast(msg) { this.toast = msg; setTimeout(() => { this.toast = ''; }, 4000); },

    async logout() {
      await fetch('api/auth/logout', {method: 'POST'});
      window.location.href = 'login';
    },
  };
}

// Settings → Checking and fixing
function fixingSection() {
  return {
    f: null, error: '',
    withCurrent(list, value) { return [...new Set([...list, value])].sort((a, b) => a - b); },
    async load() { try { this.f = await api('api/fixing'); } catch (e) { this.error = e.message; } },
    roundLine() {
      if (!this.f) return '';
      const s = this.f.round_seconds, t = s < 90 ? Math.max(1, Math.round(s)) + ' seconds' : Math.round(s / 60) + ' minutes';
      return (this.f.round_measured ? 'A check round takes about ' : 'A check round should take about ') + t +
        ' with the containers you watch, so every ' + this.f.min_interval + ' minutes is the shortest that fits. ' +
        'Shorter catches problems sooner but keeps your AI server busier.';
    },
    async save(change) {
      this.error = '';
      try { this.f = await api('api/fixing', {method: 'PUT', body: change}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.error = e.message; await this.load(); }
    },
  };
}

// Settings → Machines: adding a machine with DockLlama Remote
function addMachine() {
  return {
    open: false, step: 1, allowRestarts: false, snippet: null, name: '', address: '', busy: false, problem: '',
    async start() { this.open = true; this.step = 1; this.problem = ''; this.snippet = null; this.name = ''; this.address = ''; },
    async makeSnippet() {
      try { this.snippet = await api('api/machines/remote-snippet', {method: 'POST', body: {allow_restarts: this.allowRestarts}}); this.step = 2; }
      catch (e) { this.problem = e.message; }
    },
    async copy() {
      try { await navigator.clipboard.writeText(this.snippet.compose); this.$dispatch('toast', 'Copied.'); }
      catch (e) { this.$dispatch('toast', 'Select the text and copy it.'); }
    },
    async connect() {
      this.busy = true; this.problem = '';
      try {
        const r = await api('api/machines', {method: 'POST', body: {name: this.name, address: this.address, token: this.snippet.token}});
        if (!r.ok) { this.problem = r.problem; }
        else {
          this.open = false;
          this.$dispatch('toast', 'Added. ' + r.containers + ' containers found; pick the ones to watch in Containers.');
          this.$dispatch('machines-changed');
        }
      } catch (e) { this.problem = e.message; }
      this.busy = false;
    },
  };
}

// Settings → Notifications → Daily summary
function summaryBox() {
  return {
    s: null, p: null, busy: false,
    async load() { try { this.s = await api('api/summary'); } catch (e) { /* section still works */ } },
    async save(change) {
      try { this.s = await api('api/summary', {method: 'PUT', body: change}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.$dispatch('toast', e.message); await this.load(); }
    },
    async preview() {
      this.busy = true;
      try { this.p = await api('api/summary/preview', {method: 'POST'}); } catch (e) { this.$dispatch('toast', e.message); }
      this.busy = false;
    },
    async sendNow() {
      this.busy = true;
      try { this.p = await api('api/summary/send-now', {method: 'POST'}); this.$dispatch('toast', 'Sent.'); }
      catch (e) { this.$dispatch('toast', e.message); }
      this.busy = false;
    },
  };
}

// Settings → Account → Bring over a DockLlama v1 setup
function v1Import() {
  return {
    text: '', preview: null, error: '', busy: false, done: '',
    async read(file) {
      this.error = ''; this.preview = null; this.done = '';
      if (!file) return;
      this.text = await file.text();
      try { this.preview = (await api('api/import/v1', {method: 'POST', body: {config_text: this.text}})).preview; }
      catch (e) { this.error = e.message; }
    },
    async apply() {
      this.busy = true;
      try {
        const r = (await api('api/import/v1', {method: 'POST', body: {config_text: this.text, apply: true}})).result;
        this.preview = null;
        this.done = 'Imported ' + (r.containers_added + r.containers_updated) + ' containers and ' + r.notify_targets_added
          + ' alert destinations. ' + r.notes.join(' ');
        this.$dispatch('toast', 'Imported.');
      } catch (e) { this.error = e.message; }
      this.busy = false;
    },
  };
}
