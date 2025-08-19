# Valera — AI Wingman Telegram Bot

Valera is an AI‑powered dating assistant bot that helps users analyze dating profiles
and conversations, suggests the next best reply, and tracks engagement.  It
integrates with OpenAI to provide sophisticated natural‑language analysis and
utilizes Telegram's in‑app currency (Stars) for paid usage.

## Features

* **Subscription gating** — Users must join your public channel before they can
  use the bot.  The bot checks membership and prompts users to subscribe if
  necessary.
* **Free trial & referral program** — Each new user receives an initial
  allocation of free generations.  Sharing a referral link grants both the
  referrer and the new user bonus credits.
* **Conversation analysis** — Users can submit a text conversation (or
  screenshots) and Valera returns a structured JSON summary including
  interest score, green/yellow/red flags, best reply, next steps and a
  fallback suggestion.
* **Profile audit** — Users can submit photos and bio information to receive
  targeted suggestions for improving their dating profile, new bio examples
  and opening lines.
* **Payments via Telegram Stars** — Users can purchase additional credits
  directly inside Telegram using Stars.  The bot presents a list of packages
  and handles payment callbacks.

## Running Locally

1. Create and activate a virtual environment.

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```sh
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your tokens.

4. Run the bot:

   ```sh
   python -m app.main
   ```

## Deploying to Heroku

1. Create a new Heroku app and add the [Heroku Postgres](https://elements.heroku.com/addons/heroku-postgresql) add‑on if you want persistent storage.
2. Set the environment variables specified in `.env.example` under **Settings → Config Vars**.
3. Deploy the contents of this directory to Heroku (e.g. via Git or GitHub).  Heroku
   installs the dependencies listed in `requirements.txt`, runs the bot via the
   `Procfile` and uses the version specified in `runtime.txt`.

For detailed instructions on integrating Telegram Stars payments, see the
[official documentation](https://core.telegram.org/bots/payments#payments-in-telegram-stars).
