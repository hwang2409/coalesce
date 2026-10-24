const [modulePath, url] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
await page.addInitScript(() => { window.coalesceDebug = true; });
await page.goto(url);
await page.waitForFunction(() => window.coalesceViewer?.revision === 3, null, { timeout: 15000 });
console.log(JSON.stringify({
  labelVisible: await page.locator('#rejected-label').isVisible(),
  diagnostic: await page.evaluate(() => window.coalesceViewer.diagnostic),
  failureVisible: await page.locator('#failure').isVisible(),
  errors,
}));
await browser.close();
