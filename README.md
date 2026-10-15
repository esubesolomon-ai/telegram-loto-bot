# Telegram Lottery Bot — starter repository

A FastAPI + SQLite Telegram bot starter with webhook handling, `/lottery`, `/tickets`, basic administrator commands, audit logging, health checks, and a fail-closed Telebirr callback placeholder.

## Important limits

- **This repository does not collect real payments.** The Telebirr callback returns `503` by default and `501` even if you set `TELEBIRR_ENABLED=true`, until the provider's official signature validation and server-to-server transaction verification are implemented.
- **This repository does not select or pay live cash-prize winners.** `LIVE_DRAWS_ENABLED` is a guard/configuration flag for future work, not a finished draw system.
- Check local law and obtain any required lottery/gambling, consumer-protection, tax, and payment-provider approvals before selling tickets or advertising cash prizes.
- Do not deploy a second webhook for the same Telegram bot token. If a bot is already deployed, integrate this handler into the existing application and preserve the existing webhook route/commands.
- SQLite is suitable for a small starter, but a production paid-entry system should have independent security review, reliable backups, stronger access controls, payment reconciliation, refunds/disputes handling, and auditable draw procedures.

## Commands

Public:
- `/start` or `/help`
- `/lottery` — list open draws
- `/tickets` — list tickets recorded for your Telegram user ID

Admin (Telegram numeric user ID must be in `ADMIN_IDS`):
- `/admin` — command help
- `/newdraw TITLE | PRICE_ETB | PRIZE DESCRIPTION`
- `/draws` — list draws
- `/closedraw ID` — close an open draw
- `/stats` — counts

Example:

```text
/newdraw Demo draw | 50 | Demonstration prize
```

The bot can create and list draw records, but it intentionally does not offer a real purchase command while payment verification is unfinished.

## Run locally

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, fill in `BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and your numeric `ADMIN_IDS`. Export those variables in your shell or use a local environment loader of your choice. Do not commit `.env`.

```bash
uvicorn app:app --reload --port 8000
```

Check `http://127.0.0.1:8000/health`. Telegram requires a public HTTPS endpoint to deliver webhooks.

## Deploy on Render

1. Push this repository to a private GitHub repository.
2. In Render, create a new Web Service from the repository. The included `render.yaml` is a starting point.
3. Add environment variables `BOT_TOKEN` and `ADMIN_IDS`. Keep `TELEBIRR_ENABLED=false` and `LIVE_DRAWS_ENABLED=false`.
4. Ensure the persistent disk is mounted at `/var/data` and `DB_PATH=/var/data/lottery.sqlite3`.
5. Deploy and verify `/health` returns `{"ok":true,"database":"ok"}`.
6. Configure Telegram's webhook once using your deployed HTTPS URL and the same secret value as `TELEGRAM_WEBHOOK_SECRET`.

Example webhook registration (run locally in a trusted shell; replace values and do not share the token):

```bash
curl -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=https://YOUR-SERVICE.onrender.com/telegram/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

Check Telegram's response; it should contain `"ok":true`. To inspect webhook delivery details:

```bash
curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

Never paste a real bot token into chat, source control, or public logs. If it has been exposed, revoke it with BotFather and update the deployment secret.

## Debugging a bot that returns HTTP 200 but does not reply

HTTP 200 from the webhook only means your server acknowledged the update. The bot's call to Telegram's `sendMessage` can still fail. This app logs Telegram API errors, including status and response body. In Render logs, inspect:

- whether the webhook endpoint receives the update;
- `Telegram API failure method=sendMessage` messages;
- whether the second account actually sent `/start` to the bot (a bot cannot start a private conversation first);
- whether `BOT_TOKEN` belongs to the bot whose webhook you configured;
- `getWebhookInfo` for `last_error_message` and pending updates.

Do not register multiple webhook URLs for the same bot token. Telegram allows one active webhook per bot.

## Database notes

The database stores Telegram user IDs, draw records, payment records, tickets, and an audit log. Protect the disk and backups. Add a privacy notice and data-retention process before real use.

## Next steps before real payments

1. Confirm local legal eligibility and provider approval.
2. Obtain the official Telebirr merchant/API documentation and credentials.
3. Implement the exact documented callback signature check and a server-to-server transaction status lookup.
4. Verify order ID, merchant account, amount, currency, final transaction status, and replay/idempotency handling before issuing one ticket.
5. Add refunds, reconciliation, support procedures, rate limiting, monitoring, backup/restore tests, and an independent security review.
6. Implement an auditable draw protocol and publish clear rules, odds, eligibility, closing time, prize delivery, and dispute procedures.
