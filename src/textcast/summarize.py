"""Section summaries from a language model.

The endpoint is OpenAI-compatible, which every provider now speaks, so one
dependency reaches the whole field: point ``base_url`` at any entry in
``PROVIDERS``, name a model, and nothing else changes. litellm was weighed and
refused — 183 MB across 114 packages against 21 MB, and all it adds is the
providers with no OpenAI endpoint at all (Bedrock, Vertex, SageMaker).

The model, the endpoint and the key all live in the ``setting`` table, so they
are edited in the app rather than in a restart. The environment is the default
and a saved value wins, or the settings page would appear to do nothing.

A summary is a *block*, like everything else: a stable id, searched, spoken,
and hideable in the player. There is no second place where text lives.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .document import Article, Block, BlockKind

log = logging.getLogger("textcast.summarize")

#: Gemini's OpenAI-compatible endpoint. The default because it is the provider
#: this project started against, and its flash models are cheap and quick.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-2.5-flash"

#: Endpoints that speak the OpenAI protocol. Anything not listed works too:
#: type its address in the model box.
#:
#: Addresses only. There used to be a hand-written list of each provider's
#: models beside them, offered as a menu. It was wrong within weeks — a list
#: of model names maintained in a file is a promise to keep chasing releases,
#: and the one name you wanted was always behind "Something else". The model
#: is typed.
PROVIDERS = [
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    ("openai", "OpenAI", "https://api.openai.com/v1/"),
    ("anthropic", "Anthropic", "https://api.anthropic.com/v1/"),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1/"),
    ("groq", "Groq", "https://api.groq.com/openai/v1/"),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1/"),
    ("mistral", "Mistral", "https://api.mistral.ai/v1/"),
    ("xai", "xAI Grok", "https://api.x.ai/v1/"),
    ("together", "Together", "https://api.together.xyz/v1/"),
    ("cerebras", "Cerebras", "https://api.cerebras.ai/v1/"),
    ("ollama", "Ollama, on this machine", "http://127.0.0.1:11434/v1/"),
    ("lmstudio", "LM Studio, on this machine", "http://127.0.0.1:1234/v1/"),
]

PROVIDER_URLS = {url: name for _id, name, url in PROVIDERS}
PROVIDER_NAMES = {pid: name for pid, name, _url in PROVIDERS}
PROVIDER_ADDRESSES = {pid: url for pid, _name, url in PROVIDERS}


def provider_for(base_url: str) -> str:
    """Which listed provider an endpoint belongs to, or an empty string."""
    return PROVIDER_URLS.get((base_url or "").strip(), "")


#: Keys in the ``setting`` table. What is *in use*, as against what is stored:
#: the key itself lives in `summary_key`, and this names which one.
KEY_MODEL = "summary_model"
KEY_BASE_URL = "summary_base_url"
KEY_CREDENTIAL = "summary_credential"
KEY_PROMPT = "summary_prompt"

#: Read by the migration that turned endpoint-scoped keys into named ones.
KEY_API_KEY = "summary_api_key"
PREFIX_API_KEY = "summary_api_key:"
PREFIX_MODEL = "summary_model:"


def endpoint_id(base_url: str) -> str:
    """The stable name of an endpoint.

    Lower-cased and stripped of a trailing slash, because
    ``https://api.deepseek.com/v1`` and ``https://api.deepseek.com/v1/`` are
    the same endpoint.
    """
    return (base_url or "").strip().rstrip("/").lower()


#: Hosts that are this machine. A model served here is not behind an account,
#: so a key may be left blank for one and `ready` must not withhold it.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal"}


def is_local(base_url: str) -> bool:
    """Whether this endpoint is served from this machine: Ollama, LM Studio."""
    from urllib.parse import urlsplit

    try:
        return (urlsplit(base_url or "").hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


def fingerprint(key: str) -> str:
    """The tail of a key, which is enough to tell two of them apart.

    Shown on the page so a stored key is identifiable without being readable.
    Short keys show nothing: there is no tail that is not most of the key.
    """
    key = (key or "").strip()
    return key[-4:] if len(key) >= 12 else ""


#: Written for the ear: no markdown, no bullets, no heading, no "this section
#: discusses" preamble. 150 words is about a minute, and it is spoken before
#: the section — past that you are listening to the summary, not the article.
DEFAULT_PROMPT = """You are summarising one section of an article for someone who will hear it read aloud, before they hear the section itself.

Write two or three sentences of plain prose, under 150 words in total. Say what the section is about and why it matters. Keep the author's tone, including any humour. Explain a financial term the first time it appears rather than assuming it.

Do not use markdown, bullet points, headings or quotation marks. Do not begin with "This section". Write only the summary.

Here is the section:
---
{text}
---"""

#: Enough of a section to summarise well, bounded so one long chapter cannot
#: cost a fortune or blow the context window.
MAX_INPUT_CHARS = 24000

#: Sections are independent, so they go out together. The bound is politeness
#: to the provider's rate limit, not a limit of this machine.
MAX_PARALLEL = 4


class SummaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    #: The name of the stored key in use, empty when none is chosen.
    credential: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    prompt: str = DEFAULT_PROMPT

    @property
    def needs_key(self) -> bool:
        """A model on this machine is not behind an account."""
        return not is_local(self.base_url)

    @property
    def ready(self) -> bool:
        return bool(self.model and (self.api_key or not self.needs_key))

    @property
    def provider(self) -> str:
        return provider_for(self.base_url) or endpoint_id(self.base_url)


# --------------------------------------------------------------------------
# stored keys
# --------------------------------------------------------------------------


@dataclass
class Credential:
    """One stored API key, under the name whoever typed it gave it.

    Named, not keyed by endpoint. Two accounts with the same provider are two
    keys and neither is "the Gemini key"; a gateway of your own may hold
    several. The name is what the model picker offers.
    """

    name: str
    provider: str = ""
    #: Only read when `provider` is empty. A listed provider's address comes
    #: from PROVIDERS, so it stays right when that provider moves it.
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    @property
    def endpoint(self) -> str:
        return PROVIDER_ADDRESSES.get(self.provider, "") or self.base_url

    @property
    def provider_name(self) -> str:
        if self.provider:
            return PROVIDER_NAMES.get(self.provider, self.provider)
        from urllib.parse import urlsplit

        try:
            return urlsplit(self.base_url).hostname or "Custom"
        except ValueError:
            return "Custom"

    @property
    def hint(self) -> str:
        return fingerprint(self.api_key)


def _row_to_credential(row) -> Credential:
    return Credential(
        name=row["name"], provider=row["provider"], base_url=row["base_url"],
        api_key=row["api_key"], model=row["model"],
    )


def credentials(conn=None) -> list[Credential]:
    """Every stored key, oldest first, which is the order they were added."""
    from . import db

    conn = conn or db.connect()
    # rowid is the order they were added; `added_at` is only to the second,
    # so two keys typed a moment apart would otherwise sort by name.
    rows = conn.execute("SELECT * FROM summary_key ORDER BY added_at, rowid").fetchall()
    return [_row_to_credential(row) for row in rows]


def credential(name: str, conn=None) -> Credential | None:
    from . import db

    conn = conn or db.connect()
    row = conn.execute("SELECT * FROM summary_key WHERE name = ?", ((name or "").strip(),)).fetchone()
    return _row_to_credential(row) if row else None


def save_credential(
    name: str,
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    conn=None,
) -> str:
    """Store or update one named key. Returns the name it was stored under.

    A blank `api_key` on an update keeps the key already there, because the
    field is a password box and an untouched one posts empty. On a new entry
    it is allowed only for an endpoint on this machine, which needs none.
    """
    from . import db

    conn = conn or db.connect()
    name = (name or "").strip()
    if not name:
        raise SummaryError("a stored key needs a name")

    provider = (provider or "").strip()
    if provider and provider not in PROVIDER_ADDRESSES:
        raise SummaryError(f"no provider called {provider!r}")
    base_url = "" if provider else (base_url or "").strip()
    if not provider and not base_url:
        raise SummaryError("a custom provider needs an endpoint")

    endpoint = PROVIDER_ADDRESSES.get(provider, "") or base_url
    existing = credential(name, conn)
    key = (api_key or "").strip() or (existing.api_key if existing else "")
    if not key and not is_local(endpoint):
        raise SummaryError(f"{name} needs a key: {endpoint} is not on this machine")

    conn.execute(
        """
        INSERT INTO summary_key (name, provider, base_url, api_key, model, added_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT (name) DO UPDATE SET
            provider = excluded.provider,
            base_url = excluded.base_url,
            api_key  = excluded.api_key
        """,
        (name, provider, base_url, key, existing.model if existing else "", db.now()),
    )
    return name


def forget_credential(name: str, conn=None) -> bool:
    """Delete one stored key. The choice of which is in use goes with it."""
    from . import db

    conn = conn or db.connect()
    cursor = conn.execute("DELETE FROM summary_key WHERE name = ?", ((name or "").strip(),))
    if cursor.rowcount and db.get_setting(KEY_CREDENTIAL, "", conn) == name:
        db.set_setting(KEY_CREDENTIAL, "", conn)
    return bool(cursor.rowcount)


# --------------------------------------------------------------------------
# what is in use
# --------------------------------------------------------------------------


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def config(conn=None) -> Config:
    """Which key, endpoint, model and prompt are in use.

    The endpoint comes from the chosen key's provider unless one was typed
    over it, and only a *difference* is stored — so a provider that moves its
    address moves everyone who did not override it.
    """
    from . import db

    def stored(key: str, default: str = "") -> str:
        try:
            return db.get_setting(key, "", conn) or default
        except Exception:
            # A summary is never what breaks a page that has other things to do.
            log.debug("could not read %s", key, exc_info=True)
            return default

    chosen = None
    try:
        chosen = credential(stored(KEY_CREDENTIAL), conn)
    except Exception:
        log.debug("could not read the stored keys", exc_info=True)

    base_url = stored(KEY_BASE_URL) or (chosen.endpoint if chosen else "") or _default_base_url()
    return Config(
        credential=chosen.name if chosen else "",
        model=stored(KEY_MODEL, (chosen.model if chosen else "") or _default_model()),
        base_url=base_url,
        api_key=chosen.api_key if chosen else "",
        prompt=stored(KEY_PROMPT, DEFAULT_PROMPT),
    )


def _default_base_url() -> str:
    """The endpoint before anything is chosen. Read and write must agree."""
    return _env("TEXTCAST_SUMMARY_BASE_URL") or DEFAULT_BASE_URL


def _default_model() -> str:
    return _env("TEXTCAST_SUMMARY_MODEL") or DEFAULT_MODEL


def save_config(
    conn=None,
    *,
    credential_name: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    prompt: str | None = None,
) -> None:
    """Store whichever fields were given. An empty string clears a field.

    The endpoint is stored only where it *differs* from the chosen key's own,
    the same bargain the reading pace makes with 1.0: an override sticks, and
    everything else follows the address in the code.
    """
    from . import db

    if credential_name is not None:
        db.set_setting(KEY_CREDENTIAL, credential_name.strip(), conn)
    if prompt is not None:
        db.set_setting(KEY_PROMPT, prompt.strip(), conn)
    if model is not None:
        db.set_setting(KEY_MODEL, model.strip(), conn)

    name = db.get_setting(KEY_CREDENTIAL, "", conn)
    chosen = credential(name, conn) if name else None

    if base_url is not None:
        typed = base_url.strip()
        canonical = chosen.endpoint if chosen else ""
        db.set_setting(KEY_BASE_URL, "" if typed == canonical else typed, conn)

    # The model this key was last used with, so choosing it again brings it
    # back rather than carrying the previous provider's name into a 404.
    if model is not None and chosen:
        conn = conn or db.connect()
        conn.execute("UPDATE summary_key SET model = ? WHERE name = ?", (model.strip(), chosen.name))


def is_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("openai") is not None


def _client(cfg: Config):
    if not is_installed():
        raise SummaryError("summaries need the openai package: uv sync --extra summaries")
    if not cfg.api_key and cfg.needs_key:
        raise SummaryError(f"no API key is stored for {cfg.base_url}. Add one on the Summaries page")

    from openai import OpenAI

    # The client refuses to be built without one, and a local server ignores
    # whatever it is sent.
    key = cfg.api_key or "not-needed"
    return OpenAI(api_key=key, base_url=cfg.base_url or None, timeout=90.0, max_retries=2)


def summarize_text(text: str, cfg: Config | None = None, client=None) -> str:
    """One section in, two or three spoken sentences out."""
    cfg = cfg or config()
    body = (text or "").strip()[:MAX_INPUT_CHARS]
    if not body:
        return ""

    client = client or _client(cfg)
    prompt = (cfg.prompt or DEFAULT_PROMPT)
    # `replace`, not `format`. The prompt is editable on the Summaries page,
    # and `format` reads every brace in it: a prompt asking for JSON, with
    # `{"summary": "..."}` in it as an example, raised KeyError('"summary"')
    # and every section of every article failed with that as the reason.
    # `{text}` is the only placeholder there has ever been.
    filled = prompt.replace("{text}", body) if "{text}" in prompt else f"{prompt}\n\n{body}"

    try:
        response = client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "user", "content": filled}],
        )
    except Exception as exc:
        raise SummaryError(f"{cfg.model} failed: {exc}") from exc

    choices = getattr(response, "choices", None)
    if not choices:
        raise SummaryError(f"{cfg.model} returned nothing")
    return (choices[0].message.content or "").strip()


@dataclass(frozen=True)
class SectionOutcome:
    """One section's turn at the model, reported the moment it resolves."""

    index: int          #: which section of the article, not which call
    done: int           #: how many of the calls have resolved
    total: int          #: how many calls were sent
    added: int          #: how many summaries have landed so far
    failed: int         #: how many sections have failed so far
    title: str = ""
    error: str = ""


@dataclass(frozen=True)
class SummaryRun:
    """What a whole pass over an article came to."""

    total: int = 0
    added: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.errors)


def summarize_article(
    article: Article,
    cfg: Config | None = None,
    client=None,
    on_section=None,
    replace: bool = False,
) -> SummaryRun:
    """Put a summary block at the head of every section. Returns what happened.

    Written onto the article in place; the caller stores it. A section that
    already has one is left alone, so running this twice costs nothing —
    unless ``replace`` is set, which drops the old summaries first and asks
    the model again for every section.

    **One section's failure costs only that section.** The sections go out
    together and each is caught on its own, because the usual failure is a
    free tier's rate limit, which hits some of the calls and not the rest.
    ``on_section`` is called as each one resolves, on this thread, with the
    block already in the article — so the caller can store what has arrived
    rather than waiting for a pass that may never finish cleanly.

    With ``replace``, a section whose new call fails keeps the summary it
    already had. Losing text to a rate limit would be the worst of both.
    """
    cfg = cfg or config()
    client = client or _client(cfg)

    previous: dict[int, str] = {}
    if replace:
        for index, section in enumerate(article.sections):
            for block in section.blocks:
                if block.kind is BlockKind.SUMMARY:
                    previous[index] = block.text
                    break
            section.blocks = [b for b in section.blocks if b.kind is not BlockKind.SUMMARY]

    sent = [
        (index, section)
        for index, section in enumerate(article.sections)
        if any(b.kind is not BlockKind.SUMMARY for b in section.blocks)
        and not any(b.kind is BlockKind.SUMMARY for b in section.blocks)
    ]
    if not sent:
        return SummaryRun()

    def work(section) -> str:
        text = "\n\n".join(
            b.text for b in section.blocks if b.kind is not BlockKind.HEADING
        )
        return summarize_text(text, cfg, client)

    added = 0
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(sent))) as pool:
        futures = {pool.submit(work, section): (index, section) for index, section in sent}
        for done, future in enumerate(as_completed(futures), start=1):
            index, section = futures[future]
            name = section.title or f"section {index + 1}"
            summary, error = "", ""
            try:
                summary = future.result()
                if not summary:
                    error = f"{cfg.model} returned an empty summary"
            except SummaryError as exc:
                error = str(exc)
            except Exception as exc:
                # A client of the caller's own making may raise anything.
                error = f"{type(exc).__name__}: {exc}"

            if summary:
                section.blocks.insert(0, Block(kind=BlockKind.SUMMARY, text=summary))
                added += 1
            else:
                errors.append(f"{name}: {error[:200]}")
                log.warning("summary failed for %s: %s", name, error)
                if index in previous:
                    section.blocks.insert(0, Block(kind=BlockKind.SUMMARY, text=previous[index]))
            article.renumber()

            if on_section:
                on_section(SectionOutcome(
                    index=index,
                    done=done,
                    total=len(sent),
                    added=added,
                    failed=len(errors),
                    title=name,
                    error=error,
                ))

    return SummaryRun(total=len(sent), added=added, errors=errors)
