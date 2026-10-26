# Bid News Agent — GitHub Actions Setup (free, no credit card)

This is the recommended deployment for users who:
- Don't have a credit card (so Oracle Cloud / Vultr / DigitalOcean are out)
- Want alerts more than once a day (PythonAnywhere free is only once/day)
- Are okay with **3 runs/day** (every 8 hours)

Cost: **$0/month, forever.**

---

## Schedule

Runs 3 times a day at:

| UTC | East Africa Time (UTC+3) | Why |
|---|---|---|
| 06:00 | 09:00 — morning | Catches overnight posts |
| 12:00 | 15:00 — afternoon | Catches morning posts |
| 18:00 | 21:00 — evening | Catches afternoon posts |

To change: edit the `cron:` line in `.github/workflows/bid-agent.yml`.

---

## Step-by-step setup

### 1. Create a free GitHub account (if you don't have one)

https://github.com/signup — just an email and password, no card needed.

### 2. Create a new PUBLIC repo

Go to: https://repo.new

- Repository name: `bid-news-agent`
- Visibility: **Public** (required for unlimited Actions minutes)
- ✅ Add a README file (optional)
- Click **Create repository**

### 3. Upload the agent files

You can do this entirely in the GitHub web UI (no terminal needed):

For each file in `/home/z/my-project/download/bid_agent/`, click **Add file → Upload files**:

- `.gitignore` (toggle "Show hidden files" to see dotfiles — or just create it via the web UI)
- `.github/workflows/bid-agent.yml` — critical: keep the folder structure `.github/workflows/`
- `README.md`
- `bid_agent.py`
- `config.yaml`
- `install_cron.sh` (you don't need this on GitHub, but harmless)
- `requirements.txt`

⚠️ **Do NOT upload `.env`** — it contains your real credentials and must never be in git. The `.gitignore` file already excludes it, but the web UI doesn't honor `.gitignore` for uploads, so just don't upload it.

Commit with any message.

### 4. Add the 5 secrets

In your repo: **Settings** → **Secrets and variables** → **Actions** → **New repository secret** (do this 5 times):

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `8949923935:AAEovkNe-gAJBogyZvA7SzhLglqxKQyICBQ` |
| `TELEGRAM_CHAT_ID` | `384173598` |
| `SITE_LOGIN_EMAIL` | `0911513087` |
| `SITE_LOGIN_PASSWORD` | `Abay2025` |
| `GEMINI_API_KEY` | `AIzaSyAJ_N4s5gkfyytk8_DBdjsJNlrVQrjuZlI` |

GitHub encrypts these at rest. They never appear in logs.

### 5. Trigger the first run manually

In your repo: **Actions** tab → click the workflow **"Bid News Agent"** in the left sidebar → click the green **Run workflow** button (top right) → click the blue **Run workflow** button.

Watch the run go green (≈ 2 minutes). After it finishes, check your Telegram — you should see vehicle tender alerts.

### 6. (Optional) Inspect logs

In the same Actions tab: click any completed run → scroll down to the **"agent-log"** artifact → download to see the full agent.log.

### 7. Profit — every 8 hours, GitHub runs the agent automatically

You just receive Telegram alerts. No maintenance, no VPS to babysit, no cron to install. Truly free, forever.

---

## How the dedupe DB persists across runs

GitHub Actions gives each workflow a fresh VM — the SQLite DB that tracks "what's already been sent" would normally be wiped after each run, causing duplicate alerts.

The workflow solves this with `actions/cache@v4`:
- **Restore step** (before the agent runs): pulls `data/sent_items.db` from cache, if it exists
- **Save step** (after the agent runs): pushes the updated `data/sent_items.db` back to cache
- The cache survives between workflow runs (10 GB free, no expiry if accessed weekly)

Result: if a tender was sent in the morning run, the afternoon run will see it as already-sent and skip it. No duplicate alerts.

---

## Troubleshooting

**Symptom: Workflow shows "skipped" without running**
- Public repo? Yes → should run. Check the Actions tab settings: Settings → Actions → General → "Allow all actions".

**Symptom: Run fails with "User location is not supported" (Gemini error)**
- This shouldn't happen since GitHub's runners are in Google-friendly regions (US/EU). If you see it, switch `GEMINI_MODEL=gemini-3.8-flash` to `gemini-2.5-flash` or vice versa — Google sometimes deprecates model names.

**Symptom: "Cannot connect to Telegram"**
- Verify `TELEGRAM_BOT_TOKEN` secret is correct (no trailing spaces, full token including colon).
- Verify `TELEGRAM_CHAT_ID` secret is `384173598` (just the digits, no quotes).
- Send any new message to @bidupdate2_bot in Telegram — if the bot can't reach you, it's because you haven't started the chat.

**Symptom: 2merkato login fails**
- Verify `SITE_LOGIN_EMAIL=0911513087` and `SITE_LOGIN_PASSWORD=Abay2025` in secrets.
- Open 2merkato.com manually and try logging in with the same creds — if it fails for you too, your password is wrong.

**Symptom: Workflow runs but no Telegram alerts arrive**
- Check the workflow logs (click the run → click the "Run the agent" step). Look for `Sent: ...` lines.
- If you see `Skip (not relevant, score=0)` for everything, the LLM is rejecting all items as not-vehicle-related. Edit `filter_brief` in `config.yaml` to loosen the criteria.

**Symptom: Schedule drifts (runs not at exact 6:00/12:00/18:00 UTC)**
- Normal. GitHub Actions cron can drift by 5–15 min during high load. The actual content of the run is unaffected.

---

## Maintenance

- **Update the agent code**: edit `bid_agent.py` directly in the GitHub web UI (click the file → pencil icon). Commit. Next scheduled run uses the new code.
- **Change schedule**: edit `.github/workflows/bid-agent.yml` → change `cron:` line → commit.
- **Rotate secrets**: repo → Settings → Secrets → Actions → update the value.
- **See history**: Actions tab → see all past runs with their logs.
