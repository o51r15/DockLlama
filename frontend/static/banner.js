// "Model testing finished" banner, shown on the next page load anywhere (plan 3.2 / 8A).
// DockLlama only suggests: switching happens when the user clicks "Switch to it".
function testBanner() {
  return {
    b: null, error: '',
    async load() {
      try {
        const r = await fetch('api/ai/banner');
        if (r.ok) this.b = (await r.json()).banner;
      } catch (e) { /* the banner is optional */ }
    },
    async dismiss() {
      const runId = this.b.run_id; this.b = null;
      try {
        await fetch('api/ai/banner/dismiss', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({run_id: runId})});
      } catch (e) { /* shows again next time; harmless */ }
    },
    async switchTo() {
      this.error = '';
      const r = await fetch('api/ai/model', {method: 'PUT', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({name: this.b.name})});
      if (!r.ok) { this.error = (await r.json()).detail || 'Could not switch.'; return; }
      const name = this.b.name;
      await this.dismiss();
      window.dispatchEvent(new CustomEvent('toast', {detail: 'Now using ' + name + '.'}));
      window.dispatchEvent(new CustomEvent('model-changed'));
    },
    async review() {
      await this.dismiss();
      if (location.pathname.endsWith('/settings')) { location.hash = 'ai'; window.dispatchEvent(new CustomEvent('model-changed')); }
      else location.href = 'settings#ai';
    },
  };
}
