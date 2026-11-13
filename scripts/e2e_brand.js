// Browser test of the Brand page and the branding options on the new-job form (no render needed).
//   clipcrow serve --demo --data /tmp/cc-brand --port 8766 &
//   NODE_PATH=$(npm root -g) node scripts/e2e_brand.js /path/logo.png /path/outro.mp4 /tmp/shots
// Fails on any console error, CSP violation or failed request.
const { chromium } = require('playwright');
const [logo, outro, out = '.'] = process.argv.slice(2);
const base = process.env.BASE_URL || 'http://127.0.0.1:8766';
if (!logo || !outro) { console.error('usage: e2e_brand.js <logo.png> <outro.mp4> [shots]'); process.exit(2); }
const fail = (m) => { console.error('FAIL:', m); process.exit(1); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text()); });
  page.on('pageerror', (e) => problems.push(e.message));
  page.on('requestfailed', (r) => { if (!/ERR_ABORTED/.test(r.failure()?.errorText || '')) problems.push(`request failed: ${r.url()}`); });
  page.on('dialog', (d) => d.accept());

  await page.goto(`${base}/#/new`);
  await page.waitForSelector('h1:has-text("New job")');
  if (!(await page.locator('#logo').isDisabled()) || !(await page.locator('#outro').isDisabled())) fail('logo/outro are offered before anything is uploaded');
  if (await page.locator('#cover').isDisabled()) fail('the cover needs no upload');

  await page.click('nav a[data-nav=brand]');
  await page.waitForSelector('h1:has-text("Brand")');
  await page.setInputFiles('#logo_file', logo);
  await page.click('button:has-text("Upload logo")');
  await page.waitForSelector('img[alt="Your logo"]', { timeout: 15000 });
  if (!(await page.evaluate(() => document.querySelector('img[alt="Your logo"]').complete && document.querySelector('img[alt="Your logo"]').naturalWidth > 0))) fail('the logo preview did not load');
  await page.setInputFiles('#outro_file', outro);
  await page.click('button:has-text("Upload ending video")');
  await page.waitForSelector('video[aria-label="Your ending video"]', { timeout: 60000 });

  await page.selectOption('#logo_position', 'bl');
  await page.fill('#cover_ms', '400');
  await page.check('#default_cover');
  await page.click('button:has-text("Save")');
  await page.waitForSelector('text=Saved. New jobs use this.');
  await page.screenshot({ path: `${out}/brand.png`, fullPage: true });

  await page.click('nav a[data-nav=new]');
  await page.waitForSelector('#logo');
  if (await page.locator('#logo').isDisabled() || await page.locator('#outro').isDisabled()) fail('logo/outro are still disabled after uploading them');
  if (!(await page.locator('#cover').isChecked())) fail('"cover by default" was not applied to the form');
  await page.screenshot({ path: `${out}/new-job.png`, fullPage: true });

  await page.click('nav a[data-nav=brand]');
  await page.waitForSelector('img[alt="Your logo"]');
  if ((await page.inputValue('#logo_position')) !== 'bl' || (await page.inputValue('#cover_ms')) !== '400') fail('the saved look was not shown again');
  await page.click('button:has-text("Remove") >> nth=0');
  await page.waitForSelector('text=No logo yet.');

  if (problems.length) fail(problems.join('\n'));
  await browser.close();
  console.log('ok');
})().catch((e) => fail(e.stack || e.message));
