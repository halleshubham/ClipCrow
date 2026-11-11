// Browser end-to-end test of the web service (needs Playwright + a running server).
//   clipcrow serve --demo --data /tmp/cc-data --port 8765 &
//   NODE_PATH=$(npm root -g) node scripts/e2e_ui.js /path/to/video-over-3.5-min.mp4 /tmp/cc-shots
// Uploads the video, waits for the real render, checks the clips play, and fails on any console error or CSP violation.
const { chromium } = require('playwright');
const [video, out = '.'] = process.argv.slice(2);
const base = process.env.BASE_URL || 'http://127.0.0.1:8765';
if (!video) { console.error('usage: e2e_ui.js <video> [screenshot-dir]'); process.exit(2); }
const fail = (m) => { console.error('FAIL:', m); process.exit(1); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text()); });
  page.on('pageerror', (e) => problems.push(e.message));
  // Chromium aborts a <video> range request once it has the metadata it wanted: that is not a failure.
  page.on('requestfailed', (r) => { if (!/ERR_ABORTED/.test(r.failure()?.errorText || '')) problems.push(`request failed: ${r.url()}`); });

  await page.goto(base);
  await page.waitForSelector('h1:has-text("New job")');
  await page.click('button:has-text("Generate clips")');
  if (!(await page.locator('[role=alert]').innerText()).includes('Choose a video')) fail('no message without a file');
  await page.setInputFiles('#file', video);
  await page.click('button:has-text("Generate clips")');
  if (!(await page.locator('[role=alert]').innerText()).includes('rights')) fail('no message without the rights box');
  await page.check('#rights');
  await page.click('button:has-text("Generate clips")');
  await page.waitForURL(/#\/jobs\/[a-f0-9]{12}/, { timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('.badge')?.textContent === 'Done', null, { timeout: 480000 });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${out}/job-done.png`, fullPage: true });

  if (await page.locator('.clipcard').count() < 1) fail('no clip cards');
  if (await page.locator('button:has-text("Cancel job")').isVisible()) fail('a finished job still offers Cancel');
  const meta = await page.evaluate(async () => {
    const v = document.querySelector('video');
    await new Promise((r) => (v.readyState >= 1 ? r() : v.addEventListener('loadedmetadata', r, { once: true })));
    return { w: v.videoWidth, h: v.videoHeight, d: v.duration };
  });
  if (!(meta.h > meta.w && meta.d > 5)) fail(`first clip is not a vertical video: ${JSON.stringify(meta)}`);
  await page.click('details summary');
  if (!(await page.locator('details').innerText()).includes('Platform AI disclosure')) fail('no AI disclosure line');
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) fail('horizontal scroll on a phone');
  if (problems.length) fail(`browser problems: ${JSON.stringify(problems)}`);
  console.log(`OK: ${await page.locator('.clipcard').count()} clips, first ${meta.w}x${meta.h}, ${Math.round(meta.d)} s`);
  await browser.close();
})().catch((e) => fail(e.message));
