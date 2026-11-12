// Browser test of the account screens (needs Playwright and a server started with accounts and billing configured):
//   RAZORPAY_KEY_ID=x RAZORPAY_KEY_SECRET=y RAZORPAY_WEBHOOK_SECRET=z CLIPCROW_PLANS=plans.json \
//   clipcrow serve --demo --accounts --data /tmp/cc-acc --port 8766 &   NODE_PATH=$(npm root -g) node scripts/e2e_accounts.js /tmp/shots
const { chromium } = require('playwright');
const out = process.argv[2] || '.';
const base = process.env.BASE_URL || 'http://127.0.0.1:8766';
const fail = (m) => { console.error('FAIL:', m); process.exit(1); };
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d|status of 502/.test(m.text())) problems.push(m.text()); });
  page.on('pageerror', (e) => problems.push(e.message));
  page.on('dialog', (d) => d.accept());
  await page.goto(base);
  await page.waitForSelector('h1:has-text("Sign in")');
  await page.screenshot({ path: `${out}/acc-1-signin.png` });
  await page.click('button:has-text("Create an account")');
  await page.waitForSelector('h1:has-text("Create your account")');
  await page.fill('#email', 'tester@example.com');
  await page.fill('#password', 'short');
  await page.click('button:has-text("Create account")');
  await page.waitForSelector('[role=alert]:has-text("at least 10")');
  await page.fill('#password', 'a long enough password');
  await page.click('button:has-text("Create account")');
  await page.waitForSelector('h1:has-text("New job")');
  if (!(await page.locator('#acct').innerText()).includes('tester@example.com')) fail('the header does not show who is signed in');
  await page.click('#acct a');
  await page.waitForSelector('h1:has-text("Account")');
  const text = await page.locator('main').innerText();
  if (!/0 of 30/.test(text)) fail(`no usage meter: ${text.slice(0, 200)}`);
  await page.waitForSelector('text=Plan and minutes');
  await page.screenshot({ path: `${out}/acc-2-account.png`, fullPage: true });
  await page.click('button:has-text("Choose Pro")');                       // the real payment service is not reachable from here
  await page.waitForSelector('[role=alert]:has-text("payment service")', { timeout: 20000 });
  await page.click('button:has-text("Sign out")');
  await page.waitForSelector('h1:has-text("Sign in")');
  await page.fill('#email', 'tester@example.com');
  await page.fill('#password', 'the wrong password');
  await page.click('button:has-text("Sign in")');
  await page.waitForSelector('[role=alert]:has-text("do not match")');
  await page.fill('#password', 'a long enough password');
  await page.click('button:has-text("Sign in")');
  await page.waitForSelector('h1:has-text("New job")');
  await page.click('#acct a');
  await page.waitForSelector('h1:has-text("Delete account")', { state: 'attached' }).catch(() => {});
  await page.fill('#del-pw', 'a long enough password');
  await page.click('button:has-text("Delete my account")');
  await page.waitForSelector('h1:has-text("Sign in")');
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) fail('horizontal scroll on a phone');
  if (problems.length) fail(`browser problems: ${JSON.stringify(problems)}`);
  console.log('OK: sign up, usage meter, billing section, sign out/in, delete account');
  await browser.close();
})().catch((e) => fail(e.message));
