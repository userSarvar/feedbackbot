"""
Livegram-style Telegram Feedback Bot
-------------------------------------
Builder bot: Users register their own bot token → their bot starts forwarding
messages to them. Owner replies in the builder bot chat → forwarded back.
"""

import os
import logging
import asyncio
import hmac
import hashlib
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, Column, String, Integer, Text, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func
from cryptography.fernet import Fernet

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
BUILDER_TOKEN   = os.environ["BUILDER_BOT_TOKEN"]       # Your main bot token
WEBHOOK_SECRET  = os.environ["WEBHOOK_SECRET"]           # Random secret string
FERNET_KEY      = os.environ["FERNET_KEY"]               # Encrypt stored tokens
PUBLIC_URL      = os.environ["PUBLIC_URL"].rstrip("/")   # e.g. https://xyz.up.railway.app
DATABASE_URL    = os.environ.get("DATABASE_URL", "sqlite:////data/feedbackbot.db")

fernet = Fernet(FERNET_KEY.encode())

# ── Database ──────────────────────────────────────────────────────────────────
Base = declarative_base()

class RegisteredBot(Base):
    __tablename__ = "registered_bots"
    id            = Column(Integer, primary_key=True)
    bot_username  = Column(String, unique=True, index=True)
    token_enc     = Column(Text)          # Fernet-encrypted token
    owner_chat_id = Column(String)        # Owner's Telegram chat_id
    created_at    = Column(DateTime, server_default=func.now())
    active        = Column(Boolean, default=True)

class ConversationThread(Base):
    """Maps visitor chat_id → thread info."""
    __tablename__ = "threads"
    id            = Column(Integer, primary_key=True)
    bot_username  = Column(String, index=True)
    visitor_id    = Column(String)
    visitor_name  = Column(String)
    last_msg_id   = Column(Integer, default=0)

class MessageMap(Base):
    """Maps every forwarded message_id in owner's chat → visitor_id."""
    __tablename__ = "message_map"
    id            = Column(Integer, primary_key=True)
    bot_username  = Column(String, index=True)
    owner_msg_id  = Column(Integer, index=True)  # message_id in owner's chat
    visitor_id    = Column(String)

# SQLite fix for Railway (DATABASE_URL may start with postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine  = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
Session = sessionmaker(bind=engine)

def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()

# ── Telegram API helpers ──────────────────────────────────────────────────────
TG = "https://api.telegram.org/bot"

async def tg(token: str, method: str, **kwargs) -> dict:
    url = f"{TG}{token}/{method}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=kwargs)
    data = r.json()
    if not data.get("ok"):
        log.warning("TG API error %s %s: %s", method, kwargs, data)
    return data

async def send_message(token: str, chat_id, text: str, **kwargs) -> dict:
    return await tg(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", **kwargs)

async def forward_message(token: str, to_chat: str, from_chat, message_id: int) -> dict:
    return await tg(token, "forwardMessage", chat_id=to_chat, from_chat_id=from_chat, message_id=message_id)

async def copy_message(token: str, to_chat, from_chat, message_id: int, **kwargs) -> dict:
    return await tg(token, "copyMessage", chat_id=to_chat, from_chat_id=from_chat, message_id=message_id, **kwargs)

async def get_me(token: str) -> dict:
    return await tg(token, "getMe")

async def set_webhook(token: str, url: str, secret: str) -> dict:
    return await tg(token, "setWebhook", url=url, secret_token=secret, drop_pending_updates=True)

async def delete_webhook(token: str) -> dict:
    return await tg(token, "deleteWebhook", drop_pending_updates=True)

# ── Token encryption ──────────────────────────────────────────────────────────
def encrypt_token(token: str) -> str:
    return fernet.encrypt(token.encode()).decode()

def decrypt_token(token_enc: str) -> str:
    return fernet.decrypt(token_enc.encode()).decode()

# ── Webhook signature verification ───────────────────────────────────────────
def verify_secret(request_secret: str | None) -> bool:
    if not request_secret:
        return False
    expected = WEBHOOK_SECRET.encode()
    got      = request_secret.encode()
    return hmac.compare_digest(expected, got)

# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    log.info("DB tables created/verified")

    # Register builder bot webhook
    webhook_url = f"{PUBLIC_URL}/webhook/builder"
    result = await set_webhook(BUILDER_TOKEN, webhook_url, WEBHOOK_SECRET)
    log.info("Builder bot webhook set: %s", result)

    # Re-register all active child bots on startup
    db = Session()
    try:
        bots = db.query(RegisteredBot).filter_by(active=True).all()
        for bot in bots:
            token = decrypt_token(bot.token_enc)
            url   = f"{PUBLIC_URL}/webhook/{bot.bot_username}"
            await set_webhook(token, url, WEBHOOK_SECRET)
            log.info("Re-registered webhook for @%s", bot.bot_username)
    finally:
        db.close()

    yield
    log.info("Shutting down")

app = FastAPI(lifespan=lifespan)

# ── Builder bot logic ─────────────────────────────────────────────────────────
async def handle_builder_update(update: dict):
    """Handles messages sent to the main builder bot."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()

    # /start
    if text.startswith("/start"):
        await send_message(
            BUILDER_TOKEN, chat_id,
            "👋 <b>Welcome to FeedbackBot Builder</b>\n\n"
            "Send me your bot token (from @BotFather) and I'll turn it into "
            "a feedback bot that forwards all messages to you.\n\n"
            "📌 <b>How to get a token:</b>\n"
            "1. Open @BotFather\n"
            "2. Send /newbot\n"
            "3. Follow the steps\n"
            "4. Copy the token and paste it here"
        )
        return

    # /mybots
    if text.startswith("/mybots"):
        db = Session()
        try:
            bots = db.query(RegisteredBot).filter_by(owner_chat_id=chat_id, active=True).all()
            if not bots:
                await send_message(BUILDER_TOKEN, chat_id, "You have no registered bots yet.")
            else:
                lines = "\n".join(f"• @{b.bot_username}" for b in bots)
                await send_message(BUILDER_TOKEN, chat_id, f"<b>Your bots:</b>\n{lines}")
        finally:
            db.close()
        return

    # /remove @username
    if text.startswith("/remove"):
        parts = text.split()
        if len(parts) < 2:
            await send_message(BUILDER_TOKEN, chat_id, "Usage: /remove @yourbotusername")
            return
        username = parts[1].lstrip("@").lower()
        db = Session()
        try:
            bot = db.query(RegisteredBot).filter_by(bot_username=username, owner_chat_id=chat_id, active=True).first()
            if not bot:
                await send_message(BUILDER_TOKEN, chat_id, f"Bot @{username} not found or not yours.")
            else:
                token = decrypt_token(bot.token_enc)
                await delete_webhook(token)
                bot.active = False
                db.commit()
                await send_message(BUILDER_TOKEN, chat_id, f"✅ @{username} has been removed.")
        finally:
            db.close()
        return

    # /help
    if text.startswith("/help"):
        await send_message(
            BUILDER_TOKEN, chat_id,
            "<b>Commands:</b>\n"
            "/start — Welcome message\n"
            "/mybots — List your registered bots\n"
            "/remove @username — Remove a bot\n"
            "/help — This message\n\n"
            "To register a bot, just paste its BotFather token here."
        )
        return

    # Token registration — looks like a bot token (digits:alphanum)
    if ":" in text and len(text) > 30 and not text.startswith("/"):
        token = text.strip()
        await send_message(BUILDER_TOKEN, chat_id, "⏳ Validating your token...")

        me = await get_me(token)
        if not me.get("ok"):
            await send_message(
                BUILDER_TOKEN, chat_id,
                "❌ Invalid token. Make sure you copied it correctly from @BotFather."
            )
            return

        bot_info     = me["result"]
        bot_username = bot_info["username"].lower()

        db = Session()
        try:
            existing = db.query(RegisteredBot).filter_by(bot_username=bot_username).first()
            if existing and existing.active:
                if existing.owner_chat_id != chat_id:
                    await send_message(BUILDER_TOKEN, chat_id, "❌ This bot is already registered by someone else.")
                else:
                    await send_message(BUILDER_TOKEN, chat_id, f"ℹ️ @{bot_username} is already registered to you.")
                return

            webhook_url = f"{PUBLIC_URL}/webhook/{bot_username}"
            wh_result   = await set_webhook(token, webhook_url, WEBHOOK_SECRET)
            if not wh_result.get("ok"):
                await send_message(BUILDER_TOKEN, chat_id, "❌ Could not set webhook. Is the token valid?")
                return

            if existing:
                existing.token_enc     = encrypt_token(token)
                existing.owner_chat_id = chat_id
                existing.active        = True
            else:
                db.add(RegisteredBot(
                    bot_username  = bot_username,
                    token_enc     = encrypt_token(token),
                    owner_chat_id = chat_id,
                ))
            db.commit()

        finally:
            db.close()

        await send_message(
            BUILDER_TOKEN, chat_id,
            f"✅ <b>@{bot_username} is now live!</b>\n\n"
            f"Anyone who messages @{bot_username} will be forwarded here.\n"
            f"Reply to any forwarded message to respond to that person.\n\n"
            f"Use /mybots to manage your bots."
        )
        return

    # Anything else
    await send_message(
        BUILDER_TOKEN, chat_id,
        "Send me a BotFather token to register a bot, or use /help."
    )

# ── Child bot logic ───────────────────────────────────────────────────────────
async def handle_child_update(bot_username: str, update: dict):
    """Handles messages sent to a registered child bot."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    visitor_id   = str(msg["chat"]["id"])
    visitor_name = msg["chat"].get("first_name", "")
    if last := msg["chat"].get("last_name"):
        visitor_name += f" {last}"
    visitor_username = msg["chat"].get("username", "")
    message_id       = msg["message_id"]

    db = Session()
    try:
        bot_rec = db.query(RegisteredBot).filter_by(bot_username=bot_username, active=True).first()
        if not bot_rec:
            log.warning("Received update for unknown/inactive bot @%s", bot_username)
            return

        token        = decrypt_token(bot_rec.token_enc)
        owner_id     = bot_rec.owner_chat_id

        # Handle /start from visitor
        text = msg.get("text", "")
        if text.strip() == "/start":
            await send_message(
                token, visitor_id,
                f"👋 Hi {visitor_name}! Send me a message and the owner will get back to you."
            )
            return

        # Get or create thread record
        thread = db.query(ConversationThread).filter_by(
            bot_username=bot_username, visitor_id=visitor_id
        ).first()

        if not thread:
            thread = ConversationThread(
                bot_username=bot_username,
                visitor_id=visitor_id,
                visitor_name=visitor_name,
            )
            db.add(thread)
            db.flush()

        # Forward visitor's message to owner — clean "Forwarded from" style, no header
        forwarded = await forward_message(token, owner_id, visitor_id, message_id)

        if forwarded.get("ok"):
            fwd_msg_id = forwarded["result"]["message_id"]
            thread.last_msg_id = fwd_msg_id
            # Store every forwarded message so owner can reply to any of them
            db.add(MessageMap(
                bot_username=bot_username,
                owner_msg_id=fwd_msg_id,
                visitor_id=visitor_id,
            ))

        db.commit()

    finally:
        db.close()

async def handle_owner_reply(bot_username: str, update: dict):
    """Owner replied in the child bot chat — find visitor and forward reply."""
    msg = update.get("message")
    if not msg:
        return

    # Owner must reply-to a message for routing
    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return

    owner_id   = str(msg["chat"]["id"])
    message_id = msg["message_id"]

    db = Session()
    try:
        bot_rec = db.query(RegisteredBot).filter_by(bot_username=bot_username, active=True, owner_chat_id=owner_id).first()
        if not bot_rec:
            return

        token = decrypt_token(bot_rec.token_enc)

        # Look up visitor from MessageMap — works for ANY forwarded message, not just the last one
        replied_msg_id = reply_to["message_id"]
        msg_map = db.query(MessageMap).filter_by(
            bot_username=bot_username,
            owner_msg_id=replied_msg_id,
        ).first()

        if not msg_map:
            await send_message(token, owner_id, "⚠️ Could not find the visitor for this message. Make sure you reply directly to a forwarded message.")
            return

        visitor_id = msg_map.visitor_id

        # Copy owner reply to visitor (preserves media)
        result = await copy_message(token, visitor_id, owner_id, message_id)
        if result.get("ok"):
            await tg(token, "setMessageReaction", chat_id=owner_id, message_id=message_id, reaction=[{"type": "emoji", "emoji": "✅"}])
        else:
            await send_message(token, owner_id, "⚠️ Failed to send reply to visitor.")

    finally:
        db.close()

# ── Webhook endpoints ─────────────────────────────────────────────────────────
@app.post("/webhook/builder")
async def builder_webhook(request: Request):
    if not verify_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        raise HTTPException(status_code=403, detail="Forbidden")
    update = await request.json()
    asyncio.create_task(handle_builder_update(update))
    return Response(status_code=200)

@app.post("/webhook/{bot_username}")
async def child_webhook(bot_username: str, request: Request):
    if not verify_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()
    msg    = update.get("message") or update.get("edited_message")
    if not msg:
        return Response(status_code=200)

    db = Session()
    try:
        bot_rec = db.query(RegisteredBot).filter_by(bot_username=bot_username, active=True).first()
        if not bot_rec:
            return Response(status_code=200)
        owner_id   = bot_rec.owner_chat_id
        sender_id  = str(msg["chat"]["id"])
    finally:
        db.close()

    # Route: owner messaging their own bot = reply to visitor
    if sender_id == owner_id and msg.get("reply_to_message"):
        asyncio.create_task(handle_owner_reply(bot_username, update))
    else:
        asyncio.create_task(handle_child_update(bot_username, update))

    return Response(status_code=200)

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/")
async def root():
    return JSONResponse({"service": "FeedbackBot", "status": "running"})