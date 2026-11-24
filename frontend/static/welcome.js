// Setup guide (/welcome). Builds on the Settings → AI component (settings-ai.js) for connecting,
// models, downloads and tests, and adds the guided steps around it.
function welcome() {
  const ai = aiSection();
  return Object.assign(ai, {
    steps: ['Password', 'AI', 'Model', 'Containers', 'Notifications'],
    step: 1, // index into steps; 0 (password) is already done on /setup
    noOllama: false, withoutAi: false,
    chosen: '', starting: false, stepError: '',
    containers: [], containersError: '', ticks: {}, loadingContainers: false,
    setup: {interval_minutes: 10, seconds_per_check: 5, per_check_source: 'guess'},
    finishing: false,

    async init() {
      await this.loadStatus();
      this.setup = await api('api/setup/state');
      if (this.status.connected) { await this.loadModels(); this.pollTests(); } else this.discover();
      this.pollPulls();
    },

    // --- step 2: AI ---
    async tryAgain() { if (this.address.trim()) await this.connect(this.address); else { this.problem = null; await this.discover(); } },
    skipAi() { this.withoutAi = true; this.go(3); },
    composeSnippet() {
      return [
        '  ollama:',
        '    image: ollama/ollama',
        '    container_name: ollama',
        '    restart: unless-stopped',
        '    volumes:',
        '      - ollama:/root/.ollama',
        '    # With an NVIDIA graphics card (needs the NVIDIA Container Toolkit):',
        '    # deploy:',
        '    #   resources:',
        '    #     reservations:',
        '    #       devices:',
        '    #         - driver: nvidia',
        '    #           count: all',
        '    #           capabilities: [gpu]',
      ].join('\n');
    },
    async copySnippet() {
      try { await navigator.clipboard.writeText(this.composeSnippet()); this.$dispatch('toast', 'Copied.'); }
      catch (e) { this.$dispatch('toast', 'Select the text and copy it.'); }
    },

    // --- step 3: model ---
    usableModels() { return this.models.filter(m => m.usable); },
    preselect() {
      if (this.chosen && this.models.some(m => m.name === this.chosen && m.usable)) return;
      this.chosen = this.status.model || (this.suggestion && this.suggestion.name) || (this.usableModels()[0] || {}).name || '';
    },
    async quickStart() {
      if (!this.chosen) { this.stepError = 'Pick a model first.'; return; }
      this.starting = true; this.stepError = '';
      try {
        await api('api/ai/model', {method: 'PUT', body: {name: this.chosen}});
        this.status.model = this.chosen;
        // Then test every model that fits, in the background. The banner tells the user if one does better.
        // Even if a test is already running (e.g. a model just downloaded), this one is queued
        // behind it, never skipped.
        const plan = await api('api/ai/tests/plan');
        const names = plan.models.filter(m => m.ticked).map(m => m.name);
        if (names.length > 1) await api('api/ai/tests', {method: 'POST', body: {names, kind: 'all', mode: 'background', queue: true}});
        this.go(3);
      } catch (e) { this.stepError = e.message; }
      this.starting = false;
    },
    async testAllNow() {
      this.stepError = '';
      try {
        const plan = await api('api/ai/tests/plan');
        const names = plan.models.filter(m => m.ticked).map(m => m.name);
        if (!names.length) { this.stepError = 'None of the installed models fit your hardware. Try a suggested download.'; return; }
        const r = await api('api/ai/tests', {method: 'POST', body: {names, kind: 'all', mode: 'watch'}});
        this.watching = r.run_id; this.pollTests();
      } catch (e) { this.stepError = e.message; }
    },
    async useAfterWatch(name) {
      try {
        await api('api/ai/model', {method: 'PUT', body: {name}});
        this.status.model = name; this.chosen = name;
        await this.dismissBannerFor(this.watching);
      } catch (e) { this.stepError = e.message; }
    },
    async leaveWatch() {
      const w = this.watchedRun();
      if (w && ['running', 'paused'].includes(w.status)) await this.runAction(this.watching, 'background');
      this.watching = null;
    },
    modelStepReady() { return !!this.status.model; },

    // --- step 4: containers ---
    async loadContainers() {
      this.loadingContainers = true; this.containersError = '';
      try {
        const d = await api('api/machines/1/containers');
        this.containersError = d.error || '';
        this.containers = d.containers.filter(c => !c.gone);
        const anyWatched = this.containers.some(c => c.monitored);
        const ticks = {};
        for (const c of this.containers) {
          // First time: running containers are ticked; DockLlama itself and the Ollama container aren't.
          ticks[c.db_id] = anyWatched ? !!c.monitored : (c.state === 'running' && !c.is_self && !c.is_ollama);
        }
        this.ticks = ticks;
      } catch (e) { this.containersError = e.message; }
      this.loadingContainers = false;
    },
    tickedCount() { return Object.values(this.ticks).filter(Boolean).length; },
    setAll(value) { for (const c of this.containers) if (!c.is_self) this.ticks[c.db_id] = value; },
    roundSeconds() {
      const per = this.withoutAi || !this.status.model ? 0.2 : this.setup.seconds_per_check;
      return this.tickedCount() * per;
    },
    roundText() {
      const s = this.roundSeconds();
      if (s < 60) return 'each round takes under a minute';
      return 'each round takes about ' + Math.round(s / 60) + ' minute' + (Math.round(s / 60) === 1 ? '' : 's');
    },
    async saveContainers() {
      this.stepError = '';
      try {
        const on = this.containers.filter(c => this.ticks[c.db_id]).map(c => c.db_id);
        const off = this.containers.filter(c => !this.ticks[c.db_id]).map(c => c.db_id);
        if (on.length) await api('api/containers/monitor', {method: 'POST', body: {db_ids: on, monitored: true}});
        if (off.length) await api('api/containers/monitor', {method: 'POST', body: {db_ids: off, monitored: false}});
        this.go(4);
      } catch (e) { this.stepError = e.message; }
    },
    async finish() {
      this.finishing = true; this.stepError = '';
      try {
        await api('api/setup/finish', {method: 'POST', body: {}});
        window.location.href = './';
      } catch (e) { this.stepError = e.message; this.finishing = false; }
    },

    // --- moving between steps ---
    async go(i) {
      this.stepError = '';
      if (i === 2) { await this.loadModels(); this.preselect(); this.setup = await api('api/setup/state'); }
      if (i === 3) { this.setup = await api('api/setup/state'); await this.loadContainers(); }
      this.step = i; window.scrollTo(0, 0);
    },
    back() {
      if (this.step === 3 && (this.withoutAi || !this.status.connected)) { this.withoutAi = false; this.go(1); return; }
      this.go(this.step - 1);
    },
  });
}
