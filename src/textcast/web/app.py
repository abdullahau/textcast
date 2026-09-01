"""The web app.

Server-rendered HTML from one process. No build step, no bundler, no
client-side framework: the reader is a document, and the player is about two
hundred lines of vanilla JavaScript driven by the timing map.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, db
from ..document import BlockKind
from ..jobs import Worker
from ..service import IngestError, ingest, rebuild, reparse
from ..settings import get_settings
from ..tts import ENGINES, get_engine

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


def require_auth(request: Request) -> None:
    """Off by default, which suits a private network.

    Set TEXTCAST_REQUIRE_AUTH=1 and a token for anything internet-facing.
    """
    if not settings.require_auth:
        return
    token = request.cookies.get("textcast_token") or request.headers.get("x-textcast-token")
    if token != settings.auth_token or not settings.auth_token:
        raise HTTPException(status_code=401, detail="unauthorised")


Auth = Depends(require_auth)


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
templates.env.globals["engines"] = list(ENGINES)
# One cache-busting suffix for every static asset, so they cannot drift apart.
# Keep it in step with BUILD in static/sw.js.
templates.env.globals["assets"] = __version__


def render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("q", "")
    return templates.TemplateResponse(request, name, context)


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
    )


@app.get("/tags", response_class=HTMLResponse, dependencies=[Auth])
def tags_page(request: Request):
    return render(request, "tags.html", tags=db.list_tags())


@app.get("/a/{slug}", response_class=HTMLResponse, dependencies=[Auth])
def reader(request: Request, slug: str):
    row = article_or_404(slug)
    conn = db.connect()
    article = db.load_article(row["id"], conn)
    position = db.get_position(row["id"], conn)
    job = conn.execute(
        "SELECT * FROM job WHERE article_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
    ).fetchone()

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
        build=db.get_build_options(row["id"], conn),
        voices=_voices(),
        default_voice=settings.voice or "default",
        active_engine=settings.engine,
    )


@app.get("/search", response_class=HTMLResponse, dependencies=[Auth])
def search_page(request: Request, q: str = ""):
    return render(request, "search.html", hits=db.search(q) if q else [], q=q)


@app.get("/add", response_class=HTMLResponse, dependencies=[Auth])
def add_page(request: Request, url: str = "", title: str = "", text: str = ""):
    # The share target lands here on a GET from some clients.
    return render(
        request,
        "add.html",
        url=url or text,
        shared_title=title,
        all_tags=db.list_tags(),
        voices=_voices(),
        default_voice=settings.voice or "default",
    )


@app.get("/jobs", response_class=HTMLResponse, dependencies=[Auth])
def jobs_page(request: Request):
    return render(request, "jobs.html", jobs=db.recent_jobs(limit=40))


@lru_cache(maxsize=4)
def _voices_cached(engine: str) -> tuple:
    """Voices for an engine. Cached: building one loads the whole model."""
    try:
        return tuple(get_engine(engine, **settings.engine_options()).voices())
    except Exception:
        log.warning("could not list voices for %s", engine, exc_info=True)
        return ()


def _voices() -> list:
    return list(_voices_cached(settings.engine))


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------


def _parse_tags(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _build_options(voice: str, quote_voice: str, engine: str, skip_footnotes: bool) -> dict:
    """Only what was actually chosen; blanks mean "use the default"."""
    options = {
        "voice": voice.strip(),
        "quote_voice": quote_voice.strip(),
        "engine": engine.strip(),
    }
    options = {k: v for k, v in options.items() if v}
    if skip_footnotes:
        options["skip_footnotes"] = True
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
    voice: str = Form(default=""),
    quote_voice: str = Form(default=""),
    engine: str = Form(default=""),
    skip_footnotes: bool = Form(default=False),
    build: bool = Form(default=True),
    file: UploadFile | None = None,
):
    """The single ingestion door: text, URL, page source, or a file.

    The bookmarklet and the share target both post here.
    """
    upload = None
    eml = None
    if file is not None and file.filename:
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
            build=build,
            tags=_parse_tags(tags),
            options=_build_options(voice, quote_voice, engine, skip_footnotes),
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


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _ingest_error(request: Request, message: str):
    if _wants_html(request):
        return render(
            request,
            "add.html",
            url="",
            error=message,
            all_tags=db.list_tags(),
            voices=_voices(),
            default_voice=settings.voice or "default",
        )
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
    engine: str = Form(default=""),
    skip_footnotes: bool = Form(default=False),
):
    options = _build_options(voice, quote_voice, engine, skip_footnotes)
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


@app.post("/api/articles/{article_id}/delete", dependencies=[Auth])
def api_delete(request: Request, article_id: int):
    row = db.get_article(article_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")

    media = settings.media_dir / row["slug"]
    for child in media.glob("*"):
        child.unlink(missing_ok=True)
    media.rmdir() if media.exists() else None
    db.delete_article(article_id)

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
def api_set_tags(request: Request, article_id: int, tags: str = Form(default="")):
    row = db.get_article(article_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")
    applied = db.set_tags(article_id, _parse_tags(tags))
    if _wants_html(request):
        return RedirectResponse(f"/a/{row['slug']}", status_code=303)
    return {"tags": applied}


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


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Served from the root so its scope covers the whole app."""
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "engine": settings.engine, "articles": db.stats()["articles"]}


@app.get("/api/blocks/{article_id}", dependencies=[Auth])
def api_blocks(article_id: int, kinds: str = Query(default="")):
    """Block text, for the offline cache to store alongside the audio."""
    wanted = {k for k in kinds.split(",") if k} or {str(k) for k in BlockKind}
    rows = db.connect().execute(
        "SELECT block_id, kind, text FROM block WHERE article_id = ? ORDER BY section_idx, idx",
        (article_id,),
    ).fetchall()
    return {"blocks": [dict(r) for r in rows if r["kind"] in wanted]}
