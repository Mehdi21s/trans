import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Railway Variables required: API_ID, API_HASH, SESSION_STRING
# Optional: SOURCE_CHANNEL (default @Ali_bk_8), TARGET_CHANNEL (default @rasedpersian)
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
SOURCE_CHANNEL = os.environ.get("SOURCE_CHANNEL", "@Ali_bk_8").strip()
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "@rasedpersian").strip()
DB_PATH = os.environ.get("STATE_DB_PATH", "reposter_state.sqlite3")

if not API_ID or not API_HASH or not SESSION_STRING:
    raise RuntimeError("Set API_ID, API_HASH and SESSION_STRING in Railway Variables.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("persian_only_reposter")

# Keep Persian-script lines/sentences and drop English-only material.
PERSIAN_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
LATIN_RE = re.compile(r"[A-Za-z]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟。])\s+|\n+")


def persian_only(text: str | None) -> str:
    if not text:
        return ""
    kept: list[str] = []
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # If the line contains both languages, split into sentence-like chunks.
        chunks = SENTENCE_SPLIT_RE.split(line)
        if len(chunks) > 1:
            for chunk in chunks:
                chunk = chunk.strip()
                if chunk and PERSIAN_RE.search(chunk):
                    kept.append(chunk)
            continue
        if PERSIAN_RE.search(line):
            # If English and Persian occur in one unsplittable line, remove Latin words
            # while preserving Persian text, digits, punctuation and URLs only when they
            # are part of a Persian-containing line. English-only words are discarded.
            if LATIN_RE.search(line):
                pieces = re.findall(r"[^\s]+", line)
                cleaned = []
                for piece in pieces:
                    if PERSIAN_RE.search(piece) or not LATIN_RE.search(piece):
                        cleaned.append(piece)
                line = " ".join(cleaned).strip()
            if line and PERSIAN_RE.search(line):
                kept.append(line)
    # Preserve paragraph breaks without duplicating lines.
    result = "\n".join(kept).strip()
    return result[:4000]


conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("CREATE TABLE IF NOT EXISTS sent_messages (source_id INTEGER PRIMARY KEY, sent_at TEXT DEFAULT CURRENT_TIMESTAMP)")
conn.commit()

def was_sent(message_id: int) -> bool:
    return conn.execute("SELECT 1 FROM sent_messages WHERE source_id = ?", (message_id,)).fetchone() is not None

def mark_sent(message_ids: Iterable[int]) -> None:
    conn.executemany("INSERT OR IGNORE INTO sent_messages(source_id) VALUES (?)", [(int(i),) for i in message_ids])
    conn.commit()

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
source_entity = None
target_entity = None

async def publish(messages):
    """Publish a text post or an album, keeping only Persian text and original media."""
    messages = sorted(messages, key=lambda m: m.id)
    ids = [m.id for m in messages]
    if all(was_sent(mid) for mid in ids):
        return

    # Use the first non-empty caption/text from an album; for ordinary messages this is its text.
    original_text = "\n".join((m.message or "").strip() for m in messages if (m.message or "").strip())
    caption = persian_only(original_text)
    if not caption:
        log.info("Skipping source message(s) %s: no Persian text found", ids)
        # Mark skipped items to avoid processing them repeatedly after reconnects.
        mark_sent(ids)
        return

    media_messages = [m for m in messages if m.media]
    try:
        if media_messages:
            media = [m.media for m in media_messages]
            # Telethon sends multiple media as an album; a single item stays a normal media post.
            await client.send_file(target_entity, media if len(media) > 1 else media[0], caption=caption)
        else:
            await client.send_message(target_entity, caption, link_preview=False)
        mark_sent(ids)
        log.info("Published source message(s) %s to %s (media=%s)", ids, TARGET_CHANNEL, bool(media_messages))
    except Exception:
        log.exception("Failed to publish source message(s) %s; will not mark as sent", ids)

async def main():
    global source_entity, target_entity
    log.info("STARTUP_MARKER=PERSIAN_ONLY_REPOSTER_V1")
    await client.start()
    source_entity = await client.get_entity(SOURCE_CHANNEL)
    target_entity = await client.get_entity(TARGET_CHANNEL)
    log.info("Listening to source %s; publishing Persian-only posts to %s", SOURCE_CHANNEL, TARGET_CHANNEL)
    log.info("Logged in as %s", getattr(await client.get_me(), "username", None) or "Telegram account")

    @client.on(events.NewMessage(chats=source_entity))
    async def on_new_message(event):
        # Album messages are handled once by Album event, not individually.
        if getattr(event.message, "grouped_id", None):
            return
        await publish([event.message])

    @client.on(events.Album(chats=source_entity))
    async def on_album(event):
        await publish(event.messages)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
