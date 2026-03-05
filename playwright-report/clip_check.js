const { chromium } = require('playwright');

// Repro checker for bottom-line clipping inside #readerSurface.
//
// Usage:
//   node playwright-report/clip_check.js
//
// Requirements:
// - Opens /api/books, navigates to first book's /read/{id}
// - Turns pages >= 20 times
// - For each page counts text line rects (Range.getClientRects) that are visible
//   within #readerSurface and flags lines clipped at the bottom:
//     rect.bottom > container.bottom + 0.5
// - Prints summary: maxClippedBottom + pagesWithClipping

(async () => {
  const BASE = process.env.BOOKWORM_BASE_URL || 'http://localhost:8000';
  const TURNS = Number(process.env.PAGE_TURNS || 25);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

  const data = await (await page.request.get(`${BASE}/api/books`)).json();
  const bookId = (data.books || [])[0]?.id;
  if (!bookId) throw new Error('No books returned from /api/books');

  await page.goto(`${BASE}/read/${bookId}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  async function pageMetrics() {
    return await page.evaluate(() => {
      const container = document.querySelector('#readerSurface');
      const content = container && container.querySelector('.bw-chapter-content');
      if (!container || !content) return { error: 'missing #readerSurface or .bw-chapter-content' };

      const crect = container.getBoundingClientRect();
      const cTop = crect.top;
      const cBottom = crect.bottom;

      const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
      let node;
      let clippedBottom = 0; let clippedTop = 0;
      let visibleLines = 0;

      while ((node = walker.nextNode())) {
        const text = node.textContent || '';
        if (!text.trim()) continue;

        const range = document.createRange();
        range.selectNodeContents(node);
        const rects = range.getClientRects();

        for (const rect of rects) {
          if (rect.height <= 0) continue;
          // Count only lines that intersect the container vertically.
          if (rect.bottom <= cTop || rect.top >= cBottom) continue;
          visibleLines += 1;
          if (rect.bottom > cBottom + 0.5) clippedBottom += 1; if (rect.top < cTop - 0.5) clippedTop += 1;
        }
      }

      return {
        scrollTop: container.scrollTop,
        clientHeight: container.clientHeight,
        scrollHeight: container.scrollHeight,
        clippedBottom, clippedTop,
        visibleLines,
      };
    });
  }

  const next = page.locator('#nextPageBtn');
  const prev = page.locator('#prevPageBtn');
  await next.waitFor({ state: 'visible', timeout: 10000 });
  await prev.waitFor({ state: 'visible', timeout: 10000 });

  let maxClippedBottom = 0;
  let maxClippedTop = 0; let pagesWithClipping = 0;

  // include the initial page as page 0
  for (let i = 0; i <= TURNS; i++) {
    const m = await pageMetrics();
    if (m.error) throw new Error(m.error);

    maxClippedBottom = Math.max(maxClippedBottom, m.clippedBottom); maxClippedTop = Math.max(maxClippedTop, m.clippedTop);
    if (m.clippedBottom > 0 || m.clippedTop > 0) pagesWithClipping += 1;

    console.log(`page${i}`, JSON.stringify(m));

    if (i < TURNS) {
      await next.click();
      await page.waitForTimeout(250);
    }
  }

  // basic sanity: make sure prev also works (no stuck state)
  await prev.click();
  await page.waitForTimeout(250);

  console.log('SUMMARY', JSON.stringify({ maxClippedBottom, maxClippedTop, pagesWithClipping, turns: TURNS + 1 }));

  await browser.close();
})();
