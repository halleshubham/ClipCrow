// Fills the price from the server (the numbers live in the operator's plans file, not in this page). Text only; if the request fails the page keeps its plain words.
// The example jobs are the real shape of what ClipCrow has processed: recordings of 20 to 87 minutes gave 6 to 10 reels; clean-ups of raw takes are 1 to 2 minutes.
const EXAMPLES = [
  { label: 'Interview, 20 min', minutes: 20, reels: '6 to 10 reels' },
  { label: 'Talk, 30 min', minutes: 30, reels: 'about 6 reels' },
  { label: 'Podcast, 1 hour', minutes: 60, reels: 'about 10 reels' },
  { label: 'Full event, 90 min', minutes: 90, reels: 'about 10 reels' },
  { label: 'Raw take, 10 min', minutes: 10, reels: 'one clean video' },
];
const reelsFor = (minutes) => Math.min(10, Math.max(1, Math.round(minutes / 5.5)));

(async () => {
  try {
    const res = await fetch('/api/pricing', { credentials: 'omit' });
    if (!res.ok) return;
    const g = (await res.json()).payg;
    if (!g) return;
    const rupees = (n) => `₹${Math.round(n).toLocaleString('en-IN')}`;
    const set = (id, text) => { const n = document.getElementById(id); if (n) n.textContent = text; };
    set('price-amount', rupees(g.price_per_minute));
    set('price-unit', ' for each minute of video');
    if (g.signup_bonus_minutes) {
      set('price-cta', `Start free with ${g.signup_bonus_minutes} minutes`);
      const li = document.createElement('li');
      li.textContent = `New accounts get ${g.signup_bonus_minutes} free minutes to try it.`;
      document.getElementById('price-points').prepend(li);
    }
    const cost = (minutes) => minutes * g.price_per_minute;
    // the estimator
    const input = document.getElementById('est-min');
    const show = () => {
      const m = Math.min(600, Math.max(1, Number(input.value) || 1));
      set('est-cost', rupees(cost(m)));
      set('est-reels', m >= 4 ? `for about ${reelsFor(m)} reel${reelsFor(m) > 1 ? 's' : ''}, or one clean video from a raw take` : 'for one short video');
    };
    if (input) { input.addEventListener('input', show); show(); }
    // what typical jobs cost
    const ex = document.getElementById('examples');
    for (const e of EXAMPLES) {
      const box = document.createElement('div');
      box.className = 'q';
      const a = document.createElement('strong'); a.textContent = `${rupees(cost(e.minutes))}`;
      const b = document.createElement('span'); b.textContent = `${e.label}: ${e.reels}`;
      box.append(a, b);
      ex.append(box);
    }
    // the two use cases
    set('case1-cost', `A 1-hour podcast costs ${rupees(cost(60))}`);
    set('case1-note', 'and becomes about 10 reels, about ' + rupees(cost(60) / 10) + ' a reel.');
    set('case2-cost', `A 10-minute raw recording costs ${rupees(cost(10))}`);
    set('case2-note', 'and becomes one clean, branded video.');
    // recharges
    const quick = document.getElementById('price-quick');
    for (const q of g.quick) {
      const box = document.createElement('div');
      box.className = 'q';
      const a = document.createElement('strong'); a.textContent = `₹${q.rupees}`;
      const b = document.createElement('span'); b.textContent = `${q.minutes} minutes` + (q.bonus_percent ? `, +${q.bonus_percent}% bonus` : '');
      box.append(a, b);
      quick.append(box);
    }
  } catch (_) { /* the plain words stay */ }
})();
