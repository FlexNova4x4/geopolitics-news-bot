# Geopolitics News Bot (English)

A small Python bot that reads **The Diplomat plus Indian Express RSS feeds** and optionally **one YouTube channel RSS feed**, filters duplicates, prepares concise English posts with Gemini and falls back to Groq, then sends individual posts to one Telegram group.

## What it does

- Checks configured sources every 30 minutes.
- Processes every newly eligible story exposed by the configured RSS feeds; there is no fixed cap of 6 or 10 posts per cycle. AI calls are split into internal batches of 4 stories (configurable) for token control.
- Telegram group sends are spaced 3.2 seconds apart and explicit HTTP 429 responses are retried after Telegram's `retry_after` wait. Network/5xx outcomes are not retried automatically because delivery may be ambiguous.
- Reads The Diplomat's all-site RSS plus Indian Express World, Explained Global, and India RSS feeds. The RSS publisher decides which recent entries are available; no feed guarantees a fixed daily story count.
- Keeps only stories published on the current calendar date in Asia/Kolkata; undated entries and previous-day stories are skipped. Oldest eligible stories are processed first, all separate Telegram posts for eligible stories, without a fixed per-cycle post cap, to drain today's backlog without flooding AI requests.
- Includes a publication timestamp, geographic region, topic labels/hashtags, source link, and India-relevance analysis in each post.
- Reads website RSS and only fetches article pages when the site's `robots.txt` permits it.
- Reads YouTube channel RSS metadata (video title, description, publication time and link). It does **not** download videos or scrape transcripts; the AI is instructed not to infer video contents from a title alone.
- Sends every new eligible story exposed by the configured feeds; there is no fixed 6-post cap per 30-minute cycle. AI calls use small internal batches (default 4 stories/request) to manage token usage. Telegram messages are spaced 3.2 seconds apart and explicit 429 FloodWait responses are retried after Telegram's retry_after delay.
- Makes one AI request per batch when possible, rather than one request for every story.
- Tries Gemini first (`gemini-3.5-flash-lite`), then Groq (`qwen/qwen3.8-27b`) if the Gemini call fails. Each provider still has its own quota and model availability limits.
- Keeps a durable deduplication state in the `bot-state` GitHub branch when running in Actions; local runs use `data/state.local.json`.
- Records source keys **before** posting. This prioritizes avoiding duplicate posts after a crash. If a job crashes after a story is reserved but before Telegram sends it, that story may be skipped rather than resent.
- Uses public news sources only. There is no scraping of X or other Telegram channels.

## The 5h50m GitHub Actions runtime pattern

GitHub-hosted jobs have a 6-hour maximum execution time. This workflow runs the bot for 350 minutes (5h50m), then exits, and starts scheduled sessions every 6 hours at `:13` UTC. That leaves roughly 10 minutes between the expected service windows. GitHub schedule events can be delayed or occasionally missed, so this is **not** a guaranteed 24/7 server. Public repositories using standard GitHub-hosted runners have free Actions usage under GitHub's current policy.

The bot state is committed to the separate `bot-state` branch through the workflow's limited `GITHUB_TOKEN`, keeping routine state writes off `main`. The state contains public article links/titles/statuses only—never credentials.

## Deploy on GitHub (public repository)

1. In Termux, after extracting the ZIP and entering this directory, install the GitHub CLI, sign in, and create/push the new public repository:

   ```bash
   pkg install git gh -y
   gh auth login
   GH_USER=$(gh api user --jq .login)
   git config user.name "$GH_USER"
   git config user.email "${GH_USER}@users.noreply.github.com"
   git init -b main
   git add .
   git commit -m "Initial public Geopolitics News Bot"
   gh repo create geopolitics-news-bot --public --source=. --remote=origin --push
   ```

   If the repository name is already taken in your account, choose another name in the `gh repo create` command. Do not reuse a website repository. Keep `.env` out of GitHub; `.env.example` is safe.
2. Open the new repository on GitHub. Under **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID` (your target group's numeric ID, typically negative; add the bot to that group and grant posting permission)
   - `GEMINI_API_KEY`
   - `GROQ_API_KEY`
3. Open **Settings → Secrets and variables → Actions → Variables** and optionally add:
   - `WEBSITE_RSS_URLS` — defaults to The Diplomat RSS: `https://thediplomat.com/feed/,https://indianexpress.com/section/world/feed/,https://indianexpress.com/section/explained/explained-global/feed/,https://indianexpress.com/section/india/feed/`
   - `YOUTUBE_CHANNEL_ID` — channel ID normally beginning with `UC`; leave blank until you choose the one channel
   - `GEMINI_MODEL` — defaults to `gemini-3.5-flash-lite`
   - `GROQ_MODEL` — defaults to `qwen/qwen3.8-27b`
4. In **Settings → Actions → General → Workflow permissions**, allow read and write permissions if repository policy requires it. The workflow declares `contents: write` to maintain `bot-state`.
5. Go to **Actions → Geopolitics News Bot → Run workflow** for a manual test. The full workflow run then stays active for up to 5h50m. Check its logs and your Telegram group.
6. After verifying the test post and source URLs, leave the workflow enabled. If you want to prevent test posts, do not run it until `TELEGRAM_CHAT_ID` is set to the correct target group—the workflow will fail validation without it.

## Add secrets safely

**Never place bot tokens or API keys in source code, README, `.env.example`, or commit messages.** If a key is accidentally committed, revoke/rotate it immediately; deleting the file in a later commit does not make the old key secret again.

## Run locally in Termux

```bash
pkg update -y
pkg install python git unzip -y
cd geopolitics-news-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py --once
```

For a local run, the state is stored in `data/state.local.json`; in GitHub Actions, the workflow supplies `GITHUB_REPOSITORY` and `GITHUB_TOKEN` and uses the `bot-state` branch. Use `python main.py --once` to test just one collection cycle.

## Reliability notes

- AI quota errors switch to the backup provider, but no provider has unlimited free access.
- Invalid API keys, missing group permissions, blocked feeds, website downtime and YouTube feed changes still require attention in the Actions logs.
- AI output is not independent fact verification. Posts include the source link and should not be treated as confirmed when the source itself reports an unverified claim.
- Duplicate prevention is strongest when the state branch is writable. If GitHub state cannot be read/written, the bot refuses to post rather than risk repeating content.
- Cross-platform delivery cannot be made perfectly atomic: if Telegram accepts a message and the runner crashes before updating its state, the pre-send reservation is still present and prevents a repost, although it may make delivery uncertain.

## License

MIT. Source website, YouTube and Telegram use must comply with each service's terms and access rules.
