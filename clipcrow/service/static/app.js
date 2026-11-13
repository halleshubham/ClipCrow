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
  if (res.status === 401 && path !== '/login' && path !== '/auth/login') {
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
  if (state.config.accounts) return accountLoginView();
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

function accountLoginView() {
  let mode = 'login';
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const email = el('input', { id: 'email', type: 'text', inputmode: 'email', autocomplete: 'username', required: true });
  const pw = el('input', { id: 'password', type: 'password', autocomplete: 'current-password', required: true });
  const title = el('h1');
  const submit = el('button', { class: 'btn dark', type: 'submit' });
  const toggle = el('button', { class: 'btn', type: 'button' });
  const hint = el('div', { class: 'hint', hidden: true, text: 'At least 10 characters.' });
  function paint() {
    title.textContent = mode === 'login' ? 'Sign in' : 'Create your account';
    submit.textContent = mode === 'login' ? 'Sign in' : 'Create account';
    toggle.textContent = mode === 'login' ? 'New here? Create an account' : 'Have an account? Sign in';
    toggle.hidden = !state.config.signup;
    pw.setAttribute('autocomplete', mode === 'login' ? 'current-password' : 'new-password');
    hint.hidden = mode === 'login';
    err.hidden = true;
  }
  toggle.addEventListener('click', () => { mode = mode === 'login' ? 'register' : 'login'; paint(); });
  const form = el('form', { class: 'card stack', style: { maxWidth: '420px' }, onsubmit: async (e) => {
    e.preventDefault();
    err.hidden = true;
    try {
      await api(mode === 'login' ? '/auth/login' : '/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.value, password: pw.value }) });
      state.config = await api('/config');
      location.hash = '#/';
      route();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } }, title,
    el('div', {}, el('label', { class: 'field', for: 'email', text: 'Email' }), email),
    el('div', {}, el('label', { class: 'field', for: 'password', text: 'Password' }), pw, hint),
    err, el('div', { class: 'row' }, submit, toggle));
  paint();
  app.replaceChildren(form);
}

function renderAcct() {
  const box = document.getElementById('acct');
  box.replaceChildren();
  const c = state.config;
  if (!c || !c.accounts || !c.signed_in) return;
  box.append(el('a', { href: '#/account', text: c.email, translate: 'no' }),
    el('button', { class: 'btn', type: 'button', text: 'Sign out', onclick: async () => {
      try { await api('/auth/logout', { method: 'POST' }); } catch (_) { /* already signed out */ }
      state.config = await api('/config'); renderAcct(); location.hash = '#/'; route();
    } }));
}

async function accountView() {
  if (!state.config.accounts) { app.replaceChildren(el('div', { class: 'card empty', text: 'Accounts are not turned on.' })); return; }
  const me = await api('/me');
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const pct = me.plan.monthly_minutes ? Math.min(100, (me.used_minutes / me.plan.monthly_minutes) * 100) : 100;
  const pw = el('input', { id: 'del-pw', type: 'password', autocomplete: 'current-password' });
  const delForm = el('form', { class: 'stack', onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    if (!confirm('Delete your account and every job and clip in it? This cannot be undone.')) return;
    try {
      await api('/account', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw.value }) });
      state.config = await api('/config'); renderAcct(); location.hash = '#/'; route();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } },
    el('div', {}, el('label', { class: 'field', for: 'del-pw', text: 'Your password' }), pw),
    el('div', {}, el('button', { class: 'btn', type: 'submit', style: { color: 'var(--bad)' }, text: 'Delete my account' })));
  const billing = el('div', { id: 'billing' });
  app.replaceChildren(el('div', { class: 'stack' },
    el('h1', { text: 'Account' }),
    el('section', { class: 'card stack' },
      el('div', { class: 'row', style: { justifyContent: 'space-between' } },
        el('div', {}, el('strong', { translate: 'no', text: me.email }), el('div', { class: 'small muted', text: `${me.plan.name} plan${me.plan.price_label ? ' · ' + me.plan.price_label : ''}` })),
        me.plan_until && el('div', { class: 'small muted', text: `Renews or ends ${new Date(me.plan_until).toLocaleDateString()}` })),
      el('div', {}, el('div', { class: 'row small', style: { justifyContent: 'space-between' } }, el('span', { text: 'Minutes of video used this month' }),
          el('span', { text: `${me.used_minutes} of ${me.plan.monthly_minutes}` })),
        el('div', { class: me.left_minutes < 5 ? 'meter low' : 'meter', role: 'img', 'aria-label': `${me.used_minutes} of ${me.plan.monthly_minutes} minutes used` }, el('div', { style: { width: `${pct}%` } }))),
      me.credit_minutes > 0 && el('div', { class: 'small' }, `Plus ${me.credit_minutes} purchased minutes. ${me.left_minutes} minutes left in all.`),
      el('div', { class: 'small muted', text: `Finished jobs are kept for ${me.plan.retention_days} days, then deleted.` })),
    billing,
    el('section', { class: 'card stack' }, el('h2', { text: 'Delete account' }),
      el('p', { class: 'small muted', style: { margin: 0 }, text: 'Removes your account, every job and every clip. Cancel running jobs first.' }), err, delForm)));
  if (state.config.billing) await renderBilling(billing);
}

async function renderBilling(box) {
  let info;
  try { info = await api('/billing'); } catch (_) { return; }
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const go = async (path, body, label, btn) => {
    err.hidden = true; btn.disabled = true;
    try {
      const r = await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (r.url) { location.assign(r.url); return; }          // the hosted checkout page; the plan changes when the payment is confirmed
      await route();
    } catch (ex) { err.textContent = ex.message; err.hidden = false; btn.disabled = false; }
  };
  const buy = info.plans.filter((p) => p.can_subscribe && p.id !== info.current);
  const parts = [el('h2', { text: 'Plan and minutes' })];
  for (const p of buy) {
    const b = el('button', { class: 'btn dark', type: 'button', text: info.subscribed ? 'Cancel your current plan first' : `Choose ${p.name}`, disabled: info.subscribed });
    if (!info.subscribed) b.addEventListener('click', () => go('/billing/subscribe', { plan: p.id }, p.name, b));
    parts.push(el('div', { class: 'row', style: { justifyContent: 'space-between' } },
      el('div', {}, el('strong', { text: p.name }), el('div', { class: 'small muted', text: `${p.monthly_minutes} minutes of video a month${p.price_label ? ' · ' + p.price_label : ''}` })), b));
  }
  if (info.subscribed) {
    const b = el('button', { class: 'btn', type: 'button', text: 'Cancel subscription' });
    b.addEventListener('click', () => { if (confirm('Cancel your subscription? Your plan stays active until the end of the period you paid for.')) go('/billing/cancel', {}, 'cancel', b); });
    parts.push(el('div', { class: 'row' }, b, el('span', { class: 'small muted', text: 'Your plan stays active until the end of the period you paid for.' })));
  }
  for (const k of info.packs) {
    const b = el('button', { class: 'btn', type: 'button', text: 'Buy' });
    b.addEventListener('click', () => go('/billing/credits', { pack: k.id }, k.label, b));
    parts.push(el('div', { class: 'row', style: { justifyContent: 'space-between' } },
      el('div', {}, el('strong', { text: k.label }), el('div', { class: 'small muted', text: `${k.minutes} extra minutes that do not expire at the end of the month` })), b));
  }
  if (parts.length === 1) return;
  box.replaceChildren(el('section', { class: 'card stack' }, ...parts, err));
}

// ---- uploads ----------------------------------------------------------------------------------------

// The ordinary way: the file goes through this server (and any proxy in front of it, with its own size limit).
function classicUpload(file, options, show) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('rights', 'true');
    for (const [k, v] of Object.entries(options)) fd.append(k, v);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/jobs');
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) show(ev.loaded, ev.total); };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status === 201) return resolve(body);
      if (xhr.status === 401) { state.config.signed_in = false; return reject(new SignInNeeded('Sign in first.')); }
      const d = body.detail;
      reject(new Error(Array.isArray(d) ? d.map((x) => x.msg).join('; ') : d || 'The upload failed.'));
    };
    xhr.onerror = () => reject(new Error('The upload could not reach the server.'));
    xhr.send(fd);
  });
}

// The direct way (when the server has object storage): the browser sends the file to the bucket in parts, in parallel,
// each retried on its own, and then tells this server it is done. The bytes never pass through this site's proxy.
const PART_PARALLEL = 3;
const PART_RETRIES = 3;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class DirectUpload {
  constructor(file, options, show) {
    this.file = file; this.options = options; this.show = show;
    this.cancelled = false; this.xhrs = new Set(); this.id = null;
  }

  put(url, blob, onLoaded) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      this.xhrs.add(x);
      x.open('PUT', url);
      x.upload.onprogress = (ev) => onLoaded(ev.loaded);
      x.onload = () => {
        this.xhrs.delete(x);
        if (x.status >= 200 && x.status < 300) {
          const etag = x.getResponseHeader('ETag');
          if (etag) return resolve(etag);
          const e = new Error('The browser could not read the storage service\'s answer. The bucket needs a CORS rule for this site: ask whoever runs the server to run "clipcrow r2-cors".');
          e.kind = 'cors';
          return reject(e);
        }
        const e = new Error(`The storage service refused part of the upload (${x.status}).`);
        e.kind = 'status';
        reject(e);
      };
      x.onerror = () => {
        this.xhrs.delete(x);
        const e = new Error('The upload to storage was interrupted. Check your connection and try again. If it keeps failing, the bucket may be missing its CORS rule for this site.');
        e.kind = 'network';
        reject(e);
      };
      x.onabort = () => { this.xhrs.delete(x); const e = new Error('cancelled'); e.cancelled = true; reject(e); };
      x.send(blob);
    });
  }

  cancel() {
    this.cancelled = true;
    for (const x of this.xhrs) x.abort();
    if (this.id) api(`/uploads/${this.id}`, { method: 'DELETE' }).catch(() => { /* the server expires it anyway */ });
  }

  async run() {
    const json = { 'Content-Type': 'application/json' };
    const start = await api('/uploads', { method: 'POST', headers: json, body: JSON.stringify({ filename: this.file.name, size: this.file.size, rights: true }) });
    this.id = start.id;
    const n = start.parts.length;
    const loaded = new Array(n).fill(0);
    const etags = new Array(n);
    const total = this.file.size;
    const report = () => this.show(loaded.reduce((a, b) => a + b, 0), total);
    let next = 0, failure = null;
    const worker = async () => {
      while (failure === null && !this.cancelled) {
        const i = next++;
        if (i >= n) return;
        const part = start.parts[i];
        const blob = this.file.slice(i * start.part_size, i * start.part_size + part.length);
        for (let attempt = 0; ; attempt++) {
          try {
            etags[i] = await this.put(part.url, blob, (b) => { loaded[i] = b; report(); });
            loaded[i] = part.length; report();
            break;
          } catch (e) {
            loaded[i] = 0;
            if (e.cancelled || e.kind === 'cors' || attempt >= PART_RETRIES) { failure = e; return; }
            await sleep(1000 * 2 ** attempt);
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PART_PARALLEL, n) }, worker));
    if (this.cancelled) { const e = new Error('cancelled'); e.cancelled = true; throw e; }
    if (failure) {
      api(`/uploads/${this.id}`, { method: 'DELETE' }).catch(() => { /* expires on its own */ });
      throw failure;
    }
    this.show(total, total);
    return api(`/uploads/${this.id}/complete`, { method: 'POST', headers: json, body: JSON.stringify({ parts: etags.map((etag, i) => ({ n: i + 1, etag })), options: this.options }) });
  }
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
  const cancelBtn = el('button', { class: 'btn', type: 'button', text: 'Cancel upload', hidden: true });

  const form = el('form', { class: 'card', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    err.hidden = true;
    const fail = (m) => { err.textContent = m; err.hidden = false; submit.disabled = false; cancelBtn.hidden = true; progress.hidden = true; status.textContent = ''; };
    if (!file) return fail('Choose a video first.');
    if (file.size > c.max_upload_mb * 1048576) return fail(`That file is larger than the ${c.max_upload_mb} MB limit.`);
    if (!rights.checked) return fail('Confirm that you own this video or have the rights to process it.');
    const [minClip, maxClip] = $('length').value.split('-');
    const options = {
      language: $('language').value, clips: $('clips').value, min_clip: minClip, max_clip: maxClip, spread: $('spread').value,
      multi: $('multi').value, speaker_name: $('speaker').value, speaker_title: $('role').value,
      cutout: $('cutout').checked ? 'true' : '', immersive: $('immersive').checked ? 'true' : '',
      explainer: $('explainer').checked ? 'true' : '', links: $('links').value, ai_images: $('ai_images').checked ? 'true' : '',
    };
    submit.disabled = true;
    progress.hidden = false;
    bar.style.width = '0%';
    const show = (done, total) => {
      bar.style.width = `${Math.round((done / total) * 100)}%`;
      status.textContent = done >= total ? 'Starting…' : `Uploading ${Math.round((done / total) * 100)}% (${(done / 1048576).toFixed(0)} of ${(total / 1048576).toFixed(0)} MB)`;
    };
    try {
      let job;
      if (c.direct_upload) {
        const up = new DirectUpload(file, options, show);
        cancelBtn.hidden = false;
        cancelBtn.onclick = () => { up.cancel(); fail('Upload cancelled.'); };
        try {
          job = await up.run();
        } catch (ex) {
          // a file small enough for the proxy in front of this site can still go the ordinary way
          if (ex.kind === 'network' && file.size <= 90 * 1048576 && !up.cancelled) job = await classicUpload(file, options, show);
          else throw ex;
        }
      } else {
        job = await classicUpload(file, options, show);
      }
      location.hash = `#/jobs/${job.id}`;
    } catch (ex) {
      if (ex instanceof SignInNeeded) { route(); return; }
      if (!(ex.cancelled)) fail(ex.message || 'The upload failed.');
    }
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
      select('multi', 'Several people on screen', [['switch', 'Cut to whoever is talking (recommended)'], ['split', 'Stack two people'], ['off', 'Follow one face']],
        'For interviews and podcasts filmed with a wide shot.'),
      text('speaker', 'Speaker name (lower third)', { autocomplete: 'off', class: 'deva' }),
      text('role', 'Role', { autocomplete: 'off', class: 'deva' })),
    el('div', { class: 'stack', style: { marginTop: '24px' } },
      el('label', { class: 'check', for: 'cutout' }, el('input', { type: 'checkbox', id: 'cutout' }),
        el('span', {}, el('strong', { text: 'Text behind the speaker' }), el('br'),
          el('span', { class: 'muted', text: 'Cuts the person out so a big word can sit behind them. Slower to render; best for a single face.' }))),
      el('label', { class: 'check', for: 'explainer' }, el('input', { type: 'checkbox', id: 'explainer' }),
        el('span', {}, el('strong', { text: 'Explainer: show pictures' }), el('br'),
          el('span', { class: 'muted', text: 'Puts a picture over the video where the speaker names something: a page you link below, or an AI-generated image.' }))),
      el('div', { id: 'explainer-opts', class: 'stack', hidden: true, style: { paddingLeft: '30px' } },
        el('div', {}, el('label', { class: 'field', for: 'links', text: 'Pages to show (one address per line, up to 6)' }),
          el('textarea', { id: 'links', rows: '3', placeholder: 'Paste a link, one per line', autocomplete: 'off', spellcheck: 'false' }),
          el('div', { class: 'hint', text: 'The AI chooses which of your pages fits which moment. It never invents an address. Only public web pages work.' })),
        el('label', { class: c.ai_images ? 'check' : 'check muted', for: 'ai_images' }, el('input', { type: 'checkbox', id: 'ai_images', disabled: !c.ai_images }),
          el('span', {}, el('strong', { text: 'Also generate images with AI' }), el('br'),
            el('span', { class: 'muted', text: c.ai_images ? 'AI-generated pictures mean most platforms ask you to label the video as AI content.' : 'Not set up on this server.' })))),
      el('label', { class: 'check', for: 'immersive' }, el('input', { type: 'checkbox', id: 'immersive' }),
        el('span', {}, el('strong', { text: 'Slow push-ins' }), el('br'),
          el('span', { class: 'muted', text: 'The picture eases in as a new sentence begins, then snaps back. Suits a person talking to camera.' })))),
    el('label', { class: 'row small muted', for: 'rights', style: { marginTop: '24px', alignItems: 'flex-start', flexWrap: 'nowrap' } }, rights,
      el('span', { text: 'I own this video or have the rights to process it.' })),
    err,
    el('div', { class: 'row', style: { marginTop: '24px' } }, submit, cancelBtn, status),
    progress);
  const $ = (id) => form.querySelector('#' + id);
  $('explainer').addEventListener('change', () => { $('explainer-opts').hidden = !$('explainer').checked; });

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
const STAGE_LABEL = { transcribe: '1 · TRANSCRIBE', plan: '2 · PLAN', assets: '3 · PICTURES', frame: '4 · FRAME', render: '5 · RENDER' };
const STAGE_WORD = { transcribe: 'Transcribing', plan: 'Planning', assets: 'Making pictures', frame: 'Framing clips', render: 'Rendering' };
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

  const FRAMING_TEXT = { track: 'face-tracked crop', fit_blur: 'whole frame', cover: 'centre crop', split: 'two people stacked' };
  const busy = (c) => c.render === 'queued' || c.render === 'rendering';

  function clipCard(c, i) {
    const url = `/api/jobs/${jobId}/clips/${c.id}`;
    const player = el('div', { class: 'player' });
    if (c.ready) {
      player.replaceChildren(el('video', { controls: true, playsinline: true, preload: 'metadata', src: `${url}/video?v=${c.rev}`, 'aria-label': `Clip ${i + 1}: ${c.title}` }));
    } else {
      player.textContent = job.status === 'running' ? 'Rendering…' : 'Not rendered';
    }
    const score = c.score == null ? null : Math.round(c.score * 100);
    const actions = el('div', { class: 'row' });
    if (c.ready) actions.append(el('a', { class: 'btn dark', href: `${url}/download`, text: 'Download MP4' }));
    if (c.ready && job.status === 'done' && state.config.publishing) actions.append(el('a', { class: 'btn', href: `#/jobs/${jobId}/clips/${c.id}/share`, text: 'Share' }));
    if (c.ready && job.status === 'done' && job.source_removed) actions.append(el('span', { class: 'small muted', text: 'Original video removed: no more edits' }));
    else if (c.ready && job.status === 'done') actions.append(el('a', { class: 'btn', href: `#/jobs/${jobId}/clips/${c.id}`, text: 'Edit' }));
    return el('article', { class: 'card clipcard', 'data-clip': c.id }, player,
      el('div', { class: 'grow stack', style: { gap: '12px', minWidth: '220px' } },
        el('div', { class: 'chips' },
          el('span', { class: 'chip plain', text: `${fmt(c.start)} – ${fmt(c.end)}` }),
          el('span', { class: 'chip plain', text: `${Math.round(c.end - c.start)} s` }),
          el('span', { class: 'chip plain', text: FRAMING_TEXT[c.framing] || c.framing }),
          c.edited && el('span', { class: 'chip', text: 'edited by you' })),
        el('h2', { class: 'deva', style: { fontSize: '20px', lineHeight: '1.4', margin: 0 }, translate: 'no', text: c.title }),
        c.hook && el('div', { class: 'small muted' }, 'Hook: ', el('span', { class: 'deva', text: c.hook })),
        c.why && el('p', { class: 'small muted', style: { margin: 0 }, text: c.why }),
        c.notes && c.notes.map((n) => el('div', { class: 'small', style: { color: 'var(--warn)' }, text: n })),
        busy(c) && el('div', { class: 'badge running', role: 'status', text: c.render === 'queued' ? 'Re-render queued…' : 'Re-rendering…' }),
        c.render === 'failed' && el('div', { class: 'alert', role: 'alert', style: { margin: 0 } }, el('div', { text: `Re-render failed: ${c.render_error}` }),
          el('button', { class: 'btn', type: 'button', style: { marginTop: '8px' }, text: 'Try again', onclick: async () => {
            try { job = await api(`/jobs/${jobId}`); await api(`${'/jobs/' + jobId}/clips/${c.id}/render`, { method: 'POST' }); job = await api(`/jobs/${jobId}`); update(); schedule(); } catch (e) { alert(e.message); }
          } })),
        score != null && el('div', {}, el('div', { class: 'row tiny muted', style: { justifyContent: 'space-between' } }, el('span', { text: 'Clip score' }), el('span', { text: `${score} / 100` })),
          el('div', { class: 'bar-score' }, el('div', { style: { width: `${score}%` } }))),
        c.ai_decided.length > 0 && el('div', {}, el('div', { class: 'tiny muted', style: { marginBottom: '4px' }, text: 'AI decided' }),
          el('div', { class: 'chips' }, c.ai_decided.map((t) => el('span', { class: 'chip', text: t })))),
        actions));
  }

  function renderClips() {
    clipsSection.hidden = job.clips.length === 0;
    job.clips.forEach((c, i) => {
      // a card is rebuilt only when something it shows has changed, so a playing video is not interrupted
      const sig = JSON.stringify([c.ready, c.render, c.rev, c.title, c.hook, c.start, c.end, c.framing, c.edited, c.render_error, c.notes, job.status === 'done']);
      const entry = cards.get(c.id);
      if (entry && entry.sig === sig) return;
      const card = clipCard(c, i);
      if (entry) entry.card.replaceWith(card); else clipsGrid.append(card);
      cards.set(c.id, { card, sig });
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
    meta.textContent = `${len}started ${when(job.created_at)}${job.demo ? ' · demo mode' : ''}${job.expires_at ? ` · kept until ${when(job.expires_at)}` : ''}`;
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

  const needsPolling = () => ['queued', 'running'].includes(job.status) || job.clips.some(busy)
    || (job.status === 'done' && !usageLoaded && job.clips.length);
  async function poll() {
    if (dead) return;
    if (needsPolling()) {
      try { job = await api(`/jobs/${jobId}`); update(); }
      catch (e) { if (e instanceof SignInNeeded) { route(); return; } }
      if (!dead) timer = setTimeout(poll, 1500);
    }
  }
  function schedule() { clearTimeout(timer); if (!dead) timer = setTimeout(poll, 1500); }

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

// ---- clip editor ----------------------------------------------------------------------------------

async function clipEditorView(jobId, clipId) {
  let view = await api(`/jobs/${jobId}/clips/${clipId}`);
  let timer = null, dead = false, pick = 'start', shownRev = null;
  const base = `/jobs/${jobId}/clips/${clipId}`;
  const f = {};

  const player = el('div', { class: 'player', style: { width: '270px' } });
  const status = el('div', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const field = (label, control, hint) => el('div', {}, el('label', { class: 'field', for: control.id, text: label }), control,
    hint && el('div', { class: 'hint', text: hint }));
  const textIn = (id, value, max, deva = true) => (f[id] = el('input', { id, type: 'text', maxlength: String(max), autocomplete: 'off', class: deva ? 'deva' : null, value }));
  const choose = (id, options, value) => {
    const sel = (f[id] = el('select', { id }, options.map(([v, label]) => el('option', { value: v, text: label }))));
    sel.value = value;
    return sel;
  };

  textIn('title', view.title, 120);
  textIn('hook', view.hook, 120);
  textIn('speaker_name', view.speaker_name, 60);
  textIn('speaker_title', view.speaker_title, 60);
  textIn('stat', view.stat, 40, false);

  const num = (id, value) => (f[id] = el('input', { id, type: 'number', min: '0', max: String(Math.ceil(view.source_duration)), step: '0.1', value: String(value) }));
  num('source_in', view.source_in); num('source_out', view.source_out);
  const length = el('span', { class: 'small muted' });
  const wordBox = el('div', { class: 'words', role: 'group', 'aria-label': 'Transcript around the clip' });
  const wordEls = view.words.map((w) => el('button', {
    type: 'button', class: 'word deva', translate: 'no', title: `${fmt(w.s)}`, text: w.w,
    onclick: () => { f[pick === 'start' ? 'source_in' : 'source_out'].value = (pick === 'start' ? w.s : w.e).toFixed(2); trimChanged(); },
  }));
  wordBox.append(...wordEls);
  const pickBtn = (which, label) => el('button', { type: 'button', class: 'btn', 'data-pick': which, text: label, 'aria-pressed': which === pick ? 'true' : 'false',
    onclick: () => { pick = which; for (const b of pickRow.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.pick === pick)); } });
  const pickRow = el('div', { class: 'row' }, pickBtn('start', 'Click a word to set the start'), pickBtn('end', 'Click a word to set the end'));
  function trimChanged() {
    const a = parseFloat(f.source_in.value), b = parseFloat(f.source_out.value);
    length.textContent = Number.isFinite(a) && Number.isFinite(b) ? `${(b - a).toFixed(1)} s` : '';
    wordEls.forEach((el_, i) => { const w = view.words[i]; el_.classList.toggle('in', w.s >= a - 0.01 && w.e <= b + 0.01); });
  }
  f.source_in.addEventListener('input', trimChanged); f.source_out.addEventListener('input', trimChanged);

  const cap = view.captions;
  f.cap_enabled = el('input', { id: 'cap_enabled', type: 'checkbox' }); f.cap_enabled.checked = cap.enabled;
  choose('cap_words', [1, 2, 3, 4, 5, 6, 7, 8].map((n) => [String(n), `${n} word${n > 1 ? 's' : ''} at a time`]), String(cap.max_words));
  choose('cap_font', view.fonts.map((x) => [x, x]), cap.font_family);
  f.cap_color = el('input', { id: 'cap_color', type: 'color', value: cap.color });
  f.cap_hl = el('input', { id: 'cap_hl', type: 'color', value: cap.highlight_color });
  const capPos = el('output', { text: String(cap.top_px) });
  f.cap_top = el('input', { id: 'cap_top', type: 'range', min: '0', max: '1800', step: '10', value: String(cap.top_px) });
  f.cap_top.addEventListener('input', () => { capPos.textContent = f.cap_top.value; });

  choose('framing', [['', `Keep (${{ track: 'face-tracked crop', fit_blur: 'whole frame', cover: 'centre crop', split: 'two people stacked' }[view.framing]})`],
    ['auto', 'Follow the faces (plan it again)'], ['cover', 'Centre crop'], ['fit_blur', 'Whole frame over a blurred copy']], '');
  choose('multi', [['switch', 'Cut to whoever is talking'], ['split', 'Stack two people'], ['off', 'Follow one face']], 'switch');
  const multiField = field('When several people are on screen', f.multi);
  const syncMulti = () => { multiField.hidden = f.framing.value !== 'auto'; };
  f.framing.addEventListener('change', syncMulti); syncMulti();
  f.punch = el('input', { id: 'punch', type: 'checkbox' }); f.punch.checked = view.punch_ins;

  function body() {
    const out = {};
    const text = (key, id = key) => { if (f[id].value !== view[key]) out[key] = f[id].value; };
    text('title'); text('hook'); text('speaker_name'); text('speaker_title'); text('stat');
    const a = parseFloat(f.source_in.value), b = parseFloat(f.source_out.value);
    if (Math.abs(a - view.source_in) > 0.001) out.source_in = a;
    if (Math.abs(b - view.source_out) > 0.001) out.source_out = b;
    const c = {};
    if (f.cap_enabled.checked !== cap.enabled) c.enabled = f.cap_enabled.checked;
    if (Number(f.cap_words.value) !== cap.max_words) c.max_words = Number(f.cap_words.value);
    if (f.cap_font.value !== cap.font_family) c.font_family = f.cap_font.value;
    if (f.cap_color.value.toLowerCase() !== cap.color.toLowerCase()) c.color = f.cap_color.value.toUpperCase();
    if (f.cap_hl.value.toLowerCase() !== cap.highlight_color.toLowerCase()) c.highlight_color = f.cap_hl.value.toUpperCase();
    if (Number(f.cap_top.value) !== cap.top_px) c.top_px = Number(f.cap_top.value);
    if (Object.keys(c).length) out.captions = c;
    if (f.punch.checked !== view.punch_ins) out.punch_ins = f.punch.checked;
    const drop = [...app.querySelectorAll('[data-drop]')].filter((x) => x.checked).map((x) => x.dataset.drop);
    if (drop.length) out.drop_pictures = drop;
    if (f.framing.value) { out.framing = f.framing.value; if (f.framing.value === 'auto') out.multi = f.multi.value; }
    return out;
  }

  function showPlayer(r) {
    if (shownRev === r.rev && player.firstChild) return;
    shownRev = r.rev;
    player.replaceChildren(el('video', { controls: true, playsinline: true, preload: 'metadata', src: `/api/jobs/${jobId}/clips/${clipId}/video?v=${r.rev}`, 'aria-label': 'Preview of this clip' }));
  }
  function showStatus(r) {
    if (r.render === 'queued' || r.render === 'rendering') status.textContent = 'Re-rendering with your changes… the video above updates when it is ready.';
    else if (r.render === 'failed') status.textContent = `The re-render failed: ${r.render_error} Press Save again to retry.`;
    else status.textContent = '';
    saveBtn.disabled = r.render === 'queued' || r.render === 'rendering';
    resetBtn.hidden = !view.edited;
  }
  const refresh = async () => {
    view = await api(base);
    showPlayer(view.render); showStatus(view.render);
    return view.render;
  };
  async function poll() {
    if (dead) return;
    try {
      const r = await refresh();
      if (r.render === 'queued' || r.render === 'rendering') { timer = setTimeout(poll, 1500); }
    } catch (e) { if (e instanceof SignInNeeded) { route(); return; } if (!dead) timer = setTimeout(poll, 3000); }
  }

  const saveBtn = el('button', { class: 'btn primary', type: 'button', text: 'Save and re-render', onclick: async () => {
    err.hidden = true;
    const b = body();
    if (!Object.keys(b).length) { status.textContent = 'Nothing has changed.'; return; }
    saveBtn.disabled = true;
    try {
      const r = await api(base, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
      status.textContent = r.changed ? 'Saved. Re-rendering…' : 'Nothing has changed.';
      await refresh();
      f.framing.value = ''; syncMulti();
      view.render.render === 'ready' || (clearTimeout(timer), (timer = setTimeout(poll, 1500)));
    } catch (e) { if (e instanceof SignInNeeded) { route(); return; } err.textContent = e.message; err.hidden = false; saveBtn.disabled = false; }
  } });
  const resetBtn = el('button', { class: 'btn', type: 'button', text: 'Reset to the AI plan', hidden: true, onclick: async () => {
    if (!confirm('Throw away your edits to this clip and go back to the AI plan?')) return;
    try { await api(`${base}/reset`, { method: 'POST' }); await route(); } catch (e) { err.textContent = e.message; err.hidden = false; }
  } });

  const group = (title, ...kids) => el('fieldset', { class: 'card group' }, el('legend', { text: title }), ...kids);
  app.replaceChildren(el('div', { class: 'stack' },
    el('div', { class: 'row' }, el('div', { class: 'grow' }, el('h1', { text: 'Edit clip' }),
      el('div', { class: 'small muted', translate: 'no', text: view.title })), el('a', { class: 'btn', href: `#/jobs/${jobId}`, text: 'Back to the job' })),
    el('div', { class: 'cols' },
      el('div', { class: 'stack', style: { gap: '12px' } }, player, status),
      el('div', { class: 'stack grow', style: { flex: '999 1 420px' } },
        group('Words on screen', field('Title', f.title), field('Hook line (first seconds)', f.hook, 'Leave empty to remove it.'),
          el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Speaker name', f.speaker_name), field('Role', f.speaker_title)),
          field('Stat pill', f.stat, 'A short figure like "10x". Leave empty to remove it.')),
        group('Cut points', el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Start (seconds)', f.source_in), field('End (seconds)', f.source_out)),
          length, pickRow, wordBox),
        group('Captions', el('label', { class: 'check', for: 'cap_enabled' }, f.cap_enabled, el('span', { text: 'Show captions' })),
          el('div', { class: 'grid2', style: { marginTop: '12px' } }, field('Words per line', f.cap_words), field('Font', f.cap_font),
            field('Text colour', f.cap_color), field('Highlight colour', f.cap_hl)),
          el('div', { style: { marginTop: '12px' } }, el('label', { class: 'field', for: 'cap_top' }, 'Height on screen ', capPos, ' px from the top'), f.cap_top)),
        view.pictures.length > 0 && group('Pictures over the video',
          ...view.pictures.map((p) => el('label', { class: 'check', for: `drop-${p.id}` },
            el('input', { type: 'checkbox', id: `drop-${p.id}`, 'data-drop': p.id }),
            el('span', {}, el('strong', { translate: 'no', text: p.caption || 'Picture' }), ` at ${fmt(p.t || 0)}`, el('br'),
              el('span', { class: 'muted', text: `${p.ai === 'generated' ? 'AI-generated' : 'Page screenshot'} · tick to remove it` }))))),
        group('Framing', field('Picture', f.framing), multiField,
          el('label', { class: 'check', for: 'punch' }, f.punch, el('span', {}, el('strong', { text: 'Slow push-ins' }), el('br'),
            el('span', { class: 'muted', text: 'The picture eases in as a new sentence begins, then snaps back.' })))),
        err,
        el('div', { class: 'row' }, saveBtn, resetBtn)))));
  trimChanged();
  showPlayer(view.render); showStatus(view.render);
  if (view.render.render === 'queued' || view.render.render === 'rendering') timer = setTimeout(poll, 1500);
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

// ---- sharing ---------------------------------------------------------------------------------------

const STATUS_WORD = { queued: 'Waiting…', sent: 'Posted', scheduled: 'Scheduled', failed: 'Failed' };
const PLATFORM_WORD = { youtube: 'YouTube', instagram: 'Instagram', 'instagram-standalone': 'Instagram', tiktok: 'TikTok' };

async function shareView(jobId, clipId) {
  const [job, channels, usage] = await Promise.all([api(`/jobs/${jobId}`), api('/publish/channels'), api(`/jobs/${jobId}/usage`).catch(() => ({ disclosure_recommended: false }))]);
  const clip = job.clips.find((c) => c.id === clipId);
  if (!clip) { app.replaceChildren(el('div', { class: 'card empty', text: 'No such clip.' })); return { cleanup() {} }; }
  let timer = null, dead = false;
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const list = el('div', { class: 'stack', style: { gap: '8px' } });
  const boxes = channels.map((ch) => ({ ch, box: el('input', { type: 'checkbox', id: `ch-${ch.id}`, 'data-ch': ch.id }) }));
  const title = el('input', { id: 'sh-title', type: 'text', maxlength: '100', class: 'deva', value: clip.title });
  const caption = el('textarea', { id: 'sh-caption', rows: '4', class: 'deva', maxlength: '2000' });
  caption.value = [clip.title, clip.hook].filter(Boolean).join('\n');
  const privacy = el('select', { id: 'sh-privacy' }, [['public', 'Public'], ['unlisted', 'Unlisted (YouTube; followers only on TikTok)'], ['private', 'Private']].map(([v, t]) => el('option', { value: v, text: t })));
  const when = el('input', { id: 'sh-when', type: 'datetime-local' });
  const kids = el('input', { id: 'sh-kids', type: 'checkbox' });
  const ai = el('input', { id: 'sh-ai', type: 'checkbox' });
  const send = el('button', { class: 'btn primary', type: 'button', text: 'Post' });

  function paint(rows) {
    list.replaceChildren(...rows.map((r) => el('div', { class: 'row small' },
      el('span', { class: `badge ${r.status === 'failed' ? 'failed' : r.status === 'queued' ? 'running' : 'done'}`, text: STATUS_WORD[r.status] || r.status }),
      el('span', { translate: 'no', text: `${PLATFORM_WORD[r.platform] || r.platform} · ${r.name}` }),
      r.when && el('span', { class: 'muted', text: `for ${when_(r.when)}` }),
      r.detail && el('span', { class: 'muted', text: r.detail }))));
  }
  const when_ = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  async function refresh() {
    if (dead) return;
    try {
      const rows = await api(`/jobs/${jobId}/clips/${clipId}/posts`);
      paint(rows);
      if (rows.some((r) => r.status === 'queued')) timer = setTimeout(refresh, 1500);
    } catch (e) { if (e instanceof SignInNeeded) route(); }
  }
  send.addEventListener('click', async () => {
    err.hidden = true;
    const chosen = boxes.filter((b) => b.box.checked).map((b) => b.ch.id);
    if (!chosen.length) { err.textContent = 'Choose at least one channel.'; err.hidden = false; return; }
    const body = { channels: chosen, title: title.value, caption: caption.value, privacy: privacy.value, made_for_kids: kids.checked, ai_confirmed: ai.checked };
    if (when.value) body.when = new Date(when.value).toISOString();
    send.disabled = true;
    try {
      await api(`/jobs/${jobId}/clips/${clipId}/publish`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      await refresh();
    } catch (e) { if (e instanceof SignInNeeded) { route(); return; } err.textContent = e.message; err.hidden = false; }
    send.disabled = false;
  });
  const field = (label, control, hint) => el('div', {}, el('label', { class: 'field', for: control.id, text: label }), control, hint && el('div', { class: 'hint', text: hint }));
  app.replaceChildren(el('div', { class: 'stack' },
    el('div', { class: 'row' }, el('div', { class: 'grow' }, el('h1', { text: 'Share clip' }), el('div', { class: 'small muted', translate: 'no', text: clip.title })),
      el('a', { class: 'btn', href: `#/jobs/${jobId}`, text: 'Back to the job' })),
    channels.length === 0 ? el('div', { class: 'card empty', text: 'No YouTube, Instagram or TikTok channels are connected in SocioBird yet.' }) : el('div', { class: 'card stack' },
      el('fieldset', { class: 'stack', style: { border: 0, padding: 0, margin: 0 } }, el('legend', { class: 'field', text: 'Post to' }),
        ...boxes.map(({ ch, box }) => el('label', { class: 'check', for: box.id }, box, el('span', {}, el('strong', { translate: 'no', text: ch.name }), el('br'), el('span', { class: 'muted', text: PLATFORM_WORD[ch.platform] || ch.platform }))))),
      field('Title', title, 'YouTube and TikTok show it; Instagram uses the caption.'),
      field('Caption', caption, 'Plain text. Hashtags and links are fine.'),
      el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Who can see it', privacy), field('Post at (your time, optional)', when, 'Leave empty to post now.')),
      el('label', { class: 'check', for: 'sh-kids' }, kids, el('span', {}, el('strong', { text: 'This video is made for kids' }), el('br'), el('span', { class: 'muted', text: 'YouTube asks every creator to declare this.' }))),
      usage.disclosure_recommended && el('label', { class: 'check', for: 'sh-ai' }, ai, el('span', {}, el('strong', { text: 'I will label this video as AI content' }), el('br'),
        el('span', { class: 'muted', text: 'It includes AI-generated pictures. TikTok’s own AI label is switched on for you; on YouTube and Instagram set the label in the app if it asks.' }))),
      err, el('div', {}, send)),
    el('section', { class: 'card stack', hidden: false }, el('h2', { text: 'Posts' }), list)));
  await refresh();
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

// ---- router -----------------------------------------------------------------------------------
let cleanup = null;
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || '#/';
  renderAcct();
  document.querySelectorAll('nav a').forEach((a) => {
    const on = (a.dataset.nav === 'new' && (hash === '#/' || hash === '#' || hash === '#/new')) || (a.dataset.nav === 'jobs' && hash.startsWith('#/jobs'));
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  try {
    if (state.config.needs_login && !state.config.signed_in) return loginView();
    let m;
    if (hash === '#/' || hash === '#' || hash === '#/new') newJobView();
    else if (hash === '#/jobs') await jobsView();
    else if (hash === '#/account') await accountView();
    else if ((m = hash.match(/^#\/jobs\/([a-f0-9]{12})\/clips\/(c\d{1,3})\/share$/))) { const v = await shareView(m[1], m[2]); cleanup = v.cleanup; }
    else if ((m = hash.match(/^#\/jobs\/([a-f0-9]{12})\/clips\/(c\d{1,3})$/))) { const v = await clipEditorView(m[1], m[2]); cleanup = v.cleanup; }
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
