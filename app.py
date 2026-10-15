"""Telegram lottery bot starter. Payments and live draws are disabled by default."""
import os
import json
import hmac
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("lottery-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "./data/lottery.sqlite3")
TELEBIRR_ENABLED = os.getenv("TELEBIRR_ENABLED", "false").lower() == "true"
LIVE_DRAWS_ENABLED = os.getenv("LIVE_DRAWS_ENABLED", "false").lower() == "true"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

app = FastAPI(title="Telegram Lottery Bot", version="1.0.0")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def db():
    con = connect_db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS draws (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          ticket_price_minor INTEGER NOT NULL CHECK(ticket_price_minor >= 0),
          currency TEXT NOT NULL DEFAULT 'ETB',
          prize_description TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','completed')),
          created_by INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          closes_at TEXT,
          winner_ticket_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS payments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          provider TEXT NOT NULL,
          provider_order_id TEXT NOT NULL UNIQUE,
          telegram_user_id INTEGER NOT NULL,
          draw_id INTEGER NOT NULL REFERENCES draws(id),
          amount_minor INTEGER NOT NULL,
          currency TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','failed','refunded')),
          provider_transaction_id TEXT UNIQUE,
          verified_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          public_code TEXT NOT NULL UNIQUE,
          draw_id INTEGER NOT NULL REFERENCES draws(id),
          telegram_user_id INTEGER NOT NULL,
          payment_id INTEGER UNIQUE REFERENCES payments(id),
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor_id INTEGER,
          action TEXT NOT NULL,
          details_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(telegram_user_id, draw_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_draw ON tickets(draw_id);
        """)


init_db()


def audit(actor_id: Optional[int], action: str, details: dict):
    with db() as con:
        con.execute("INSERT INTO audit_log(actor_id,action,details_json,created_at) VALUES(?,?,?,?)",
                    (actor_id, action, json.dumps(details, ensure_ascii=False), now_iso()))


async def telegram_api(method: str, payload: dict):
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not configured")
        return {"ok": False, "description": "BOT_TOKEN is not configured"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload)
            data = response.json()
            if response.status_code >= 400 or not data.get("ok"):
                log.error("Telegram API failure method=%s status=%s response=%s", method, response.status_code, data)
            return data
    except Exception:
        log.exception("Telegram API request failed method=%s", method)
        return {"ok": False, "description": "Telegram API request failed"}


async def send_message(chat_id: int, text: str):
    return await telegram_api("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def money(minor: int, currency: str) -> str:
    return f"{minor / 100:,.2f} {currency}"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def list_open_draws():
    with db() as con:
        return con.execute("SELECT * FROM draws WHERE status='open' ORDER BY id DESC LIMIT 20").fetchall()


def draw_summary(draw) -> str:
    with db() as con:
        count = con.execute("SELECT COUNT(*) AS n FROM tickets WHERE draw_id=?", (draw["id"],)).fetchone()["n"]
    return (f"#{draw['id']} — {draw['title']}\nTicket price: {money(draw['ticket_price_minor'], draw['currency'])}\n"
            f"Prize: {draw['prize_description']}\nTickets issued: {count}\nStatus: {draw['status']}")


async def handle_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("chat") or not message.get("from"):
        return
    chat_id = int(message["chat"]["id"])
    user_id = int(message["from"]["id"])
    text = (message.get("text") or "").strip()
    if not text:
        return
    command = text.split()[0].split("@")[0].lower()

    if command in ("/start", "/help"):
        await send_message(chat_id,
            "Welcome to the lottery bot.\n\n/lottery — view open draws\n/tickets — view your tickets\n/help — commands\n\n"
            "Payments are not enabled in this installation. Never send money until the operator announces an approved, verified payment method.")
        return

    if command == "/lottery":
        draws = list_open_draws()
        if not draws:
            await send_message(chat_id, "There are no open draws right now.")
        else:
            await send_message(chat_id, "Open draws:\n\n" + "\n\n".join(draw_summary(d) for d in draws) +
                                "\n\nTicket purchase is currently disabled in this installation.")
        return

    if command == "/tickets":
        with db() as con:
            rows = con.execute("SELECT t.public_code,t.draw_id,d.title,t.created_at FROM tickets t JOIN draws d ON d.id=t.draw_id WHERE t.telegram_user_id=? ORDER BY t.id DESC LIMIT 50", (user_id,)).fetchall()
        if not rows:
            await send_message(chat_id, "You don't have any tickets recorded on this account.")
        else:
            await send_message(chat_id, "Your tickets:\n" + "\n".join(f"{r['public_code']} — draw #{r['draw_id']} {r['title']}" for r in rows))
        return

    if command == "/admin":
        if not is_admin(user_id):
            await send_message(chat_id, "This command is for administrators only.")
            return
        await send_message(chat_id,
            "Admin commands:\n/newdraw TITLE | PRICE_ETB | PRIZE DESCRIPTION\n/draws — list draws\n/closedraw ID — close a draw\n/stats — summary\n\n"
            "Example: /newdraw Demo draw | 50 | Demonstration prize\nPrices are entered in ETB. This starter does not collect payments or announce live winners.")
        return

    if command == "/newdraw":
        if not is_admin(user_id):
            await send_message(chat_id, "This command is for administrators only.")
            return
        parts = text[len("/newdraw"):].strip().split("|")
        if len(parts) != 3 or not parts[0].strip() or not parts[1].strip() or not parts[2].strip():
            await send_message(chat_id, "Usage: /newdraw TITLE | PRICE_ETB | PRIZE DESCRIPTION")
            return
        try:
            price_minor = int(round(float(parts[1].strip()) * 100))
            if price_minor < 0:
                raise ValueError()
        except ValueError:
            await send_message(chat_id, "Price must be a non-negative number, e.g. 50 or 12.50.")
            return
        with db() as con:
            cur = con.execute("INSERT INTO draws(title,ticket_price_minor,currency,prize_description,created_by,created_at) VALUES(?,?,?,?,?,?)",
                              (parts[0].strip()[:120], price_minor, "ETB", parts[2].strip()[:500], user_id, now_iso()))
            draw_id = cur.lastrowid
        audit(user_id, "draw_created", {"draw_id": draw_id, "title": parts[0].strip()})
        await send_message(chat_id, f"Created draw #{draw_id}. It is visible in /lottery, but ticket sales are disabled until payment integration is approved and implemented.")
        return

    if command == "/draws":
        if not is_admin(user_id):
            await send_message(chat_id, "This command is for administrators only.")
            return
        with db() as con:
            rows = con.execute("SELECT * FROM draws ORDER BY id DESC LIMIT 50").fetchall()
        await send_message(chat_id, "No draws yet." if not rows else "Draws:\n\n" + "\n\n".join(draw_summary(r) for r in rows))
        return

    if command == "/closedraw":
        if not is_admin(user_id):
            await send_message(chat_id, "This command is for administrators only.")
            return
        args = text.split(maxsplit=1)
        if len(args) != 2 or not args[1].isdigit():
            await send_message(chat_id, "Usage: /closedraw ID")
            return
        draw_id = int(args[1])
        with db() as con:
            cur = con.execute("UPDATE draws SET status='closed' WHERE id=? AND status='open'", (draw_id,))
        if cur.rowcount:
            audit(user_id, "draw_closed", {"draw_id": draw_id})
            await send_message(chat_id, f"Draw #{draw_id} closed. No winner has been selected.")
        else:
            await send_message(chat_id, "Open draw not found.")
        return

    if command == "/stats":
        if not is_admin(user_id):
            await send_message(chat_id, "This command is for administrators only.")
            return
        with db() as con:
            draws = con.execute("SELECT COUNT(*) n FROM draws").fetchone()["n"]
            tickets = con.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"]
            payments = con.execute("SELECT COUNT(*) n FROM payments WHERE status='paid'").fetchone()["n"]
        await send_message(chat_id, f"Database summary\nDraws: {draws}\nTickets: {tickets}\nVerified paid transactions: {payments}\nPayment collection is disabled by default.")
        return

    await send_message(chat_id, "I didn't recognize that command. Send /help for available commands.")


@app.get("/")
async def root():
    return {"service": "telegram-lottery-bot", "status": "ok", "payments_enabled": TELEBIRR_ENABLED, "live_draws_enabled": LIVE_DRAWS_ENABLED}


@app.get("/health")
async def health():
    with db() as con:
        con.execute("SELECT 1").fetchone()
    return {"ok": True, "database": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(default=None)):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    try:
        await handle_update(update)
    except Exception:
        log.exception("Failed to handle Telegram update update_id=%s", update.get("update_id"))
        # Telegram receives an HTTP response; inspect app logs for command failures.
        return JSONResponse({"ok": False}, status_code=500)
    return {"ok": True}


@app.post("/payments/telebirr/callback")
async def telebirr_callback(request: Request):
    # Deliberately fail closed: do not issue tickets from an unverified callback.
    if not TELEBIRR_ENABLED:
        raise HTTPException(status_code=503, detail="Telebirr payments are disabled")
    # Enabling the env var alone is not sufficient. Add the official provider's
    # signature verification and server-to-server transaction status lookup first.
    raise HTTPException(status_code=501, detail="Provider-specific verification is not implemented")


@app.post("/admin/dev/issue-test-ticket")
async def issue_test_ticket(request: Request, x_admin_key: Optional[str] = Header(default=None)):
    """Local testing only; disabled unless DEV_ADMIN_KEY is explicitly configured."""
    key = os.getenv("DEV_ADMIN_KEY", "")
    if not key or not x_admin_key or not hmac.compare_digest(key, x_admin_key):
        raise HTTPException(status_code=404, detail="Not found")
    if os.getenv("APP_ENV", "production").lower() == "production":
        raise HTTPException(status_code=403, detail="Disabled in production")
    payload = await request.json()
    user_id = int(payload["telegram_user_id"])
    draw_id = int(payload["draw_id"])
    with db() as con:
        draw = con.execute("SELECT * FROM draws WHERE id=? AND status='open'", (draw_id,)).fetchone()
        if not draw:
            raise HTTPException(status_code=404, detail="Open draw not found")
        code = "TEST-" + secrets.token_hex(5).upper()
        con.execute("INSERT INTO tickets(public_code,draw_id,telegram_user_id,created_at) VALUES(?,?,?,?)", (code, draw_id, user_id, now_iso()))
    audit(user_id, "test_ticket_issued", {"draw_id": draw_id, "ticket": code})
    return {"ok": True, "ticket": code, "test_only": True}


@app.on_event("startup")
async def startup_log():
    log.info("Lottery bot started payments_enabled=%s live_draws_enabled=%s db=%s", TELEBIRR_ENABLED, LIVE_DRAWS_ENABLED, DB_PATH)
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is missing")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty; no Telegram user can use admin commands")
