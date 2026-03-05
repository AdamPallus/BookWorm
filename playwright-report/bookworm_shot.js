const { chromium } = require('playwright');
(async()=>{
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto('http://localhost:8000', { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'playwright-report/bookworm-page0.png', fullPage: false });
  const next = page.locator('#nextPageBtn');
  for (let i = 1; i <= 3; i++) {
    await next.click();
    await page.waitForTimeout(350);
    await page.screenshot({ path: `playwright-report/bookworm-page${i}.png`, fullPage: false });
  }
  await browser.close();
  console.log('ok');
})().catch((e)=>{ console.error(e); process.exit(1); });
