// Small helpers shared by the dashboard and the container page.
async function api(path, opts = {}) {
  const init = {method: opts.method || 'GET', headers: {}};
  if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  if (r.status === 401) { window.location.href = 'login'; throw new Error('Please sign in.'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('Something went wrong (' + r.status + ').'));
  return data;
}

function ago(ts, now) {
  if (!ts) return 'never';
  const s = Math.max(0, (now || Date.now() / 1000) - ts);
  if (s < 45) return 'just now';
  if (s < 90) return 'a minute ago';
  if (s < 3600) return Math.round(s / 60) + ' minutes ago';
  if (s < 5400) return 'an hour ago';
  if (s < 86400) return Math.round(s / 3600) + ' hours ago';
  return Math.round(s / 86400) + ' days ago';
}

function forHowLong(ts, now) {
  if (!ts) return '';
  const s = Math.max(0, (now || Date.now() / 1000) - ts);
  if (s < 90) return 'for a minute';
  if (s < 3600) return 'for ' + Math.round(s / 60) + ' minutes';
  if (s < 86400) return 'for ' + Math.round(s / 3600) + ' hours';
  return 'for ' + Math.round(s / 86400) + ' days';
}

// Status colours (plan 3.3): green / amber / red / grey.
function statusClass(status) {
  return {healthy: 'ok', degraded: 'warn', unhealthy: 'bad', critical: 'bad', stopped: 'grey',
          unknown: 'grey', gone: 'grey', pending: 'grey'}[status] || 'grey';
}

function clock(ts) { return new Date(ts * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}); }

// Check one container now. While it waits for the AI, calls onLine("Waiting in line (2 ahead)", details)
// every second, then onLine(null) once it's being checked.
async function checkNowWithLine(id, onLine) {
  let done = false;
  const poll = async () => {
    while (!done) {
      try {
        const l = await api('api/containers/' + id + '/check/line');
        if (done) break;
        onLine(l.waiting ? 'Waiting in line (' + l.ahead + ' ahead)' : null, l.what || []);
      } catch (e) { /* the check itself reports errors */ }
      await new Promise(r => setTimeout(r, 1000));
    }
  };
  poll();
  try { return await api('api/containers/' + id + '/check', {method: 'POST'}); }
  finally { done = true; onLine(null, []); }
}
