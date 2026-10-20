import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const [modulePath, url, screenshots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
const errors = [];
page.on('console', (message) => {
  if (message.type() === 'error') errors.push(message.text());
});
page.on('pageerror', (error) => errors.push(String(error)));

if (screenshots) fs.mkdirSync(screenshots, { recursive: true });
await page.goto(url);

async function capture(revision) {
  await page.getByTestId('revision').getByText(String(revision), { exact: true }).waitFor({ timeout: 15000 });
  await page.locator('#parts li').waitFor({ timeout: 15000 });
  await page.waitForTimeout(300);
  if (screenshots) await page.screenshot({ path: path.join(screenshots, `revision-${revision}.png`) });
  console.log(JSON.stringify({
    revision: await page.getByTestId('revision').textContent(),
    parts: await page.locator('#parts li').count(),
    errors,
  }));
}

await capture(2);
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
await lines[Symbol.asyncIterator]().next();
await capture(3);
await browser.close();
process.exit(0);
