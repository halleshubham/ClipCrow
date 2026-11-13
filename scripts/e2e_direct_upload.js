// Browser test of direct-to-bucket uploads (Playwright). Needs an S3-compatible server and a ClipCrow with R2_* pointing at it
// (docs/deploy.md, "Testing direct uploads"). Modes:
//   direct    the CORS rule is right: parts go straight to the bucket, the file never passes through the server
//   fallback  the bucket cannot be reached by the browser (blocked here): a small file falls back to the ordinary upload
//   expose    the bucket answers but does not expose the ETag header: a clear message about the CORS rule, no fallback
// NODE_PATH=$(npm root -g) node scripts/e2e_direct_upload.js <video> <shots dir> direct|fallback|expose
const { chromium } = require('playwright');
const [video, out = '.', mode = 'direct'] = process.argv.slice(2);
const base = process.env.BASE_URL || 'http://127.0.0.1:8767';
const bucketOrigin = process.env.BUCKET_ORIGIN || 'http://127.0.0.1:9100';
const fail = (m) => { console.error('FAIL:', m); process.exit(1); };
if (!video) fail('usage: e2e_direct_upload.js <video> [dir] direct|fallback');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const seen = { puts: [], posts: [], failed: [] };
  const problems = [];
  page.on('request', (r) => {
    if (r.method() === 'PUT' && r.url().startsWith(bucketOrigin)) seen.puts.push(r.url());
    if (r.method() === 'POST' && r.url().startsWith(base)) seen.posts.push(new URL(r.url()).pathname);
  });
  page.on('requestfailed', (r) => { if (r.url().startsWith(bucketOrigin)) seen.failed.push(r.url()); });
  page.on('console', (m) => { if (m.type() === 'error' && mode === 'direct') problems.push(m.text()); });
  page.on('pageerror', (e) => problems.push(e.message));
  if (mode === 'fallback') await page.route(`${bucketOrigin}/**`, (r) => (r.request().method() === 'GET' ? r.continue() : r.abort('failed')));
  await page.goto(base);
  await page.waitForSelector('h1:has-text("New job")');
  await page.setInputFiles('#file', video);
  await page.check('#rights');
  await page.click('button:has-text("Generate clips")');
  if (mode === 'expose') {
    await page.waitForSelector('[role=alert]:has-text("CORS")', { timeout: 120000 });
    if (seen.posts.includes('/api/jobs')) fail('it fell back although the file reached the bucket');
    if (!seen.posts.some((p) => /\/api\/uploads$/.test(p))) fail('the upload was never started');
    console.log('OK (expose): ' + (await page.locator('[role=alert]').innerText()).slice(0, 140));
    await browser.close();
    return;
  }
  await page.waitForURL(/#\/jobs\/[a-f0-9]{12}/, { timeout: 120000 });
  await page.screenshot({ path: `${out}/direct-${mode}-job.png` });
  const jobId = page.url().match(/jobs\/([a-f0-9]{12})/)[1];
  console.log(`job ${jobId}: bucket PUTs ${seen.puts.length}, failed ${seen.failed.length}, server POSTs ${JSON.stringify(seen.posts)}`);
  if (mode === 'direct') {
    if (seen.puts.length < 2) fail(`expected several parts PUT straight to the bucket, saw ${seen.puts.length}`);
    if (seen.failed.length) fail(`bucket requests failed: ${seen.failed.length}`);
    if (!seen.posts.includes('/api/uploads') || !seen.posts.some((p) => /\/api\/uploads\/[a-f0-9]{12}\/complete$/.test(p))) fail(`no start/complete calls: ${seen.posts}`);
    if (seen.posts.includes('/api/jobs')) fail('the file was also POSTed through the server');
    await page.waitForFunction(() => /fetching your upload from storage/.test(document.querySelector('.log pre')?.textContent || ''), null, { timeout: 120000 });
    if (process.env.WAIT_DONE) await page.waitForFunction(() => document.querySelector('.badge')?.textContent === 'Done', null, { timeout: 900000 });
  } else {
    if (!seen.failed.length) fail('the bucket requests were expected to fail');
    if (!seen.posts.includes('/api/jobs')) fail('the upload did not fall back to the ordinary way');
  }
  if (problems.length) fail(`browser problems: ${JSON.stringify(problems)}`);
  console.log(`OK (${mode})`);
  await browser.close();
})().catch((e) => fail(e.message));
