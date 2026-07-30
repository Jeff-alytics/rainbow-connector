const { chromium } = require('playwright');

const TARGET_URL = 'http://localhost:8888';

(async () => {
  const browser = await chromium.launch({ headless: false });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1400, height: 900 });

  await page.goto(TARGET_URL, { waitUntil: 'networkidle', timeout: 15000 });
  console.log('Page loaded:', await page.title());

  // Enter ZIP and submit
  await page.fill('#zip', '70124');
  await page.click('#addbtn');

  // Wait for the forecast to load
  await page.waitForTimeout(8000);

  await page.screenshot({ path: 'C:/Users/jeffm/rainbow-dial-test.png', fullPage: true });
  console.log('Screenshot saved');

  await browser.close();
})();
