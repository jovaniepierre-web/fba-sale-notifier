# 📦 FBA Sale Notifier

Get a **push notification on your phone every time a product sells** from your
Amazon FBA account — for free, running 24/7 in the cloud.

It works by checking your Amazon Selling Partner account for new FBA orders
every ~5 minutes and sending you a Telegram message for each new sale, like:

```
💰 New Amazon sale!
• 2× Widget Pro Max 3-Pack  SKU-WPM-3

Total: 29.99 USD
Channel: FBA
Status: Unshipped
Order: 111-2223334-5556667
Placed: 2026-07-27T14:03:00Z
```

**Why Telegram?** It's a free phone app (iPhone + Android), notifications are
instant, and a "bot" can message you without you building an actual app-store
app. If you'd rather use Discord, Slack, or email, that's an easy swap later.

> ⏱️ **Time to set up:** about 30–45 minutes, most of it waiting on Amazon.
> You do **not** need to know how to code — just follow the steps.

---

## ✅ Before you start

You need:

1. An **Amazon Professional selling account** (the $39.99/mo plan). The free
   *Individual* plan cannot access Amazon's developer API. If you're on the
   Individual plan, this won't work until you upgrade.
2. A **free GitHub account** — sign up at https://github.com (this is where the
   app runs for free, 24/7).
3. A phone with the **free Telegram app** installed.

---

## Part 1 — Set up Telegram (≈5 min)

You'll create a bot and find out where to send messages.

### 1a. Create your bot

1. Install **Telegram** on your phone and create an account.
2. In Telegram, search for **@BotFather** (the official bot maker) and open it.
3. Send the message `/newbot`.
4. It asks for a **name** (anything, e.g. "My FBA Sales") and a **username**
   (must end in `bot`, e.g. `my_fba_sales_bot`).
5. BotFather replies with a **token** that looks like
   `1234567890:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
   **Copy and save it** — this is your `TELEGRAM_BOT_TOKEN`.

### 1b. Get your chat ID

1. In Telegram, search for **your new bot** by its username and open it.
2. Tap **Start** (or send it any message like "hi"). This is required — a bot
   can't message you until you message it first.
3. Now search for **@userinfobot**, open it, and press **Start**.
4. It replies with your numeric **Id** (e.g. `123456789`).
   **Save it** — this is your `TELEGRAM_CHAT_ID`.

✅ You now have your two Telegram values.

---

## Part 2 — Get your Amazon SP-API credentials (≈20 min + approval wait)

This is the fiddly part. You'll register as a developer on your own account and
create a private "app" that can read your orders.

### 2a. Register as a developer

1. Log in to **Amazon Seller Central**.
2. Go to **Settings** (gear, top right) → **Account Info**, or open
   **Apps & Services → Develop Apps** from the top menu.
3. If prompted, **register as a developer**. Amazon asks a few questions about
   what you'll use the API for. For "Are you a public or private developer?"
   choose **Private** (you're only accessing your own account). For data-use
   questions, say you're building an internal tool to monitor your own orders.
4. Submit. **Amazon may take a few hours to a few days to approve** developer
   access. You can continue once it's approved.

### 2b. Create your app and get the client ID + secret

1. Go to **Apps & Services → Develop Apps**.
2. Click **Add new app client**.
3. Give it a name (e.g. "FBA Notifier"), and for **API type** select **SP-API**.
4. For **roles**, tick **Orders** (this lets it read your orders). Save.
   - *Note:* Amazon may require extra approval for order data that contains
     buyer personal info. This tool does **not** need buyer names/addresses —
     it only reads product, price, and status — so basic Orders access is
     enough.
5. Once created, click **View** / **LWA credentials** on your app. You'll see:
   - **Client identifier** → this is your `LWA_CLIENT_ID`
   - **Client secret** → this is your `LWA_CLIENT_SECRET`
   **Save both.**

### 2c. Generate your refresh token (self-authorization)

Because this app only touches your own account, you use "self-authorization":

1. Still on the **Develop Apps** page, find your app and open the
   **⋯ (Edit / Authorize)** menu → **Authorize**.
2. Click **Authorize** / **Generate refresh token**.
3. Amazon shows a **refresh token** starting with `Atzr|...`.
   **Copy and save it** — this is your `SP_API_REFRESH_TOKEN`. It's long-lived
   (it won't expire as long as the app stays authorized).

### 2d. Note your marketplace + region

- **US sellers:** `MARKETPLACE_ID = ATVPDKIKX0DER`, `SP_API_REGION = na` (already the defaults).
- Other marketplaces: see
  https://developer-docs.amazon.com/sp-api/docs/marketplace-ids
  and use region `eu` for Europe or `fe` for Far East/Australia.

✅ You now have all five Amazon + Telegram credentials:
`LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `SP_API_REFRESH_TOKEN`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## Part 3 — (Optional) Test on your own computer first

If you have Python installed, you can confirm everything works before deploying:

```bash
pip install -r requirements.txt
cp .env.example .env
# open .env in a text editor and paste in your real values
python test_connection.py
```

You should get a **test message on your phone** and see three green checkmarks.
If Telegram fails, re-check your token/chat ID. If Amazon fails, re-check your
three Amazon values and that your app is authorized.

*(No Python? Skip this — you can test straight from GitHub in Part 4.)*

---

## Part 4 — Deploy it free, 24/7 on GitHub Actions

GitHub will run the checker every 5 minutes for you, for free.

### 4a. Create the repository

1. Go to https://github.com/new.
2. Name it e.g. `fba-sale-notifier`.
3. **Public** is fine and recommended — it makes GitHub Actions **unlimited and
   free**. ⚠️ Your credentials are **never** stored in the code; they live in
   GitHub *Secrets* (next step), so a public repo does **not** expose them.
   (Private repos also work but only include 2,000 free run-minutes/month.)
4. Create the repository.

### 4b. Upload these files

Easiest way (no git needed):

1. On your new repo page, click **Add file → Upload files**.
2. Drag in the main files: `fba_notifier.py`, `test_connection.py`,
   `requirements.txt`, `.env.example`, and `.gitignore`.
3. The workflow file needs to live at `.github/workflows/check-sales.yml` in the
   repo. On disk it was saved as **`github-workflow--check-sales.yml`** (Windows
   protects the `.github` name from being written directly). To add it: on your
   repo click **Add file → Create new file**, type
   `.github/workflows/check-sales.yml` as the filename, then open
   `github-workflow--check-sales.yml` from this folder in Notepad, copy all of
   it, and paste it in. Commit.
3. Commit the files.
   - Do **not** upload your `.env` file. Only `.env.example` (the blank
     template) should be there.

### 4c. Add your credentials as Secrets

1. In your repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add each of these (name on the left,
   your value on the right):

   | Secret name                | Value                                  |
   |----------------------------|----------------------------------------|
   | `LWA_CLIENT_ID`            | your Amazon client id (shared)          |
   | `LWA_CLIENT_SECRET`        | your Amazon client secret (shared)      |
   | `SP_API_REFRESH_TOKEN_US`  | your **US** `Atzr|...` refresh token    |
   | `SP_API_REFRESH_TOKEN_UK`  | your **UK** `Atzr|...` refresh token    |
   | `TELEGRAM_BOT_TOKEN`       | your Telegram bot token                 |
   | `TELEGRAM_CHAT_ID`         | your Telegram chat id                   |

   The marketplace and region for each store are already set inside the workflow
   file (US = North America, UK = Europe), so they are **not** secrets.
   `FBA_ONLY` and `LOOKBACK_MINUTES` are optional — leave them out to use the
   defaults of FBA-only and 60 minutes.

   > **Running only one store?** If you only want US alerts, just add
   > `SP_API_REFRESH_TOKEN_US` and delete the "Check UK FBA sales" step from the
   > workflow (or leave the UK token out — that store's check will simply error
   > and be skipped). Same idea in reverse for UK-only.

### 4d. Turn it on and test

1. Go to the **Actions** tab. If it says workflows are disabled, click
   **"I understand my workflows, go ahead and enable them."**
2. Click **"Check FBA sales"** in the left sidebar → **Run workflow** →
   **Run workflow** (this runs it immediately instead of waiting).
3. Open the run and watch the log. The **first run records a baseline** of your
   recent orders **without** notifying you (so you don't get flooded by old
   orders). From then on, every new sale triggers a Telegram message.

That's it — you're live. 🎉 Sales will now ping your phone within about 5
minutes of happening.

---

## ❓ FAQ & troubleshooting

**How fast are notifications?** Every 5 minutes on the schedule. GitHub
sometimes delays scheduled runs during busy periods, so 5–15 minutes is
typical. You can lower/raise the frequency by editing the `cron` line in
`.github/workflows/check-sales.yml` (5 minutes is the minimum GitHub allows).

**Is it really free?** Yes, for a public repo (unlimited Actions minutes) and
Telegram (free). Amazon requires the Professional selling plan you already pay
for; the API itself is free.

**I got a "test" order notification but not real ones.** New Amazon orders start
in `Pending` status until payment clears; this tool skips `Pending` and
notifies once the order is confirmed (usually within minutes).

**It stopped after ~2 months.** GitHub pauses scheduled workflows in repos with
no activity for 60 days. Just open the repo and click **Run workflow** once, or
make any small commit, to wake it up.

**I want merchant-fulfilled (FBM) sales too.** Add a secret `FBA_ONLY` with
value `false`.

**I want email / Slack / Discord instead of Telegram.** The notification is sent
by the `send_telegram()` function in `fba_notifier.py`. Swapping in an email or
webhook call there is a small change — ask and it can be added.

**Can it be truly real-time (instant)?** Amazon offers a push-notification
service (via AWS EventBridge/SQS) that fires the instant an order is placed, but
it's significantly more complex to set up. The 5-minute polling here is the
simplest free approach; the real-time version is a possible upgrade later.

---

## 📁 What's in this folder

| File | What it does |
|------|--------------|
| `fba_notifier.py` | The main app — checks for new FBA sales and sends notifications |
| `test_connection.py` | One-time tester to confirm your credentials work |
| `.github/workflows/check-sales.yml` | Tells GitHub to run the check every 5 min for free |
| `.env.example` | Template for your credentials (copy to `.env` for local testing) |
| `requirements.txt` | The one Python library it needs (`requests`) |
| `.gitignore` | Makes sure your real `.env` is never uploaded |
| `seen_orders.json` | Auto-created — remembers which sales you've been told about |
