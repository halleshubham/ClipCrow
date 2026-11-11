// ClipCrow web UI. No framework, no build step. Every value that came from a job (file names, model text)
// is inserted as text, never parsed as markup, and there are no inline handlers (the CSP forbids them).

const app = document.getElementById('app');
const banner = document.getElementById('banner');
const state = { config: null };

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') Object.assign(node.style, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

class SignInNeeded extends Error {}

async function api(path, opts = {}) {
  const res = await fetch('/api' + path, { credentials: 'same-origin', ...opts });
  if (res.status === 401 && path !== '/login') {
    state.config.signed_in = false;
    throw new SignInNeeded('Sign in first.');
  }
  if (!res.ok) {
    let msg = res.statusText || 'Something went wrong.';
    try {
      const body = await res.json();
      msg = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join('; ') : body.detail || msg;
    } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

const purposeLabel = (p) => {
  const [kind, n] = p.split(':');
  return { score: 'Score the sections', detail: `Pick clips, section ${Number(n) + 1}`, repair: `Fix the script, section ${Number(n) + 1}` }[kind] || p;
};
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

// ---- global banner ------------------------------------------------------------------------
function showBanner() {
  const c = state.config;
  banner.hidden = true;
  banner.className = 'banner';
  if (c.ready) {
    banner.textContent = c.ready;
    banner.classList.add('bad');
    banner.hidden = false;
  } else if (c.demo) {
    banner.textContent = 'Demo mode: any video of 3.5 minutes or more is used with a built-in Marathi transcript and recorded model answers, and your options are ignored. Nothing here calls an AI service.';
    banner.hidden = false;
  }
}

// ---- views ----------------------------------------------------------------------------------
function loginView() {
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const input = el('input', { id: 'token', type: 'password', autocomplete: 'current-password', required: true });
  const form = el('form', { class: 'card stack', style: { maxWidth: '420px' }, onsubmit: async (e) => {
    e.preventDefault();
    err.hidden = true;
    try {
      await api('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: input.value }) });
      state.config.signed_in = true;
      route();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } },
    el('h1', { text: 'Sign in' }),
    el('div', {}, el('label', { class: 'field', for: 'token', text: 'Access token' }), input),
    err,
    el('button', { class: 'btn dark', type: 'submit', text: 'Sign in' }));
  app.replaceChildren(form);
}

function newJobView() {
  const c = state.config;
  let file = null;
  const fileName = el('div', { class: 'small muted', text: `MP4, MOV, MKV or WebM, up to ${c.max_upload_mb} MB` });
  const fileTitle = el('div', { style: { fontWeight: '600' }, text: 'Drop a video here, or choose a file' });
  const fileInput = el('input', { type: 'file', id: 'file', accept: 'video/*,.mkv,.m4v', 'aria-label': 'Video file' });
  const drop = el('label', { class: 'drop', for: 'file' }, fileInput, el('div', {}, fileTitle, fileName));
  const setFile = (f) => {
    file = f;
    fileTitle.textContent = f ? f.name : 'Drop a video here, or choose a file';
    fileName.textContent = f ? `${(f.size / 1048576).toFixed(0)} MB` : `MP4, MOV, MKV or WebM, up to ${c.max_upload_mb} MB`;
  };
  fileInput.addEventListener('change', () => setFile(fileInput.files[0] || null));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('over');
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });

  const select = (id, label, options, hint) => el('div', {},
    el('label', { class: 'field', for: id, text: label }),
    el('select', { id }, options.map(([v, t]) => el('option', { value: v, text: t }))),
    hint && el('div', { class: 'hint', text: hint }));
  const text = (id, label, extra = {}, hint) => el('div', {},
    el('label', { class: 'field', for: id, text: label }),
    el('input', { id, type: 'text', ...extra }),
    hint && el('div', { class: 'hint', text: hint }));

  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const bar = el('div', {});
  const progress = el('div', { class: 'progress', hidden: true }, bar);
  const status = el('span', { class: 'small muted', 'aria-live': 'polite' });
  const rights = el('input', { type: 'checkbox', id: 'rights' });
  const submit = el('button', { class: 'btn primary', type: 'submit', text: 'Generate clips' });

  const form = el('form', { class: 'card', novalidate: true, onsubmit: (e) => {
    e.preventDefault();
    err.hidden = true;
    const fail = (m) => { err.textContent = m; err.hidden = false; submit.disabled = false; progress.hidden = true; };
    if (!file) return fail('Choose a video first.');
    if (file.size > c.max_upload_mb * 1048576) return fail(`That file is larger than the ${c.max_upload_mb} MB limit.`);
    if (!rights.checked) return fail('Confirm that you own this video or have the rights to process it.');
    const [minClip, maxClip] = $('length').value.split('-');
    const fd = new FormData();
    fd.append('file', file);
    fd.append('rights', 'true');
    fd.append('language', $('language').value);
    fd.append('clips', $('clips').value);
    fd.append('min_clip', minClip);
    fd.append('max_clip', maxClip);
    fd.append('spread', $('spread').value);
    fd.append('speaker_name', $('speaker').value);
    fd.append('speaker_title', $('role').value);
    fd.append('cutout', $('cutout').checked ? 'true' : '');
    submit.disabled = true;
    progress.hidden = false;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/jobs');
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const p = Math.round((ev.loaded / ev.total) * 100);
      bar.style.width = `${p}%`;
      status.textContent = p < 100 ? `Uploading ${p}%` : 'Starting…';
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status === 201) { location.hash = `#/jobs/${body.id}`; return; }
      if (xhr.status === 401) { state.config.signed_in = false; route(); return; }
      const d = body.detail;
      fail(Array.isArray(d) ? d.map((x) => x.msg).join('; ') : d || 'The upload failed.');
    };
    xhr.onerror = () => fail('The upload could not reach the server.');
    xhr.send(fd);
  } },
    el('h1', { text: 'New job' }),
    el('p', { class: 'lead', text: "Turn one long video into short vertical clips, with captions in the speaker's language." }),
    drop,
    el('div', { class: 'grid2' },
      select('language', 'Spoken language', c.languages.map((l) => [l.code, l.name]),
        'Marathi, Hindi and Nepali get a script-aware recogniser so text stays in Devanagari.'),
      el('div', {}, el('label', { class: 'field', for: 'clips', text: 'Clips to make' }),
        el('input', { id: 'clips', type: 'number', min: '1', max: '20', placeholder: 'auto' }),
        el('div', { class: 'hint', text: 'Leave empty for about one per two minutes of video.' })),
      select('length', 'Clip length', [['15-60', '15–60 seconds'], ['30-90', '30–90 seconds']]),
      select('spread', 'Spread across the video', [['0.5', 'Balanced (recommended)'], ['0', 'Off: best clips first'], ['0.8', 'Wide']],
        'Keeps clips from being slices of one section.'),
      text('speaker', 'Speaker name (lower third)', { autocomplete: 'off', class: 'deva' }),
      text('role', 'Role', { autocomplete: 'off', class: 'deva' })),
    el('div', { class: 'stack', style: { marginTop: '24px' } },
      el('label', { class: 'check', for: 'cutout' }, el('input', { type: 'checkbox', id: 'cutout' }),
        el('span', {}, el('strong', { text: 'Text behind the speaker' }), el('br'),
          el('span', { class: 'muted', text: 'Cuts the person out so a big word can sit behind them. Slower to render; best for a single face.' })))),
    el('label', { class: 'row small muted', for: 'rights', style: { marginTop: '24px', alignItems: 'flex-start', flexWrap: 'nowrap' } }, rights,
      el('span', { text: 'I own this video or have the rights to process it.' })),
    err,
    el('div', { class: 'row', style: { marginTop: '24px' } }, submit, status),
    progress);
  const $ = (id) => form.querySelector('#' + id);

  const aside = el('div', { class: 'stack grow' },
    el('section', { class: 'card' }, el('h2', { text: 'What happens next' }),
      el('ol', { class: 'stack small', style: { paddingLeft: '20px', margin: 0, gap: '10px' } },
        ['Transcribe: word-by-word timing, in the spoken language.', 'Plan: AI picks self-contained moments and writes the hook and title.',
          'Frame: a vertical crop that follows the speaker.', 'Render: captions, hook, stat and name card burned in.'].map((t) => el('li', { text: t })))),
    el('section', { class: 'card', style: { background: 'var(--accent-soft)', borderColor: '#E6C85A' } }, el('h2', { text: 'How AI is used' }),
      el('p', { class: 'small', style: { margin: 0 }, text: 'AI chooses the cut points, titles, hooks and emphasised words. Your footage stays real. Nothing AI-generated is added, and every job keeps a record of what the AI decided.' })));
  app.replaceChildren(el('div', { class: 'cols' }, el('div', { style: { flex: '999 1 640px', minWidth: '0' } }, form), aside));
}

const STATUS_CLASS = { queued: 'queued', running: 'running', done: 'done', failed: 'failed', cancelled: 'cancelled' };
const STAGE_LABEL = { transcribe: '1 · TRANSCRIBE', plan: '2 · PLAN', frame: '3 · FRAME', render: '4 · RENDER' };
const STAGE_WORD = { transcribe: 'Transcribing', plan: 'Planning', frame: 'Framing clips', render: 'Rendering' };
const STATE_TEXT = { pending: 'Waiting', running: 'Working…', done: 'Done', skipped: 'Skipped', failed: 'Failed' };

function statusText(job) {
  if (job.status === 'queued') return job.queue_position > 1 ? `Waiting (${job.queue_position - 1} ahead)` : 'Waiting to start';
  if (job.status === 'running') {
    const s = job.stages.find((x) => x.state === 'running');
    return s ? STAGE_WORD[s.name] : 'Working';
  }
  return { done: 'Done', failed: 'Failed', cancelled: 'Cancelled' }[job.status];
}

async function jobView(jobId) {
  let job = await api(`/jobs/${jobId}`);
  let timer = null, dead = false, usageLoaded = false;

  const title = el('h1', { translate: 'no' });
  const meta = el('div', { class: 'small muted' });
  const badge = el('span', { class: 'badge', 'aria-live': 'polite' });
  const cancelBtn = el('button', { class: 'btn', type: 'button', text: 'Cancel job', onclick: async () => {
    cancelBtn.disabled = true;
    try { job = await api(`/jobs/${jobId}/cancel`, { method: 'POST' }); update(); } catch (e) { cancelBtn.disabled = false; alert(e.message); }
  } });
  const deleteBtn = el('button', { class: 'btn', type: 'button', text: 'Delete', onclick: async () => {
    if (!confirm('Delete this job and its clips?')) return;
    try { await api(`/jobs/${jobId}`, { method: 'DELETE' }); location.hash = '#/jobs'; } catch (e) { alert(e.message); }
  } });
  const errBox = el('div', { class: 'alert', role: 'alert', hidden: true });
  const stages = el('div', { class: 'stages' });
  const logPre = el('pre', { tabindex: '0', 'aria-label': 'Job log' });
  const logCard = el('section', { class: 'log grow', style: { flex: '999 1 520px' } }, el('h2', { text: 'LIVE LOG' }), logPre);
  const timeline = el('div', {});
  const clipsGrid = el('div', { class: 'clips' });
  const clipsSection = el('section', { hidden: true }, el('h2', { text: 'Clips' }), timeline, el('div', { style: { height: '20px' } }), clipsGrid);
  const usageBody = el('div', { class: 'small muted', text: 'The usage record appears once the plan is ready.' });
  const usage = el('details', { class: 'card', hidden: true }, el('summary', { text: 'AI usage and disclosure' }), el('div', { style: { marginTop: '12px' } }, usageBody));
  const cards = new Map();

  function renderStages() {
    stages.replaceChildren(...job.stages.map((s) => el('div', { class: `stage ${s.state}` },
      el('div', { class: 'k', text: STAGE_LABEL[s.name] }),
      el('div', { class: 'v', text: STATE_TEXT[s.state] }),
      s.detail && el('div', { class: 'small muted', text: s.detail }))));
  }

  function renderTimeline() {
    const d = job.source.duration;
    if (!d || !job.clips.length) { timeline.replaceChildren(); return; }
    const span = (a, b) => ({ left: `${(a / d) * 100}%`, width: `${((b - a) / d) * 100}%` });
    const strip = el('div', { class: 'timeline', role: 'img', 'aria-label': 'Where the clips come from in the video' },
      job.left_out.map((o) => el('div', { class: 'out', style: span(o.start, o.end), title: o.reason })),
      job.clips.map((c, i) => el('div', { class: 'clip', style: span(c.start, c.end), text: `Clip ${i + 1}` })));
    timeline.replaceChildren(
      el('div', { class: 'row small', style: { justifyContent: 'space-between' } }, el('strong', { text: 'Where the clips come from' }),
        job.left_out.length ? el('span', { class: 'muted', text: 'One clip left out for sitting right next to another' }) : null),
      strip,
      el('div', { class: 'legend' }, el('span', { text: '0:00' }), el('span', {}, el('i'), 'chosen'),
        job.left_out.length ? el('span', {}, el('i', { class: 'out' }), 'left out (too close)') : null,
        el('span', { style: { marginLeft: 'auto' }, text: fmt(d) })));
  }

  function clipCard(c, i) {
    const player = el('div', { class: 'player' });
    const score = c.score == null ? null : Math.round(c.score * 100);
    const card = el('article', { class: 'card clipcard' }, player,
      el('div', { class: 'grow stack', style: { gap: '12px', minWidth: '220px' } },
        el('div', { class: 'chips' },
          el('span', { class: 'chip plain', text: `${fmt(c.start)} – ${fmt(c.end)}` }),
          el('span', { class: 'chip plain', text: `${Math.round(c.end - c.start)} s` }),
          el('span', { class: 'chip plain', text: { track: 'face-tracked crop', fit_blur: 'whole frame', cover: 'centre crop' }[c.framing] || c.framing })),
        el('h2', { class: 'deva', style: { fontSize: '20px', lineHeight: '1.4', margin: 0 }, translate: 'no', text: c.title }),
        c.hook && el('div', { class: 'small muted' }, 'Hook: ', el('span', { class: 'deva', text: c.hook })),
        c.why && el('p', { class: 'small muted', style: { margin: 0 }, text: c.why }),
        score != null && el('div', {}, el('div', { class: 'row tiny muted', style: { justifyContent: 'space-between' } }, el('span', { text: 'Clip score' }), el('span', { text: `${score} / 100` })),
          el('div', { class: 'bar-score' }, el('div', { style: { width: `${score}%` } }))),
        c.ai_decided.length > 0 && el('div', {}, el('div', { class: 'tiny muted', style: { marginBottom: '4px' }, text: 'AI decided' }),
          el('div', { class: 'chips' }, c.ai_decided.map((t) => el('span', { class: 'chip', text: t })))),
        el('div', { class: 'row', 'data-actions': true })));
    return { card, player, ready: false };
  }

  function renderClips() {
    clipsSection.hidden = job.clips.length === 0;
    job.clips.forEach((c, i) => {
      let entry = cards.get(c.id);
      if (!entry) { entry = clipCard(c, i); cards.set(c.id, entry); clipsGrid.append(entry.card); }
      if (c.ready && !entry.ready) {
        entry.ready = true;
        const url = `/api/jobs/${jobId}/clips/${c.id}`;
        entry.player.replaceChildren(el('video', { controls: true, playsinline: true, preload: 'metadata', src: `${url}/video`, 'aria-label': `Clip ${i + 1}: ${c.title}` }));
        entry.card.querySelector('[data-actions]').replaceChildren(
          el('a', { class: 'btn dark', href: `${url}/download`, text: 'Download MP4' }));
      } else if (!c.ready && !entry.ready) {
        entry.player.textContent = job.status === 'running' ? 'Rendering…' : 'Not rendered';
      }
    });
  }

  async function loadUsage() {
    if (usageLoaded || !job.clips.length) return;
    try {
      const u = await api(`/jobs/${jobId}/usage`);
      usageLoaded = true;
      const cost = (n) => (n == null ? '—' : `$${n.toFixed(4)}`);
      usage.hidden = false;
      usageBody.className = '';
      usageBody.replaceChildren(
        el('p', { class: 'small muted', text: 'Estimated from list prices, not an invoice.' }),
        el('table', {}, el('thead', {}, el('tr', {}, ['Step', 'Model', 'Tokens in', 'Tokens out', 'Est. cost'].map((h, i) => el('th', { class: i > 1 ? 'num' : '', text: h })))),
          el('tbody', {}, u.calls.map((c) => el('tr', {}, el('td', { text: purposeLabel(c.purpose) }), el('td', { text: c.model }),
            el('td', { class: 'num', text: c.input_tokens.toLocaleString() }), el('td', { class: 'num', text: c.output_tokens.toLocaleString() }),
            el('td', { class: 'num', text: cost(c.est_cost_usd) }))),
            el('tr', {}, el('td', { text: 'Total' }), el('td'), el('td', { class: 'num', text: u.input_tokens.toLocaleString() }),
              el('td', { class: 'num', text: u.output_tokens.toLocaleString() }), el('td', { class: 'num', text: cost(u.est_cost_usd) })))),
        el('h2', { style: { marginTop: '20px' }, text: 'Assets and AI involvement' }),
        el('div', { class: 'chips' }, u.assets.map((a) => el('span', { class: 'chip plain', text: `${a.id}: AI ${a.ai}` }))),
        el('p', { class: 'small', style: { marginTop: '12px' } },
          el('strong', { text: 'Platform AI disclosure: ' }), u.disclosure_recommended ? 'recommended (AI-generated media is included).' : 'not recommended. The footage is yours and unaltered; the AI chose cuts and wrote text.'));
    } catch (_) { /* the plan is not on disk yet: try again on the next poll */ }
  }

  function update() {
    title.textContent = job.name;
    const len = job.source.duration ? `${fmt(job.source.duration)} · ` : '';
    meta.textContent = `${len}started ${when(job.created_at)}${job.demo ? ' · demo mode' : ''}`;
    badge.textContent = statusText(job);
    badge.className = `badge ${STATUS_CLASS[job.status]}`;
    cancelBtn.hidden = !['queued', 'running'].includes(job.status);
    deleteBtn.hidden = ['queued', 'running'].includes(job.status);
    errBox.hidden = !job.error;
    errBox.textContent = job.error || '';
    renderStages();
    const nearBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
    logPre.textContent = job.log.length ? job.log.join('\n') : 'Waiting to start…';
    if (nearBottom) logPre.scrollTop = logPre.scrollHeight;
    renderTimeline();
    renderClips();
    loadUsage();
  }

  async function poll() {
    if (dead) return;
    if (['queued', 'running'].includes(job.status) || (job.status === 'done' && !usageLoaded && job.clips.length)) {
      try { job = await api(`/jobs/${jobId}`); update(); }
      catch (e) { if (e instanceof SignInNeeded) { route(); return; } }
      if (!dead) timer = setTimeout(poll, 1500);
    }
  }

  app.replaceChildren(
    el('div', { class: 'stack' },
      el('div', { class: 'row' }, el('div', { class: 'grow' }, title, meta), badge, cancelBtn, deleteBtn),
      errBox,
      el('section', { class: 'card' }, stages),
      el('div', { class: 'cols' }, logCard),
      clipsSection,
      usage));
  update();
  timer = setTimeout(poll, 1500);
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

async function jobsView() {
  const jobs = await api('/jobs');
  const body = jobs.length
    ? el('div', { class: 'card', style: { padding: '8px 24px' } }, el('table', {},
      el('thead', {}, el('tr', {}, ['Video', 'Status', 'Started', 'Clips'].map((h) => el('th', { text: h })))),
      el('tbody', {}, jobs.map((j) => el('tr', {},
        el('td', {}, el('a', { href: `#/jobs/${j.id}`, translate: 'no', text: j.name })),
        el('td', {}, el('span', { class: `badge ${STATUS_CLASS[j.status]}`, text: statusText(j) })),
        el('td', { text: when(j.created_at) }),
        el('td', { text: j.clips.length ? `${j.clips.filter((c) => c.ready).length} of ${j.clips.length}` : '—' }))))))
    : el('div', { class: 'card empty' }, 'No jobs yet. ', el('a', { href: '#/', text: 'Start one' }), '.');
  app.replaceChildren(el('h1', { text: 'Jobs' }), el('div', { style: { height: '16px' } }), body);
}

// ---- router -----------------------------------------------------------------------------------
let cleanup = null;
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || '#/';
  document.querySelectorAll('nav a').forEach((a) => {
    const on = (a.dataset.nav === 'new' && (hash === '#/' || hash === '#' || hash === '#/new')) || (a.dataset.nav === 'jobs' && hash.startsWith('#/jobs'));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  try {
    if (state.config.needs_login && !state.config.signed_in) return loginView();
    let m;
    if (hash === '#/' || hash === '#' || hash === '#/new') newJobView();
    else if (hash === '#/jobs') await jobsView();
    else if ((m = hash.match(/^#\/jobs\/([a-f0-9]{12})$/))) { const v = await jobView(m[1]); cleanup = v.cleanup; }
    else app.replaceChildren(el('div', { class: 'card empty', text: 'Page not found.' }));
    document.title = 'ClipCrow';
  } catch (e) {
    if (e instanceof SignInNeeded) return loginView();
    app.replaceChildren(el('div', { class: 'alert', role: 'alert', text: e.message }));
  }
}

window.addEventListener('hashchange', route);
(async () => {
  try {
    state.config = await api('/config');
  } catch (e) {
    app.replaceChildren(el('div', { class: 'alert', role: 'alert', text: 'Could not reach the ClipCrow server.' }));
    return;
  }
  showBanner();
  route();
})();
