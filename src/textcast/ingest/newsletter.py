"""Newsletters — the main source of input for this app.

Handles a raw ``.eml`` message: picks the HTML part, strips the chrome that
every newsletter carries (tracking pixels, unsubscribe footers, "view in
browser" links, social rows), and identifies the *series* an issue belongs to
so the library can group Money Stuff apart from everything else.

Newsletters are laid out in nested tables, so when the normal walk finds too
little text this falls back to reading leaf cells directly.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from ..document import Article, Block, BlockKind, Section
from .base import blocks_from_dom, clean, drop, finish, make_soup, text_of

#: Once a line starts with one of these, the article is over.
CUTOFFS = (
    "if you'd like to get money stuff in handy email form",
    "you received this message because",
    "you are receiving this email because",
    "you're receiving this email because",
    "this email was sent to",
    "was this email forwarded to you",
    "unsubscribe from this",
    "to unsubscribe",
    "manage your subscription",
    "update your preferences",
    "no longer wish to receive",
    "copyright ©",
    "© 20",
    "all rights reserved",
)

#: Individual lines that are chrome wherever they appear.
NOISE = (
    "view this email in your browser",
    "view in browser",
    "view online",
    "read in browser",
    "open in browser",
    "add us to your address book",
    "forward to a friend",
    "share this email",
    "follow us on",
    "sponsored by",
    "advertisement",
    "click here to unsubscribe",
    "having trouble viewing",
    "email preferences",
    "privacy policy",
    "terms of service",
)

_EMAIL_NOISE = [
    "script", "style", "head", "title",
    "img[width='1']", "img[height='1']",
    "[class*=unsubscribe]", "[class*=footer]", "[id*=footer]",
    "[class*=preheader]", "[class*=social]", "[class*=tracking]",
]

_LIST_ID = re.compile(r"<([^>]+)>")
_ISSUE_NOISE = re.compile(r"^\s*(re|fwd):\s*", re.I)


@dataclass
class MailMeta:
    subject: str
    sender_name: str
    sender_addr: str
    list_id: str
    date: str | None

    @property
    def series(self) -> str:
        """A stable name for the publication this issue belongs to.

        Prefers the List-Id header because senders rename themselves; falls
        back to the From display name.
        """
        if self.list_id:
            head = self.list_id.split(".")[0].replace("-", " ").replace("_", " ")
            if len(head) > 2:
                return head.title()
        return self.sender_name or self.sender_addr.split("@")[-1]


def parse_eml(raw: bytes | str) -> tuple[str, MailMeta]:
    """Pull the HTML body and the identifying headers out of a message."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)

    body = msg.get_body(preferencelist=("html",))
    if body is None:
        text_part = msg.get_body(preferencelist=("plain",))
        plain = text_part.get_content() if text_part else ""
        html = "<html><body>" + "".join(f"<p>{line}</p>" for line in plain.splitlines() if line.strip()) + "</body></html>"
    else:
        html = body.get_content()

    name, addr = email.utils.parseaddr(msg.get("From", ""))
    list_id_raw = msg.get("List-Id", "") or msg.get("List-Post", "")
    match = _LIST_ID.search(list_id_raw)

    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"]).isoformat()
        except (TypeError, ValueError):
            date = None

    meta = MailMeta(
        subject=_ISSUE_NOISE.sub("", (msg.get("Subject") or "Untitled").strip()),
        sender_name=name.strip(),
        sender_addr=addr.strip(),
        list_id=(match.group(1) if match else list_id_raw).strip(),
        date=date,
    )
    return html, meta


def is_noise(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in NOISE)


def is_cutoff(text: str) -> bool:
    low = text.lower().lstrip()
    return any(low.startswith(marker) or marker in low[:80] for marker in CUTOFFS)


def strip_chrome(sections: list[Section]) -> list[Section]:
    """Drop chrome blocks and everything after the sign-off."""
    out: list[Section] = []
    for section in sections:
        kept: list[Block] = []
        for block in section.blocks:
            if is_cutoff(block.text):
                section.blocks = kept
                if kept:
                    out.append(section)
                return out
            if is_noise(block.text):
                continue
            # A bare URL or a one-word line is layout debris, not prose.
            if len(block.text) < 3 or block.text.startswith(("http://", "https://")):
                continue
            kept.append(block)
        section.blocks = kept
        if kept:
            out.append(section)
    return out


def _table_fallback(container: BeautifulSoup) -> list[Section]:
    """Read leaf table cells and divs as paragraphs.

    Newsletters often put prose straight into a <td> with no <p> around it,
    which the normal walk cannot see.
    """
    section = Section(title="")
    seen: set[str] = set()
    for node in container.find_all(["td", "div", "span"]):
        # Only leaf-ish nodes, or we would emit every ancestor's text too.
        if node.find(["td", "div", "table", "p"]):
            continue
        text = text_of(node)
        if len(text) < 25 or text in seen:
            continue
        seen.add(text)
        section.blocks.append(Block(kind=BlockKind.PARA, text=text))
    return [section] if section.blocks else []


class NewsletterAdapter:
    """Parses newsletter HTML, whether from an ``.eml`` or a saved web version."""

    name = "newsletter"

    def matches(self, url: str, soup: BeautifulSoup) -> bool:
        if soup.find("table", attrs={"class": re.compile("(body|wrapper|container)", re.I)}):
            return True
        text = soup.get_text(" ", strip=True).lower()[:6000]
        return "unsubscribe" in text and "view this email" in text

    def parse(self, soup: BeautifulSoup, url: str = "", meta: MailMeta | None = None) -> Article:
        drop(soup, _EMAIL_NOISE)
        container = soup.body or soup

        sections = blocks_from_dom(container, heading_tags=("h1", "h2", "h3"))
        if sum(len(s.blocks) for s in sections) < 3:
            sections = _table_fallback(container)
        sections = strip_chrome(sections)

        title = meta.subject if meta else self._title(soup)
        article = Article(
            title=title,
            subtitle="",
            sections=sections,
            source=(meta.sender_name if meta else "") or "Newsletter",
            url=url,
            series=meta.series if meta else None,
            published_at=meta.date if meta else None,
        )
        return finish(article)

    def _title(self, soup: BeautifulSoup) -> str:
        h1 = soup.find("h1")
        if h1:
            return text_of(h1)
        if soup.title and soup.title.string:
            return clean(soup.title.string)
        return "Untitled"


def article_from_eml(raw: bytes | str, url: str = "") -> Article:
    """Full path from a raw message to a ready-to-synthesise article."""
    from . import parse_html  # local import keeps the registry the single source

    html, meta = parse_eml(raw)
    soup = make_soup(html)

    # A publication with a real adapter (Bloomberg's web version arrives by
    # email too) parses better with that adapter; only its metadata comes
    # from the message headers.
    article = parse_html(html, url=url, prefer=None, soup=soup)
    if article.word_count < 120:
        article = NewsletterAdapter().parse(make_soup(html), url=url, meta=meta)

    article.series = article.series or meta.series
    article.published_at = article.published_at or meta.date
    if meta.subject and len(meta.subject) > 3:
        article.title = meta.subject
    if not article.source:
        article.source = meta.sender_name
    return article
