// Notifications form: used by Settings → Notifications and setup step 5. Needs api() from settings.js.
const NOTIFY_SERVICES = [
  {id: 'ntfy', label: 'ntfy (recommended)', blurb: 'Free phone app, nothing to sign up for. DockLlama makes a private topic for you.'},
  {id: 'discord', label: 'Discord', blurb: 'Messages to a channel through a webhook.'},
  {id: 'telegram', label: 'Telegram', blurb: 'Messages from a bot you create with @BotFather.'},
  {id: 'gotify', label: 'Gotify', blurb: 'Your own Gotify server.'},
  {id: 'pushover', label: 'Pushover', blurb: 'Pushover app (paid once).'},
  {id: 'email', label: 'Email', blurb: 'Gmail, Outlook, Yahoo and most others. Use an app password, not your normal one.'},
  {id: 'apprise', label: 'Other (Apprise URL)', blurb: 'Any of the 100+ services Apprise supports, if you know its URL format.'},
];
const NOTIFY_FIELDS = {
  ntfy: [{key: 'topic', label: 'Topic', hint: 'Made up for you. Anyone who knows it can read the messages, so keep it hard to guess.'},
         {key: 'server', label: 'Server', hint: 'Leave as ntfy.sh unless you run your own.', placeholder: 'https://ntfy.sh'}],
  discord: [{key: 'webhook', label: 'Webhook URL', hint: 'Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.'}],
  telegram: [{key: 'bot_token', label: 'Bot token', hint: 'Message @BotFather, send /newbot, copy the token.'},
             {key: 'chat_id', label: 'Your chat ID', hint: 'Message @userinfobot to see it. Then send your new bot any message once.'}],
  gotify: [{key: 'server', label: 'Gotify server', placeholder: 'https://gotify.example.com'},
           {key: 'app_token', label: 'App token', hint: 'In Gotify: Apps → Create application → copy its token.'}],
  pushover: [{key: 'user_key', label: 'Your user key', hint: 'On your Pushover dashboard.'},
             {key: 'app_token', label: 'App token', hint: 'Create an application on pushover.net for DockLlama.'}],
  email: [{key: 'address', label: 'Your email address'},
          {key: 'password', label: 'App password', type: 'password', hint: 'Gmail: Google account → Security → App passwords. Outlook and Yahoo have the same.'},
          {key: 'smtp', label: 'SMTP server (only if yours isn\'t found)', placeholder: 'smtp.example.com', optional: true}],
  apprise: [{key: 'url', label: 'Apprise URL', placeholder: 'service://…'}],
};
const NOTIFY_EVENTS = [
  {key: 'problems', label: 'A container has a problem'},
  {key: 'recoveries', label: 'It recovers'},
  {key: 'reminders', label: '"Still broken" reminders'},
  {key: 'needs_look', label: 'Small problems too ("Needs a look")'},
  {key: 'checking', label: 'DockLlama can\'t check (e.g. the AI server is down)'},
  {key: 'restarts', label: 'DockLlama restarted something'},
  {key: 'summary', label: 'Daily summary'},
];

function notifyForm() {
  return {
    services: NOTIFY_SERVICES, fieldsFor: NOTIFY_FIELDS, eventList: NOTIFY_EVENTS,
    targets: [], reminderHours: 6, adding: false,
    form: null, testResult: null, saving: false, testing: false, formError: '',

    async loadTargets() {
      try {
        const d = await api('api/notify/targets');
        this.targets = d.targets; this.reminderHours = d.reminder_hours;
        if (!this.targets.length && !this.form) this.startAdd('ntfy');
      } catch (e) { this.formError = e.message; }
    },
    async startAdd(service) {
      this.testResult = null; this.formError = '';
      const fields = {};
      if (service === 'ntfy') { fields.topic = (await api('api/notify/ntfy-topic')).topic; fields.server = 'https://ntfy.sh'; }
      this.form = {service, fields, events: {problems: true, recoveries: true, reminders: true, needs_look: false,
                                             checking: true, restarts: true, summary: true}};
      this.adding = true;
    },
    subscribeLink() {
      if (!this.form || this.form.service !== 'ntfy') return '';
      let s = (this.form.fields.server || 'https://ntfy.sh').trim().replace(/\/+$/, '');
      if (!/^https?:\/\//.test(s)) s = 'https://' + s;
      return s + '/' + (this.form.fields.topic || '');
    },
    qrUrl(link) { return 'api/notify/qr?text=' + encodeURIComponent(link); },
    blurb() { const s = this.services.find(x => x.id === (this.form && this.form.service)); return s ? s.blurb : ''; },
    async sendTest() {
      this.testing = true; this.testResult = null; this.formError = '';
      try { this.testResult = await api('api/notify/test', {method: 'POST', body: {service: this.form.service, fields: this.form.fields}}); }
      catch (e) { this.formError = e.message; }
      this.testing = false;
    },
    async save() {
      this.saving = true; this.formError = '';
      try {
        await api('api/notify/targets', {method: 'POST', body: {service: this.form.service, fields: this.form.fields,
          events: this.form.events, base_url: location.origin + location.pathname.replace(/\/[^/]*$/, '')}});
        this.form = null; this.adding = false; this.testResult = null;
        await this.loadTargets();
        this.$dispatch('toast', 'Saved. DockLlama will message you there.');
      } catch (e) { this.formError = e.message; }
      this.saving = false;
    },
    async toggleEvent(t, key, value) {
      try { const v = await api('api/notify/targets/' + t.id, {method: 'PATCH', body: {events: {[key]: value}}}); Object.assign(t, v); }
      catch (e) { this.formError = e.message; }
    },
    async setEnabled(t, value) {
      try { const v = await api('api/notify/targets/' + t.id, {method: 'PATCH', body: {enabled: value}}); Object.assign(t, v); }
      catch (e) { this.formError = e.message; }
    },
    async testSaved(t) {
      try {
        const r = await api('api/notify/targets/' + t.id + '/test', {method: 'POST'});
        this.$dispatch('toast', r.ok ? 'Test sent. Check your phone.' : r.problem);
        await this.loadTargets();
      } catch (e) { this.formError = e.message; }
    },
    async remove(t) {
      if (!window.confirm('Stop sending messages to ' + t.description + '?')) return;
      await api('api/notify/targets/' + t.id, {method: 'DELETE'}); await this.loadTargets();
    },
    async saveReminder() {
      try { await api('api/notify/settings', {method: 'PUT', body: {reminder_hours: Number(this.reminderHours)}}); this.$dispatch('toast', 'Saved.'); }
      catch (e) { this.formError = e.message; }
    },
  };
}
