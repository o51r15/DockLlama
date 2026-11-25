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
