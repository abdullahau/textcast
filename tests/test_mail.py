"""The mailbox poll: what it takes, what it skips, and what it stops retrying.

`mail.py` talks IMAP and had no test, because nothing stood in for a server.
This is that stand-in — enough of `imaplib.IMAP4_SSL` to drive `fetch`, which
is a handful of methods.
"""

from __future__ import annotations

import pytest

from textcast import mail
from textcast.service import IngestError

CONFIG = mail.MailConfig(host="imap.test", user="reader", password="secret")


def message(subject: str, body: str, newsletter: bool = True) -> tuple[bytes, bytes]:
    """A message as the two fetches see it: headers alone, then the whole thing."""
    headers = f"From: A Publication <hello@example.test>\r\nSubject: {subject}\r\n"
    if newsletter:
        headers += "List-Id: <money-stuff.example.test>\r\n"
    whole = headers + "Content-Type: text/plain\r\n\r\n" + body + "\r\n"
    return headers.encode(), whole.encode()


class FakeIMAP:
    """Just enough of imaplib to answer `fetch`. Records what was marked seen."""

    def __init__(self, messages: dict[bytes, tuple[bytes, bytes]]) -> None:
        self.messages = messages
        self.seen: list[bytes] = []
        self.fetched: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return "OK", []

    def select(self, folder):
        return "OK", []

    def search(self, charset, *terms):
        return "OK", [b" ".join(self.messages)]

    def fetch(self, message_id, what):
        headers, whole = self.messages[message_id]
        if "HEADER" in what:
            return "OK", [(b"1", headers)]
        self.fetched.append(message_id)
        return "OK", [(b"1", whole)]

    def store(self, message_id, flags, value):
        if value == "\\Seen":
            self.seen.append(message_id)
        return "OK", []


@pytest.fixture
def imap(monkeypatch):
    """Install a mailbox and hand the test the server standing in for it."""

    def install(messages):
        server = FakeIMAP(messages)
        monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", lambda host, port: server)
        return server

    return install


def test_a_newsletter_becomes_an_article_and_is_marked_seen(imap, monkeypatch, settings):
    server = imap({b"1": message("Money Stuff: Ker-CHING", "A paragraph. " * 40)})
    taken = []
    monkeypatch.setattr(
        mail, "ingest",
        lambda **kwargs: taken.append(kwargs) or _added("Money Stuff: Ker-CHING"),
    )

    result = mail.fetch(CONFIG, settings=settings)

    assert result.added == 1 and result.failed == 0
    assert server.seen == [b"1"], "a message that landed must not be fetched again"


def test_personal_mail_is_left_unread_and_never_fetched_whole(imap, monkeypatch, settings):
    """The headers are peeked at first, so a personal note stays unread."""
    server = imap({b"1": message("lunch?", "are you free", newsletter=False)})
    monkeypatch.setattr(mail, "ingest", lambda **kwargs: _added("nope"))

    result = mail.fetch(CONFIG, settings=settings)

    assert result.skipped == 1 and result.added == 0
    assert server.fetched == [], "a personal message was downloaded in full"
    assert server.seen == [], "and it was marked read"


def test_a_message_that_cannot_be_parsed_is_not_fetched_for_ever(imap, monkeypatch, settings):
    """It was left unread, so every poll downloaded it whole and failed again.

    An `IngestError` is a verdict on the message — it parsed to nothing, or to
    less than MIN_WORDS — so the next attempt fails identically. It is marked
    seen and named in the log instead.
    """
    server = imap({b"1": message("A wall", "Subscribe to read")})

    def refuse(**kwargs):
        raise IngestError("only 3 words extracted")

    monkeypatch.setattr(mail, "ingest", refuse)

    result = mail.fetch(CONFIG, settings=settings)

    assert result.failed == 1 and result.added == 0
    assert server.seen == [b"1"], "it will be downloaded again on every poll"


def test_a_transient_failure_leaves_the_message_unread(imap, monkeypatch, settings):
    """The other half of the bargain: only a verdict on the message marks it.

    A database that was locked, or a disk that was full, says nothing about
    the newsletter, so it must still be there on the next poll.
    """
    server = imap({b"1": message("Money Stuff: Ker-CHING", "A paragraph. " * 40)})

    def blow_up(**kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(mail, "ingest", blow_up)

    with pytest.raises(OSError):
        mail.fetch(CONFIG, settings=settings)

    assert server.seen == [], "a transient failure must not consume the message"


def test_the_subject_is_named_when_a_message_fails(imap):
    headers, _whole = message("Money Stuff: Ker-CHING", "body")

    assert mail._subject(headers) == "Money Stuff: Ker-CHING"
    assert mail._subject(b"From: nobody\r\n") == "a message with no subject"


def _added(title: str):
    from textcast.service import Ingested

    return Ingested(
        article_id=1, slug="x", title=title, word_count=400, series=None, job_id=None
    )
