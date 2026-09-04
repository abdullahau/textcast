"""Pull newsletters straight out of a mailbox.

Newsletters arrive by email, so the shortest path from "it was sent" to "it is
on my phone" is to read the mailbox. IMAP over inbound SMTP: no public mail
server to run, no DNS, no spam surface — the app only ever connects outwards.

Point this at a dedicated address (or a filtered folder), and every unread
issue becomes an article with its audio queued.
"""

from __future__ import annotations

import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import db
from .service import IngestError, ingest
from .settings import Settings, get_settings

log = logging.getLogger("textcast.mail")

#: Only messages that look like a bulk mailing, so a personal note is skipped.
NEWSLETTER_HEADERS = ("List-Id", "List-Unsubscribe", "List-Post")


@dataclass
class MailConfig:
    host: str
    user: str
    password: str
    folder: str = "INBOX"
    port: int = 993
    #: Mark handled messages as seen so they are not fetched twice.
    mark_seen: bool = True
    #: Ignore anything older than this on the first run.
    days: int = 14

    @classmethod
    def from_env(cls) -> MailConfig | None:
        import os

        host = os.environ.get("TEXTCAST_IMAP_HOST", "")
        user = os.environ.get("TEXTCAST_IMAP_USER", "")
        password = os.environ.get("TEXTCAST_IMAP_PASSWORD", "")
        if not (host and user and password):
            return None
        return cls(
            host=host,
            user=user,
            password=password,
            folder=os.environ.get("TEXTCAST_IMAP_FOLDER", "INBOX"),
            port=int(os.environ.get("TEXTCAST_IMAP_PORT", "993")),
            mark_seen=os.environ.get("TEXTCAST_IMAP_MARK_SEEN", "1") != "0",
            days=int(os.environ.get("TEXTCAST_IMAP_DAYS", "14")),
        )


@dataclass
class Fetched:
    seen: int = 0
    added: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.seen} messages: {self.added} added, {self.duplicates} already stored, "
            f"{self.skipped} not newsletters, {self.failed} failed"
        )


def looks_like_newsletter(headers: bytes) -> bool:
    text = headers.decode("utf-8", errors="replace")
    return any(re.search(rf"^{h}:", text, re.I | re.M) for h in NEWSLETTER_HEADERS)


def _subject(headers: bytes) -> str:
    """Enough of a name to find the message by, for a line in the log.

    A failure that says only "could not ingest a message" leaves you with a
    mailbox and no idea which one.
    """
    text = headers.decode("utf-8", errors="replace")
    found = re.search(r"^Subject:\s*(.+)$", text, re.I | re.M)
    return found.group(1).strip()[:80] if found else "a message with no subject"


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")


def fetch(config: MailConfig | None = None, settings: Settings | None = None, limit: int = 50) -> Fetched:
    """Fetch unread newsletters and turn each into a queued article."""
    settings = settings or get_settings()
    config = config or MailConfig.from_env()
    if config is None:
        raise IngestError(
            "No mailbox configured. Set TEXTCAST_IMAP_HOST, TEXTCAST_IMAP_USER "
            "and TEXTCAST_IMAP_PASSWORD."
        )

    settings.ensure_dirs()
    db.init(settings.db_path)
    result = Fetched()

    with imaplib.IMAP4_SSL(config.host, config.port) as imap:
        imap.login(config.user, config.password)
        imap.select(config.folder)

        status, data = imap.search(None, "UNSEEN", "SINCE", _since(config.days))
        if status != "OK":
            raise IngestError(f"IMAP search failed: {status}")

        ids = data[0].split()[:limit]
        log.info("%d unread message(s) in %s", len(ids), config.folder)

        for message_id in ids:
            result.seen += 1
            # Peek at the headers first: a personal email should stay unread.
            status, head = imap.fetch(message_id, "(BODY.PEEK[HEADER])")
            if status != "OK" or not head or not isinstance(head[0], tuple):
                result.failed += 1
                continue

            if not looks_like_newsletter(head[0][1]):
                result.skipped += 1
                continue

            status, body = imap.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK" or not body or not isinstance(body[0], tuple):
                result.failed += 1
                continue

            raw = body[0][1]
            try:
                outcome = ingest(eml=raw, settings=settings)
            except IngestError as exc:
                # Marked seen even though it failed, and only for this error.
                # An `IngestError` is a verdict on the message itself — it
                # parsed to nothing, or to less than `MIN_WORDS` — so it will
                # fail exactly the same way next time. Left unread, one broken
                # issue was fetched whole on every poll for ever. Anything
                # else that can be raised here is transient, and is left
                # unread on purpose so the next poll tries again.
                log.warning("could not ingest %s: %s", _subject(head[0][1]), exc)
                result.failed += 1
                if config.mark_seen:
                    imap.store(message_id, "+FLAGS", "\\Seen")
                continue

            if outcome.duplicate:
                result.duplicates += 1
            else:
                result.added += 1
                log.info("added %s [%s]", outcome.title, outcome.series or "-")

            if config.mark_seen:
                imap.store(message_id, "+FLAGS", "\\Seen")

    return result
