// ClipCrow web UI. No framework, no build step. Every value that came from a job (file names, model text)
// is inserted as text, never parsed as markup, and there are no inline handlers (the CSP forbids them).
// Look and feel: read docs/ui.md before adding a screen or a control (shared classes, tokens only, every input styled like a field, both themes).

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

function icon(name, size = 22) {          // drawn by the stylesheet (a CSS mask), so no markup is built here
  const i = el('span', { class: `i i-${name}`, 'aria-hidden': 'true' });
  i.style.width = i.style.height = `${size}px`;
  return i;
}

// ---- small help: an "i" holds the longer explanation, so the page itself stays short --------------------------
// Opens on hover, focus or tap; stays open while the pointer is on it; Escape closes it (WCAG 1.4.13). Nothing essential lives only here.
const tipPop = el('div', { class: 'tip-pop', role: 'tooltip', id: 'tip-pop', hidden: true });
let tipOwner = null, tipPinned = false, tipTimer = 0;
function placeTip(btn) {
  tipPop.hidden = false;
  const r = btn.getBoundingClientRect(), w = tipPop.offsetWidth, h = tipPop.offsetHeight;
  const left = Math.min(Math.max(r.left + r.width / 2 - w / 2, 12), Math.max(12, window.innerWidth - w - 12));
  const top = r.bottom + 8 + h <= window.innerHeight ? r.bottom + 8 : Math.max(12, r.top - 8 - h);
  tipPop.style.left = `${left}px`;
  tipPop.style.top = `${top}px`;
}
function openTip(btn, pin) {
  clearTimeout(tipTimer);
  if (tipOwner && tipOwner !== btn) tipOwner.setAttribute('aria-expanded', 'false');
  tipOwner = btn;
  tipPinned = pin;
  tipPop.textContent = btn.dataset.tip;
  btn.setAttribute('aria-expanded', 'true');
  btn.setAttribute('aria-describedby', 'tip-pop');
  placeTip(btn);
}
function closeTip(force) {
  clearTimeout(tipTimer);
  if (!tipOwner || (tipPinned && !force)) return;
  tipOwner.setAttribute('aria-expanded', 'false');
  tipOwner.removeAttribute('aria-describedby');
  tipOwner = null;
  tipPinned = false;
  tipPop.hidden = true;
}
const closeSoon = () => { clearTimeout(tipTimer); tipTimer = setTimeout(() => closeTip(false), 220); };
tipPop.addEventListener('pointerenter', () => clearTimeout(tipTimer));
tipPop.addEventListener('pointerleave', closeSoon);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && tipOwner) closeTip(true); });
document.addEventListener('pointerdown', (e) => { if (tipOwner && e.target !== tipOwner && !tipOwner.contains(e.target) && !tipPop.contains(e.target)) closeTip(true); });
window.addEventListener('scroll', () => closeTip(true), { passive: true });
document.body.append(tipPop);

function tip(text, about = '') {
  const b = el('button', { class: 'info', type: 'button', 'aria-label': about ? `More about ${about}` : 'More about this', 'aria-expanded': 'false' }, icon('info', 16));
  b.dataset.tip = text;
  b.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') { clearTimeout(tipTimer); tipTimer = setTimeout(() => openTip(b, false), 120); } });
  b.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') closeSoon(); });
  b.addEventListener('focus', () => { if (b.matches(':focus-visible')) openTip(b, false); });
  b.addEventListener('blur', () => closeTip(false));
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); if (tipOwner === b && tipPinned) closeTip(true); else openTip(b, true); });
  return b;
}

// A platform's logo (its colour comes from a token, in both themes).
const PLATFORM_ICON = { youtube: 'youtube', instagram: 'instagram', 'instagram-standalone': 'instagram', tiktok: 'tiktok', facebook: 'facebook', x: 'x', linkedin: 'linkedin',
  'linkedin-page': 'linkedin', telegram: 'telegram', threads: 'threads', pinterest: 'pinterest', bluesky: 'bluesky', reddit: 'reddit', whatsapp: 'whatsapp', snapchat: 'snapchat', mastodon: 'mastodon' };
function platformMark(platform) {
  const name = PLATFORM_ICON[platform];
  return name ? el('span', { class: `i i-plat-${name} plat plat-${name}`, 'aria-hidden': 'true' }) : null;
}

// ---- the same long explanations, tucked away once and for every page --------------------------------------------
// A hint longer than a few words, and the grey explanation under a checkbox, become an "i" next to their label. A hint that must stay visible
// (a limit someone needs before choosing a file) carries data-keep; a status line (role/aria-live) never moves.
const BUTTON_ICONS = [[/^Download all/, 'archive'], [/^Replace/, 'refresh-cw'], [/^Download/, 'download'], [/^Share$/, 'share-2'], [/^Edit\b/, 'pencil'], [/^(Delete|Remove)/, 'trash-2'], [/^Generate/, 'sparkles'],
  [/^Upload/, 'upload'], [/^(Save|Apply)/, 'check'], [/^Post\b/, 'send'], [/^(Cancel|Stop)/, 'x'], [/^Cut again/, 'scissors'], [/^Run again/, 'refresh-cw'], [/^Find who/, 'users'], [/^Cover image/, 'image'],
  [/^(Re-?render|Render)/, 'refresh-cw'], [/^Reset/, 'rotate-ccw'], [/^(Sign out|Log out)/, 'log-out'], [/^(Add|Create|New|Start)\b/, 'plus'], [/^Choose all/, 'circle-check'],
  [/^Copy/, 'copy'], [/^Back/, 'chevron-right']];
const HEADING_ICONS = [[/^(Logo|Pictures)/, 'image'], [/^Ending video/, 'film'], [/^Background music/, 'music'], [/^Look/, 'palette'], [/^Clips$/, 'film'], [/^Posts$/, 'send'], [/^Post to/, 'share-2'],
  [/^What happens next/, 'list'], [/^Speech recognition/, 'mic'], [/^Reference|^Styles?\b/, 'palette'], [/^Jobs$/, 'list'], [/^New job/, 'plus'], [/^Share clip/, 'share-2'], [/^Brand$/, 'badge-check']];
const SUMMARY_ICONS = [[/^Activity/, 'list'], [/^What was cut/, 'scissors'], [/^Who is who/, 'users'], [/^Check or fix/, 'type'], [/^AI usage/, 'sparkles'], [/^More options/, 'sliders-horizontal']];
function decorate(root) {
  for (const hint of root.querySelectorAll('.hint:not([data-done]):not([data-keep])')) {
    hint.dataset.done = '1';
    const label = hint.parentElement && hint.parentElement.querySelector(':scope > label.field');
    if (!label || hint.getAttribute('role') || hint.hasAttribute('aria-live') || hint.children.length || hint.textContent.trim().length < 24) continue;
    label.append(tip(hint.textContent.trim(), label.textContent.trim()));
    hint.remove();
  }
  for (const note of root.querySelectorAll('label.check > span > .muted:not([data-done])')) {
    note.dataset.done = '1';
    const strong = note.parentElement.querySelector(':scope > strong');
    if (!strong || note.children.length || note.textContent.trim().length < 24) continue;
    strong.after(tip(note.textContent.trim(), strong.textContent.trim()));
    const br = note.previousElementSibling;
    if (br && br.tagName === 'BR') br.remove();
    note.remove();
  }
  for (const b of root.querySelectorAll('.btn:not([data-icon])')) {
    b.dataset.icon = '1';
    if (b.querySelector('.i')) continue;
    const hit = BUTTON_ICONS.find(([re]) => re.test(b.textContent.trim()));
    if (!hit) continue;
    const i = icon(hit[1], 17);
    if (hit[1] === 'chevron-right' && /^Back/.test(b.textContent.trim())) i.classList.add('i-flip');
    b.prepend(i);
  }
  for (const h of root.querySelectorAll('h1:not([data-icon]), .card > h2:not([data-icon]), section > h2:not([data-icon])')) {
    h.dataset.icon = '1';
    const hit = HEADING_ICONS.find(([re]) => re.test(h.textContent.trim()));
    if (hit) h.prepend(icon(hit[1], h.tagName === 'H1' ? 26 : 20));
  }
  for (const s of root.querySelectorAll('details > summary:not([data-icon])')) {
    s.dataset.icon = '1';
    const hit = SUMMARY_ICONS.find(([re]) => re.test(s.textContent.trim()));
    if (hit) s.prepend(icon(hit[1], 18));
  }
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
  return { score: 'Score the sections', detail: n === undefined ? 'Write the title and hook' : `Pick clips, section ${Number(n) + 1}`, repair: `Fix the script, section ${Number(n) + 1}`,
    'retake-judge': 'Read the recording and judge the cuts', 'retake-verify': 'Read the result through', 'text-refine': 'Correct the Marathi/Hindi text' }[kind] || p;
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
function authLogo() {
  return el('img', { class: 'auth-logo', src: '/logo.png', alt: 'ClipCrow', width: 240, height: 200 });
}

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
    authLogo(),
    el('h1', { text: 'Sign in' }),
    el('div', {}, el('label', { class: 'field', for: 'token', text: 'Access token' }), input),
    err,
    el('button', { class: 'btn dark', type: 'submit', text: 'Sign in' }));
  app.replaceChildren(el('div', { class: 'authwrap' }, form));
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
  } }, authLogo(), title,
    el('div', {}, el('label', { class: 'field', for: 'email', text: 'Email' }), email),
    el('div', {}, el('label', { class: 'field', for: 'password', text: 'Password' }), pw, hint),
    err, el('div', { class: 'row' }, submit, toggle));
  paint();
  app.replaceChildren(el('div', { class: 'authwrap' }, form));
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


function pictureField() {
  const picked = [];
  const list = el('div', { class: 'pic-list' });
  const msg = el('div', { class: 'hint', role: 'status' });
  const input = el('input', { type: 'file', id: 'pictures_file', accept: 'image/png,image/jpeg,image/webp', multiple: true });
  const paint = () => {
    list.replaceChildren(...picked.map((p, i) => el('div', { class: 'pic' },
      el('img', { src: `/api/pictures/${p.id}`, alt: `Picture ${i + 1}`, loading: 'lazy' }),
      el('button', { class: 'btn', type: 'button', text: 'Remove', 'aria-label': `Remove picture ${i + 1}`, onclick: async () => {
        try { await api(`/pictures/${p.id}`, { method: 'DELETE' }); } catch (_) { /* already gone */ }
        picked.splice(picked.indexOf(p), 1);
        paint();
      } }))));
    input.disabled = picked.length >= 6;
  };
  input.addEventListener('change', async () => {
    msg.textContent = '';
    for (const file of [...input.files]) {
      if (picked.length >= 6) { msg.textContent = 'Up to 6 pictures.'; break; }
      try {
        const fd = new FormData();
        fd.append('file', file);
        picked.push({ id: (await api('/pictures', { method: 'POST', body: fd })).id });
        paint();
      } catch (ex) { msg.textContent = `${file.name}: ${ex.message}`; }
    }
    input.value = '';
  });
  const node = el('div', {}, el('label', { class: 'field', for: 'pictures_file', text: 'Pictures to show (up to 6)' }), input, list, msg,
    el('div', { class: 'hint', text: 'Screenshots of a news page, a chart or a document. PNG, JPEG or WebP, up to 10 MB each. The AI looks at every picture and decides which moment it fits, and may leave one out. The pictures are sent to the AI service so it can see them: only add what you may share.' }));
  return { node, ids: () => picked.map((p) => p.id).join(',') };
}

async function newJobView() {
  const c = state.config;
  let brand = { logo: false, outro: false, settings: {} };
  try { brand = await api('/brand'); } catch (e) { if (e instanceof SignInNeeded) throw e; }
  let styles = [];
  try { styles = (await api('/styles')).filter((x) => x.status === 'ready'); } catch (e) { if (e instanceof SignInNeeded) throw e; }
  let file = null;
  const fileName = el('div', { class: 'small muted', text: `MP4, MOV, MKV or WebM, up to ${c.max_upload_mb} MB` });
  const fileTitle = el('div', { class: 'ttl', text: 'Drop a video here, or choose a file' });
  const fileInput = el('input', { type: 'file', id: 'file', accept: 'video/*,.mkv,.m4v', 'aria-label': 'Video file' });
  const drop = el('label', { class: 'drop', for: 'file' }, fileInput, el('span', { class: 'ico' }, icon('upload', 24)), el('div', {}, fileTitle, fileName));
  const setFile = (f) => {
    file = f;
    drop.classList.toggle('has', !!f);
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

  const pics = pictureField();
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

  const form = el('form', { class: 'stack', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    err.hidden = true;
    const fail = (m) => { err.textContent = m; err.hidden = false; submit.disabled = false; cancelBtn.hidden = true; progress.hidden = true; status.textContent = ''; };
    if (!file) return fail('Choose a video first.');
    if (file.size > c.max_upload_mb * 1048576) return fail(`That file is larger than the ${c.max_upload_mb} MB limit.`);
    if (!rights.checked) return fail('Confirm that you own this video or have the rights to process it.');
    const lo = $('min_clip').value.trim(), hi = $('max_clip').value.trim();
    if ((lo && !(Number(lo) >= 5)) || (hi && !(Number(hi) >= 10))) return fail('The shortest reel is at least 5 seconds and the longest at least 10.');
    if (lo && hi && Number(lo) >= Number(hi)) return fail('The shortest reel must be shorter than the longest.');
    if (Number(lo) > 900 || Number(hi) > 900) return fail('A reel can be at most 15 minutes (900 seconds).');
    const whole = !lo && !hi && !$('style').value;                       // no length asked for: the whole video is one reel
    // only one end given: the other follows from it
    let minClip = lo, maxClip = hi;
    if (lo && !hi) maxClip = String(Math.min(900, Math.max(Number(lo) * 2, Number(lo) + 30)));
    if (hi && !lo) minClip = String(Math.max(5, Math.min(Number(hi) - 5, Math.round(Number(hi) / 3))));
    if ($('style').value) { minClip = ''; maxClip = ''; }               // a style sets the clip length
    const options = {
      style: $('style').value,
      language: $('language').value, clips: $('clips').value, min_clip: minClip, max_clip: maxClip, spread: $('spread').value,
      multi: $('multi').value, graphics: $('graphics').checked ? 'true' : '', whole: whole ? 'true' : '', clean: $('clean').checked ? 'true' : '', clean_pace: $('clean_pace').value, clean_take: $('clean_take').value, fresh_transcript: $('fresh_transcript').checked ? 'true' : '', brand: $('brand') ? $('brand').value : '', speaker_name: $('speaker').value, speaker_title: $('role').value,
      cutout: $('cutout').checked ? 'true' : '', immersive: $('immersive').checked ? 'true' : '',
      explainer: $('explainer').checked ? 'true' : '', pictures: pics.ids(), ai_images: $('ai_images').checked ? 'true' : '',
      cover: $('cover').checked ? 'true' : '', logo: $('logo').checked ? 'true' : '', outro: $('outro').checked ? 'true' : '', sounds: $('sounds').checked ? 'true' : '', music: $('music').checked ? 'true' : '', transition: $('transition').value,
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
      el('div', {}, el('label', { class: 'field', for: 'min_clip', text: 'Shortest reel (seconds)' }),
        el('input', { id: 'min_clip', type: 'number', min: '5', max: '900', value: '15', inputmode: 'numeric' }),
        el('div', { class: 'hint', text: 'Leave both lengths empty and the whole video becomes one reel (up to 15 minutes), with one title and hook.' })),
      el('div', {}, el('label', { class: 'field', for: 'max_clip', text: 'Longest reel (seconds)' }),
        el('input', { id: 'max_clip', type: 'number', min: '10', max: '900', value: '60', inputmode: 'numeric' }),
        el('div', { class: 'hint', text: 'Give only one and the other follows from it. Longer reels suit a cleaned raw recording.' })),
      select('style', 'Style from a reference', [['', 'None'], ...styles.map((x) => [x.id, x.name])],
        styles.length ? 'Uses what was measured in that reference: clip length, caption size, colour and position, and how often the picture pushes in.'
          : 'Measure a reference video on the Styles page, then pick it here.')),
    el('details', { class: 'adv' }, el('summary', {}, 'More options', el('span', { class: 'sub', text: 'spread, speakers, text behind, cover, logo, push-ins' })),
      el('div', { class: 'stack' },
      el('div', { class: 'grid2', style: { marginTop: 0 } },
      select('spread', 'Spread across the video', [['0.5', 'Balanced (recommended)'], ['0', 'Off: best clips first'], ['0.8', 'Wide']],
        'Keeps clips from being slices of one section.'),
      select('multi', 'Several people on screen', [['off', 'One person on screen (recommended, quickest)'], ['switch', 'Several people: cut to whoever is talking'], ['split', 'Several people: stack two']],
        'Choose “Several people” for interviews and podcasts filmed with a wide shot. One or two people are cut the usual way, which is quicker.'),
      el('label', { class: 'check', for: 'fresh_transcript' }, el('input', { type: 'checkbox', id: 'fresh_transcript' }),
        el('span', {}, el('strong', { text: 'Listen to the recording again' }), el('br'),
          el('span', { class: 'muted', text: 'If you already made a job from this very file, its words are used again and nothing is transcribed twice. Tick this to transcribe it afresh (for example after changing the recogniser).' }))),
      text('speaker', 'Speaker name (lower third)', { autocomplete: 'off', class: 'deva' }),
      text('role', 'Role', { autocomplete: 'off', class: 'deva' })),
      el('div', { class: 'stack' },
      el('label', { class: 'check', for: 'cutout' }, el('input', { type: 'checkbox', id: 'cutout' }),
        el('span', {}, el('strong', { text: 'Text behind the speaker' }), el('br'),
          el('span', { class: 'muted', text: 'Cuts the person out so a big word can sit behind them. Slower to render; best for a single face.' }))),
      el('label', { class: 'check', for: 'explainer' }, el('input', { type: 'checkbox', id: 'explainer' }),
        el('span', {}, el('strong', { text: 'Explainer: show pictures' }), el('br'),
          el('span', { class: 'muted', text: 'Puts a picture over the video where the speaker names something: a picture you add below (the AI looks at it and picks the moment), or an AI-generated image.' }))),
      el('div', { id: 'explainer-opts', class: 'stack', hidden: true, style: { paddingLeft: '30px' } },
        pics.node,
        el('label', { class: c.ai_images ? 'check' : 'check muted', for: 'ai_images' }, el('input', { type: 'checkbox', id: 'ai_images', disabled: !c.ai_images }),
          el('span', {}, el('strong', { text: 'Also generate images with AI' }), el('br'),
            el('span', { class: 'muted', text: c.ai_images ? 'AI-generated pictures mean most platforms ask you to label the video as AI content.' : 'Not set up on this server.' })))),
      el('div', {}, el('label', { class: 'field', for: 'transition', text: 'Effect on every hard cut' }),
        el('select', { id: 'transition' }, [['none', 'None'], ['flash', 'Flash'], ['blur', 'Blur'], ['dip', 'Dip to black'], ['whip', 'Whip']].map(([v, t]) => el('option', { value: v, text: t }))),
        el('div', { class: 'hint', text: 'A short effect where two stretches of the recording were joined, the speaker changes or a slide starts. You can change it per clip afterwards.' })),
      el('label', { class: 'check', for: 'sounds' }, el('input', { type: 'checkbox', id: 'sounds' }),
        el('span', {}, el('strong', { text: 'Quiet sound cues' }), el('br'),
          el('span', { class: 'muted', text: 'A soft pop, whoosh or tick under the speech where a title, card, picture or push-in appears, never on every word. Switch it off per clip when you edit.' }))),
      brand.brands && brand.brands.length > 1 && el('div', {}, el('label', { class: 'field', for: 'brand', text: 'Brand' }),
        el('select', { id: 'brand' }, brand.brands.map((b) => el('option', { value: b.id, text: b.name + (b.default ? ' (default)' : '') }))),
        el('div', { class: 'hint', text: 'Logo, ending video, music, look, speaker and captions come from this brand.' })),
      el('label', { class: 'check', for: 'cover' }, el('input', { type: 'checkbox', id: 'cover', checked: !!brand.settings.default_cover }),
        el('span', {}, el('strong', { text: 'Cover title on the first frames' }), el('br'),
          el('span', { class: 'muted', text: 'A bold title (the hook line) for the first quarter-second, so the poster frame says what the clip is about. You also get it as a picture. Platforms choose their own thumbnail, so this raises the odds, it cannot force it.' }))),
      el('label', { class: brand.logo ? 'check' : 'check muted', for: 'logo' }, el('input', { type: 'checkbox', id: 'logo', disabled: !brand.logo, checked: brand.logo && !!brand.settings.default_logo }),
        el('span', {}, el('strong', { text: 'Logo in a corner' }), el('br'),
          el('span', { class: 'muted', id: 'logo-hint' }, brand.logo ? 'Your logo, from the Brand page.' : ['No logo yet. ', el('a', { href: '#/brand', text: 'Add one on the Brand page' }), '.']))),
      el('label', { class: brand.outro ? 'check' : 'check muted', for: 'outro' }, el('input', { type: 'checkbox', id: 'outro', disabled: !brand.outro, checked: brand.outro && !!brand.settings.default_outro }),
        el('span', {}, el('strong', { text: 'Ending video after each clip' }), el('br'),
          el('span', { class: 'muted', id: 'outro-hint' }, brand.outro ? `Your branding video (${brand.outro_seconds} s), added at the end.` : ['No ending video yet. ', el('a', { href: '#/brand', text: 'Add one on the Brand page' }), '.']))),
      el('label', { class: brand.music ? 'check' : 'check muted', for: 'music' }, el('input', { type: 'checkbox', id: 'music', disabled: !brand.music, checked: brand.music && !!brand.settings.default_music }),
        el('span', {}, el('strong', { text: 'Background music (ducks under the speech)' }), el('br'),
          el('span', { class: 'muted', id: 'music-hint' }, brand.music ? `${brand.music_name} (${brand.music_seconds} s), kept below the voice and lowered further while someone talks.` : ['No music chosen. ', el('a', { href: '#/brand', text: 'Pick a track or add your own on the Brand page' }), '.']))),
      el('label', { class: 'check', for: 'clean' }, el('input', { type: 'checkbox', id: 'clean' }),
        el('span', {}, el('strong', { text: 'Raw recording: cut fumbles and dead air' }), el('br'),
          el('span', { class: 'muted', text: 'Removes ums and ahs, stutters, a sentence the speaker started again, “sorry, once more” takes and long silences, keeping picture and sound in sync. Best for one person talking to camera; the clips are made from the cleaned video. Uses the AI to spot restarted takes.' }))),
      el('div', { id: 'clean-opts', hidden: true }, select('clean_pace', 'How tight', [['natural', 'Natural: keep short pauses (talks, courses, interviews)'], ['fast', 'Fast: tighter (YouTube talking head)'], ['tight', 'Tight: jump-cut style (Reels and Shorts)']]),
        select('clean_take', 'When a passage is said twice', [['last', 'Keep the last take'], ['best', 'Keep the best take (the AI judges which)']])),
      el('label', { class: 'check', for: 'graphics' }, el('input', { type: 'checkbox', id: 'graphics' }),
        el('span', {}, el('strong', { text: 'Video has slides, cards or graphics' }), el('br'),
          el('span', { class: 'muted', text: 'For explainers that cut away from the speaker to text cards, charts or pictures. Those moments are shown whole instead of cropped to the middle, so the text is not cut off. Moments with the speaker are still framed on their face.' }))),
      el('label', { class: 'check', for: 'immersive' }, el('input', { type: 'checkbox', id: 'immersive' }),
        el('span', {}, el('strong', { text: 'Slow push-ins' }), el('br'),
          el('span', { class: 'muted', text: 'The picture eases in as a new sentence begins, then snaps back. Suits a person talking to camera.' })))))),
    err, progress,
    el('div', { class: 'actionbar' }, el('div', { class: 'row' },
      el('label', { class: 'row small muted', for: 'rights', style: { alignItems: 'flex-start', flexWrap: 'nowrap', gap: '10px' } }, rights,
        el('span', { text: 'I own this video or have the rights to process it.' })),
      el('div', { class: 'row' }, status, cancelBtn, submit))));
  const $ = (id) => form.querySelector('#' + id);
  const lengthChanged = () => {
    const whole = !$('min_clip').value.trim() && !$('max_clip').value.trim() && !$('style').value;
    const styled = !!$('style').value;
    $('clips').disabled = whole; $('style').disabled = whole;
    $('min_clip').disabled = styled; $('max_clip').disabled = styled;
  };
  $('min_clip').addEventListener('input', lengthChanged); $('max_clip').addEventListener('input', lengthChanged); $('style').addEventListener('change', lengthChanged);
  $('clean').addEventListener('change', () => {
    $('clean-opts').hidden = !$('clean').checked;
    const hi = Number($('max_clip').value);
    if ($('clean').checked && !$('style').value && hi > 0 && hi <= 90) { $('min_clip').value = ''; $('max_clip').value = ''; lengthChanged(); }       // a cleaned recording is one reel
    else if (!$('clean').checked && !$('min_clip').value.trim() && !$('max_clip').value.trim()) { $('min_clip').value = '15'; $('max_clip').value = '60'; lengthChanged(); }
  });
  $('explainer').addEventListener('change', () => { $('explainer-opts').hidden = !$('explainer').checked; });
  const applyBrand = (b) => {                                                                      // the rows that depend on the chosen brand
    const set = (id, have, on, hint) => { const box = $(id); box.disabled = !have; box.checked = have && !!on; box.closest('label').className = have ? 'check' : 'check muted'; $(id + '-hint').replaceChildren(...[].concat(hint)); };
    const link = (t) => [t, el('a', { href: '#/brand', text: 'Open the Brand page' }), '.'];
    $('cover').checked = !!b.settings.default_cover;
    set('logo', b.logo, b.settings.default_logo, b.logo ? 'Your logo, from the Brand page.' : link('No logo for this brand. '));
    set('outro', b.outro, b.settings.default_outro, b.outro ? `Your branding video (${b.outro_seconds} s), added at the end.` : link('No ending video for this brand. '));
    set('music', b.music, b.settings.default_music, b.music ? `${b.music_name} (${b.music_seconds} s), kept below the voice and lowered further while someone talks.` : link('No music for this brand. '));
    $('speaker').placeholder = b.settings.speaker_name || ''; $('role').placeholder = b.settings.speaker_title || '';
  };
  if ($('brand')) {
    $('brand').value = brand.id;
    $('brand').addEventListener('change', async () => { try { applyBrand(await api(`/brand?brand=${$('brand').value}`)); } catch (e) { if (e instanceof SignInNeeded) route(); } });
  }
  applyBrand(brand);
  $('style').addEventListener('change', lengthChanged);                                              // the style sets the clip length

  const aside = el('div', { class: 'aside' },
    el('section', { class: 'card' }, el('h2', { text: 'What happens next' }),
      el('ol', { class: 'steps' },
        ['Transcribe: word-by-word timing, in the spoken language.', 'Plan: AI picks self-contained moments and writes the hook and title.',
          'Frame: a vertical crop that follows the speaker.', 'Render: captions, hook, stat and name card burned in.'].map((t) => el('li', { text: t })))),
    c.gpu_worker && c.gpu_worker.enabled && el('section', { class: 'card' }, el('h2', { text: 'Speech recognition' }),
      el('p', { class: 'small', style: { margin: 0 }, text: c.gpu_worker.online ? (c.gpu_worker.render ? 'A GPU worker is online: transcription is fast and clips are rendered on it too.' : 'A GPU worker is online: transcription should be fast.') : 'No GPU worker is online right now, so the server transcribes on its CPU, which is slower. Start the worker and it takes over by itself.' })),
    el('section', { class: 'callout' }, el('h2', { text: 'How AI is used' }),
      el('p', { class: 'small', style: { margin: 0 }, text: 'AI chooses the cut points, titles, hooks and emphasised words. Your footage stays real. Nothing AI-generated is added, and every job keeps a record of what the AI decided.' })));
  app.replaceChildren(el('div', { class: 'layout' }, form, aside));
}

const STATUS_CLASS = { queued: 'queued', running: 'running', done: 'done', failed: 'failed', cancelled: 'cancelled' };
const STAGE_LABEL = { transcribe: 'Transcribe', plan: 'Plan', assets: 'Pictures', frame: 'Frame', render: 'Render' };
const STAGE_WORD = { transcribe: 'Transcribing', plan: 'Planning', assets: 'Making pictures', frame: 'Framing clips', render: 'Rendering' };
const STATE_TEXT = { pending: 'Waiting', running: 'Working…', done: 'Done', skipped: 'Skipped', failed: 'Failed' };

function statusText(job) {
  if (job.status === 'queued') return job.queue_position > 1 ? `Waiting (${job.queue_position - 1} ahead)` : 'Waiting to start';
  if (job.status === 'running') {
    if (job.cancel_requested) return 'Stopping…';
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
  const retryBtn = el('button', { class: 'btn primary', type: 'button', text: 'Run again', hidden: true, onclick: async () => {
    retryBtn.disabled = true;
    try { job = await api(`/jobs/${jobId}/retry`, { method: 'POST' }); retryBtn.disabled = false; update(); } catch (e) { retryBtn.disabled = false; alert(e.message); }
  } });
  const deleteBtn = el('button', { class: 'btn', type: 'button', text: 'Delete', onclick: async () => {
    if (!confirm('Delete this job and its clips?')) return;
    try { await api(`/jobs/${jobId}`, { method: 'DELETE' }); location.hash = '#/jobs'; } catch (e) { alert(e.message); }
  } });
  const errBox = el('div', { class: 'alert', role: 'alert', hidden: true });
  const stages = el('div', { class: 'stages' });
  const logPre = el('pre', { tabindex: '0', 'aria-label': 'Job log' });
  const logCard = el('details', { class: 'activity log', open: true }, el('summary', { text: 'Activity' }), logPre);
  let logClosed = false;
  const timeline = el('div', {});
  const clipsGrid = el('div', { class: 'clips' });
  const zipLink = el('a', { class: 'btn dark', href: `/api/jobs/${jobId}/clips.zip`, hidden: true });
  const clipsSection = el('section', { hidden: true }, el('div', { class: 'row', style: { justifyContent: 'space-between' } }, el('h2', { text: 'Clips' }), zipLink), timeline, el('div', { style: { height: '20px' } }), clipsGrid);
  const usageBody = el('div', { class: 'small muted', text: 'The usage record appears once the plan is ready.' });
  const usage = el('details', { class: 'card usage', hidden: true }, el('summary', { text: 'AI usage and disclosure' }), el('div', { style: { marginTop: '12px' } }, usageBody));
  const cutBody = el('div', { class: 'small muted', text: 'Loading…' });
  const cutCard = el('details', { class: 'card usage', hidden: true }, el('summary', { text: 'What was cut from your recording' }), el('div', { style: { marginTop: '12px' } }, cutBody));
  const trBody = el('div', { class: 'small muted', text: 'Loading…' });
  const trCard = el('details', { class: 'card usage', hidden: true }, el('summary', { text: 'Check or fix the words (optional)' }), el('div', { style: { marginTop: '12px' } }, trBody));
  const whoBody = el('div', { class: 'small muted', text: 'Loading…' });
  const whoCard = el('details', { class: 'card usage', hidden: true }, el('summary', { text: 'Who is who on screen (optional)' }), el('div', { style: { marginTop: '12px' } }, whoBody));
  let whoLoaded = false;
  async function loadWho() {
    if (whoLoaded || job.status !== 'done' || job.source_removed) return;
    whoLoaded = true;
    try {
      const r = await api(`/jobs/${jobId}/speakers`);
      if (!r.available) {
        if (!r.can_build && !r.building) { whoCard.hidden = true; return; }
        whoCard.hidden = false;
        const go = el('button', { class: 'btn', type: 'button', text: 'Find who is speaking', disabled: !!r.building });
        const note = el('div', { class: 'small', role: 'status', text: r.building ? 'Working on it. This takes a few minutes; the activity log says how far it is.' : '' });
        go.addEventListener('click', async () => {
          go.disabled = true;
          try { await api(`/jobs/${jobId}/speakers/build`, { method: 'POST' }); note.textContent = 'Started. This takes a few minutes; the clips are framed again when it is done.'; } catch (ex) { note.textContent = ex.message; go.disabled = false; }
        });
        whoBody.className = '';
        whoBody.replaceChildren(el('p', { class: 'small muted', text: 'If several people sit in the picture and one or two talk at a time, the app can listen to the voices and show the one who is speaking. This video was made without it.' }), el('div', { class: 'row' }, go, note));
        setTimeout(() => { whoLoaded = false; }, 15000);
        return;
      }
      whoCard.hidden = false;
      const msg = el('div', { class: 'small', role: 'status' });
      const save = el('button', { class: 'btn primary', type: 'button', text: 'Save and frame the clips again', disabled: true });
      const picks = [];
      const rows = r.voices.map((v) => {
        const sel = el('select', { id: `voice-${v.voice}` }, [el('option', { value: '', text: 'Off screen (nobody here)' }), ...r.seats.map((s) => el('option', { value: String(s.n), text: `Person ${s.n}` }))]);
        sel.value = v.seat ? String(v.seat) : '';
        sel.addEventListener('change', () => { save.disabled = !picks.some(([vv, ss]) => ss.value !== (vv.seat ? String(vv.seat) : '')); });
        picks.push([v, sel]);
        const mins = v.seconds >= 90 ? `${Math.round(v.seconds / 60)} min` : `${v.seconds} s`;
        return el('div', { class: 'who-row' },
          el('div', {}, el('strong', { text: `Voice ${v.voice + 1}` }), el('span', { class: 'muted', text: ` · speaks for about ${mins}${v.changed ? ' · set by you' : ''}` })),
          el('audio', { controls: true, preload: 'none', src: `/api/jobs/${jobId}/speakers/${v.voice}/sample`, 'aria-label': `Hear voice ${v.voice + 1}` }),
          el('div', {}, el('label', { class: 'field', for: sel.id, text: 'Whose voice is this?' }), sel));
      });
      save.addEventListener('click', async () => {
        const voices = {};
        for (const [v, sel] of picks) voices[String(v.voice)] = sel.value ? parseInt(sel.value, 10) : null;
        save.disabled = true;
        msg.textContent = 'Saving…';
        try {
          const done = await api(`/jobs/${jobId}/speakers`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ voices }) });
          msg.textContent = done.clips.length ? `Saved. ${done.clips.length} clip(s) are being framed again.` : 'Nothing changed.';
          whoLoaded = false;
          setTimeout(() => { whoBody.replaceChildren(); loadWho(); }, 1200);
        } catch (ex) { msg.textContent = ex.message; }
      });
      whoBody.className = '';
      whoBody.replaceChildren(...[
        el('p', { class: 'small muted', text: 'Each clip cuts to the person whose voice you hear. The app matched the voices to the people by watching whose mouth moves. If a voice is matched to the wrong person, or to nobody because the speaker is off camera, say so here: only the clips that followed the voices are framed again.' }),
        r.picture ? el('img', { class: 'who-pic', src: `/api/jobs/${jobId}/speakers/picture`, alt: 'The people on screen, numbered from the left' }) : null,
        el('div', {}, rows), el('div', { class: 'row' }, save, msg)].filter(Boolean));
    } catch (_) { whoLoaded = false; whoCard.hidden = true; }
  }
  let trLoaded = false;
  async function loadTranscript() {
    if (trLoaded || job.status !== 'done' || job.source_removed) return;
    trLoaded = true;
    try {
      const r = await api(`/jobs/${jobId}/transcript`);
      trCard.hidden = false;
      const areas = new Map();
      const msg = el('div', { class: 'small', role: 'status' });
      const save = el('button', { class: 'btn primary', type: 'button', text: 'Save and re-render the clips', disabled: true });
      const dirty = () => { save.disabled = ![...areas].some(([s, a]) => a.value.trim() !== s.text.trim()); };
      const rows = r.sentences.map((s) => {
        const a = el('textarea', { class: 'tr-text deva', rows: '2', translate: 'no', maxlength: '600', spellcheck: 'false', 'aria-label': `Words at ${fmt(s.start)}` });
        a.value = s.text;
        a.addEventListener('input', dirty);
        areas.set(s, a);
        return el('div', { class: 'tr-row' },
          el('div', { class: 'tr-meta small muted' }, el('span', { class: 'num', text: fmt(s.start) }),
            s.original && el('span', { class: 'chip plain', text: 'AI corrected' }),
            s.clips.length ? el('span', { text: s.clips.join(', ') }) : el('span', { text: 'not in a clip' })),
          a,
          s.original && el('details', { class: 'small muted' }, el('summary', { text: 'What was first heard' }), el('div', { class: 'deva', translate: 'no', text: s.original })));
      });
      save.addEventListener('click', async () => {
        const edits = [...areas].filter(([s, a]) => a.value.trim() !== s.text.trim()).map(([s, a]) => ({ id: s.id, start: s.start, text: a.value }));
        save.disabled = true;
        msg.textContent = 'Saving…';
        try {
          const done = await api(`/jobs/${jobId}/transcript`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ edits }) });
          msg.textContent = done.changed ? `Saved. ${done.clips.length} clip(s) are rendering again with the new words.` : 'Nothing changed.';
          trLoaded = false;
          setTimeout(() => { trBody.replaceChildren(); loadTranscript(); }, 1200);
        } catch (ex) { msg.textContent = ex.message; dirty(); }
      });
      trBody.className = '';
      trBody.replaceChildren(
        el('p', { class: 'small muted', text: 'The words the captions are made from. Fix a word and save: only the clips that use that sentence are rendered again, and the cut points stay where they are. You never have to do this.' }),
        el('div', { class: 'tr-list' }, rows), el('div', { class: 'row' }, save, msg));
    } catch (ex) { trBody.textContent = ex.message; trLoaded = false; }
  }
  let cutLoaded = false;
  const KIND = { filler: 'Hesitation sound', stutter: 'Stutter', false_start: 'False start', retake: 'Earlier take', restart: 'Earlier take', judged: 'Judged by the AI', hidden_filler: 'Noise with no word', suggested: 'Suggested (not cut)' };
  async function loadCuts() {
    if (cutLoaded || job.status !== 'done' || !job.params.clean) return;
    cutLoaded = true;
    try {
      const r = await api(`/jobs/${jobId}/cleanup`);
      cutCard.hidden = false;
      const boxes = [];
      const msg = el('div', { class: 'small', role: 'status' });
      const again = el('button', { class: 'btn primary', type: 'button', text: 'Cut again with my choices', disabled: true });
      const changed = () => { again.disabled = !boxes.some(([c, cb]) => cb.checked !== c.applied); };
      const rows = r.cuts.map((c) => {
        const cb = el('input', { type: 'checkbox', 'aria-label': `Cut ${fmt(c.start)} to ${fmt(c.end)}` });
        cb.checked = c.applied;
        cb.disabled = !r.can_review;
        cb.addEventListener('change', changed);
        boxes.push([c, cb]);
        return el('tr', {}, el('td', {}, cb), el('td', { class: 'num', text: `${fmt(c.start)}–${fmt(c.end)}` }), el('td', { text: KIND[c.kind] || c.kind }),
          el('td', {}, el('div', { class: 'deva', translate: 'no', text: c.text || c.reason }), c.kind === 'suggested' && c.reason ? el('div', { class: 'small muted', text: c.reason }) : null));
      });
      again.addEventListener('click', async () => {
        if (!confirm('Cut the recording again with these choices? Every clip is planned and rendered again from your original recording, so any edits you made to the clips or the words are lost.')) return;
        again.disabled = true;
        msg.textContent = 'Starting…';
        try {
          await api(`/jobs/${jobId}/cleanup/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ applied: boxes.filter(([, cb]) => cb.checked).map(([c]) => c.index) }) });
          location.reload();
        } catch (ex) { msg.textContent = ex.message; changed(); }
      });
      cutBody.className = '';
      const rd = r.reading || {};
      cutBody.replaceChildren(...[
        rd.intent ? el('div', {}, el('h3', { style: { margin: '0 0 6px', fontSize: '14px' }, text: 'How the AI read your recording' }),
          el('p', { class: 'small deva', translate: 'no', text: rd.intent }),
          rd.passages && rd.passages.length ? el('div', { class: 'chips' }, rd.passages.map((p) => el('span', { class: 'chip plain', translate: 'no', text: `${fmt(p.start)}–${fmt(p.end)} ${p.role}` }))) : null,
          rd.verify ? el('p', { class: 'small', style: { marginTop: '10px' }, text: rd.verify.reads_cleanly ? 'Its read-through of the result: it flows as one clean take.'
            : `Its read-through of the result found: ${rd.verify.problems.join('; ') || 'something to check'}.` }) : null) : null,
        r.applied ? el('p', { class: 'small', text: `${fmt(r.before_seconds)} became ${fmt(r.after_seconds)}. Times are in your original recording. Pauses were also shortened (${r.pace}).` })
          : el('p', { class: 'alert', role: 'alert', text: `These cuts were NOT applied: they would have removed ${Math.round((r.removed_share || 0) * 100)}% of your recording, which is more than the cleanup trusts itself with, so the video was used as it is. Check the list below; if it is right, tell us and we will let it through.` }),
        r.missed_speech.length ? el('p', { class: 'small', text: `Speech the recogniser could not write down was kept as it is, without captions: ${r.missed_speech.map(([a, b]) => `${fmt(a)}–${fmt(b)}`).join(', ')}.` }) : null,
        r.reviewed ? el('p', { class: 'small', text: 'This is the cut you chose.' }) : null,
        rows.length ? el('div', {}, el('p', { class: 'small muted', text: r.can_review ? 'Tick what should be cut. Untick a cut to keep those words, or tick a suggestion to cut it, then cut again.' : 'These cuts can no longer be changed (the original recording is gone).' }),
          el('div', { style: { overflowX: 'auto' } }, el('table', {}, el('thead', {}, el('tr', {}, ['Cut', 'Original time', 'What', 'Removed'].map((h) => el('th', { text: h })))), el('tbody', {}, rows))),
          r.can_review ? el('div', { class: 'row' }, again, msg) : null)
          : el('p', { class: 'small muted', text: 'No words were removed; only long pauses were shortened.' })].filter(Boolean));
    } catch (_) { cutLoaded = false; cutCard.hidden = true; }
  }
  const cards = new Map();

  function renderStages() {
    // a job that has stopped has no step that is still working, whatever an older record says
    const stopped = ['failed', 'cancelled'].includes(job.status);
    stages.replaceChildren(...job.stages.map((raw) => {
      const s = stopped && raw.state === 'running' ? { ...raw, state: job.status === 'failed' ? 'failed' : 'pending', detail: raw.detail || 'stopped' } : raw;
      return el('div', { class: `stage ${s.state}` },
        el('div', { class: 'k', text: STAGE_LABEL[s.name] }),
        el('div', { class: 'v', text: STATE_TEXT[s.state] }),
        s.detail && el('div', { class: 'small muted', text: s.detail }),
        s.seconds != null && s.state !== 'pending' && s.seconds >= 1 ? el('div', { class: 'small muted', text: `took ${s.seconds >= 90 ? `${Math.floor(s.seconds / 60)} min ${Math.round(s.seconds % 60)} s` : `${Math.round(s.seconds)} s`}` }) : null);
    }));
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

  function scoreRing(score) {
    const ring = el('div', { class: 'ring', role: 'img', 'aria-label': `Clip score ${score} out of 100`, title: `Clip score ${score} / 100` }, el('span', { text: String(score) }));
    ring.style.setProperty('--p', `${score}%`);
    return ring;
  }

  function clipCard(c, i) {
    const url = `/api/jobs/${jobId}/clips/${c.id}`;
    const player = el('div', { class: 'player' });
    if (c.ready) {
      player.replaceChildren(el('video', { controls: true, playsinline: true, preload: 'metadata', src: `${url}/video?v=${c.rev}`, 'aria-label': `Clip ${i + 1}: ${c.title}` }));
    } else {
      player.textContent = job.status === 'running' ? 'Rendering…' : 'Not rendered';
      player.classList.toggle('wait', job.status === 'running');
    }
    const score = c.score == null ? null : Math.round(c.score * 100);
    const actions = el('div', { class: 'row' });
    if (c.ready) actions.append(el('a', { class: 'btn dark', href: `${url}/download`, text: 'Download MP4' }));
    if (c.ready && c.has_cover) actions.append(el('a', { class: 'btn', href: `${url}/cover?download=1`, text: 'Cover image' }));
    if (c.ready && job.status === 'done' && state.config.publishing) actions.append(el('a', { class: 'btn', href: `#/jobs/${jobId}/clips/${c.id}/share`, text: 'Share' }));
    if (c.ready && job.status === 'done' && job.source_removed) actions.append(el('span', { class: 'small muted', text: 'Original video removed: no more edits' }));
    else if (c.ready && job.status === 'done') actions.append(el('a', { class: 'btn', href: `#/jobs/${jobId}/clips/${c.id}`, text: 'Edit' }));
    return el('article', { class: 'card clipcard', 'data-clip': c.id }, player,
      el('div', { class: 'clipbody' },
        el('div', { class: 'chips' },
          el('span', { class: 'chip plain', text: `${fmt(c.start)} – ${fmt(c.end)}` }),
          el('span', { class: 'chip plain', text: `${Math.round(c.end - c.start)} s` }),
          el('span', { class: 'chip plain', text: FRAMING_TEXT[c.framing] || c.framing }),
          c.edited && el('span', { class: 'chip', text: 'edited by you' })),
        el('div', { class: 'cliphead' }, el('h2', { class: 'deva', style: { fontSize: '18px', lineHeight: '1.4', margin: 0 }, translate: 'no', text: c.title }),
          score != null && scoreRing(score)),
        c.hook && el('div', { class: 'small muted' }, 'Hook: ', el('span', { class: 'deva', text: c.hook })),
        c.why && el('p', { class: 'small muted', style: { margin: 0 }, text: c.why }),
        c.notes && c.notes.map((n) => el('div', { class: 'small', style: { color: 'var(--warn)' }, text: n })),
        busy(c) && el('div', { class: 'badge running', role: 'status', text: c.render === 'queued' ? 'Re-render queued…' : 'Re-rendering…' }),
        c.render === 'failed' && el('div', { class: 'alert', role: 'alert', style: { margin: 0 } }, el('div', { text: `Re-render failed: ${c.render_error}` }),
          el('button', { class: 'btn', type: 'button', style: { marginTop: '8px' }, text: 'Try again', onclick: async () => {
            try { job = await api(`/jobs/${jobId}`); await api(`${'/jobs/' + jobId}/clips/${c.id}/render`, { method: 'POST' }); job = await api(`/jobs/${jobId}`); update(); schedule(); } catch (e) { alert(e.message); }
          } })),
        c.ai_decided.length > 0 && el('div', {}, el('div', { class: 'tiny muted', style: { marginBottom: '4px' }, text: 'AI decided' }),
          el('div', { class: 'chips' }, c.ai_decided.map((t) => el('span', { class: 'chip', text: t })))),
        actions));
  }

  function renderClips() {
    clipsSection.hidden = job.clips.length === 0;
    const ready = job.clips.filter((c) => c.ready).length;                       // once the job has finished: every reel in one file
    zipLink.hidden = job.status !== 'done' || ready < 1;
    zipLink.textContent = ready === 1 ? 'Download the reel' : `Download all ${ready} reels (.zip)`;
    if (ready === 1) zipLink.href = `/api/jobs/${jobId}/clips/${job.clips.find((c) => c.ready).id}/download`;
    else zipLink.href = `/api/jobs/${jobId}/clips.zip`;
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
    retryBtn.hidden = !['failed', 'cancelled'].includes(job.status);
    errBox.hidden = !job.error;
    errBox.textContent = job.error || '';
    renderStages();
    if (job.status === 'done' && !logClosed) { logClosed = true; logCard.open = false; }      // the result matters now, not the log
    const nearBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
    logPre.textContent = job.log.length ? job.log.join('\n') : 'Waiting to start…';
    if (nearBottom) logPre.scrollTop = logPre.scrollHeight;
    renderTimeline();
    renderClips();
    loadUsage();
    loadCuts();
    loadWho();
    loadTranscript();
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
      el('div', { class: 'row' }, el('div', { class: 'grow' }, title, meta), badge, cancelBtn, retryBtn, deleteBtn),
      errBox,
      el('section', { class: 'card' }, stages),
      clipsSection,
      logCard,
      cutCard, whoCard, trCard, usage));
  update();
  timer = setTimeout(poll, 1500);
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

async function jobsView() {
  const jobs = await api('/jobs');
  const body = jobs.length
    ? el('div', { class: 'card', style: { padding: '8px 24px' } }, el('table', {},
      el('thead', {}, el('tr', {}, ['Video', 'Status', 'Started', 'Clips'].map((h) => el('th', { text: h })))),
      el('tbody', {}, jobs.map((j) => el('tr', { class: 'jobrow' },
        el('td', {}, icon('film', 18), el('a', { href: `#/jobs/${j.id}`, translate: 'no', text: j.name })),
        el('td', {}, el('span', { class: `badge ${STATUS_CLASS[j.status]}`, text: statusText(j) })),
        el('td', { text: when(j.created_at) }),
        el('td', { text: j.clips.length ? `${j.clips.filter((c) => c.ready).length} of ${j.clips.length}` : '—' }))))))
    : el('div', { class: 'card empty' }, el('div', { class: 'ico' }, icon('film', 26)), 'No jobs yet. ', el('a', { href: '#/', text: 'Start one' }), '.');
  app.replaceChildren(el('h1', { text: 'Jobs' }), el('div', { style: { height: '16px' } }), body);
}

// ---- clip editor ----------------------------------------------------------------------------------

async function clipEditorView(jobId, clipId) {
  let view = await api(`/jobs/${jobId}/clips/${clipId}`);
  let seatInfo = null;
  try { seatInfo = await api(`/jobs/${jobId}/speakers`); } catch (_) { seatInfo = null; }        // only a video with several people has seats
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
  choose('tx', [['none', 'None'], ['flash', 'Flash'], ['blur', 'Blur'], ['dip', 'Dip to black'], ['whip', 'Whip']], view.transition.style);
  choose('cap_anim', [['pop', 'Pop (words bounce in)'], ['karaoke', 'Highlight the word being said'], ['fade', 'Fade (calm)'], ['slide', 'Slide up']], cap.animation || 'pop');
  f.cap_color = el('input', { id: 'cap_color', type: 'color', value: cap.color });
  f.cap_hl = el('input', { id: 'cap_hl', type: 'color', value: cap.highlight_color });
  const capPos = el('output', { text: String(cap.top_px) });
  f.cap_top = el('input', { id: 'cap_top', type: 'range', min: '0', max: '1800', step: '10', value: String(cap.top_px) });
  f.cap_top.addEventListener('input', () => { capPos.textContent = f.cap_top.value; });

  choose('framing', [['', `Keep (${{ track: 'face-tracked crop', fit_blur: 'whole frame', cover: 'centre crop', split: 'two people stacked' }[view.framing]})`],
    ['auto', 'Follow the faces (plan it again)'], ['cover', 'Centre crop'], ['fit_blur', 'Whole frame over a blurred copy']], '');
  choose('multi', [['switch', 'Cut to whoever is talking'], ['split', 'Stack two people'], ['off', 'One person on screen']], 'switch');
  const multiField = field('When several people are on screen', f.multi);
  choose('follow', [['', 'Leave as it is'], ['-1', 'Follow whoever is speaking'], ...(seatInfo ? seatInfo.seats.map((s) => [String(s.n - 1), `Stay on person ${s.n} (from the left)`]) : [])], '');
  const followField = field('Which person', f.follow, 'The picture stays on one person for the whole clip, or follows the voices.');
  followField.hidden = !seatInfo;
  const syncMulti = () => { multiField.hidden = f.framing.value !== 'auto'; };
  f.framing.addEventListener('change', syncMulti); syncMulti();
  f.punch = el('input', { id: 'punch', type: 'checkbox' }); f.punch.checked = view.punch_ins;
  f.cover = el('input', { id: 'cover_on', type: 'checkbox' }); f.cover.checked = view.cover.on;
  textIn('cover_text', view.cover.text, 80);
  f.logo = el('input', { id: 'logo_on', type: 'checkbox', disabled: !view.logo.available }); f.logo.checked = view.logo.on;
  f.outro = el('input', { id: 'outro_on', type: 'checkbox', disabled: !view.outro.available }); f.outro.checked = view.outro.on;
  f.sfx = el('input', { id: 'sfx_on', type: 'checkbox' }); f.sfx.checked = view.sfx.on;
  f.music = el('input', { id: 'music_on', type: 'checkbox', disabled: !view.music.available }); f.music.checked = view.music.on;

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
    if (f.cap_anim.value !== (cap.animation || 'pop')) c.animation = f.cap_anim.value;
    if (f.cap_color.value.toLowerCase() !== cap.color.toLowerCase()) c.color = f.cap_color.value.toUpperCase();
    if (f.cap_hl.value.toLowerCase() !== cap.highlight_color.toLowerCase()) c.highlight_color = f.cap_hl.value.toUpperCase();
    if (Number(f.cap_top.value) !== cap.top_px) c.top_px = Number(f.cap_top.value);
    if (Object.keys(c).length) out.captions = c;
    if (f.punch.checked !== view.punch_ins) out.punch_ins = f.punch.checked;
    if (f.cover.checked !== view.cover.on) out.cover = f.cover.checked;
    if (f.cover.checked && f.cover_text.value.trim() !== view.cover.text) out.cover_text = f.cover_text.value;
    if (f.logo.checked !== view.logo.on) out.logo = f.logo.checked;
    if (f.outro.checked !== view.outro.on) out.outro = f.outro.checked;
    if (f.sfx.checked !== view.sfx.on) out.sfx = f.sfx.checked;
    if (f.music.checked !== view.music.on) out.music = f.music.checked;
    if (f.tx.value !== view.transition.style) out.transition = f.tx.value;
    const drop = [...app.querySelectorAll('[data-drop]')].filter((x) => x.checked).map((x) => x.dataset.drop);
    if (drop.length) out.drop_pictures = drop;
    if (f.framing.value) { out.framing = f.framing.value; if (f.framing.value === 'auto') out.multi = f.multi.value; }
    if (f.follow.value !== '') out.follow = parseInt(f.follow.value, 10);
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
      el('div', { class: 'stack edcol sticky', style: { gap: '12px' } }, player, status),
      el('div', { class: 'stack grow', style: { flex: '999 1 420px' } },
        group('Words on screen', field('Title', f.title), field('Hook line (first seconds)', f.hook, 'Leave empty to remove it.'),
          el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Speaker name', f.speaker_name), field('Role', f.speaker_title)),
          field('Stat pill', f.stat, 'A short figure like "10x". Leave empty to remove it.')),
        group('Cut points', el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Start (seconds)', f.source_in), field('End (seconds)', f.source_out)),
          length, pickRow, wordBox),
        group('Captions', el('label', { class: 'check', for: 'cap_enabled' }, f.cap_enabled, el('span', { text: 'Show captions' })),
          el('div', { class: 'grid2', style: { marginTop: '12px' } }, field('Words per line', f.cap_words), field('Font', f.cap_font), field('Motion', f.cap_anim),
            field('Text colour', f.cap_color), field('Highlight colour', f.cap_hl)),
          el('div', { style: { marginTop: '12px' } }, el('label', { class: 'field', for: 'cap_top' }, 'Height on screen ', capPos, ' px from the top'), f.cap_top)),
        view.pictures.length > 0 && group('Pictures over the video',
          ...view.pictures.map((p) => el('label', { class: 'check', for: `drop-${p.id}` },
            el('input', { type: 'checkbox', id: `drop-${p.id}`, 'data-drop': p.id }),
            el('span', {}, el('strong', { translate: 'no', text: p.caption || 'Picture' }), ` at ${fmt(p.t || 0)}`, el('br'),
              el('span', { class: 'muted', text: `${p.ai === 'generated' ? 'AI-generated' : 'Page screenshot'} · tick to remove it` }))))),
        group('Cover and branding',
          el('label', { class: 'check', for: 'cover_on' }, f.cover, el('span', {}, el('strong', { text: 'Cover title on the first frames' }), el('br'),
            el('span', { class: 'muted', text: 'Shown for the first fraction of a second, so the poster frame carries the title.' }))),
          field('Cover words', f.cover_text, 'Leave empty to use the hook line.'),
          el('label', { class: view.logo.available ? 'check' : 'check muted', for: 'logo_on' }, f.logo, el('span', { text: view.logo.available ? 'Logo in a corner' : 'Logo (this job has none)' })),
          el('label', { class: view.outro.available ? 'check' : 'check muted', for: 'outro_on' }, f.outro, el('span', { text: view.outro.available ? 'Ending video' : 'Ending video (this job has none)' }))),
        group('Cuts', field('Effect on every cut', f.tx, view.transition.cuts
          ? `This clip has ${view.transition.cuts} hard cut${view.transition.cuts > 1 ? 's' : ''} (a joined stretch, a speaker change or a slide).`
          : 'This clip has no hard cuts, so nothing would show.')),
        group('Sound', el('label', { class: view.music.available ? 'check' : 'check muted', for: 'music_on' }, f.music, el('span', { text: view.music.available ? 'Background music (ducks under the speech)' : 'Background music (this job has none)' })),
          el('label', { class: 'check', for: 'sfx_on' }, f.sfx, el('span', {}, el('strong', { text: 'Quiet sound cues' }), el('br'),
          el('span', { class: 'muted', text: 'A soft pop, whoosh or tick under the speech where a title, card, picture or push-in appears. Never on every word.' })))),
        group('Framing', field('Picture', f.framing), multiField, followField,
          el('label', { class: 'check', for: 'punch' }, f.punch, el('span', {}, el('strong', { text: 'Slow push-ins' }), el('br'),
            el('span', { class: 'muted', text: 'The picture eases in as a new sentence begins, then snaps back.' })))),
        err,
        el('div', { class: 'row' }, saveBtn, resetBtn)))));
  trimChanged();
  showPlayer(view.render); showStatus(view.render);
  if (view.render.render === 'queued' || view.render.render === 'rendering') timer = setTimeout(poll, 1500);
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

// ---- brand kit -------------------------------------------------------------------------------------

async function brandView(bid) {
  let brand;
  try { brand = await api(bid ? `/brand?brand=${bid}` : '/brand'); } catch (e) { if (e instanceof SignInNeeded) throw e; location.hash = '#/brand'; return; }
  bid = brand.id;
  const q = `?brand=${bid}`;
  const s = brand.settings;
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const note = el('div', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const fail = (e) => { if (e instanceof SignInNeeded) { route(); return; } err.textContent = e.message; err.hidden = false; };
  const stamp = () => `?v=${Date.now()}`;

  async function send(path, input, what) {
    err.hidden = true;
    if (!input.files[0]) return fail(new Error(`Choose ${what} first.`));
    const fd = new FormData();
    fd.append('file', input.files[0]);
    note.textContent = 'Uploading…';
    try { await api(path + q, { method: 'POST', body: fd }); await route(); } catch (e) { note.textContent = ''; fail(e); }
  }
  async function remove(what) {
    if (!confirm('Remove it? Clips already made keep their copy.')) return;
    try { await api(`/brand/${what}${q}`, { method: 'DELETE' }); await route(); } catch (e) { fail(e); }
  }

  const switcher = el('section', { class: 'card stack' },
    el('div', { class: 'row' },
      el('div', {}, el('label', { class: 'field', for: 'brand_pick', text: 'Brand' }),
        (() => { const sel = el('select', { id: 'brand_pick' }, brand.brands.map((b) => el('option', { value: b.id, text: b.name + (b.default ? ' (default)' : '') }))); sel.value = bid; sel.onchange = () => { location.hash = `#/brand/${sel.value}`; }; return sel; })()),
      el('button', { class: 'btn', type: 'button', text: 'New brand', onclick: async () => {
        const name = prompt('Name of the new brand (up to 40 characters)'); if (!name) return;
        try { const b = await api('/brands', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); location.hash = `#/brand/${b.id}`; } catch (e) { fail(e); }
      } }),
      el('button', { class: 'btn', type: 'button', text: 'Rename', onclick: async () => {
        const name = prompt('New name', brand.name); if (!name) return;
        try { await api(`/brands/${bid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); await route(); } catch (e) { fail(e); }
      } }),
      !brand.is_default && el('button', { class: 'btn', type: 'button', text: 'Make default', onclick: async () => {
        try { await api(`/brands/${bid}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ default: true }) }); await route(); } catch (e) { fail(e); }
      } }),
      brand.brands.length > 1 && el('button', { class: 'btn', type: 'button', text: 'Delete brand', onclick: async () => {
        if (!confirm('Delete this brand and its files? Clips already made keep their copy.')) return;
        try { await api(`/brands/${bid}`, { method: 'DELETE' }); location.hash = '#/brand'; await route(); } catch (e) { fail(e); }
      } })));

  const logoIn = el('input', { type: 'file', id: 'logo_file', accept: 'image/png,image/jpeg,image/webp' });
  const outroIn = el('input', { type: 'file', id: 'outro_file', accept: 'video/*' });
  const musicIn = el('input', { type: 'file', id: 'music_file', accept: 'audio/*' });
  const logoBox = el('section', { class: 'card stack' }, el('h2', { text: 'Logo' }),
    brand.logo ? el('img', { src: `/api/brand/logo/file${q}&v=${Date.now()}`, alt: 'Your logo', class: 'logo-preview' })
      : el('p', { class: 'small muted', text: 'No logo yet.' }),
    el('div', {}, el('label', { class: 'field', for: 'logo_file' }, 'PNG, JPEG or WebP · up to 3 MB', tip('A transparent PNG looks best. SVG is not accepted.', 'the logo file')), logoIn),
    el('div', { class: 'row' }, el('button', { class: 'btn primary', type: 'button', text: brand.logo ? 'Replace logo' : 'Upload logo', onclick: () => send('/brand/logo', logoIn, 'a logo') }),
      brand.logo && el('button', { class: 'btn', type: 'button', text: 'Remove', onclick: () => remove('logo') })));
  const trackSel = el('select', { id: 'music_track' }, [['', brand.uploaded_music ? 'Your upload' : 'None'], ...brand.tracks.map((t) => [t.id, `${t.title}: ${t.mood}`])]
    .map(([v, t]) => el('option', { value: v, text: t })));
  trackSel.value = brand.settings.music_track || '';
  trackSel.addEventListener('change', async () => {
    try { await api('/brand' + q, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ music_track: trackSel.value }) }); await route(); } catch (e) { fail(e); }
  });
  const previewSrc = brand.settings.music_track ? `/api/brand/tracks/${brand.settings.music_track}/file` : (brand.music ? `/api/brand/music/file${q}&v=${Date.now()}` : '');
  const musicBox = el('section', { class: 'card stack' }, el('h2', { text: 'Background music' }),
    el('div', {}, el('label', { class: 'field', for: 'music_track', text: 'Track' }), trackSel,
      el('div', { class: 'hint', text: 'The built-in tracks are instrumental and made for this app. Or add your own below.' })),
    previewSrc ? el('audio', { src: previewSrc, controls: true, preload: 'none', style: { width: '100%' }, 'aria-label': 'Preview of the chosen track' }) : el('p', { class: 'small muted', text: 'No music chosen.' }),
    el('div', {}, el('label', { class: 'field', for: 'music_file' }, 'Your own track · MP3, M4A or WAV, up to 30 MB', tip('5 seconds to 15 minutes. Only upload music you have the right to use in your videos: platforms can mute or claim music they recognise. It plays under the voice and is lowered further while someone talks.', 'your own music track')), musicIn),
    el('div', { class: 'row' }, el('button', { class: 'btn primary', type: 'button', text: brand.uploaded_music ? 'Replace my upload' : 'Upload music', onclick: async () => {
      if (musicIn.files[0]) { try { await api('/brand' + q, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ music_track: '' }) }); } catch (_) { /* the upload reports its own problem */ } }
      send('/brand/music', musicIn, 'a music file');
    } }),
      brand.uploaded_music && el('button', { class: 'btn', type: 'button', text: 'Remove my upload', onclick: () => remove('music') })));
  const outroBox = el('section', { class: 'card stack' }, el('h2', { text: 'Ending video' }),
    brand.outro ? el('video', { src: `/api/brand/outro/file${q}&v=${Date.now()}`, controls: true, playsinline: true, preload: 'metadata', style: { width: '180px' }, 'aria-label': 'Your ending video' })
      : el('p', { class: 'small muted', text: 'No ending video yet.' }),
    el('div', {}, el('label', { class: 'field', for: 'outro_file' }, 'A short branding clip · up to 90 MB', tip('0.5 to 20 seconds. It is converted to a vertical 1080×1920 video.', 'the ending video file')), outroIn),
    el('div', { class: 'row' }, el('button', { class: 'btn primary', type: 'button', text: brand.outro ? 'Replace ending video' : 'Upload ending video', onclick: () => send('/brand/outro', outroIn, 'a video') }),
      brand.outro && el('button', { class: 'btn', type: 'button', text: 'Remove', onclick: () => remove('outro') })));

  const f = {};
  const opt = (id, label, options, value) => {
    const sel = (f[id] = el('select', { id }, options.map(([v, t]) => el('option', { value: v, text: t })))); sel.value = value;
    return el('div', {}, el('label', { class: 'field', for: id, text: label }), sel);
  };
  const num = (id, label, min, max, step, value) => {
    f[id] = el('input', { id, type: 'number', min: String(min), max: String(max), step: String(step), value: String(value) });
    return el('div', {}, el('label', { class: 'field', for: id, text: label }), f[id]);
  };
  const color = (id, label, value) => { f[id] = el('input', { id, type: 'color', value }); return el('div', {}, el('label', { class: 'field', for: id, text: label }), f[id]); };
  const tick = (id, label, value) => { f[id] = el('input', { id, type: 'checkbox' }); f[id].checked = value; return el('label', { class: 'check', for: id }, f[id], el('span', { text: label })); };

  const look = el('section', { class: 'card stack' }, el('h2', { text: 'Look' }),
    el('div', { class: 'grid2', style: { marginTop: 0 } },
      opt('logo_position', 'Logo corner', [['tr', 'Top right'], ['tl', 'Top left'], ['br', 'Bottom right'], ['bl', 'Bottom left']], s.logo_position),
      num('logo_size_pct', 'Logo width (% of the screen)', 5, 40, 1, s.logo_size_pct),
      num('logo_opacity', 'Logo opacity (0.1–1)', 0.1, 1, 0.1, s.logo_opacity),
      num('logo_margin_px', 'Logo margin (px)', 0, 300, 10, s.logo_margin_px),
      num('cover_ms', 'Cover title shows for (ms)', 100, 1500, 50, s.cover_ms),
      opt('cover_background', 'Cover background', [['frame', 'The video, darkened'], ['color', 'A solid colour']], s.cover_background),
      color('cover_color', 'Cover text colour', s.cover_color), color('cover_accent', 'Cover last-word colour', s.cover_accent),
      color('cover_bg_color', 'Cover solid colour', s.cover_bg_color),
      num('outro_max_seconds', 'Use at most (seconds) of the ending video', 1, 30, 0.5, s.outro_max_seconds),
      num('outro_gap_ms', 'Pause before the ending video (ms, 0 = straight cut)', 0, 1500, 50, s.outro_gap_ms)),
    el('h3', { text: 'Speaker' }),
    el('div', { class: 'grid2', style: { marginTop: 0 } },
      el('div', {}, el('label', { class: 'field', for: 'speaker_name', text: 'Name shown on every clip' }), (f.speaker_name = el('input', { id: 'speaker_name', maxlength: '60', value: s.speaker_name || '' }))),
      el('div', {}, el('label', { class: 'field', for: 'speaker_title', text: 'Role' }), (f.speaker_title = el('input', { id: 'speaker_title', maxlength: '60', value: s.speaker_title || '' })))),
    el('h3', { text: 'Captions' }),
    el('div', { class: 'small muted' }, 'Used on new jobs unless you pick a reference style.', tip('Leave a field on “App default” to keep the built-in look. A style you choose on a job overrides these.', 'the caption defaults')),
    el('div', { class: 'grid2', style: { marginTop: 0 } },
      opt('caption_font', 'Font', [['', 'App default'], ...brand.fonts.map((x) => [x, x])], s.caption_font || ''),
      opt('caption_animation', 'Animation', [['', 'App default'], ...brand.animations.map((x) => [x, x])], s.caption_animation || ''),
      num('caption_words', 'Words per line (0 = default)', 0, 8, 1, s.caption_words || 0),
      num('caption_top_px', 'Caption height from top (px, 0 = default)', 0, 1800, 10, s.caption_top_px || 0),
      el('div', {}, el('label', { class: 'field', for: 'caption_color', text: 'Caption colour' }), (f.caption_color = el('input', { id: 'caption_color', type: 'color', value: s.caption_color || brand.caption_base.color })),
        tick('caption_color_on', 'Use this colour', !!s.caption_color)),
      el('div', {}, el('label', { class: 'field', for: 'caption_highlight', text: 'Highlight colour' }), (f.caption_highlight = el('input', { id: 'caption_highlight', type: 'color', value: s.caption_highlight || brand.caption_base.highlight })),
        tick('caption_highlight_on', 'Use this colour', !!s.caption_highlight))),
    el('div', { class: 'stack' }, el('div', { class: 'small muted', text: 'Tick by default on a new job:' }),
      tick('default_cover', 'Cover title', s.default_cover), tick('default_logo', 'Logo', s.default_logo), tick('default_outro', 'Ending video', s.default_outro), tick('default_music', 'Background music', s.default_music)),
    el('div', { class: 'row' }, el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: async () => {
      err.hidden = true;
      const num_ = (id) => Number(f[id].value);
      const body = { logo_position: f.logo_position.value, logo_size_pct: num_('logo_size_pct'), logo_opacity: num_('logo_opacity'),
        logo_margin_px: num_('logo_margin_px'), cover_ms: num_('cover_ms'), cover_background: f.cover_background.value,
        cover_color: f.cover_color.value.toUpperCase(), cover_accent: f.cover_accent.value.toUpperCase(), cover_bg_color: f.cover_bg_color.value.toUpperCase(),
        outro_max_seconds: num_('outro_max_seconds'), outro_gap_ms: num_('outro_gap_ms'),
        speaker_name: f.speaker_name.value, speaker_title: f.speaker_title.value, caption_font: f.caption_font.value, caption_animation: f.caption_animation.value,
        caption_words: num_('caption_words'), caption_top_px: num_('caption_top_px'),
        caption_color: f.caption_color_on.checked ? f.caption_color.value.toUpperCase() : '', caption_highlight: f.caption_highlight_on.checked ? f.caption_highlight.value.toUpperCase() : '',
        default_cover: f.default_cover.checked, default_logo: f.default_logo.checked, default_outro: f.default_outro.checked, default_music: f.default_music.checked };
      try { await api('/brand' + q, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); note.textContent = 'Saved. New jobs use this.'; } catch (e) { fail(e); }
    } })));

  app.replaceChildren(el('div', { class: 'stack' },
    el('div', {}, el('h1', { text: 'Brand' }), el('p', { class: 'small muted' }, 'Added to clips when you tick them on a new job.', tip('Clips already made keep the copy they were made with.', 'the brand page'))),
    switcher, err, note, el('div', { class: 'pair' }, logoBox, outroBox), musicBox, look));
}

// ---- sharing ---------------------------------------------------------------------------------------

const STATUS_WORD = { queued: 'Waiting…', sent: 'Posted', scheduled: 'Scheduled', failed: 'Failed' };
const PLATFORM_WORD = { youtube: 'YouTube', instagram: 'Instagram', 'instagram-standalone': 'Instagram', tiktok: 'TikTok', facebook: 'Facebook', x: 'X', linkedin: 'LinkedIn', 'linkedin-page': 'LinkedIn', telegram: 'Telegram' };
const YT_SHORTS_SECONDS = 180;               // YouTube's help page: vertical videos up to 3 minutes are Shorts (checked 2026-11; other platforms' limits are not known reliably, so only this one is mentioned)
const MAX_CHANNELS = 6;                      // the server accepts at most this many channels in one post

async function shareView(jobId, clipId) {
  const [job, channels, usage] = await Promise.all([api(`/jobs/${jobId}`), api('/publish/channels'), api(`/jobs/${jobId}/usage`).catch(() => ({ disclosure_recommended: false }))]);
  const clip = job.clips.find((c) => c.id === clipId);
  if (!clip) { app.replaceChildren(el('div', { class: 'card empty', text: 'No such clip.' })); return { cleanup() {} }; }
  let timer = null, dead = false;
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const list = el('div', { class: 'stack', style: { gap: '8px' } });
  const boxes = channels.map((ch) => ({ ch, box: el('input', { type: 'checkbox', id: `ch-${ch.id}`, 'data-ch': ch.id }) }));
  const title = el('input', { id: 'sh-title', type: 'text', maxlength: '100', class: 'deva', value: clip.title });
  const caption = el('textarea', { id: 'sh-caption', rows: '4', class: 'deva', maxlength: '5000' });
  const capWarn = el('div', { class: 'hint', role: 'status' });
  caption.value = [clip.title, clip.hook].filter(Boolean).join('\n');
  const privacy = el('select', { id: 'sh-privacy' }, [['public', 'Public'], ['unlisted', 'Unlisted (YouTube; followers only on TikTok)'], ['private', 'Private']].map(([v, t]) => el('option', { value: v, text: t })));
  const when = el('input', { id: 'sh-when', type: 'datetime-local' });
  const kids = el('input', { id: 'sh-kids', type: 'checkbox' });
  const ai = el('input', { id: 'sh-ai', type: 'checkbox' });
  const send = el('button', { class: 'btn primary', type: 'button', text: 'Post' });
  const counter = el('span', { class: 'chip plain', text: `0 of ${MAX_CHANNELS} chosen` });
  const chosenBoxes = () => boxes.filter((b) => b.box.checked);
  function onPick() {                                       // at most MAX_CHANNELS at once; warn where the caption is longer than a platform allows
    const n = chosenBoxes().length;
    counter.textContent = `${n} of ${MAX_CHANNELS} chosen`;
    for (const b of boxes) b.box.disabled = !b.box.checked && n >= MAX_CHANNELS;
    const over = [...new Map(chosenBoxes().filter((b) => caption.value.length > b.ch.limit).map((b) => [b.ch.platform, b.ch])).values()];
    const notes = [];
    if (over.length) notes.push(`Too long for ${over.map((c) => `${PLATFORM_WORD[c.platform] || c.platform} (${c.limit})`).join(', ')}: the end is cut off there.`);
    if (clip.end - clip.start > YT_SHORTS_SECONDS && chosenBoxes().some((b) => b.ch.platform === 'youtube')) {
      notes.push(`This clip is ${fmt(clip.end - clip.start)} long. YouTube only treats vertical videos up to 3 minutes as Shorts, so there it will be a regular video.`);
    }
    capWarn.textContent = notes.join(' ');
  }
  for (const b of boxes) b.box.addEventListener('change', onPick);
  caption.addEventListener('input', onPick);
  const byBrand = new Map();                                // brand -> channels; channels with no brand go last
  for (const b of boxes) { const k = b.ch.brand || ''; if (!byBrand.has(k)) byBrand.set(k, []); byBrand.get(k).push(b); }
  const brandNames = [...byBrand.keys()].sort((a, c) => (a === '') - (c === '') || a.localeCompare(c));
  const brandBlock = (name) => {
    const rows = byBrand.get(name).sort((a, c) => (PLATFORM_WORD[a.ch.platform] || '').localeCompare(PLATFORM_WORD[c.ch.platform] || '') || a.ch.name.localeCompare(c.ch.name));
    const all = el('button', { class: 'btn small', type: 'button', text: 'Choose all' });
    all.addEventListener('click', () => {
      const every = rows.every((b) => b.box.checked);
      for (const b of rows) { if (every) b.box.checked = false; else if (!b.box.checked && chosenBoxes().length < MAX_CHANNELS) b.box.checked = true; }
      all.textContent = rows.every((b) => b.box.checked) ? 'Clear' : 'Choose all';
      onPick();
    });
    return el('div', { class: 'brand-group' },
      el('div', { class: 'brand-head' }, el('h3', { translate: 'no', text: name || 'Other channels' }), el('span', { class: 'chip plain', text: String(rows.length) }), el('span', { class: 'grow' }), all),
      el('div', { class: 'brand-channels' },
        ...rows.map(({ ch, box }) => el('label', { class: 'check', for: box.id }, box, platformMark(ch.platform), el('span', {}, el('strong', { translate: 'no', text: ch.name }), el('br'), el('span', { class: 'muted', text: PLATFORM_WORD[ch.platform] || ch.platform }))))));
  };

  function paint(rows) {
    if (!rows.length) { list.replaceChildren(el('p', { class: 'small muted', text: 'Nothing has been posted from this clip yet.' })); return; }
    list.replaceChildren(...rows.map((r) => el('div', { class: 'row small' },
      el('span', { class: `badge ${r.status === 'failed' ? 'failed' : r.status === 'queued' ? 'running' : 'done'}`, text: STATUS_WORD[r.status] || r.status }),
      el('span', { translate: 'no' }, platformMark(r.platform), `${PLATFORM_WORD[r.platform] || r.platform} · ${r.name}`),
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
    const chosen = chosenBoxes().map((b) => b.ch.id);
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
    channels.length === 0 ? el('div', { class: 'card empty', text: 'No channels we can post to are connected in SocioBird yet.' }) : el('div', { class: 'card stack' },
      el('fieldset', { class: 'stack', style: { border: 0, padding: 0, margin: 0 } },
        el('legend', { class: 'field', text: 'Post to' }), el('div', { class: 'row' }, counter, el('span', { class: 'small muted', text: 'Channels are grouped by brand, as in SocioBird.' })),
        ...brandNames.map(brandBlock)),
      field('Title', title, 'YouTube and TikTok show it; the other platforms use the caption.'),
      el('div', {}, field('Caption', caption, 'Plain text. Hashtags and links are fine. X allows 280 characters.'), capWarn),
      el('div', { class: 'grid2', style: { marginTop: 0 } }, field('Who can see it', privacy), field('Post at (your time, optional)', when, 'Leave empty to post now.')),
      el('label', { class: 'check', for: 'sh-kids' }, kids, el('span', {}, el('strong', { text: 'This video is made for kids' }), el('br'), el('span', { class: 'muted', text: 'YouTube asks every creator to declare this.' }))),
      usage.disclosure_recommended && el('label', { class: 'check', for: 'sh-ai' }, ai, el('span', {}, el('strong', { text: 'I will label this video as AI content' }), el('br'),
        el('span', { class: 'muted', text: 'It includes AI-generated pictures. TikTok’s own AI label is switched on for you; on YouTube and Instagram set the label in the app if it asks.' }))),
      err, el('div', {}, send)),
    el('section', { class: 'card stack', hidden: false }, el('h2', { text: 'Posts' }), list)));
  await refresh();
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

// ---- styles: measure a reference video once, apply its pacing to your own clips ------------------------------------

async function stylesView() {
  let list = await api('/styles');
  let dead = false, timer = null;
  const err = el('div', { class: 'alert', role: 'alert', hidden: true });
  const note = el('div', { class: 'small', role: 'status', 'aria-live': 'polite' });
  const fail = (e) => { if (e instanceof SignInNeeded) { route(); return; } err.textContent = e.message; err.hidden = false; };

  const fileIn = el('input', { type: 'file', id: 'style_file', accept: 'video/*,.mkv,.m4v' });
  const nameIn = el('input', { id: 'style_name', type: 'text', maxlength: '60', autocomplete: 'off', placeholder: 'e.g. Fast podcast reel' });
  const rights = el('input', { type: 'checkbox', id: 'style_rights' });
  const measureBtn = el('button', { class: 'btn primary', type: 'button', text: 'Measure this reference', onclick: async () => {
    err.hidden = true;
    if (!fileIn.files[0]) return fail(new Error('Choose a reference video first.'));
    if (!rights.checked) return fail(new Error('Confirm that you may use this video as a reference.'));
    const fd = new FormData();
    fd.append('file', fileIn.files[0]);
    fd.append('name', nameIn.value);
    fd.append('rights', 'true');
    measureBtn.disabled = true;
    note.textContent = 'Uploading…';
    try { await api('/styles', { method: 'POST', body: fd }); list = await api('/styles'); draw(); } catch (e) { fail(e); } finally { measureBtn.disabled = false; note.textContent = ''; }
  } });

  const num = (id, label, value, min, max, step, hint) => {
    const input = el('input', { id, type: 'number', min, max, step, value });
    return { input, node: el('div', {}, el('label', { class: 'field', for: id, text: label }), input, hint && el('div', { class: 'hint', text: hint })) };
  };

  function card(s) {
    const m = s.measured || {};
    const head = el('div', { class: 'row' }, el('h2', { class: 'grow', translate: 'no', text: s.name }),
      el('span', { class: 'small muted', text: s.status === 'ready' ? 'Ready' : s.status === 'analyzing' ? 'Measuring…' : 'Could not be measured' }));
    const del = el('button', { class: 'btn', type: 'button', text: 'Delete', onclick: async () => {
      if (!confirm('Delete this style? Jobs already made keep their copy.')) return;
      try { await api(`/styles/${s.id}`, { method: 'DELETE' }); list = await api('/styles'); draw(); } catch (e) { fail(e); }
    } });
    if (s.status === 'analyzing') {
      const minutes = (Date.now() - new Date(s.created_at).getTime()) / 60000;
      const slow = minutes > 3;
      return el('section', { class: 'card stack' }, head,
        el('p', { class: 'small muted', text: slow ? 'This is taking longer than usual. You can delete it and upload the reference again.' : 'Measuring cuts, loudness and the caption look. This normally takes under a minute.' }),
        slow ? el('div', {}, del) : null);
    }
    if (s.status === 'failed') return el('section', { class: 'card stack' }, head, el('p', { class: 'small', translate: 'no', text: s.error }), el('div', {}, del));
    const st = s.settings;
    const f = {
      name: el('input', { id: `${s.id}-name`, type: 'text', maxlength: '60', value: s.name, autocomplete: 'off' }),
      min: num(`${s.id}-min`, 'Shortest clip (s)', st.min_clip, 10, 170, 1),
      max: num(`${s.id}-max`, 'Longest clip (s)', st.max_clip, 15, 180, 1),
      words: num(`${s.id}-words`, 'Words per caption', st.caption_max_words, 1, 8, 1, 'Fast speech reads better in longer groups.'),
      pause: num(`${s.id}-pause`, 'Caption pause (s)', st.caption_pause_break, 0.05, 1, 0.05, 'A silence this long starts a new caption.'),
      gap: num(`${s.id}-gap`, 'Rest between push-ins (s)', st.punch.min_gap, 0.5, 8, 0.5),
      per: num(`${s.id}-per`, 'Push-ins per clip, at most', st.punch.max_per_clip, 0, 12, 1),
      on: el('input', { type: 'checkbox', id: `${s.id}-on`, checked: st.punch_ins }),
      top: num(`${s.id}-top`, 'Caption height from the top (px of 1920)', st.captions.top_px, 0, 1800, 10),
      scale: num(`${s.id}-scale`, 'Caption size', st.captions.size_scale, 0.6, 1.6, 0.05, '1 is ClipCrow\u2019s own size.'),
      color: el('input', { type: 'color', id: `${s.id}-color`, value: st.captions.color }),
      hot: el('input', { type: 'color', id: `${s.id}-hot`, value: st.captions.highlight_color }),
      caps: el('input', { type: 'checkbox', id: `${s.id}-caps`, checked: st.captions.uppercase }),
    };
    const saved = el('span', { class: 'small', role: 'status', 'aria-live': 'polite' });
    const save = el('button', { class: 'btn primary', type: 'button', text: 'Save', onclick: async () => {
      err.hidden = true; saved.textContent = '';
      const body = { name: f.name.value, settings: {
        min_clip: Number(f.min.input.value), max_clip: Number(f.max.input.value), caption_max_words: Number(f.words.input.value),
        caption_pause_break: Number(f.pause.input.value), punch_ins: f.on.checked,
        punch: { min_gap: Number(f.gap.input.value), max_per_clip: Number(f.per.input.value) },
        captions: { from_reference: st.captions.from_reference, top_px: Number(f.top.input.value), size_scale: Number(f.scale.input.value),
          uppercase: f.caps.checked, color: f.color.value.toUpperCase(), highlight_color: f.hot.value.toUpperCase(), face: st.captions.face } } };
      try { await api(`/styles/${s.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); saved.textContent = 'Saved. New jobs use this.'; } catch (e) { fail(e); }
    } });
    const facts = [`${m.duration} s long`, `${m.cuts} cuts (${m.cuts_per_minute} a minute)`, `typical shot ${m.median_shot_seconds} s`,
      m.speech_share == null ? null : `sound ${Math.round(m.speech_share * 100)}% of the time`,
      m.words_per_minute == null ? null : `${m.words_per_minute} words a minute`].filter(Boolean);
    const colorField = (label, input) => el('div', { class: 'row' }, el('label', { class: 'field', for: input.id, text: label }), input);
    return el('section', { class: 'card stack' }, head,
      el('p', { class: 'small muted', text: `Measured: ${facts.join(' · ')}.` }),
      m.breakdown ? el('details', {}, el('summary', { class: 'small', text: 'What this reference does' }),
        el('p', { class: 'small', style: { whiteSpace: 'pre-line' }, text: m.breakdown })) : null,
      (m.notes || []).map((n) => el('p', { class: 'small muted', text: n })),
      el('div', { class: 'grid2', style: { marginTop: 0 } },
        el('div', {}, el('label', { class: 'field', for: f.name.id, text: 'Name' }), f.name),
        f.min.node, f.max.node, f.words.node, f.pause.node, f.gap.node, f.per.node, f.top.node, f.scale.node),
      el('label', { class: 'check', for: f.on.id }, f.on, el('span', {}, el('strong', { text: 'Push the picture in on new sentences' }))),
      el('div', { class: 'row' }, colorField('Caption colour', f.color), colorField('Highlighted word', f.hot),
        el('label', { class: 'check', for: f.caps.id }, f.caps, el('span', { text: 'CAPITALS (Latin text only)' }))),
      el('p', { class: 'small muted', text: st.captions.from_reference ? 'The caption look was read from the reference’s own frames.' : 'No captions were seen in the reference, so these are ClipCrow’s own caption settings.' }),
      el('div', { class: 'row' }, save, del, saved));
  }

  function draw() {
    app.replaceChildren(el('div', { class: 'stack' },
      el('h1', { text: 'Styles' }),
      el('p', { class: 'lead', text: 'Upload a video whose pacing you like. ClipCrow measures how fast it cuts and how fast the speaker talks, and your own clips follow that rhythm. The reference itself is deleted once it has been measured.' }),
      el('section', { class: 'card stack' }, el('h2', { text: 'New style' }),
        el('div', {}, el('label', { class: 'field', for: 'style_file', text: 'Reference video (5 s to 3 minutes, up to 200 MB)' }), fileIn),
        el('div', {}, el('label', { class: 'field', for: 'style_name', text: 'Name' }), nameIn),
        el('label', { class: 'check', for: 'style_rights' }, rights, el('span', { text: 'I own this video or may use it as a reference.' })),
        el('div', { class: 'row' }, measureBtn, note)),
      err,
      list.length ? list.map(card) : el('div', { class: 'card empty', text: 'No styles yet.' })));
    clearTimeout(timer);
    if (!dead && list.some((x) => x.status === 'analyzing')) timer = setTimeout(poll, 2000);
  }

  async function poll() {
    if (dead) return;
    try {
      const next = await api('/styles');
      const key = (l) => l.map((x) => `${x.id}:${x.status}`).join(',');
      if (key(next) !== key(list)) { list = next; draw(); } else timer = setTimeout(poll, 2000);
    } catch (e) { if (e instanceof SignInNeeded) { route(); return; } if (!dead) timer = setTimeout(poll, 4000); }
  }

  draw();
  return { cleanup() { dead = true; clearTimeout(timer); } };
}

// ---- router -----------------------------------------------------------------------------------
let cleanup = null;
async function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const hash = location.hash || '#/';
  renderAcct();
  document.querySelectorAll('nav a').forEach((a) => {
    const on = (a.dataset.nav === 'new' && (hash === '#/' || hash === '#' || hash === '#/new')) || (a.dataset.nav === 'jobs' && hash.startsWith('#/jobs')) || (a.dataset.nav === 'brand' && hash.startsWith('#/brand')) || (a.dataset.nav === 'styles' && hash === '#/styles');
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  try {
    if (state.config.needs_login && !state.config.signed_in) return loginView();
    let m;
    if (hash === '#/' || hash === '#' || hash === '#/new') await newJobView();
    else if ((m = hash.match(/^#\/brand(?:\/([a-f0-9]{12}))?$/))) await brandView(m[1]);
    else if (hash === '#/styles') { const v = await stylesView(); cleanup = v.cleanup; }
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

// a page is decorated (long hints into "i" popovers, icons on buttons) every time its content changes
let decorating = false;
const decorator = new MutationObserver(() => {
  if (decorating) return;
  decorating = true;
  requestAnimationFrame(() => {
    decorator.disconnect();
    try { decorate(app); } finally { decorator.observe(app, { childList: true, subtree: true }); decorating = false; }
  });
});
decorator.observe(app, { childList: true, subtree: true });
decorate(app);
