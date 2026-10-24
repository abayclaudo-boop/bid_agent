# Bid News Agent

An autonomous Python agent that monitors a website for new **bid /
tender / RFP / procurement** notices, uses GLM (via the `z-ai` CLI)
to judge whether each new item is actually a relevant bid, and
forwards the relevant ones as rich Telegram notifications — with
inline "View original" buttons and zero duplicates.

---

## What it does

1. **Scrapes** a configured listing page (HTML via `requests` +
   BeautifulSoup, or JS-rendered pages via the `agent-browser` CLI).
2. **Extracts** candidate items (title + absolute URL).
3. **Judges** each item with GLM — outputs `{is_relevant, score,
   category, deadline, budget, summary}` as strict JSON.
4. **Sends** rich Telegram messages (Markdown) with an inline
   *View original* button linking to the source.
5. **Persists** every sent URL in SQLite so the same bid is never
   sent twice — even across restarts.

---

## Quick start (5 minutes)

### 1. Install Python deps

```bash
cd /home/z/my-project/scripts/bid_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your Telegram bot

1. Open Telegram, search **@BotFather**, send `/newbot`.
2. Pick a name and username. BotFather replies with a **bot token**
   (looks like `123456789:ABC-DEF…`).
3. Start a chat with your new bot and send any message
   (e.g. "hello"). This is required — bots can't message users who
   haven't talked to them first.
4. Open this URL in your browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. In the JSON response, find `"chat":{"id": 123456789}` — that
   number is your **chat ID**.

### 3. Configure secrets

```bash
cp .env.example .env
# Edit .env and paste in your real TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

### 4. Configure the target site

Edit `config.yaml`. At minimum, replace the `sites[0].url` with the
bid listing page you want to monitor. If the site is plain HTML,
leave `use_browser: false`. If the page is JS-rendered (React,
Vue, etc.), set `use_browser: true` and install the browser tool:

```bash
npm install -g agent-browser
agent-browser install
```

### 5. Dry-run test

Verify scraping + LLM judgement without sending any messages:

```bash
python bid_agent.py --dry-run
```

You should see log lines like:

```
[Example Bid Portal] Found 12 candidate nodes via selector 'article, .bid-item, .tender, li'
Skip (not relevant, score=20): Some press release title…
[DRY] Would send: RFP for IT consultancy services… (IT & Software, score=88)
```

### 6. Real run

```bash
python bid_agent.py          # single cycle
python bid_agent.py --daemon # run forever on the configured interval
```

### 7. Install as a cron job (recommended)

```bash
bash install_cron.sh
```

This creates a venv, installs dependencies, and adds a cron entry
that runs the agent every 30 minutes. View logs:

```bash
tail -f /home/z/my-project/scripts/bid_agent/data/agent.log
```

---

## Config reference (`config.yaml`)

| Field | Description |
|---|---|
| `sites[].name` | Human-readable label for the source. |
| `sites[].url` | Listing page URL. |
| `sites[].rss` | *(optional)* RSS/Atom feed URL — preferred over HTML scraping. |
| `sites[].container` | CSS selector for the listing container (e.g. `main`, `.tenders-list`). |
| `sites[].item_selector` | CSS selector for a single bid row (e.g. `article`, `.bid-item`, `li`). |
| `sites[].link_selector` | CSS selector for the `<a>` inside an item (default `a`). |
| `sites[].title_selector` | CSS selector for the title text. |
| `sites[].use_browser` | `true` if page is JS-rendered. Falls back to `agent-browser`. |
| `filter_brief` | Plain-English description of what counts as relevant — be specific. |
| `min_relevance_score` | 0–100 cutoff for sending (default 60). |
| `run_interval_minutes` | Daemon loop interval (default 30). |
| `max_items_per_run` | Safety cap — if exceeded, the site is skipped to avoid spam. |
| `db_path` | SQLite path (dedupe + history). |
| `log_file` / `log_level` | Logging config. |
| `message_template` | Markdown template for Telegram (supports `{title}`, `{category}`, `{deadline}`, `{budget}`, `{summary}`, `{source_url}`, `{site_name}`, `{found_at}`). |

---

## How AI filtering works

For each new candidate URL, the agent calls GLM with a strict
system prompt and the candidate's `{title, URL, HTML snippet}`. The
model is forced to return strict JSON:

```json
{
  "is_relevant": true,
  "relevance_score": 88,
  "category": "IT & Software",
  "deadline": "2026-11-30",
  "budget": "USD 250,000",
  "summary": "RFP for ERP customization services. Submission deadline Nov 30."
}
```

Items scoring below `min_relevance_score` are silently dropped
(but still recorded in SQLite so they aren't re-judged next cycle).
Relevant items get a rich Telegram message + inline button.

To tune behavior, edit `filter_brief` in `config.yaml` — make it
specific to your industry, geography, or organization type.

---

## File layout

```
bid_agent/
├── bid_agent.py          # The agent (single file, ~600 lines)
├── config.yaml           # Editable configuration
├── .env.example          # Secret template — copy to .env
├── .env                  # Your real secrets (gitignored)
├── requirements.txt
├── install_cron.sh       # One-shot cron installer
├── README.md
└── data/                 # Created on first run
    ├── sent_items.db     # SQLite dedupe store
    ├── agent.log
    └── cron.log
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Telegram credentials not configured` | `.env` is missing or values start with `your_`. Re-edit. |
| `Telegram API error: …can't parse entities…` | Agent auto-retries as plain text — usually resolves itself. If persistent, simplify `message_template`. |
| `HTTP fetch failed` | Site is blocking. Try a real `USER_AGENT` in `.env`. If still blocked, set `use_browser: true`. |
| `Found 0 candidate nodes` | Your CSS selectors don't match. Open the page in a browser, inspect the listing container, and update `container` / `item_selector` in `config.yaml`. |
| `Could not parse LLM JSON` | The model occasionally wraps JSON in markdown fences. The agent already strips them — if you still see this, lower `filter_brief` complexity. |
| `z-ai: command not found` | The `z-ai` CLI is pre-installed in the project env. Confirm with `which z-ai`. |
| Too many false positives | Raise `min_relevance_score` (e.g. 70 or 80). |
| Too many false negatives | Lower `min_relevance_score` (e.g. 50) or expand `filter_brief`. |

---

## Manual SQL queries on history

```bash
sqlite3 /home/z/my-project/scripts/bid_agent/data/sent_items.db

# Items sent in the last 7 days, by category
sqlite> SELECT category, COUNT(*) FROM sent_items
        WHERE sent_at > datetime('now','-7 days')
        GROUP BY category ORDER BY 2 DESC;

# Recent 20 notifications
sqlite> SELECT sent_at, category, title FROM sent_items
        ORDER BY sent_at DESC LIMIT 20;
```

---

## Extending

- **Multiple sites:** add more entries to the `sites:` list in
  `config.yaml`. Each site runs in its own scrape cycle.
- **Custom categories:** edit the system prompt in
  `LLMFilter.SYSTEM_PROMPT_TEMPLATE` inside `bid_agent.py`.
- **Telegram reply buttons:** add `callback_data` handlers by
  running `getUpdates` on your bot — the current "Mark as read"
  button is a placeholder that needs a webhook/poller to act on.
- **Web dashboard:** wrap the agent in a Flask app exposing a
  `/status` endpoint + simple HTML table. (Out of scope for the
  Python-script deliverable.)

Enjoy never missing a bid again. 🎯
