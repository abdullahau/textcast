"""Section summaries from a language model.

The endpoint is OpenAI-compatible, which is the one interface every provider
now speaks. That buys the whole field with a single dependency: point
``base_url`` at any entry in ``PROVIDERS`` — or anything else that speaks the
protocol — name a model, and nothing else changes.

A router like litellm was weighed and not taken: 183 MB across 114 packages
against 21 MB, and the only thing it reaches that this does not is a provider
with no OpenAI endpoint at all (Bedrock, Vertex, SageMaker), which need a
signed cloud SDK.

Three things are configurable and all three live in the ``setting`` table, so
they are edited in the app rather than in a restart: the model, the endpoint
and the key. The environment supplies the default for each, which is what a
container wants; a value saved in the app then wins over it, or the settings
page would appear to do nothing.

A summary is a *block*, like everything else. It carries a stable id, it is
searched, it is spoken, and the player can hide it. There is no second place
where text lives.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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

#: Written for the ear, not the eye. Everything here exists because a summary
#: that reads well on screen reads badly aloud: no markdown, no bullet list, no
#: heading, and no "this section discusses" preamble. The word limit is there
#: because a summary is spoken before the section it summarises — 150 words is
#: about a minute, and past that you are listening to the summary, not the
#: article.
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

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model)


def _env(key: str, *fallbacks: str) -> str:
    """The first environment variable of the lot that holds anything."""
    for name in (key, *fallbacks):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


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

    return Config(
        model=stored(KEY_MODEL, _env("TEXTCAST_SUMMARY_MODEL") or DEFAULT_MODEL),
        base_url=stored(KEY_BASE_URL, _env("TEXTCAST_SUMMARY_BASE_URL") or DEFAULT_BASE_URL),
        api_key=stored(KEY_API_KEY, _env("TEXTCAST_SUMMARY_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")),
        prompt=stored(KEY_PROMPT, DEFAULT_PROMPT),
    )


def save_config(
    conn=None,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    prompt: str | None = None,
) -> None:
    """Store whichever fields were given. An empty string clears a field."""
    from . import db

    for key, value in (
        (KEY_MODEL, model),
        (KEY_BASE_URL, base_url),
        (KEY_API_KEY, api_key),
        (KEY_PROMPT, prompt),
    ):
        if value is not None:
            db.set_setting(key, value.strip(), conn)


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


def summarize_article(
    article: Article,
    cfg: Config | None = None,
    client=None,
    progress=None,
    replace: bool = False,
) -> int:
    """Put a summary block at the head of every section. Returns how many.

    Written onto the article in place; the caller stores it. A section that
    already has one is left alone, so running this twice costs nothing —
    unless ``replace`` is set, which drops the old summaries first and asks
    the model again for every section.
    """
    cfg = cfg or config()
    client = client or _client(cfg)

    if replace:
        for section in article.sections:
            section.blocks = [b for b in section.blocks if b.kind is not BlockKind.SUMMARY]

    targets = [
        section
        for section in article.sections
        if any(b.kind is not BlockKind.SUMMARY for b in section.blocks)
        and not any(b.kind is BlockKind.SUMMARY for b in section.blocks)
    ]
    if not targets:
        return 0

    def work(item: tuple[int, object]) -> tuple[int, str]:
        index, section = item
        text = "\n\n".join(
            b.text for b in section.blocks if b.kind is not BlockKind.HEADING
        )
        return index, summarize_text(text, cfg, client)

    items = list(enumerate(targets))
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(items))) as pool:
        results = dict(pool.map(work, items))

    added = 0
    for index, section in items:
        summary = results.get(index, "")
        if progress:
            progress(index + 1, len(items))
        if not summary:
            continue
        section.blocks.insert(0, Block(kind=BlockKind.SUMMARY, text=summary))
        added += 1

    article.renumber()
    return added
