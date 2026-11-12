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

  if (process.env.JOB_ID) {                       // re-test the editor on a job that is already rendered
    await page.goto(`${base}/#/jobs/${process.env.JOB_ID}`);
    await page.waitForSelector('.clipcard video', { timeout: 30000 });
  } else {
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
  }
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
  // --- the clip editor: change text and cut points, wait for the real re-render, then reset to the AI plan
  page.on('dialog', (d) => d.accept());
  const videoLength = () => page.evaluate(async () => {
    const v = document.querySelector('video');
    await new Promise((r) => (v.readyState >= 1 ? r() : v.addEventListener('loadedmetadata', r, { once: true })));
    return v.duration;
  });
  const oldTitle = await page.locator('.clipcard h2').first().innerText();
  const rev0 = Number(new URL(await page.locator('.clipcard video').first().getAttribute('src'), base).searchParams.get('v'));
  const before = await videoLength();
  await page.locator('.clipcard a:has-text("Edit")').first().click();
  await page.waitForSelector('h1:has-text("Edit clip")');
  await page.waitForSelector('#title');
  await page.screenshot({ path: `${out}/editor.png`, fullPage: true });
  const start = parseFloat(await page.inputValue('#source_in'));
  await page.fill('#title', 'संपादित शीर्षक');
  await page.fill('#hook', 'नवीन हुक');
  await page.fill('#source_in', String(start + 3));
  await page.dispatchEvent('#source_in', 'input');
  await page.click('button:has-text("Save and re-render")');
  await page.waitForSelector('[role=status]:has-text("Re-rendering")', { timeout: 15000 });
  await page.waitForFunction((rev) => (document.querySelector('video')?.getAttribute('src') || '').endsWith(`v=${rev}`) && !document.querySelector('[role=status]').textContent, rev0 + 1, { timeout: 480000 });
  const after = await videoLength();
  if (!(after < before - 2)) fail(`the trimmed clip did not get shorter: ${before} -> ${after}`);
  await page.screenshot({ path: `${out}/editor-saved.png`, fullPage: true });
  await page.click('a:has-text("Back to the job")');
  await page.waitForSelector('.clipcard');
  if (!(await page.locator('.clipcard').first().innerText()).includes('संपादित शीर्षक')) fail('the card does not show the new title');
  if (!(await page.locator('.clipcard').first().innerText()).includes('edited by you')) fail('the card does not say it was edited');
  await page.locator('.clipcard a:has-text("Edit")').first().click();
  await page.waitForSelector('button:has-text("Reset to the AI plan")');
  await page.click('button:has-text("Reset to the AI plan")');
  await page.waitForSelector('h1:has-text("Edit clip")');                      // the editor stays open and shows the re-render
  await page.waitForFunction((rev) => (document.querySelector('#app video')?.getAttribute('src') || '').endsWith(`v=${rev}`) && !document.querySelector('[role=status]').textContent, rev0 + 2, { timeout: 480000 });
  if ((await page.inputValue('#title')) !== oldTitle) fail('reset did not restore the AI title');
  await page.click('a:has-text("Back to the job")');
  await page.waitForSelector('.clipcard');
  if ((await page.locator('.clipcard h2').first().innerText()) !== oldTitle) fail('the card does not show the AI title again');
  if ((await page.locator('.clipcard').first().innerText()).includes('edited by you')) fail('the card still says edited after a reset');
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) fail('horizontal scroll on a phone');
  if (problems.length) fail(`browser problems: ${JSON.stringify(problems)}`);
  console.log(`OK: ${await page.locator('.clipcard').count()} clips, first ${meta.w}x${meta.h}, ${Math.round(meta.d)} s`);
  await browser.close();
})().catch((e) => fail(e.message));
