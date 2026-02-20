# Bookworm Phase 2 — EPUB Reader + Contextual Q&A

## Overview
Transform Bookworm from a proof-of-concept upload-and-ask tool into an actual EPUB reader with integrated, spoiler-free AI Q&A. The core loop: **read a book in the app → tap a button → ask about what you've read → get answers that can't spoil you.**

## Stack
- **Backend:** Python / FastAPI (keep existing, extend)
- **Frontend:** Vanilla JS + CSS (no framework, no build step)
- **EPUB rendering:** [epub.js](https://github.com/futurepress/epub.js) (v0.3+)
- **Database:** SQLite + sqlite-vec (keep existing)
- **Embeddings:** OpenAI `text-embedding-3-small` (batch API for imports, sync for queries)
- **Q&A model:** OpenAI `gpt-5-mini` (configurable in settings)
- **Target:** Desktop browser + iPad Safari (local network only)

## Design Language

### Warm Dark Mode
- **Background:** Deep warm charcoal (`#1a1a1a` → `#242220`) — not cold gray
- **Surface cards:** `#2a2725` with subtle warm border `#3a3530`
- **Text:** Warm off-white `#e8e0d4` (never pure white — easy on the eyes for long reading)
- **Accent:** Amber/gold `#d4a048` — used sparingly for interactive elements, progress bars, the Q&A button
- **Reader area:** Slightly lighter warm surface `#2e2a27` for the book content, creating a subtle "page" feel without going full white
- **Typography:** Georgia or system serif for book content, system-ui for UI chrome
- **Transitions:** Subtle, 150-200ms eases. Nothing flashy. Calm.

### Layout Philosophy
- **Desktop (>1024px):** Reader centered with comfortable max-width (~720px content), Q&A panel slides in from right (400px wide), library is a grid
- **iPad/tablet (768-1024px):** Reader full-width with margins, Q&A panel slides over as overlay (80% width), library is 2-column grid
- **Mobile (<768px):** Works but not optimized — reader full-width, Q&A is full-screen overlay

## Pages & Components

### 1. Library Page (`/`)
The home screen. Grid of your books.

**Book cards:**
- Cover image extracted from EPUB metadata (fallback: generated gradient card with title/author in nice typography)
- Title, author
- Progress bar (amber, thin, at bottom of card)
- "62% · Chapter 14" subtitle
- Last read timestamp ("2 hours ago")
- Click → opens reader

**Actions:**
- Import button (top right) — drag-and-drop zone or file picker
- Import progress shown inline on the card as it processes (pulsing amber glow)
- Settings gear (top right, same as current)

**Empty state:** Warm, inviting message. "Drop an EPUB to start reading." Maybe a subtle book icon.

### 2. Reader Page (`/read/{book_id}`)
The main experience.

**Top bar (slim, 40px):**
- Back arrow → library
- Book title (truncated)
- Chapter title
- Progress: "Chapter 8 · 34% of chapter · 52% of book"
- Q&A button (amber circle, bottom-right floating, or top-right icon) — `💬` or a subtle sparkle icon

**Reader area:**
- epub.js rendered content, paginated (not scroll — paginated feels more like a book)
- Page turn: click left/right edges, swipe on touch, arrow keys
- Font size adjustable (A- / A+ in top bar or a small popover)
- The reader background and text colors should match the warm dark theme

**Position tracking:**
- epub.js provides CFI (canonical fragment identifier) for exact position
- On every page turn, update the backend with the current position
- Map CFI → chapter_index + position_index for the RAG filter
- Debounce position updates (every 3 seconds or on page turn, whichever is less frequent)

**Q&A Panel (slide-over):**
- Triggered by the floating Q&A button
- Slides in from right with a subtle backdrop dimming
- **Header:** "Ask about this book" + close button
- **Context indicator:** "I know everything up to: Chapter 8, page 34" (derived from current reading position)
- **Chat-style interface:** 
  - Scrollable message history (persisted per book)
  - User messages right-aligned, AI responses left-aligned
  - Amber accent on user bubbles, warm surface on AI bubbles
  - Streaming responses (SSE) so the answer types out
- **Input:** Text input at bottom with send button, auto-focus on open
- **History navigation:** Back/forward arrows (◀ ▶) in the panel header to browse previous Q&A pairs for this book. Works like browser history — asking a new question pushes to the stack, back/forward navigates without losing anything. Cap at ~40 entries. (Same pattern as CoachNotes answer history — see reference below.)
- **Quick actions (optional, nice-to-have):** "Summarize this chapter" / "Who is [character]?" / "What just happened?" as tappable chips above the input

### Q&A History (reference: CoachNotes pattern)
Maintain an in-memory answer history stack per book, also persisted to the `conversations` table.

**State model:**
```javascript
state = {
  answerHistory: [],      // Array of { question, answer, sources, citations, position_context, timestamp }
  answerHistoryIndex: -1  // Current position in history
}
```

**Behavior:**
- New question → truncate any forward history, push new entry, set index to end
- Back button → decrement index, re-render that Q&A pair (disable when index = 0)
- Forward button → increment index, re-render (disable when at end)
- History persists across panel open/close within the same session
- On page load, hydrate from `GET /api/books/{id}/conversations`
- Max 40 entries in memory; DB stores all

**Reference implementation** from CoachNotes (`apps/desktop/renderer/app.js`):
```javascript
function pushAnswerHistory(entry) {
  let nextHistory = state.answerHistory;
  if (state.answerHistoryIndex >= 0 && state.answerHistoryIndex < nextHistory.length - 1) {
    nextHistory = nextHistory.slice(0, state.answerHistoryIndex + 1);
  }
  nextHistory = [...nextHistory, entry];
  const maxEntries = 40;
  if (nextHistory.length > maxEntries) {
    nextHistory = nextHistory.slice(nextHistory.length - maxEntries);
  }
  state.answerHistory = nextHistory;
  state.answerHistoryIndex = state.answerHistory.length - 1;
  updateAnswerHistoryControls();
}

function updateAnswerHistoryControls() {
  const hasHistory = state.answerHistory.length > 0 && state.answerHistoryIndex >= 0;
  els.answerBackBtn.disabled = !hasHistory || state.answerHistoryIndex <= 0;
  els.answerForwardBtn.disabled = !hasHistory || state.answerHistoryIndex >= state.answerHistory.length - 1;
}
```

### 3. Settings (modal, same as current but expanded)
- OpenAI API key (keep current)
- Q&A model selector (see below)
- Reader font size default
- Theme: dark only for now (but structured so light mode could be added)

### Model Selector
Provide an in-app model picker, either in Settings or directly on the Q&A panel (small dropdown near the input).

**Available models:**
| Model | Label | Use case |
|-------|-------|----------|
| `gpt-5-mini` | Quick (default) | Fast, cheap, good for simple questions |
| `gpt-5` | Standard | Better reasoning, still fast |
| `gpt-5.2` | Deep | Best quality, slower, for complex analysis |

Store the selected model in settings (`.env` or DB). The model name is passed to the `/api/books/{id}/query` endpoint. The backend passes it through to OpenAI.

This is a personal/BYOK app — no need to restrict model access or worry about cost controls.

## API Endpoints

### Existing (keep)
- `GET /api/books` — list books
- `POST /api/books/import` — import EPUB
- `POST /api/books/{id}/position` — set reading position
- `POST /api/books/{id}/query` — ask a question
- `GET /api/settings` / `POST /api/settings` — API key management

### New
- `GET /api/books/{id}` — single book detail (title, author, total_chunks, chapters, current position, cover URL)
- `GET /api/books/{id}/chapters` — list chapters with titles and position ranges
- `GET /api/books/{id}/cover` — serve cover image extracted from EPUB
- `GET /api/books/{id}/epub` — serve the raw EPUB file for epub.js to render client-side
- `POST /api/books/{id}/position` — update to also accept CFI string, map it internally
- `GET /api/books/{id}/conversations` — get Q&A history for this book
- `POST /api/books/{id}/query` — update to return streaming SSE response + save to conversation history
- `DELETE /api/books/{id}` — remove book and all associated data

### Import Pipeline (batch optimization)
1. `POST /api/books/import` — EPUB uploaded, parsed, chunked, stored. Returns book_id immediately.
2. Embeddings submitted to OpenAI Batch API asynchronously.
3. `GET /api/books/{id}` includes `embedding_status`: `"processing"` | `"ready"` | `"failed"`
4. Client polls or uses SSE to know when embeddings are done.
5. Q&A disabled until embeddings are ready (button shows "Indexing..." state).

**Fallback:** If batch API isn't worth the complexity, just use sync embeddings. A whole book takes <10 seconds and costs ~1 cent. Implementer's call — don't over-engineer this.

## Data Model Updates

### New tables
```sql
-- Store cover images
ALTER TABLE books ADD COLUMN cover_path TEXT;
-- Store the original epub for serving to reader  
ALTER TABLE books ADD COLUMN epub_path TEXT;
-- Embedding status
ALTER TABLE books ADD COLUMN embedding_status TEXT DEFAULT 'ready';
-- Current CFI position
ALTER TABLE book_positions ADD COLUMN cfi TEXT;

-- Q&A conversation history
CREATE TABLE conversations (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  role TEXT NOT NULL, -- 'user' or 'assistant'
  content TEXT NOT NULL,
  position_context INTEGER, -- position_index at time of question
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

## EPUB Storage
- On import, save the EPUB file to `data/epubs/{book_id}.epub`
- Extract cover image to `data/covers/{book_id}.jpg`
- Serve both via API endpoints
- `data/` directory lives next to `bookworm.db`

## Key Implementation Notes

### epub.js Integration
```javascript
// Basic setup
const book = ePub("/api/books/1/epub");
const rendition = book.renderTo("reader", {
  width: "100%",
  height: "100%",
  flow: "paginated",
  theme: { body: { background: "#2e2a27", color: "#e8e0d4" } }
});
rendition.display();

// Track position
rendition.on("relocated", (location) => {
  // location.start.cfi, location.start.percentage, etc.
  updatePosition(bookId, location);
});
```

### Streaming Q&A
- Use NDJSON streaming (same pattern as CoachNotes, not SSE) for responses
- Backend streams `{"type":"delta","delta":"..."}` lines as tokens arrive from OpenAI
- Final line: `{"type":"done","data":{answer, citations, sources}}` with full parsed result
- Frontend appends deltas to the chat bubble in real-time, then processes citations on `done`
- Error handling: `{"type":"error","error":"..."}` if something goes wrong mid-stream

**Reference:** CoachNotes `apps/proxy/api/answer.js` implements this exact pattern with `beginNdjsonStream()`, `writeNdjsonEvent()`, and streaming via `openai.responses.create({stream: true})`.

### Responsive Breakpoints
- Use CSS `@container` queries if possible, otherwise media queries
- Q&A panel: `position: fixed; right: 0; top: 0; height: 100vh; transform: translateX(100%)` → slide in with `transform: translateX(0)`
- On tablet: panel width `80vw`; on desktop: `400px`

## Citations & Source Navigation

### How Citations Work
The RAG pipeline already returns source chunks with chapter_index and position_index. We extend this to enable click-to-navigate.

### Data Flow
1. When chunks are stored during import, also save the **CFI** (epub.js canonical fragment identifier) for the start of each chunk. This requires a one-time mapping pass after the EPUB is ingested: walk through the book's spine in epub.js order and map each chunk's text to its CFI location.
2. Alternatively (simpler): store the **chapter spine index** and **character offset** per chunk during ingest. When the user clicks a citation, use epub.js's `rendition.display(spineItem, charOffset)` or search for the chunk's opening text to locate it.
3. **Recommended approach:** Store each chunk's source chapter href (from the EPUB spine) and a short anchor snippet (first ~60 chars of the chunk). On citation click, use epub.js `book.spine.get(href)` + text search to navigate precisely. This avoids needing CFI at ingest time.

### Citation Display in Q&A
- AI responses include inline citations like **[Ch. 8, §3]** or **[Chapter 8: "The Passage"]**
- Each citation is a clickable link (amber text, subtle underline)
- Update the Q&A system prompt to instruct the model to cite sources using a structured format, e.g. `【1】` `【2】` etc.
- Below the answer, show a numbered source list:
  ```
  【1】 Chapter 8 — "Darrow gripped the clawDrill, feeling the vibration..."
  【2】 Chapter 5 — "The Reds worked the deep tunnels because no one else..."
  ```
- Each source entry is also clickable

### Navigation Behavior
1. **User clicks a citation** → save current reading position to a "return stack"
2. **Reader jumps to the cited passage** → briefly highlight the relevant text (amber glow, fades after 2s)
3. **"Return to reading" button appears** — floating, bottom-center, amber pill: "↩ Back to page 47"
4. Clicking it restores the saved position
5. The return button auto-dismisses after 10 seconds of reading (user has moved on) or on manual dismiss
6. Support multiple jumps — the return stack handles: citation A → citation B → back to B's origin → back to A's origin

### Chunk-to-CFI Mapping Table
```sql
-- Add to chunks table or create mapping
ALTER TABLE chunks ADD COLUMN spine_href TEXT;   -- EPUB spine item href
ALTER TABLE chunks ADD COLUMN anchor_text TEXT;   -- First ~80 chars for text search navigation
```

During import, when extracting chapters from the EPUB, also store:
- `spine_href`: the item href from `book.get_items()` that this chunk came from
- `anchor_text`: the first ~80 characters of the chunk (used by epub.js to find the location via text search)

### System Prompt Update for Citations
```
You are a spoiler-free book companion. Answer ONLY using the provided excerpts.
Do NOT use any knowledge about this book from your training data.
Cite your sources using [c:CHUNK_ID] markers (e.g. [c:42], [c:107]).
Each CHUNK_ID corresponds to the chunk_id provided with each excerpt.
If the excerpts don't contain the answer, say you don't have enough information.
Keep the answer concise and conversational.
```

### Flexible Citation Parsing (lesson from CoachNotes)
GPT-mini doesn't always follow citation format precisely. The citation parser must handle:
- Canonical format: `[c:42]`
- Bracketed variants: `[c: 42]`, `[c:42, c:107]`
- Numbered fallbacks: `【1】`, `[1]`, `(1)` — map these to source excerpts by position
- Missing citations entirely — still show the answer, just without clickable links

Reference implementation from CoachNotes (`apps/proxy/api/_shared.js`):
```javascript
function collectCitations(answerText) {
  const ids = new Set();
  const pattern = /\[c:([^\]]+)\]/g;
  let match = pattern.exec(answerText || '');
  while (match) {
    ids.add(match[1]);
    match = pattern.exec(answerText || '');
  }
  return [...ids];
}
```
Extend this for Bookworm to also handle the numbered fallback patterns. Strip citation markers from the displayed text and replace with styled clickable links.

## What's NOT in Phase 2
- User accounts / auth (local-only app)
- Cloud sync
- Highlights & annotations (Phase 3)
- Light mode
- Multiple device sync
- PDF support
- Audio book support
- Social features
- "Explain this highlight" (Phase 3, requires selection API)

## Success Criteria
1. Can import an EPUB and see it in the library with cover art
2. Can open and read paginated content that looks good in warm dark mode
3. Reading position auto-tracks and persists across sessions
4. Can open Q&A panel and ask questions with streaming answers
5. Answers respect reading position (no spoilers)
6. Works well on both desktop browser and iPad Safari on local network
7. Feels warm, calm, and pleasant to use — not like a developer tool
