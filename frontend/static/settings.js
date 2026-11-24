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
      {id: 'account', label: 'Account'},
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
      this.loadMachines();
      this.loadContainers();
    },

    async loadMachines() {
      try { this.machineList = await api('api/machines'); } catch (e) { this.showToast(e.message); }
    },

    async loadContainers() {
      this.loadingContainers = true;
      try {
        const d = await api('api/machines/1/containers');
        this.containers = d.containers;
        this.containersError = d.error || '';
      } catch (e) {
        this.containersError = e.message;
      }
      this.loadingContainers = false;
    },

    visible() {
      const q = this.search.trim().toLowerCase();
      return this.containers.filter(c => !q || c.name.toLowerCase().includes(q) ||
        (c.image || '').toLowerCase().includes(q) || (c.compose_project || '').toLowerCase().includes(q));
    },

    groups() {
      const map = new Map();
      for (const c of this.visible()) {
        const key = c.gone ? 'No longer exists' : (c.compose_project || 'Not part of a compose project');
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
