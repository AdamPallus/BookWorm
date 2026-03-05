const { chromium } = require('playwright');
(async()=>{
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const resp = await page.request.get('http://localhost:8000/api/books');
  const data = await resp.json();
  const books = data.books || [];
  if (!books.length) throw new Error('No books found');
  const bookId = books[0].id;
  await page.goto(`http://localhost:8000/read/${bookId}`, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'playwright-report/repro-page0.png' });
  const next = page.locator('#nextPageBtn');
  await next.waitFor({ state: 'visible', timeout: 15000 });
  for (let i = 1; i <= 3; i++) {
    await next.click();
    await page.waitForTimeout(350);
    await page.screenshot({ path: `playwright-report/repro-page${i}.png` });
  }
  console.log('bookId', bookId);
  await browser.close();
})();
