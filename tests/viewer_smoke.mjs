// Browser smoke test driver for tests/test_viewer_browser.py.
// Usage: node viewer_smoke.mjs <playwright module path> <viewer url> [screenshot dir]
// Protocol: prints "step <name> <json>" after each check and waits for one stdin line
// ("next") before the following step, so the Python side can publish between steps.
import readline from 'node:readline';

const [modulePath, url, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));

// share of canvas pixels covered by model edge lines, plus the topmost such row
async function drawn() {
  return page.evaluate(() => {
    const source = document.getElementById('view');
    const copy = document.createElement('canvas');
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext('2d');
    context.drawImage(source, 0, 0);
    const { data } = context.getImageData(0, 0, copy.width, copy.height);
    let dark = 0;
    let top = copy.height;
    for (let i = 0; i < data.length; i += 4) {
      // only the model's B-rep edge lines (#1d2329) are this dark; grid and axes are lighter
      if (data[i] + data[i + 1] + data[i + 2] < 200) {
        dark += 1;
        top = Math.min(top, Math.floor(i / 4 / copy.width));
      }
    }
    return { dark: dark / (data.length / 4), top };
  });
}

async function step(name, check) {
  const result = await check();
  if (shots) await page.screenshot({ path: `${shots}/${name}.png` });
  console.log(`step ${name} ${JSON.stringify({ ...result, errors })}`);
  await lines.next();
}

const revision = () => page.getByTestId('revision');
const waitRevision = (n) => page.waitForFunction((n) => window.coalesceViewer?.revision === n, n, { timeout: 15000 });

await page.goto(url);
await step('empty', async () => {
  await page.getByText('Waiting for the first revision').waitFor({ timeout: 15000 });
  return { label: await revision().textContent(), link: await page.locator('#link-text').textContent() };
});
await waitRevision(1);
await page.waitForTimeout(300);
await step('revision-1', async () => ({ label: await revision().textContent(), url: page.url(), ...(await drawn()) }));
await waitRevision(2);
await page.waitForTimeout(300);
await step('revision-2', async () => ({ label: await revision().textContent(), ...(await drawn()) }));
await page.locator('#failure').waitFor({ state: 'visible', timeout: 15000 });
await step('failure', async () => ({
  label: await revision().textContent(),
  failure: await page.locator('#failure').innerText(),
  ...(await drawn()),
}));
await page.getByRole('button', { name: 'Front' }).click();
await page.waitForTimeout(300);
await step('front', async () => ({ pressed: await page.getByRole('button', { name: 'Front' }).getAttribute('aria-pressed'), ...(await drawn()) }));
await browser.close();
process.exit(0); // stdin stays open otherwise
