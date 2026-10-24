import readline from 'node:readline';

const [modulePath, url, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const commands = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const sizes = [[1280, 800], [390, 844]];
const pages = [];

for (const [width, height] of sizes) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
  page.on('requestfailed', (request) => errors.push(`${request.method()} ${new URL(request.url()).pathname} ${request.failure()?.errorText}`));
  await page.addInitScript(() => { window.coalesceDebug = true; });
  await page.goto(url);
  await page.waitForFunction(() => window.coalesceViewer?.revision === 2, null, { timeout: 15000 });
  pages.push({ page, errors, name: `${width}x${height}` });
}

async function resultFor({ page, errors, name }, expectedKinds, screenshot) {
  await page.getByText('Rejected sketch — not accepted', { exact: true }).waitFor({ timeout: 15000 });
  await page.waitForFunction(
    (count) => window.coalesceViewer.diagnostic?.entityCount === count,
    expectedKinds.length,
    { timeout: 15000 },
  );
  if (screenshot) await page.screenshot({ path: `${shots}/${name}-${screenshot}.png` });
  const result = await page.evaluate(() => ({
    revision: window.coalesceViewer.revision,
    diagnostic: window.coalesceViewer.diagnostic,
    label: document.getElementById('rejected-label').textContent,
    alert: document.getElementById('failure').innerText,
    ariaLabel: document.getElementById('view').getAttribute('aria-describedby'),
  }));
  result.errors = errors.splice(0);
  return result;
}

console.log(JSON.stringify({ ready: true }));
await commands.next();
const rejected = {};
for (const entry of pages) rejected[entry.name] = await resultFor(entry, ['point', 'point', 'point', 'point', 'line', 'arc', 'circle'], 'rejected');
console.log(JSON.stringify({ rejected }));

await commands.next();
const large = {};
for (const entry of pages) {
  large[entry.name] = await resultFor(entry, Array(300).fill('point'), '64-kib');
}
console.log(JSON.stringify({ large }));

await commands.next();
const omitted = {};
for (const { page, errors, name } of pages) {
  await page.getByText('Preview too large', { exact: false }).waitFor({ timeout: 15000 });
  await page.waitForFunction(() => window.coalesceViewer.diagnostic?.entityCount === 0);
  omitted[name] = {
    revision: await page.evaluate(() => window.coalesceViewer.revision),
    labelVisible: await page.locator('#rejected-label').isVisible(),
    failure: await page.locator('#failure').innerText(),
    errors: errors.splice(0),
  };
}
console.log(JSON.stringify({ omitted }));

await commands.next();
const reloaded = {};
for (const { page, errors, name } of pages) {
  await page.reload();
  await page.waitForFunction(() => window.coalesceViewer?.revision === 2, null, { timeout: 15000 });
  await page.waitForTimeout(200);
  reloaded[name] = {
    labelVisible: await page.locator('#rejected-label').isVisible(),
    diagnostic: await page.evaluate(() => window.coalesceViewer.diagnostic),
    failureVisible: await page.locator('#failure').isVisible(),
    errors: errors.splice(0),
  };
}
console.log(JSON.stringify({ reloaded }));

await commands.next();
const repaired = {};
for (const { page, errors, name } of pages) {
  await page.waitForFunction(() => window.coalesceViewer?.revision === 3, null, { timeout: 15000 });
  await page.keyboard.press('Tab');
  repaired[name] = {
    labelVisible: await page.locator('#rejected-label').isVisible(),
    diagnostic: await page.evaluate(() => window.coalesceViewer.diagnostic),
    keyboardFocus: await page.evaluate(() => document.activeElement?.tagName),
    description: await page.locator('#view-description').textContent(),
    errors: errors.splice(0),
  };
}
console.log(JSON.stringify({ repaired }));

await browser.close();
