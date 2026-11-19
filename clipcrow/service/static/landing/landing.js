// Fills the price from the server (the numbers live in the operator's plans file, not in this page). Text only; if the request fails the page keeps its plain words.
(async () => {
  try {
    const res = await fetch('/api/pricing', { credentials: 'omit' });
    if (!res.ok) return;
    const g = (await res.json()).payg;
    if (!g) return;
    const set = (id, text) => { const n = document.getElementById(id); if (n) n.textContent = text; };
    set('price-amount', `\u20B9${g.price_per_minute}`);
    set('price-unit', ' for each minute of video');
    if (g.signup_bonus_minutes) {
      set('price-cta', `Start free with ${g.signup_bonus_minutes} minutes`);
      const li = document.createElement('li');
      li.textContent = `New accounts get ${g.signup_bonus_minutes} free minutes to try it.`;
      document.getElementById('price-points').prepend(li);
    }
    const quick = document.getElementById('price-quick');
    for (const q of g.quick) {
      const box = document.createElement('div');
      box.className = 'q';
      const a = document.createElement('strong'); a.textContent = `\u20B9${q.rupees}`;
      const b = document.createElement('span'); b.textContent = `${q.minutes} minutes` + (q.bonus_percent ? `, +${q.bonus_percent}% bonus` : '');
      box.append(a, b);
      quick.append(box);
    }
  } catch (_) { /* the plain words stay */ }
})();
