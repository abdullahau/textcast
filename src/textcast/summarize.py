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

#: Endpoints that speak the OpenAI protocol, with a model worth starting on.
#: Anything not listed works too, as long as it speaks the same protocol.
PROVIDERS = [
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
     ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]),
    ("openai", "OpenAI", "https://api.openai.com/v1/",
     ["gpt-4.1-mini", "gpt-4.1", "o4-mini"]),
    ("anthropic", "Anthropic", "https://api.anthropic.com/v1/",
     ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5"]),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1/",
     ["google/gemini-2.5-flash", "anthropic/claude-sonnet-4.5", "deepseek/deepseek-chat"]),
    ("groq", "Groq", "https://api.groq.com/openai/v1/",
     ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]),
    ("deepseek", "DeepSeek", "https://api.deepseek.com/v1/",
     ["deepseek-chat", "deepseek-reasoner"]),
    ("mistral", "Mistral", "https://api.mistral.ai/v1/",
     ["mistral-small-latest", "mistral-large-latest"]),
    ("xai", "xAI Grok", "https://api.x.ai/v1/",
     ["grok-4-fast", "grok-4"]),
    ("together", "Together", "https://api.together.xyz/v1/",
     ["meta-llama/Llama-3.3-70B-Instruct-Turbo"]),
    ("cerebras", "Cerebras", "https://api.cerebras.ai/v1/",
     ["llama-3.3-70b", "qwen-3-235b-a22b-instruct"]),
    ("ollama", "Ollama, on this machine", "http://127.0.0.1:11434/v1/",
     ["llama3.2", "qwen3", "gemma3"]),
    ("lmstudio", "LM Studio, on this machine", "http://127.0.0.1:1234/v1/",
     ["local-model"]),
]

#: Model names move faster than this file. The list above is a starting point,
#: not a menu: the model field takes anything the endpoint accepts.
PROVIDER_URLS = {url: name for _id, name, url, _models in PROVIDERS}


def provider_for(base_url: str) -> str:
    """Which listed provider an endpoint belongs to, or an empty string."""
    return PROVIDER_URLS.get((base_url or "").strip(), "")


#: Keys in the ``setting`` table.
KEY_MODEL = "summary_model"
KEY_BASE_URL = "summary_base_url"
KEY_API_KEY = "summary_api_key"
KEY_PROMPT = "summary_prompt"

#: A key and a model belong to an *endpoint*, not to the app. One flat
#: ``summary_api_key`` sent the old provider's key to the new one on every
#: switch — a 401 that reads like a bad model name. These prefixes scope both,
#: so picking a provider brings its own key and its own model.
PREFIX_API_KEY = "summary_api_key:"
PREFIX_MODEL = "summary_model:"


def endpoint_id(base_url: str) -> str:
    """The stable name of an endpoint, for use in a setting key.

    Lower-cased and stripped of a trailing slash, because
    ``https://api.deepseek.com/v1`` and ``https://api.deepseek.com/v1/`` are
    the same endpoint and must not hold two different keys.
    """
    return (base_url or "").strip().rstrip("/").lower()


def _scoped(prefix: str, base_url: str) -> str:
    return prefix + endpoint_id(base_url)


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
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    prompt: str = DEFAULT_PROMPT
    #: Where ``api_key`` came from: "stored", "environment", or "" for nothing.
    #: The page says which, because a key inherited from the environment is
    #: the one case where the endpoint on screen did not supply it.
    key_source: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model)

    @property
    def provider(self) -> str:
        return provider_for(self.base_url)


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def _default_base_url() -> str:
    """The endpoint when none is stored. Read and write must agree on this."""
    return _env("TEXTCAST_SUMMARY_BASE_URL") or DEFAULT_BASE_URL


def config(conn=None) -> Config:
    """What is stored, falling back to the environment for anything unset."""
    from . import db

    def stored(key: str, default: str) -> str:
        try:
            return db.get_setting(key, "", conn) or default
        except Exception:
            # A summary is never what breaks a page that has other things to do.
            log.debug("could not read %s", key, exc_info=True)
            return default

    base_url = stored(KEY_BASE_URL, _default_base_url())

    # The key is looked up under the endpoint, never under a flat name. There
    # is deliberately no fall back to another endpoint's key: sending Gemini's
    # key to DeepSeek is the bug this scoping exists to stop.
    key = stored(_scoped(PREFIX_API_KEY, base_url), "")
    source = "stored" if key else ""
    if not key:
        key = _env("TEXTCAST_SUMMARY_API_KEY")
        source = "environment" if key else ""

    return Config(
        model=stored(KEY_MODEL, _env("TEXTCAST_SUMMARY_MODEL") or DEFAULT_MODEL),
        base_url=base_url,
        api_key=key,
        prompt=stored(KEY_PROMPT, DEFAULT_PROMPT),
        key_source=source,
    )


def key_for(base_url: str, conn=None) -> str:
    """The stored key for one endpoint, ignoring the environment."""
    from . import db

    return db.get_setting(_scoped(PREFIX_API_KEY, base_url), "", conn)


def model_for(base_url: str, conn=None) -> str:
    """The model last saved against one endpoint, or an empty string."""
    from . import db

    return db.get_setting(_scoped(PREFIX_MODEL, base_url), "", conn)


def endpoints(conn=None) -> dict[str, dict]:
    """What is stored per endpoint, for the settings page. Keys never leave.

    Maps ``endpoint_id`` to the last four characters of its key and the model
    last saved with it — enough for the page to say "DeepSeek: stored" as you
    change the dropdown, and never enough to read a key back out of the page.
    """
    from . import db

    known: dict[str, dict] = {}
    for key, value in db.settings_matching(PREFIX_API_KEY, conn).items():
        if value:
            known.setdefault(key[len(PREFIX_API_KEY):], {})["hint"] = fingerprint(value)
    for key, value in db.settings_matching(PREFIX_MODEL, conn).items():
        if value:
            known.setdefault(key[len(PREFIX_MODEL):], {})["model"] = value
    return known


def stored_list(conn=None) -> list[dict]:
    """Every endpoint holding a key, named and ordered for the page.

    Listed providers come first, in the order of ``PROVIDERS``, then anything
    typed by hand. An endpoint that is only remembering a model is not here:
    the list answers "which providers can I use right now", and that is the
    key.
    """
    known = endpoints(conn)
    urls = {endpoint_id(url): (name, url) for _id, name, url, _models in PROVIDERS}

    rows: list[dict] = []
    for ident, (name, url) in urls.items():
        entry = known.get(ident)
        if entry and entry.get("hint") is not None:
            rows.append({"id": ident, "name": name, "base_url": url, **entry})
    for ident, entry in known.items():
        if ident not in urls and entry.get("hint") is not None:
            rows.append({"id": ident, "name": ident, "base_url": ident, **entry})
    return rows


def forget_key(base_url: str, conn=None) -> bool:
    """Drop one endpoint's key. Its model is left: it is not a secret."""
    from . import db

    return db.delete_setting(_scoped(PREFIX_API_KEY, base_url), conn)


def save_config(
    conn=None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    prompt: str | None = None,
) -> None:
    """Store whichever fields were given. An empty string clears a field.

    The key and the model are stored against the endpoint being saved — the
    one in this call if it was given, the one already stored otherwise. Saving
    a DeepSeek key therefore cannot overwrite the Gemini key, and switching
    back to Gemini finds both its key and the model it was last used with.
    """
    from . import db

    for key, value in (
        (KEY_MODEL, model),
        (KEY_BASE_URL, base_url),
        (KEY_PROMPT, prompt),
    ):
        if value is not None:
            db.set_setting(key, value.strip(), conn)

    if api_key is None and model is None:
        return

    endpoint = base_url if base_url is not None else db.get_setting(KEY_BASE_URL, "", conn)
    # An unset endpoint means the default one, and it must mean the same as
    # it does in `config()`: resolved apart, a key saved before a provider was
    # picked is filed under a name the read side never asks for.
    endpoint = endpoint or _default_base_url()
    if api_key is not None:
        db.set_setting(_scoped(PREFIX_API_KEY, endpoint), api_key.strip(), conn)
    if model is not None:
        db.set_setting(_scoped(PREFIX_MODEL, endpoint), model.strip(), conn)


def is_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("openai") is not None


def _client(cfg: Config):
    if not is_installed():
        raise SummaryError("summaries need the openai package: uv sync --extra summaries")
    if not cfg.api_key:
        raise SummaryError("no API key is set for summaries")

    from openai import OpenAI

    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=90.0, max_retries=2)


def summarize_text(text: str, cfg: Config | None = None, client=None) -> str:
    """One section in, two or three spoken sentences out."""
    cfg = cfg or config()
    body = (text or "").strip()[:MAX_INPUT_CHARS]
    if not body:
        return ""

    client = client or _client(cfg)
    prompt = (cfg.prompt or DEFAULT_PROMPT)
    # A prompt without the placeholder still has to see the text.
    filled = prompt.format(text=body) if "{text}" in prompt else f"{prompt}\n\n{body}"

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
