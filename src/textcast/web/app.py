"""The web app.

Server-rendered HTML from one process. No build step, no bundler, no
client-side framework: the reader is a document, and the player is about two
hundred lines of vanilla JavaScript driven by the timing map.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, db
from ..document import BlockKind
from ..jobs import Worker
from ..prefs import save_voice_defaults, voice_defaults

# summarize is renamed: the ingest form has a field of the same name.
from ..service import IngestError, delete_audio, delete_summaries, ingest, rebuild, rebuild_many, reparse
from ..service import delete as delete_article
from ..service import edit_blocks as save_block_edits
from ..service import summarize as queue_summary
from ..settings import get_settings
from ..summarize import config as summaries_config
from ..tts import ENGINES, available, catalogue, default_voice, loaded_engine, shared_engine

log = logging.getLogger("textcast.web")

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

mimetypes.add_type("audio/ogg", ".opus")

settings = get_settings()
worker: Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    # uvicorn configures its own loggers only; without this the worker builds
    # and fails silently inside the web process.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        force=False,
    )
    logging.getLogger("textcast").setLevel(logging.INFO)
    settings.ensure_dirs()
    db.init(settings.db_path)
    if settings.workers > 0:
        worker = Worker(settings)
        worker.start()
    yield
    if worker is not None:
        worker.stop()
    db.close()


app = FastAPI(title="textcast", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


COOKIE = "textcast_token"


def signed_in(request: Request) -> bool:
    token = request.cookies.get(COOKIE) or request.headers.get("x-textcast-token")
    return bool(settings.auth_token) and token == settings.auth_token


def require_auth(request: Request) -> None:
    """Off by default, which suits a private network.

    Set TEXTCAST_REQUIRE_AUTH=1 and a token for anything internet-facing.
    A browser is sent to the sign-in page; anything else gets a plain 401.
    """
    if not settings.require_auth or signed_in(request):
        return
    if _wants_html(request):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=303, headers={"Location": f"/login?next={quote(target, safe='')}"}
        )
    raise HTTPException(status_code=401, detail="unauthorised")


Auth = Depends(require_auth)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/"):
    if not settings.require_auth or signed_in(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(
        request,
        "login.html",
        next=_safe_next(next),
        unconfigured=not settings.auth_token,
    )


@app.post("/login", include_in_schema=False)
def login(request: Request, token: str = Form(default=""), next: str = Form(default="/")):
    target = _safe_next(next)
    if not settings.auth_token or token.strip() != settings.auth_token:
        return render(request, "login.html", next=target, error="That token does not match.")
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        settings.auth_token,
        max_age=31536000,
        httponly=True,
        samesite="lax",
        # Only over TLS when the page itself came over TLS, or the cookie is
        # dropped on a plain-HTTP tailnet address.
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


def _safe_next(target: str) -> str:
    """Only ever redirect inside this app, never to another host."""
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def duration(ms: int | None) -> str:
    if not ms:
        return ""
    total = round(ms / 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def short_date(value: str | None) -> str:
    if not value:
        return ""
    return value[:10]


templates.env.filters["duration"] = duration
templates.env.filters["date"] = short_date
# One cache-busting suffix for every static asset. The service worker is
# stamped with the same version when it is served, so the two cannot drift.
templates.env.globals["assets"] = __version__
# The sign-out control only makes sense when there is something to sign out of.
templates.env.globals["auth_on"] = settings.require_auth


def render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("q", "")
    return templates.TemplateResponse(request, name, context)


def _stored_sources() -> dict:
    """How many originals are kept, and how much they weigh."""
    files = [p for p in settings.source_dir.glob("*") if p.is_file()]
    return {"count": len(files), "bytes": sum(p.stat().st_size for p in files)}


def article_or_404(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")
    return row


def build_payload(article_id: int) -> dict:
    """Sections and block timings, as the player consumes them."""
    conn = db.connect()
    sections = conn.execute(
        "SELECT idx, title, file, duration_ms FROM section WHERE article_id = ? ORDER BY idx",
        (article_id,),
    ).fetchall()
    # The WebVTT track sits beside its Opus file with the same stem.
    def track_for(file: str) -> str:
        return file.rsplit(".", 1)[0] + ".vtt"
    blocks = conn.execute(
        """
        SELECT block_id, section_idx, start_ms, dur_ms
          FROM block
         WHERE article_id = ? AND start_ms IS NOT NULL
         ORDER BY section_idx, idx
        """,
        (article_id,),
    ).fetchall()

    by_section: dict[int, list] = {}
    for b in blocks:
        by_section.setdefault(b["section_idx"], []).append(
            [b["block_id"], b["start_ms"], b["dur_ms"]]
        )

    return {
        "sections": [
            {
                "idx": s["idx"],
                "title": s["title"],
                "file": s["file"],
                "track": track_for(s["file"]),
                "ms": s["duration_ms"],
                "blocks": by_section.get(s["idx"], []),
            }
            for s in sections
            if s["file"]
        ]
    }


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, dependencies=[Auth])
def library(
    request: Request,
    tag: str | None = None,
    status: str | None = None,
    shelf: str = "",
    added: int = 0,
    failed: str = "",
):
    conn = db.connect()
    archived = shelf == "archived"
    starred = shelf == "starred"
    articles = db.list_articles(
        conn, tag=tag, status=status, archived=archived, starred=starred, limit=200
    )
    return render(
        request,
        "library.html",
        articles=articles,
        article_tags=db.tags_for_many([a["id"] for a in articles], conn),
        resume=db.continue_listening(conn) if not (tag or status or shelf) else [],
        tags=db.list_tags(conn),
        stats=db.stats(conn),
        jobs=db.active_jobs(conn),
        tag=tag,
        status=status,
        archived=archived,
        starred=starred,
        added=added,
        failed=failed,
        sources=_stored_sources(),
    )


@app.get("/tags", response_class=HTMLResponse, dependencies=[Auth])
def tags_page(request: Request):
    return render(request, "tags.html", tags=db.list_tags())


@app.get("/a/{slug}", response_class=HTMLResponse, dependencies=[Auth])
def reader(request: Request, slug: str, edit: bool = False, edited: int = 0, removed: int = 0):
    row = article_or_404(slug)
    conn = db.connect()
    article = db.load_article(row["id"], conn)
    position = db.get_position(row["id"], conn)
    job = conn.execute(
        "SELECT * FROM job WHERE article_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
    ).fetchone()
    options = db.get_build_options(row["id"], conn)
    chosen = voice_defaults(conn)
    build_engine = options.get("engine") or settings.engine
    has_summary = any(b.kind is BlockKind.SUMMARY for _s, b in article.blocks())
    # A pass can land some sections and not others, so "has a summary" and
    # "is summarised" are two different questions.
    missing_summaries = sum(
        1
        for section in article.sections
        if any(b.kind is not BlockKind.SUMMARY for b in section.blocks)
        and not any(b.kind is BlockKind.SUMMARY for b in section.blocks)
    )
    # Nothing is built yet, so what to do next belongs above the text, not
    # below it. Once there is a summary, only the build is still pending.
    build_on_top = row["status"] in ("new", "failed")

    return render(
        request,
        "reader.html",
        article=article,
        row=row,
        payload=json.dumps(build_payload(row["id"]), separators=(",", ":")),
        position=position,
        job=job,
        tags=db.tags_for(row["id"], conn),
        all_tags=db.list_tags(conn),
        build=options,
        voices=_voices(build_engine),
        engines=_engines(),
        voices_by_engine=_voices_by_engine(),
        selected_engine=build_engine,
        default_voice=chosen.voice or "default",
        default_quote_voice=chosen.quote_voice,
        speeds=SPEEDS,
        selected_speed=_speed_label(options.get("speed") or chosen.speed),
        blocks=sum(len(section.blocks) for section in article.sections),
        has_summary=has_summary,
        missing_summaries=missing_summaries,
        editing=edit,
        edited=edited,
        removed=removed,
        kinds=[str(k) for k in BlockKind],
        summary_model=summaries_config(conn).model,
        build_on_top=build_on_top,
        modify_on_top=build_on_top and not has_summary,
    )


@app.get("/search", response_class=HTMLResponse, dependencies=[Auth])
def search_page(request: Request, q: str = ""):
    return render(request, "search.html", hits=db.search(q) if q else [], q=q)


@app.get("/add", response_class=HTMLResponse, dependencies=[Auth])
def add_page(request: Request, url: str = "", title: str = "", text: str = ""):
    # The share target lands here on a GET from some clients.
    return render(request, "add.html", url=url or text, shared_title=title)


@app.get("/jobs", response_class=HTMLResponse, dependencies=[Auth])
def jobs_page(request: Request):
    return render(request, "jobs.html", jobs=db.recent_jobs(limit=40))


@lru_cache(maxsize=2)
def _voices_cached(engine: str) -> tuple:
    """Voices for an engine, read from its table rather than from a model.

    Every article page carries a voice picker. Building an engine to fill it
    made the first page load after a restart wait for the weights.
    """
    try:
        return tuple(catalogue(engine))
    except Exception:
        log.warning("could not list voices for %s", engine, exc_info=True)
        return ()


def _voices(engine: str | None = None) -> list:
    return list(_voices_cached(engine or settings.engine))


def _engines() -> list[dict]:
    """The engines that could build right now, most preferred first.

    Two of them run the same weights, so the description is what tells them
    apart on the page — as the voice names do in the picker.
    """
    ready = available()
    listed = [
        {"id": name, "description": spec.description}
        for name, spec in ENGINES.items()
        if ready.get(name)
    ]
    listed.sort(key=lambda e: e["id"] != settings.engine)
    return listed


def _voices_by_engine() -> dict[str, list]:
    """Every installed engine's voices, keyed by engine.

    The picker shows one engine's at a time; the page carries them all so
    changing the engine does not cost a round trip.
    """
    return {e["id"]: _voices(e["id"]) for e in _engines()}


# --------------------------------------------------------------------------
# pronunciation
# --------------------------------------------------------------------------

#: What the code-level transforms do, shown on the settings page so the two
#: layers are visible together.
STRUCTURAL_EXAMPLES = [
    ("$72mm  £5bn  €300k", "72 million dollars, 5 billion pounds, 300 thousand euros"),
    ("$19 million  $1", "19 million dollars, 1 dollar"),
    ("150bps  12x  2.5%", "150 basis points, 12 times, 2.5 percent"),
    ("Q3  FY2024  2019-21", "quarter 3, fiscal year 2024, 2019 to 2021"),
    ("[Footnote 3: …]", "a pause, then the footnote, then a pause"),
    ("— … “ ” ’", "flattened to plain punctuation"),
]


def _pronunciation_page(request: Request, **extra):
    rows = db.pronunciation_rows()
    kind = extra.pop("changed_kind", "")
    pattern = extra.pop("changed_pattern", "")
    chosen = voice_defaults()
    return render(
        request,
        "pronunciations.html",
        rows=rows,
        enabled_count=sum(1 for r in rows if r["enabled"]),
        structural=STRUCTURAL_EXAMPLES,
        sample=extra.pop("sample", SAMPLE_TEXT),
        voices=_voices(),
        speeds=SPEEDS,
        chosen=chosen,
        changed_kind=kind,
        changed_pattern=pattern,
        affected=_affected(kind, pattern),
        queued=extra.pop("queued", 0),
        saved=extra.pop("saved", False),
        imported=extra.pop("imported", None),
        **extra,
    )


def _affected(kind: str, pattern: str) -> list:
    """Built articles the rule just edited would read differently.

    Changing a rule does not change the audio already on disk. Naming the
    articles it touches turns "rebuild each one by hand" into one button.
    """
    if not pattern:
        return []
    from ..pronounce import Rule

    return db.articles_matching(Rule(kind=kind or "word", pattern=pattern, replacement=""))


@app.get("/pronunciations", response_class=HTMLResponse, dependencies=[Auth])
def pronunciations(
    request: Request,
    kind: str = "",
    pattern: str = "",
    queued: int = 0,
    saved: bool = False,
    imported: str = "",
):
    return _pronunciation_page(
        request,
        changed_kind=kind,
        changed_pattern=pattern,
        queued=queued,
        saved=saved,
        imported=[int(n) for n in imported.split(",")] if imported.count(",") == 2 else None,
    )


@app.post("/pronunciations/defaults", dependencies=[Auth])
def pronunciations_defaults(
    voice: str = Form(default=""),
    quote_voice: str = Form(default=""),
    speed: str = Form(default=""),
):
    """The voice every future build uses unless an article names its own.

    Nothing is queued. Existing audio keeps the voice it was made with until
    you rebuild it.
    """
    save_voice_defaults(voice=voice, quote_voice=quote_voice, speed=speed)
    return RedirectResponse("/pronunciations?saved=1", status_code=303)


def _changed(kind: str, pattern: str) -> RedirectResponse:
    """Back to the rules, carrying what changed so the page can offer a rebuild."""
    query = urlencode({"kind": kind, "pattern": pattern})
    return RedirectResponse(f"/pronunciations?{query}", status_code=303)


@app.post("/pronunciations/rebuild", dependencies=[Auth])
def pronunciations_rebuild(kind: str = Form(default=""), pattern: str = Form(default="")):
    rows = _affected(kind, pattern)
    queued = rebuild_many([r["id"] for r in rows])
    return RedirectResponse(f"/pronunciations?queued={queued}", status_code=303)


SAMPLE_TEXT = (
    "Published Jul 2 2025. Thrive led a $72mm round at 12x EBITDA, up 150bps YoY, "
    "and the SEC asked about GAAP vs. the S&P 500."
)


@app.post("/api/say", dependencies=[Auth])
def api_say(
    text: str = Form(...),
    apply_rules: bool = Form(default=True),
    voice: str = Form(default=""),
    speed: str = Form(default=""),
):
    """Speak a sample and report what the rules did to it on the way.

    One call answers the whole question the Voice page asks: what will be
    spoken, which rules fired, what phonemes the engine gets, and how it
    sounds. Hearing it is the only way to judge a respelling.
    """
    import base64

    from ..audio import encode_opus_bytes
    from ..normalize import normalize
    from ..pronounce import active, preview

    raw = (text or "").strip()[:600]
    if not raw:
        return JSONResponse({"error": "nothing to say"}, status_code=400)

    chosen = voice_defaults()
    spoken = normalize(raw) if apply_rules else raw
    # Word rules see the text after the shape transforms have run, so preview
    # has to start from the same place they do.
    hits = [
        {"pattern": r.pattern, "matched": m, "replacement": r.replacement}
        for r, m in preview(normalize(raw, rules=[]), active())
    ] if apply_rules else []

    try:
        engine = shared_engine(settings.engine, **settings.engine_options())
        clip = engine.synthesize(
            spoken,
            voice=voice or chosen.voice or default_voice(settings.engine),
            speed=_as_speed(speed, chosen.speed),
        )
    except Exception as exc:
        log.warning("preview synthesis failed", exc_info=True)
        return JSONResponse({"error": f"could not synthesise: {exc}"}, status_code=500)

    if not len(clip.samples):
        return JSONResponse({"error": "the engine produced no audio"}, status_code=500)

    try:
        audio = encode_opus_bytes(clip.samples, clip.sample_rate)
        media = "audio/ogg"
    except Exception:
        # Without ffmpeg the preview still works, just larger.
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(clip.sample_rate)
            out.writeframes((clip.samples * 32767).astype("int16").tobytes())
        audio = buffer.getvalue()
        media = "audio/wav"

    return {
        "spoken": spoken,
        "phonemes": _phonemes_for(spoken, voice or chosen.voice),
        "hits": hits,
        "seconds": round(clip.duration_s, 2),
        "audio": f"data:{media};base64,{base64.b64encode(audio).decode()}",
    }


def _as_speed(value: str, fallback: float) -> float:
    try:
        return min(2.0, max(0.5, float(value)))
    except (TypeError, ValueError):
        return fallback


def _phonemes_for(text: str, voice: str = "") -> str:
    """What the engine will actually pronounce, if one is already loaded.

    Never builds an engine. A phoneme line is a nicety, and loading the model
    inside a request would stall a web-only process for seconds. Press Say
    once and the engine is resident, so the line appears from then on.
    """
    engine = loaded_engine(settings.engine)
    if engine is None or not hasattr(engine, "phonemes"):
        return ""
    try:
        return engine.phonemes(text[:400], voice=voice or voice_defaults().voice or None)
    except Exception:
        log.debug("could not produce a phoneme preview", exc_info=True)
        return ""


@app.post("/pronunciations/add", dependencies=[Auth])
def pronunciations_add(
    request: Request,
    kind: str = Form(default="word"),
    pattern: str = Form(...),
    replacement: str = Form(...),
    note: str = Form(default=""),
    is_phonemes: bool = Form(default=False),
    ignore_case: bool = Form(default=False),
):
    try:
        db.add_pronunciation(
            kind, pattern, replacement,
            is_phonemes=is_phonemes, ignore_case=ignore_case, note=note,
        )
    except ValueError as exc:
        return _pronunciation_page(request, error=str(exc), sample=SAMPLE_TEXT)
    return _changed(kind, pattern)


@app.get("/pronunciations/export", dependencies=[Auth])
def pronunciations_export():
    """Every rule as a JSON file, to keep or to carry to another machine."""
    payload = json.dumps(db.export_pronunciations(), ensure_ascii=False, indent=2)
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="textcast-pronunciations-{__version__}.json"'},
    )


@app.post("/pronunciations/import", dependencies=[Auth])
async def pronunciations_import(
    request: Request,
    file: UploadFile | None = None,
    replace: bool = Form(default=False),
):
    if file is None or not file.filename:
        return _pronunciation_page(request, error="Choose a file to import.")
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _pronunciation_page(request, error=f"That is not JSON: {exc}")

    try:
        result = db.import_pronunciations(data, replace=replace)
    except ValueError as exc:
        return _pronunciation_page(request, error=str(exc))

    query = urlencode({"imported": f"{result['added']},{result['updated']},{result['skipped']}"})
    return RedirectResponse(f"/pronunciations?{query}", status_code=303)


@app.post("/pronunciations/{rule_id}/toggle", dependencies=[Auth])
def pronunciations_toggle(rule_id: int):
    row = db.connect().execute(
        "SELECT kind, pattern, enabled FROM pronunciation WHERE id = ?", (rule_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such rule")
    db.update_pronunciation(rule_id, enabled=0 if row["enabled"] else 1)
    return _changed(row["kind"], row["pattern"])


@app.post("/pronunciations/{rule_id}/delete", dependencies=[Auth])
def pronunciations_delete(rule_id: int):
    row = db.connect().execute(
        "SELECT kind, pattern FROM pronunciation WHERE id = ?", (rule_id,)
    ).fetchone()
    db.delete_pronunciation(rule_id)
    if row is None:
        return RedirectResponse("/pronunciations", status_code=303)
    return _changed(row["kind"], row["pattern"])


# --------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------

def _summaries_page(request: Request, **extra):
    from .. import summarize as summaries

    cfg = summaries.config()
    return render(
        request,
        "summaries.html",
        cfg=cfg,
        installed=summaries.is_installed(),
        default_prompt=summaries.DEFAULT_PROMPT,
        default_model=summaries.DEFAULT_MODEL,
        providers=summaries.PROVIDERS,
        provider_name=summaries.provider_for(cfg.base_url),
        pending=db.summarisable(),
        **extra,
    )


@app.get("/summaries", response_class=HTMLResponse, dependencies=[Auth])
def summaries_page(request: Request):
    return _summaries_page(request)


@app.post("/summaries", dependencies=[Auth])
def summaries_save(
    request: Request,
    model: str = Form(default=""),
    base_url: str = Form(default=""),
    api_key: str = Form(default=""),
    prompt: str = Form(default=""),
    keep_key: bool = Form(default=False),
):
    from .. import summarize as summaries

    # An untouched password field posts blank. Saving that would wipe the key
    # every time the model was changed.
    summaries.save_config(
        model=model,
        base_url=base_url,
        api_key=None if (keep_key and not api_key.strip()) else api_key,
        prompt=prompt,
    )
    return RedirectResponse("/summaries", status_code=303)


@app.post("/summaries/test", response_class=HTMLResponse, dependencies=[Auth])
def summaries_test(request: Request, sample: str = Form(default="")):
    """Summarise one paragraph, so a key and a model name can be checked."""
    from ..summarize import SummaryError, config, summarize_text

    try:
        result = summarize_text(sample, config())
    except SummaryError as exc:
        return _summaries_page(request, sample=sample, error=str(exc))
    return _summaries_page(request, sample=sample, tested=True, result=result)


@app.post("/api/articles/{article_id}/summarize", dependencies=[Auth])
def api_summarize(request: Request, article_id: int, again: bool = Form(default=False)):
    try:
        job_id = queue_summary(article_id, replace=again)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _wants_html(request):
        row = db.get_article(article_id)
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"job": job_id}


@app.post("/summaries/run-all", dependencies=[Auth])
def summaries_run_all():
    """Summarise every article that has none yet."""
    queued = 0
    for row in db.summarisable():
        try:
            queue_summary(row["id"])
            queued += 1
        except IngestError:
            break
    return RedirectResponse(f"/summaries?queued={queued}", status_code=303)


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------


def _parse_tags(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


#: What the reading-pace picker offers. Kokoro takes anything, but far outside
#: this the delivery stops sounding like speech.
SPEEDS = ["0.8", "0.9", "1.0", "1.1", "1.2", "1.3"]


def _speed_label(value: float | str | None) -> str:
    """One decimal place, always, so it can match an entry in SPEEDS.

    Formatting with %g wrote 1.0 as "1", which matched no option, so the
    browser fell back to the first one and the picker opened at 0.8x.
    """
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "1.0"


def _build_options(
    voice: str,
    quote_voice: str,
    skip_footnotes: bool,
    summarize: bool = False,
    skip_summaries: bool = False,
    speed: str = "",
    engine: str = "",
) -> dict:
    """Only what was actually chosen; blanks mean "use the default"."""
    options = {"voice": voice.strip(), "quote_voice": quote_voice.strip()}
    # An unregistered name is silently dropped rather than saved and warned
    # about on every later build.
    if engine.strip() in ENGINES:
        options["engine"] = engine.strip()
    options = {k: v for k, v in options.items() if v}
    try:
        # A pace of 1.0 is the default, so storing it would only be noise.
        if speed and 0.5 <= float(speed) <= 2.0 and float(speed) != 1.0:
            options["speed"] = float(speed)
    except ValueError:
        pass
    if skip_footnotes:
        options["skip_footnotes"] = True
    if skip_summaries:
        options["skip_summaries"] = True
    if summarize:
        options["summarize"] = True
    return options


@app.post("/api/ingest", dependencies=[Auth])
async def api_ingest(
    request: Request,
    kind: str = Form(default=""),
    url: str | None = Form(default=None),
    html: str | None = Form(default=None),
    text: str | None = Form(default=None),
    title: str | None = Form(default=None),
    adapter: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    source: str = Form(default=""),
    files: list[UploadFile] | None = None,
):
    """The single ingestion door: text, URL, page source, or a file.

    It takes the text in and nothing else. No audio is built and no summary is
    asked for: you land on the article, see what the parser made of it, and
    decide there. Adding something and choosing how to read it are two
    different jobs, and doing both in one form meant guessing at the second
    before you had seen the first.

    The bookmarklet and the share target both post here.
    """
    chosen = [f for f in (files or []) if f is not None and f.filename]

    # More than one file is a batch: each is its own article, and the browser
    # goes to the library rather than to any one of them.
    if len(chosen) > 1:
        return await _ingest_many(request, chosen, _parse_tags(tags))

    upload = None
    eml = None
    if chosen:
        file = chosen[0]
        data = await file.read()
        name = file.filename.lower()
        if name.endswith((".eml", ".mbox", ".msg")):
            eml = data
        elif name.endswith((".html", ".htm")):
            html = data.decode("utf-8", errors="replace")
        else:
            upload = (data, file.filename)

    # A share sheet often sends the URL inside a text field.
    if url and not url.startswith(("http://", "https://")):
        found = [w for w in url.split() if w.startswith(("http://", "https://"))]
        url = found[0] if found else None

    # Each form on the Add page posts every field; honour the one it names.
    if kind == "text":
        url = html = None
    elif kind == "url":
        html = text = None
    elif kind == "html":
        url_only = url
        text = None
        url = url_only
    elif kind == "file":
        html = text = url = None

    try:
        result = ingest(
            html=html,
            url=url,
            eml=eml,
            text=text,
            title=title,
            upload=upload,
            adapter=adapter,
            source=source,
            build=False,
            tags=_parse_tags(tags),
        )
    except IngestError as exc:
        return _ingest_error(request, str(exc))

    if _wants_html(request):
        return RedirectResponse(f"/a/{result.slug}", status_code=303)
    return JSONResponse(
        {
            "id": result.article_id,
            "slug": result.slug,
            "title": result.title,
            "words": result.word_count,
            "tags": result.tags,
            "job": result.job_id,
            "duplicate": result.duplicate,
            "url": f"/a/{result.slug}",
        }
    )


async def _ingest_many(request: Request, files: list[UploadFile], tags: list[str]):
    """Take a pile of files in one go. One bad file does not stop the rest."""
    added, failed = [], []
    for upload in files:
        data = await upload.read()
        name = upload.filename.lower()
        kwargs: dict = {"tags": tags, "build": False}
        if name.endswith((".eml", ".mbox", ".msg")):
            kwargs["eml"] = data
        elif name.endswith((".html", ".htm")):
            kwargs["html"] = data.decode("utf-8", errors="replace")
        else:
            kwargs["upload"] = (data, upload.filename)
        try:
            added.append(ingest(**kwargs))
        except Exception as exc:
            # Deliberately broad. The promise of a batch is that one bad file
            # does not cost you the other nineteen, and a parser can fail in
            # whatever way its library chooses.
            log.warning("batch import failed for %s", upload.filename, exc_info=True)
            failed.append(f"{upload.filename}: {exc}")

    if _wants_html(request):
        query = urlencode({"added": len(added), "failed": " · ".join(failed)[:400]})
        return RedirectResponse(f"/?{query}", status_code=303)
    return JSONResponse(
        {"added": [a.slug for a in added], "failed": failed},
        status_code=200 if added else 400,
    )


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _ingest_error(request: Request, message: str):
    if _wants_html(request):
        return render(request, "add.html", url="", error=message)
    return JSONResponse({"error": message}, status_code=400)


@app.post("/share", dependencies=[Auth])
async def share_target(
    request: Request,
    title: str = Form(default=""),
    text: str = Form(default=""),
    url: str = Form(default=""),
):
    """PWA share target: Android's share sheet posts here."""
    candidate = (url or text or title or "").strip()
    is_link = candidate.startswith(("http://", "https://"))
    try:
        result = ingest(
            url=candidate if is_link else None,
            text=None if is_link else candidate,
            title=title if not is_link else None,
            build=False,
        )
    except IngestError as exc:
        return _ingest_error(request, str(exc))
    return RedirectResponse(f"/a/{result.slug}", status_code=303)


# --------------------------------------------------------------------------
# article actions
# --------------------------------------------------------------------------


@app.post("/api/articles/{article_id}/rebuild", dependencies=[Auth])
def api_rebuild(
    request: Request,
    article_id: int,
    voice: str = Form(default=""),
    quote_voice: str = Form(default=""),
    skip_footnotes: bool = Form(default=False),
    skip_summaries: bool = Form(default=False),
    speed: str = Form(default=""),
    engine: str = Form(default=""),
):
    options = _build_options(
        voice, quote_voice, skip_footnotes, skip_summaries=skip_summaries,
        speed=speed, engine=engine,
    )
    # Remember the choice, so a later rebuild does not silently revert.
    db.set_build_options(article_id, options)
    job_id = rebuild(article_id, options=options)
    row = db.get_article(article_id)
    if _wants_html(request):
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"job": job_id}


@app.post("/api/articles/{article_id}/reparse", dependencies=[Auth])
def api_reparse(request: Request, article_id: int, adapter: str = Form(default="")):
    try:
        result = reparse(article_id, adapter=adapter or None)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _wants_html(request):
        return RedirectResponse(f"/a/{result.slug}", status_code=303)
    return {"id": result.article_id, "slug": result.slug}


@app.post("/api/articles/{article_id}/flag", dependencies=[Auth])
def api_flag(request: Request, article_id: int, field: str = Form(...), value: bool = Form(...)):
    try:
        db.set_flag(article_id, field, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _wants_html(request):
        return RedirectResponse(request.headers.get("referer", "/"), status_code=303)
    return {"ok": True}


@app.post("/api/articles/{article_id}/blocks", dependencies=[Auth])
async def api_edit_blocks(request: Request, article_id: int):
    """Save hand edits to the text.

    Ids do not move, so the audio and its timing map stay valid — they are
    merely out of date for whatever changed. Rebuilding re-renders only those
    blocks, because the cache is keyed by the text itself.
    """
    row = db.get_article(article_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")

    form = await request.form()
    edits: dict[str, dict] = {}
    removed: set[str] = set()
    for field, value in form.multi_items():
        name, _, block_id = field.partition(":")
        if not block_id:
            continue
        if name in ("text", "kind"):
            edits.setdefault(block_id, {})[name] = str(value)
        elif name == "remove":
            removed.add(block_id)

    try:
        result = save_block_edits(article_id, edits, removed)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if _wants_html(request):
        query = urlencode({"edited": result["changed"], "removed": result["removed"]})
        return RedirectResponse(f"/a/{row['slug']}?{query}", status_code=303)
    return result


@app.post("/api/articles/{article_id}/audio/delete", dependencies=[Auth])
def api_delete_audio(request: Request, article_id: int):
    """Undo a build without losing the article."""
    try:
        removed = delete_audio(article_id)
    except IngestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if _wants_html(request):
        row = db.get_article(article_id)
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"removed": removed}


@app.post("/api/articles/{article_id}/summaries/delete", dependencies=[Auth])
def api_delete_summaries(request: Request, article_id: int):
    """Undo a summary pass. The audio goes with it, because the ids move."""
    try:
        dropped = delete_summaries(article_id)
    except IngestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if _wants_html(request):
        row = db.get_article(article_id)
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"dropped": dropped}


@app.post("/api/articles/{article_id}/delete", dependencies=[Auth])
def api_delete(request: Request, article_id: int):
    if not delete_article(article_id):
        raise HTTPException(status_code=404, detail="no such article")

    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True}


@app.post("/api/articles/{article_id}/position", dependencies=[Auth])
async def api_position(article_id: int, request: Request):
    """Called on a throttle and again via sendBeacon when the page hides."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    db.save_position(
        article_id,
        section_idx=int(body.get("section", 0)),
        ms=int(body.get("ms", 0)),
        finished=bool(body.get("finished", False)),
    )
    return Response(status_code=204)


@app.get("/api/articles/{article_id}/manifest", dependencies=[Auth])
def api_manifest(article_id: int):
    if db.get_article(article_id) is None:
        raise HTTPException(status_code=404, detail="no such article")
    return build_payload(article_id)


@app.post("/api/articles/{article_id}/tags", dependencies=[Auth])
def api_set_details(
    request: Request,
    article_id: int,
    tags: str = Form(default=""),
    author: str | None = Form(default=None),
):
    """Tags, and the byline. The parser finds the author where a publication
    publishes one; a pasted note has nowhere to find it, so it is editable."""
    row = db.get_article(article_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")
    applied = db.set_tags(article_id, _parse_tags(tags))
    if author is not None:
        db.set_author(article_id, author)
    if _wants_html(request):
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"tags": applied, "author": db.get_article(article_id)["author"]}


@app.post("/api/tags/{name}/delete", dependencies=[Auth])
def api_delete_tag(request: Request, name: str):
    db.delete_tag(name)
    if _wants_html(request):
        return RedirectResponse("/tags", status_code=303)
    return {"ok": True}


@app.get("/api/tags", dependencies=[Auth])
def api_tags():
    return {"tags": [dict(t) for t in db.list_tags()]}


@app.get("/api/jobs", dependencies=[Auth])
def api_jobs():
    """Polled by the library and reader while a build runs."""
    return {
        "jobs": [
            {
                "id": j["id"],
                "article": j["article_id"],
                "slug": j["slug"],
                "title": j["title"],
                "kind": j["kind"],
                "state": j["state"],
                "progress": round(j["progress"], 3),
                "message": j["message"],
            }
            for j in db.active_jobs()
        ]
    }


# --------------------------------------------------------------------------
# media and PWA
# --------------------------------------------------------------------------


@app.get("/media/{slug}/{name}", dependencies=[Auth])
def media(slug: str, name: str):
    """Serve audio straight off disk, with range support for seeking."""
    if "/" in name or ".." in name or ".." in slug:
        raise HTTPException(status_code=400, detail="bad path")
    path = settings.media_dir / slug / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    types = {".opus": "audio/ogg", ".vtt": "text/vtt"}
    return FileResponse(
        path,
        media_type=types.get(path.suffix),
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/sources.zip", dependencies=[Auth])
def api_sources_zip():
    """Every original, as it arrived, in one zip.

    These are the bytes each article was parsed from. They are what Re-parse
    replays, and the only part of the library that cannot be regenerated.
    """
    import io
    import zipfile

    conn = db.connect()
    titles = {row["slug"]: row["title"] for row in conn.execute("SELECT slug, title FROM article")}

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(settings.source_dir.glob("*")):
            if path.is_file():
                # Named for the article it belongs to, not its slug, so the
                # zip reads like the library does.
                title = titles.get(path.stem, path.stem).replace("/", "-")
                archive.write(path, f"{title}{path.suffix}")
    if not buffer.tell():
        raise HTTPException(status_code=404, detail="no sources are stored")

    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="textcast-sources.zip"'},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def webmanifest():
    return JSONResponse(
        {
            "name": "textcast",
            "short_name": "textcast",
            "description": "Your newsletters and articles, read aloud",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#12100f",
            "theme_color": "#12100f",
            "icons": [
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
            ],
            "share_target": {
                "action": "/share",
                "method": "POST",
                "enctype": "application/x-www-form-urlencoded",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        },
        media_type="application/manifest+json",
    )


@lru_cache(maxsize=1)
def _service_worker_source() -> str:
    """The worker with this release's version stamped into it.

    Its cache names carry BUILD, so a stale one has to be bumped with every
    static asset change. Doing that by hand was forgotten once and a stale
    stylesheet survived the deploy; taking it from ``__version__`` means the
    page and the worker cannot disagree.
    """
    source = (STATIC / "sw.js").read_text(encoding="utf-8")
    stamped, count = re.subn(
        r'const BUILD = "[^"]*"', f'const BUILD = "{__version__}"', source, count=1
    )
    if not count:
        log.error("sw.js has no BUILD line to stamp; caches will not be versioned")
    return stamped


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Served from the root so its scope covers the whole app."""
    return Response(
        _service_worker_source(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "engine": settings.engine, "articles": db.stats()["articles"]}


@app.get("/api/blocks/{article_id}", dependencies=[Auth])
def api_blocks(article_id: int, kinds: str = Query(default="")):
    """Block text, for the offline cache to store alongside the audio."""
    wanted = sorted({k for k in kinds.split(",") if k} or {str(k) for k in BlockKind})
    placeholders = ",".join("?" * len(wanted))
    rows = db.connect().execute(
        f"""
        SELECT block_id, kind, text FROM block
         WHERE article_id = ? AND kind IN ({placeholders})
         ORDER BY section_idx, idx
        """,
        (article_id, *wanted),
    ).fetchall()
    return {"blocks": [dict(r) for r in rows]}
