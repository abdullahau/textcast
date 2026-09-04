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
import secrets
from collections.abc import Iterable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import __version__, db
from ..document import VISUAL_KINDS, BlockKind, to_markdown
from ..jobs import Worker
from ..prefs import save_voice_defaults, voice_defaults

# summarize is renamed: the ingest form has a field of the same name.
from ..service import IngestError, delete_audio, delete_summaries, ingest, rebuild, rebuild_many, reparse
from ..service import delete as delete_article
from ..service import edit_blocks as save_block_edits
from ..service import summarize as queue_summary
from ..settings import get_settings
from ..summarize import config as summaries_config
from ..tts import (
    ENGINES,
    available,
    catalogue,
    default_voice,
    g2p_of,
    loaded_engine,
    shared_engine,
)
from . import limits

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


class VersionedStatic(StaticFiles):
    """Say how long a static file may be kept, because something else will.

    `StaticFiles` sends an ETag and no `Cache-Control` at all. A CDN in front
    then invents one — Cloudflare's Free default is four hours — and applies it
    to the *browser* as well, so a stylesheet is pinned for four hours on a
    host that has a CDN and not on one that does not. That is the whole of the
    difference between the tailnet address and the public one.

    Every page asks for these with `?v=<version>`, so a changed file is a
    changed URL and the answer can be kept for ever. Without the suffix —
    someone typing the path, or the service worker precaching it — it must be
    revalidated, because that URL's content does change between releases.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-cache"
        )
        return response


app.mount("/static", VersionedStatic(directory=STATIC), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


COOKIE = "textcast_token"

#: The one route the bookmarklet's key opens. Everything else — deleting an
#: article, rebuilding, even reading the library — needs a signed-in session.
#: The key used to be the session token, sitting in clear in a bookmarks bar
#: with the run of the whole app.
INGEST_PATH = "/api/ingest"

#: An uploaded article. Generous for a real report or a saved page; a PDF or
#: DOCX beyond this is parsed synchronously inside the request, and pictures.py
#: caps a single picture at 12 MB for the same reason.
UPLOAD_MAX = 40 * 1024 * 1024


def account():
    """The one account, or None before anything has been seeded."""
    from .. import accounts

    return accounts.get(db.connect(settings.db_path))


def signed_in(request: Request) -> bool:
    """The cookie holds a session secret, never the password.

    It used to hold the sign-in token itself, so the credential travelled on
    every request and changing it could not end a session. Changing the
    password rotates `account.session`, and every cookie carrying the old one
    stops working at once.
    """
    carried = request.cookies.get(COOKIE) or request.headers.get("x-textcast-token")
    if not carried:
        return False
    current = account()
    return current is not None and secrets.compare_digest(carried, current.session)


async def _ingest_key_in_request(request: Request) -> bool:
    """The bookmarklet's way in, and only the bookmarklet's.

    Its POST comes from whatever site you are reading, so it is cross-site,
    and the session cookie is SameSite=Lax: a browser sends that on a
    top-level GET and never on a POST. The cookie stays Lax — loosening it
    would let any page on the internet post to /a/<slug>/delete with your
    session attached — so the bookmarklet carries its key in the body
    instead, exactly as the iPhone Shortcut carries it in a header.

    Only a form-encoded POST is looked at. Reading the body here is safe:
    Starlette caches the parsed form on the request, so FastAPI's own parse
    for the endpoint reuses it rather than finding a drained stream.
    """
    if request.method != "POST":
        return False
    current = account()
    if current is None:
        return False
    header = request.headers.get("x-textcast-token") or ""
    if header and secrets.compare_digest(header, current.ingest_key):
        return True
    if not request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        return False
    form = await request.form()
    offered = form.get("token")
    return isinstance(offered, str) and secrets.compare_digest(offered, current.ingest_key)


def _too_many(seconds: float, what: str) -> HTTPException:
    """429, with a sentence and a header a client can act on.

    `{"detail": "unauthorised"}` is not a sentence, and the Shortcut spent an
    hour reporting success over nine straight 401s. Whatever is on the other
    end of this route has no screen, so the wording has to carry.
    """
    wait = max(1, round(seconds))
    return HTTPException(
        status_code=429,
        detail=f"{what} Try again in about {wait} second{'' if wait == 1 else 's'}.",
        headers={"Retry-After": str(wait)},
    )


async def require_auth(request: Request) -> None:
    """Off by default, which suits a private network.

    Set TEXTCAST_REQUIRE_AUTH=1 and a token for anything internet-facing.
    A browser is sent to the sign-in page; anything else gets a plain 401.

    The ingest route is counted whether or not auth is on. It is the one route
    that takes a credential in a body from anywhere on the internet and does
    real work per call, so a wrong key used to cost nothing and guessing was
    free. See `limits.py` for the budgets and why they live in the process.
    """
    who = limits.client_key(request)
    ingesting = request.url.path == INGEST_PATH

    if ingesting:
        # Checked before the secret is compared, so a spender cannot use the
        # comparison itself as an oracle, and refused early enough that a
        # body is never parsed for a client that has run out.
        waiting = limits.INGEST_ATTEMPTS.check(who)
        if waiting:
            raise _too_many(waiting, "Too many failed attempts from this address.")

    if not settings.require_auth:
        return
    if signed_in(request):
        return
    # Scoped, and the scope is checked here rather than trusted to the caller:
    # a key that can add an article must not be able to delete one.
    if ingesting and await _ingest_key_in_request(request):
        # Say so downstream, so the response can hand back a cookie and the
        # redirect to the new article is not bounced to /login.
        request.state.token_auth = True
        # A key that worked is not an attempt against the door.
        limits.INGEST_ATTEMPTS.forget(who)
        return
    if ingesting:
        limits.INGEST_ATTEMPTS.spend(who)
    if _wants_html(request):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=303, headers={"Location": f"/login?next={quote(target, safe='')}"}
        )
    raise HTTPException(status_code=401, detail="unauthorised")


Auth = Depends(require_auth)


def set_session_cookie(request: Request, response: Response) -> Response:
    """Give a browser that arrived on a token a session, so it can read on.

    The bookmarklet's POST is answered with a redirect to the article, and
    that GET carries no cookie unless one is set here.
    """
    current = account()
    if getattr(request.state, "token_auth", False) and current is not None:
        _write_session(request, response, current.session)
    return response


def _write_session(request: Request, response: Response, session: str) -> Response:
    response.set_cookie(
        COOKIE,
        session,
        max_age=31536000,
        httponly=True,
        samesite="lax",
        # Only over TLS when the page itself came over TLS, or the cookie is
        # dropped on a plain-HTTP tailnet address.
        secure=request.url.scheme == "https",
    )
    return response


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/"):
    if not settings.require_auth or signed_in(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(
        request,
        "login.html",
        next=_safe_next(next),
        unconfigured=account() is None,
    )


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """A valid-looking hash nobody's password will ever match.

    Checked whenever the username itself is wrong, so that reply costs the
    same scrypt call a wrong password does. Computed once and cached, not
    per request -- it only has to be *some* hash, never the right one.
    """
    from .. import accounts

    return accounts.hash_password(secrets.token_urlsafe(32))


@app.post("/login", include_in_schema=False)
def login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/"),
):
    from .. import accounts

    target = _safe_next(next)
    who = limits.client_key(request)
    # Checked before the password is compared, the same as the ingest key's
    # own budget and for the same reason: this grants the whole account, not
    # one scoped route, and only scrypt's own cost stood in the way before.
    waiting = limits.LOGIN_ATTEMPTS.check(who)
    if waiting:
        raise _too_many(waiting, "Too many sign-in attempts from this address.")

    current = account()
    # One message for both halves, so a wrong username cannot be told from a
    # wrong password by trying.
    wrong = "That username and password do not match."
    if current is None:
        return render(request, "login.html", next=target, unconfigured=True)
    # verify_password runs whichever hash applies, every time -- not only
    # when the username matches. `or` short-circuiting on the username above
    # answered a wrong one in a plain string compare and a wrong password in
    # scrypt's own ~100ms, which is the same oracle the ingest key's check
    # ordering exists to avoid, just left open here.
    right_user = username.strip() == current.username
    hash_to_check = current.password_hash if right_user else _dummy_password_hash()
    if not accounts.verify_password(password, hash_to_check) or not right_user:
        limits.LOGIN_ATTEMPTS.spend(who)
        return render(request, "login.html", next=target, error=wrong)
    limits.LOGIN_ATTEMPTS.forget(who)
    return _write_session(request, RedirectResponse(target, status_code=303), current.session)


# --------------------------------------------------------------------------
# the account
# --------------------------------------------------------------------------

#: What a profile picture may be. Read off the bytes, not the file name.
AVATAR_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "image/gif": ".gif", "image/avif": ".avif"}
AVATAR_MAX = 4 * 1024 * 1024


def _settings_page(request: Request, **extra):
    current = account()
    return render(
        request,
        "settings.html",
        account=current,
        password_min=8,
        origin=public_origin(request),
        **extra,
    )


@app.get("/settings", response_class=HTMLResponse, dependencies=[Auth])
def settings_page(request: Request):
    return _settings_page(request)


@app.post("/settings/profile", dependencies=[Auth])
async def save_profile(request: Request, username: str = Form(default="")):
    from .. import accounts

    conn = db.connect(settings.db_path)
    try:
        accounts.set_username(conn, username)
    except ValueError as exc:
        return _settings_page(request, error=str(exc))
    return RedirectResponse("/settings?saved=name", status_code=303)


@app.post("/settings/avatar", dependencies=[Auth])
async def save_avatar(request: Request, photo: UploadFile | None = None):
    """Store the picture, and take the old one with it.

    Named for a hash of the bytes, so uploading the same photo twice writes
    one file, and the browser can cache it for ever: a different picture is a
    different name.
    """
    import hashlib

    from .. import accounts

    if photo is None or not photo.filename:
        return _settings_page(request, error="Choose a picture first.")
    data = await photo.read()
    if not data:
        return _settings_page(request, error="That file was empty.")
    if len(data) > AVATAR_MAX:
        return _settings_page(request, error="A picture must be under 4 MB.")
    suffix = AVATAR_TYPES.get((photo.content_type or "").split(";")[0].strip().lower())
    if not suffix:
        return _settings_page(request, error="That is not a picture textcast can show.")

    conn = db.connect(settings.db_path)
    current = account()
    settings.avatar_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(data).hexdigest()[:16] + suffix
    (settings.avatar_dir / name).write_bytes(data)
    if current and current.avatar and current.avatar != name:
        (settings.avatar_dir / current.avatar).unlink(missing_ok=True)
    accounts.set_avatar(conn, name)
    return RedirectResponse("/settings?saved=photo", status_code=303)


@app.post("/settings/avatar/delete", dependencies=[Auth])
def clear_avatar(request: Request):
    from .. import accounts

    current = account()
    if current and current.avatar:
        (settings.avatar_dir / current.avatar).unlink(missing_ok=True)
    accounts.set_avatar(db.connect(settings.db_path), "")
    return RedirectResponse("/settings?saved=photo", status_code=303)


@app.post("/settings/password", dependencies=[Auth])
def save_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
):
    from .. import accounts

    current = account()
    if current is None:
        return _settings_page(request, error="There is no account to change.")
    # The current password, even though this page is already behind the
    # session: it stops a borrowed screen from becoming a permanent key.
    if not accounts.verify_password(current_password, current.password_hash):
        return _settings_page(request, error="That is not the current password.")
    if new_password != confirm_password:
        return _settings_page(request, error="The two new passwords do not match.")
    try:
        updated = accounts.set_password(db.connect(settings.db_path), new_password)
    except ValueError as exc:
        return _settings_page(request, error=str(exc))
    # Every other browser is signed out; this one carries on.
    return _write_session(
        request, RedirectResponse("/settings?saved=password", status_code=303), updated.session
    )


@app.post("/settings/ingest-key", dependencies=[Auth])
def new_ingest_key(request: Request):
    from .. import accounts

    accounts.rotate_ingest_key(db.connect(settings.db_path))
    return RedirectResponse("/settings?saved=key", status_code=303)


@app.get("/avatar", include_in_schema=False, dependencies=[Auth])
def avatar_current():
    """Where the picture used to live, for markup that still points here.

    A page held offline carries the HTML it was saved with, so an article kept
    for a commute still asks for the bare `/avatar` and would draw a broken
    image until it was next opened online. This answers, and says `no-cache`
    rather than `immutable`: the whole reason the name moved into the URL is
    that the bytes behind this one change.
    """
    current = account()
    if current is None or not current.avatar:
        raise HTTPException(status_code=404, detail="no picture")
    return RedirectResponse(f"/avatar/{current.avatar}", status_code=302,
                            headers={"Cache-Control": "no-cache"})


@app.get("/avatar/{name}", include_in_schema=False, dependencies=[Auth])
def avatar(name: str):
    """The stored picture, under the name its own bytes gave it.

    The name is in the URL because the answer is cached for a year and
    `immutable` is a promise that the bytes at this address never change. On a
    bare `/avatar` that promise was false the moment a new photograph was
    uploaded: the URL did not move, so one cache went on serving the old
    picture while another had the new one, and the mark appeared to change as
    you walked between pages.

    Only the picture the account is actually wearing is served, so a name is
    not a way to walk the directory or to read one that was replaced.
    """
    current = account()
    if current is None or not current.avatar or name != current.avatar:
        raise HTTPException(status_code=404, detail="no picture")
    path = settings.avatar_dir / current.avatar
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no picture")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.post("/logout", include_in_schema=False)
def logout():
    """Sign this browser out, and only this one.

    The session in `account` is left alone on purpose: rotating it here would
    sign the phone out because the laptop was, which is not what the button
    says. A cookie that has left the machine is what
    `/settings/sign-out-everywhere` is for.
    """
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.post("/settings/sign-out-everywhere", dependencies=[Auth])
def sign_out_everywhere(request: Request):
    """End every session, and keep this one.

    Deleting the cookie only reaches the browser doing it, so a cookie copied
    off a machine — a shared laptop, a borrowed phone — went on working, and
    the only way to stop it was to change the password. This rotates the
    session on its own. The browser asking is written a fresh cookie, exactly
    as changing the password does, because signing yourself out of the page
    you are on is not what was asked for.
    """
    from .. import accounts

    updated = accounts.rotate_session(db.connect(settings.db_path))
    return _write_session(
        request, RedirectResponse("/settings?saved=sessions", status_code=303), updated.session
    )


def _safe_next(target: str) -> str:
    """Only ever redirect inside this app, never to another host.

    A browser resolves a backslash exactly like a forward slash, so
    `/\\evil.example` arrives as the protocol-relative `//evil.example`
    even though it never starts with `//` itself. Checked after every
    backslash is turned into a slash, not before.
    """
    target = target.replace("\\", "/")
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def public_origin(request: Request) -> str:
    """Where the outside world reaches this app, without a trailing slash.

    The bookmarklet and the Shortcut both need it, and both are written once
    and kept for months. A page served straight from the container would bake
    in a host no phone can resolve, so a configured ``TEXTCAST_PUBLIC_URL``
    wins over the request. Blank means trust the request, which is right only
    where nothing sits in front of the app.
    """
    return settings.public_url or str(request.base_url).rstrip("/")


def duration(ms: int | None) -> str:
    # Negative is not a length. divmod renders minus three seconds as
    # "-1:59:57", which is how a played-out article looked in the library.
    if not ms or ms < 0:
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


def when(value: str | None, style: str = "datetime") -> Markup:
    """A stored instant, rendered where the reader is, not where the server is.

    Everything in the database is UTC, which is the only sane thing to store
    and the wrong thing to show: the server sits in UTC and the reader does
    not, so a build that started at 15:22 in Dubai read 11:22 on the page.
    The zone cannot be a setting either — the same library is read from a
    phone abroad and a laptop at home.

    So the page carries the instant and the browser formats it. The text
    inside is the fallback, and it names UTC rather than pretending: without
    JavaScript a wrong-looking time is worse than an honest one.

    A value with no time in it is left alone. A bare `2026-09-03` is a
    publication date, and converting it to a zone would move it a day.
    """
    if not value:
        return Markup("")
    if "T" not in value:
        return Markup('<time datetime="{}">{}</time>').format(value, value[:10])
    if style == "date":
        return Markup('<time datetime="{}" data-when="date">{}</time>').format(value, value[:10])
    shown = value[:16].replace("T", " ") + " UTC"
    return Markup('<time datetime="{}" data-when="datetime">{}</time>').format(value, shown)


templates.env.filters["duration"] = duration
templates.env.filters["date"] = short_date
templates.env.filters["when"] = when
# One cache-busting suffix for every static asset. The service worker is
# stamped with the same version when it is served, so the two cannot drift.
templates.env.globals["assets"] = __version__
# The sign-out control only makes sense when there is something to sign out of.
templates.env.globals["auth_on"] = settings.require_auth


def render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("q", "")
    context.setdefault("origin", public_origin(request))
    # The bar draws the profile mark, so every page needs the account — but
    # only a page whose reader is actually signed in. Handed to every page it
    # put the username, and an `<img src="/avatar">`, into `/login`: readable
    # by anyone who could reach the host, which on a public address is anyone
    # at all. The picture itself never leaked — `/avatar` is behind `Auth`, so
    # it answered 401 and the browser drew a broken image, which is how this
    # was spotted. With access control off there is no signed-in state and
    # nothing to protect.
    context.setdefault(
        "account", account() if (not settings.require_auth or signed_in(request)) else None
    )
    return templates.TemplateResponse(request, name, context)


def _export_totals() -> dict:
    """What each export would hold, so a link can say before it is clicked.

    Counted off the file system rather than the ``article`` columns, because
    ``audio_bytes`` is what the build recorded and a deleted media directory
    would not have told it.
    """
    conn = db.connect()
    slugs = [row["slug"] for row in conn.execute("SELECT slug FROM article")]

    sources = [p for p in settings.source_dir.glob("*") if p.is_file() and p.stem in slugs]
    audio = {
        slug: [p for p in (settings.media_dir / slug).glob("*") if p.is_file()] for slug in slugs
    }
    played = {slug: files for slug, files in audio.items() if files}
    # Pictures sit in a subdirectory, so the audio glob above steps over them.
    pictures = [
        p
        for slug in slugs
        for p in (settings.media_dir / slug / "images").glob("*")
        if p.is_file()
    ]
    return {
        "sources": {"count": len(sources), "bytes": sum(p.stat().st_size for p in sources)},
        "text": {"count": len(slugs)},
        "audio": {
            "count": len(played),
            # What the zip weighs, which is the whole media directory — the
            # pictures are in it and the glob above steps over them.
            "bytes": (
                sum(p.stat().st_size for files in played.values() for p in files)
                + sum(p.stat().st_size for p in pictures)
            ),
        },
        "pictures": {
            "count": len(pictures),
            "bytes": sum(p.stat().st_size for p in pictures),
        },
    }


def article_or_404(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="no such article")
    return row


def build_payload(article_id: int) -> dict:
    """Sections and block timings, as the player consumes them.

    Every audio address carries `?b=<built_at>`. `section-000.opus` is
    rewritten by every build and the path does not change, so a browser or a
    service worker holding the old file played it against the new timing map.
    The stamp makes the `immutable` header honest, and it is what lets the
    offline cache stop being thrown away on every release.
    """
    conn = db.connect()
    built_at = conn.execute(
        "SELECT built_at FROM article WHERE id = ?", (article_id,)
    ).fetchone()
    stamp = f"?b={built_at['built_at'] if built_at else 0}"
    sections = conn.execute(
        "SELECT idx, title, file, duration_ms FROM section WHERE article_id = ? ORDER BY idx",
        (article_id,),
    ).fetchall()
    # The WebVTT track sits beside its Opus file with the same stem.
    def track_for(file: str) -> str:
        return file.rsplit(".", 1)[0] + ".vtt" + stamp
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
                "file": s["file"] + stamp,
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


#: Rows on a page of the library. Small enough to read in one scroll on a
#: phone, which is where the library is read.
PAGE_SIZE = 25


@app.get("/", response_class=HTMLResponse, dependencies=[Auth])
def library(
    request: Request,
    tag: str | None = None,
    status: str | None = None,
    shelf: str = "",
    added: int = 0,
    failed: str = "",
    page: int = 1,
):
    conn = db.connect()
    archived = shelf == "archived"
    starred = shelf == "starred"
    filters = {"tag": tag, "status": status, "archived": archived, "starred": starred}

    total = db.count_articles(conn, **filters)
    pages = max(1, -(-total // PAGE_SIZE))
    # A page number out of range is a stale bookmark or a filter that has just
    # narrowed. Clamp it, so the library always shows something.
    page = min(max(page, 1), pages)
    articles = db.list_articles(conn, **filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)

    # The filters the pager has to carry with it. `page` is deliberately not
    # among them: the filter form has no page field, so changing a filter
    # starts at the first page, which is where the answer to a new filter is.
    kept = urlencode({k: v for k, v in (("tag", tag), ("status", status), ("shelf", shelf)) if v})

    return render(
        request,
        "library.html",
        articles=articles,
        article_tags=db.tags_for_many([a["id"] for a in articles], conn),
        # Continue listening belongs to the library, not to a page of it.
        resume=db.continue_listening(conn) if not (tag or status or shelf or page > 1) else [],
        tags=db.list_tags(conn),
        stats=db.stats(conn),
        jobs=db.active_jobs(conn),
        tag=tag,
        status=status,
        archived=archived,
        starred=starred,
        added=added,
        failed=failed,
        exports=_export_totals(),
        paging={
            "page": page,
            "pages": pages,
            "total": total,
            "first": (page - 1) * PAGE_SIZE + 1 if total else 0,
            "last": min(page * PAGE_SIZE, total),
            "query": f"{kept}&" if kept else "",
        },
    )


@app.get("/tags", response_class=HTMLResponse, dependencies=[Auth])
def tags_page(request: Request):
    return render(request, "tags.html", tags=db.list_tags())


def _last_summary(article_id: int, conn) -> dict:
    """How the summaries were last made, named for the page.

    Empty when nothing is recorded, which is what the reader shows for a
    summary written by hand: there is no source to name.
    """
    from ..summarize import provider_for

    record = db.last_summary(article_id, conn)
    if not record:
        return {}
    return {**record, "provider": provider_for(record["base_url"])}


@app.get("/a/{slug}", response_class=HTMLResponse, dependencies=[Auth])
def reader(
    request: Request,
    slug: str,
    edit: bool = False,
    edited: int = 0,
    removed: int = 0,
    reparsed: int = 0,
    unchanged: int = 0,
    kept: int = 0,
    lost: int = 0,
):
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
        voices_json=json.dumps(
            {
                name: [{"id": v.id, "name": v.name, "gender": v.gender} for v in list_]
                for name, list_ in _voices_by_engine().items()
            },
            separators=(",", ":"),
        ),
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
        reparsed=reparsed,
        reparse_unchanged=bool(unchanged),
        summaries_kept=kept,
        summaries_lost=lost,
        kinds=[str(k) for k in BlockKind],
        # What the reader draws rather than prints. Read off the model, so a
        # fourth visual kind does not need finding in a template as well.
        visual_kinds=[str(k) for k in VISUAL_KINDS],
        summary_model=summaries_config(conn).model,
        last_summary=_last_summary(row["id"], conn),
        build_on_top=build_on_top,
        modify_on_top=build_on_top and not has_summary,
    )


@app.get("/search", response_class=HTMLResponse, dependencies=[Auth])
def search_page(request: Request, q: str = ""):
    return render(request, "search.html", hits=db.search(q) if q else [], q=q)


@app.get("/add", response_class=HTMLResponse, dependencies=[Auth])
def add_page(request: Request, url: str = "", title: str = "", text: str = ""):
    # The share target lands here on a GET from some clients.
    #
    # The bookmarklet needs a credential in its body, so the page has to carry
    # one. It carries the *ingest* key, which reaches `/api/ingest` and
    # nothing else — so a bookmarklet copied off this page, or lifted out of a
    # bookmarks bar, can add an article and cannot delete one.
    current = account()
    return render(
        request,
        "add.html",
        url=url or text,
        shared_title=title,
        token=current.ingest_key if (settings.require_auth and current) else "",
    )


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


def _engines_by_g2p() -> dict[str, list[str]]:
    """Installed engines grouped by the phonemiser they use."""
    grouped: dict[str, list[str]] = {}
    for entry in _engines():
        grouped.setdefault(g2p_of(entry["id"])[0], []).append(entry["id"])
    return grouped


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
        # Both pickers show one engine's voices, never a mix: the two engines
        # share every voice id, so a merged list offers "Heart" twice.
        voices=_voices(chosen.engine),
        engines=_engines(),
        # Which installed engines each phonemiser speaks for, so the IPA
        # fields can name them rather than talk about "misaki" in the abstract.
        by_g2p=_engines_by_g2p(),
        voices_json=json.dumps(
            {
                name: [{"id": v.id, "name": v.name, "gender": v.gender} for v in list_]
                for name, list_ in _voices_by_engine().items()
            },
            separators=(",", ":"),
        ),
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
    engine: str = Form(default=""),
    voice: str = Form(default=""),
    quote_voice: str = Form(default=""),
    speed: str = Form(default=""),
):
    """What every future build uses unless an article names its own.

    Nothing is queued. Existing audio keeps the engine and voice it was made
    with until you rebuild it, which is why the article says which they were.
    """
    save_voice_defaults(engine=engine, voice=voice, quote_voice=quote_voice, speed=speed)
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
    "Published Jul 2nd 2025. Thrive led a $72mm round at 12x EBITDA, up 150bps YoY, "
    "and the SEC asked about GAAP vs. the S&P 500 before the £1.5bn buyout."
)


@app.post("/api/say", dependencies=[Auth])
def api_say(
    text: str = Form(...),
    apply_rules: bool = Form(default=True),
    engine: str = Form(default=""),
    voice: str = Form(default=""),
    speed: str = Form(default=""),
):
    """Speak a sample and report what the rules did to it on the way.

    One call answers the whole question the Voice page asks: what will be
    spoken, which rules fired, what phonemes the engine gets, and how it
    sounds. Hearing it is the only way to judge a respelling.

    The engine is a parameter, not the default, so two engines can be compared
    from one page without saving anything or restarting anything.
    """
    import base64

    from ..audio import encode_opus_bytes
    from ..normalize import normalize
    from ..pronounce import active, preview

    raw = (text or "").strip()[:600]
    if not raw:
        return JSONResponse({"error": "nothing to say"}, status_code=400)

    chosen = voice_defaults()
    # The form may name an engine, so the two can be compared on one page
    # without saving anything. Anything unregistered falls back to the default.
    engine_name = engine if engine in ENGINES else chosen.engine
    # /api/say is the one place that hears the rules, so it has to ask the
    # engine that will speak them: a phoneme rule is written in one
    # phonemiser's notation and does nothing in the other's.
    g2p, takes_ipa = g2p_of(engine_name)
    spoken = normalize(raw, g2p=g2p, phonemes=takes_ipa) if apply_rules else raw
    # Word rules see the text after the shape transforms have run, so preview
    # has to start from the same place they do.
    hits = [
        {
            "pattern": r.pattern,
            "matched": m,
            "replacement": r.phonemes_for(g2p) if takes_ipa and r.phonemes_for(g2p) else r.replacement,
        }
        for r, m in preview(
            normalize(raw, rules=[], g2p=g2p, phonemes=takes_ipa), active(), g2p, takes_ipa
        )
    ] if apply_rules else []

    try:
        speaker = shared_engine(engine_name, **settings.engine_options())
        clip = speaker.synthesize(
            spoken,
            voice=voice or chosen.voice or default_voice(engine_name),
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
    replacement: str = Form(default=""),
    note: str = Form(default=""),
    replacement_misaki: str = Form(default=""),
    replacement_espeak: str = Form(default=""),
    ignore_case: bool = Form(default=False),
):
    try:
        db.add_pronunciation(
            kind, pattern, replacement,
            misaki=replacement_misaki, espeak=replacement_espeak,
            ignore_case=ignore_case, note=note,
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
    # Name, provider, endpoint and the tail of each key. Never a key itself:
    # this goes into the page, where anything sent is readable.
    keys = [
        {
            "name": c.name, "provider": c.provider, "provider_name": c.provider_name,
            "endpoint": c.endpoint, "hint": c.hint, "model": c.model,
            "local": summaries.is_local(c.endpoint),
        }
        for c in summaries.credentials()
    ]
    return render(
        request,
        "summaries.html",
        cfg=cfg,
        installed=summaries.is_installed(),
        default_prompt=summaries.DEFAULT_PROMPT,
        default_model=summaries.DEFAULT_MODEL,
        providers=summaries.PROVIDERS,
        keys=keys,
        pending=db.summarisable(),
        **extra,
    )


@app.get("/summaries", response_class=HTMLResponse, dependencies=[Auth])
def summaries_page(request: Request):
    return _summaries_page(request)


@app.post("/summaries", dependencies=[Auth])
def summaries_save(
    request: Request,
    credential: str = Form(default=""),
    model: str = Form(default=""),
    base_url: str = Form(default=""),
    prompt: str = Form(default=""),
):
    """Which stored key, model and endpoint write the summaries.

    No key here. Storing one is its own act, in its own box, and its own
    route: saving a model must never be able to touch a key.
    """
    from .. import summarize as summaries

    summaries.save_config(
        credential_name=credential, model=model, base_url=base_url, prompt=prompt
    )
    return RedirectResponse("/summaries", status_code=303)


@app.post("/summaries/key", dependencies=[Auth])
def summaries_save_key(
    request: Request,
    name: str = Form(default=""),
    provider: str = Form(default=""),
    base_url: str = Form(default=""),
    api_key: str = Form(default=""),
):
    """Store one named key. Nothing else moves — not which key is in use."""
    from ..summarize import SummaryError, save_credential

    try:
        save_credential(name=name, provider=provider, base_url=base_url, api_key=api_key)
    except SummaryError as exc:
        return _summaries_page(request, error=str(exc))
    return RedirectResponse("/summaries", status_code=303)


@app.post("/summaries/forget-key", dependencies=[Auth])
def summaries_forget_key(request: Request, name: str = Form(default="")):
    """Delete one stored key, leaving every other one alone."""
    from .. import summarize as summaries

    summaries.forget_credential(name)
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
    skip_visuals: bool = False,
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
    if skip_visuals:
        options["skip_visuals"] = True
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

    Counted twice over, and for two different reasons: `require_auth` limits
    *failed* attempts, because guessing the key was free, and the budget spent
    here limits accepted ones, because a key that has leaked should not be
    able to make the server fetch and parse for ever.
    """
    waiting = limits.INGEST_WORK.retry_after(limits.client_key(request))
    if waiting:
        raise _too_many(waiting, "That is more articles at once than this accepts.")

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
        if len(data) > UPLOAD_MAX:
            return _ingest_error(request, "That file is over the 40 MB upload limit.")
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
        return set_session_cookie(request, RedirectResponse(f"/a/{result.slug}", status_code=303))
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
        # Reading the part is inside the try with everything else. A form part
        # may carry no filename at all, and `None.lower()` outside it was an
        # unhandled 500 that cost the whole batch -- the one thing a batch
        # promises not to do.
        try:
            data = await upload.read()
            filename = upload.filename or "upload"
            if len(data) > UPLOAD_MAX:
                raise IngestError("over the 40 MB upload limit")
            name = filename.lower()
            kwargs: dict = {"tags": tags, "build": False}
            if name.endswith((".eml", ".mbox", ".msg")):
                kwargs["eml"] = data
            elif name.endswith((".html", ".htm")):
                kwargs["html"] = data.decode("utf-8", errors="replace")
            else:
                kwargs["upload"] = (data, filename)
            added.append(ingest(**kwargs))
        except Exception as exc:
            # Deliberately broad. The promise of a batch is that one bad file
            # does not cost you the other nineteen, and a parser can fail in
            # whatever way its library chooses.
            log.warning("batch import failed for %s", upload.filename, exc_info=True)
            failed.append(f"{upload.filename or 'a file with no name'}: {exc}")

    if _wants_html(request):
        query = urlencode({"added": len(added), "failed": " · ".join(failed)[:400]})
        return set_session_cookie(request, RedirectResponse(f"/?{query}", status_code=303))
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
    skip_visuals: bool = Form(default=False),
    speed: str = Form(default=""),
    engine: str = Form(default=""),
):
    options = _build_options(
        voice, quote_voice, skip_footnotes, skip_summaries=skip_summaries,
        skip_visuals=skip_visuals, speed=speed, engine=engine,
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
        query = urlencode(
            {
                "reparsed": 1,
                "unchanged": int(result.unchanged),
                "kept": result.summaries_kept,
                "lost": result.summaries_lost,
            }
        )
        return RedirectResponse(f"/a/{result.slug}?{query}", status_code=303)
    return {
        "id": result.article_id,
        "slug": result.slug,
        "unchanged": result.unchanged,
        "summaries_kept": result.summaries_kept,
        "summaries_lost": result.summaries_lost,
    }


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
    try:
        found = delete_article(article_id)
    except IngestError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
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


@app.post("/api/articles/{article_id}/position/clear", dependencies=[Auth])
def api_clear_position(article_id: int):
    """Stop an article and forget where it was.

    A POST rather than a DELETE on the position path, because every other
    write in this app is a POST and sendBeacon can send nothing else.
    """
    db.clear_position(article_id)
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

#: What a stored picture is served as. Keyed by the suffix `pictures.py` gave
#: it, which it read off the Content-Type when it fetched it.
IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


@app.get("/media/{slug}/images/{name}", dependencies=[Auth])
def article_image(slug: str, name: str):
    """Serve a stored picture. Its own route because the audio's rejects a slash.

    The name is a hash of the address the picture came from, so the response
    can be cached for ever: a different picture is a different name.
    """
    if "/" in name or ".." in name or ".." in slug:
        raise HTTPException(status_code=400, detail="bad path")
    path = settings.media_dir / slug / "images" / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such picture")
    return FileResponse(
        path,
        media_type=IMAGE_TYPES.get(path.suffix, "application/octet-stream"),
        # `private`, as the avatar is: this route is behind `Auth`, and
        # `public` invites a shared cache in front to keep a copy it can hand
        # to anyone who asks for the same address.
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/media/{slug}/{name}", dependencies=[Auth])
def media(slug: str, name: str):
    """Serve audio straight off disk, with range support for seeking.

    `private`, because this route is behind `Auth` and `public` invites a CDN
    in front to keep a copy of the library's audio and hand it to anyone who
    asks for the same address.

    `immutable` is true now, and it was not before. `section-000.opus` is
    rewritten by every build and the path does not change, so the promise used
    to be a lie: a browser or a service worker holding the old file played it
    against the new timing map. Every address the player builds now carries
    `?b=<built_at>`, so a rebuilt article is a new URL.

    Weakening the header instead was tried and reverted. The audio element asks
    for byte ranges, so the browser's HTTP cache holds a *partial* entry for
    the file, and without a long-lived header Chromium satisfies the service
    worker's own plain GET as a ranged one. `Cache.addAll` then refuses the
    lot — "Partial response (status code 206) is unsupported" — and an article
    marked for offline stored nothing at all.

    The query itself is ignored here; the file on disk is the answer either
    way. It exists to name a version, not to select one.
    """
    if "/" in name or ".." in name or ".." in slug:
        raise HTTPException(status_code=400, detail="bad path")
    path = settings.media_dir / slug / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    types = {".opus": "audio/ogg", ".vtt": "text/vtt"}
    return FileResponse(
        path,
        media_type=types.get(path.suffix),
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------
#
# Three zips, because the library holds three different things and they are
# wanted for different reasons. The originals cannot be made again. The text
# can, from the originals, but only by this app. The audio can, from the text,
# but it costs hours of synthesis.


def _zip_response(files: Iterable[tuple[str, Path | bytes]], name: str) -> Response:
    """Build a zip in memory and hand it back as a download.

    The library is a few hundred megabytes at most, so nothing here streams.
    Measure before that stops being true.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    empty = True
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, item in files:
            if isinstance(item, bytes):
                archive.writestr(arcname, item)
            else:
                archive.write(item, arcname)
            empty = False
    if empty:
        raise HTTPException(status_code=404, detail="there is nothing to export")

    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _export_name(row, taken: dict[str, str]) -> str:
    """A file name that reads like the library does, not like a slug.

    Unique within one zip, which the title alone is not: a newsletter that
    names every issue the same is the ordinary case, and two entries of one
    name in a zip hand the reader whichever the archiver picks. The slug is
    what tells two articles apart, so the second one carries it.

    ``taken`` maps slug to the name already chosen for it, so an article whose
    files are yielded one at a time keeps the same name for all of them.
    """
    if row["slug"] in taken:
        return taken[row["slug"]]
    base = (row["title"] or row["slug"]).replace("/", "-").strip() or row["slug"]
    name = base if base not in taken.values() else f"{base} ({row['slug']})"
    taken[row["slug"]] = name
    return name


@app.get("/api/export/sources.zip", dependencies=[Auth])
def api_export_sources():
    """Every original, as it arrived.

    These are the bytes each article was parsed from. They are what Re-parse
    replays, and the only part of the library that cannot be made again.
    """
    conn = db.connect()
    rows = {row["slug"]: row for row in conn.execute("SELECT slug, title FROM article")}

    def files():
        taken: dict[str, str] = {}
        for path in sorted(settings.source_dir.glob("*")):
            row = rows.get(path.stem)
            if path.is_file() and row is not None:
                yield f"{_export_name(row, taken)}{path.suffix}", path

    return _zip_response(files(), "textcast-sources.zip")


@app.get("/api/export/text.zip", dependencies=[Auth])
def api_export_text():
    """Every article as Markdown, summaries included where they exist.

    The displayed text, not the spoken form: what the engine is handed is
    derived at build time and is nobody's reading copy.
    """
    conn = db.connect()

    def files():
        taken: dict[str, str] = {}
        for row in conn.execute("SELECT id, slug, title FROM article ORDER BY id"):
            article = db.load_article(row["id"], conn)
            if article is not None:
                yield f"{_export_name(row, taken)}.md", to_markdown(article).encode()

    return _zip_response(files(), "textcast-text.zip")


@app.get("/api/export/audio.zip", dependencies=[Auth])
def api_export_audio():
    """The built audio, one directory per article.

    The timing map and the manifest go with it: the Opus files alone lose the
    read-along, and the manifest is the only copy of the timings outside the
    database.
    """
    conn = db.connect()

    def files():
        taken: dict[str, str] = {}
        for row in conn.execute("SELECT slug, title FROM article ORDER BY id"):
            directory = settings.media_dir / row["slug"]
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    # rglob, so `images/` comes too. A read-along without the
                    # chart the writer pointed at is not the article.
                    yield f"{_export_name(row, taken)}/{path.relative_to(directory)}", path

    return _zip_response(files(), "textcast-audio.zip")


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
            # Three entries, not one. Chrome on Android will not install an
            # app whose only icon is an SVG, and it read "any maskable" on a
            # square that draws to its own edge as licence to crop the
            # headband off. So: the SVG for anything that scales, PNGs for
            # Android's launcher, and a separate padded square for the mask.
            "icons": [
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {
                    "src": "/static/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
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


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """A browser asks for this path whatever the page's <link> tags say.

    Nothing answered it, so every phone got the 404 page and drew no icon at
    all. Served from the root rather than /static because that is the address
    that is asked for, and left out of the auth list because the login page
    needs an icon too.
    """
    return FileResponse(
        STATIC / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_touch_icon():
    """iOS asks for these two at the root before it reads the page."""
    return FileResponse(
        STATIC / "apple-touch-icon.png",
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
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
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            # Cloudflare reads this one in preference to Cache-Control, and
            # without it the edge served a four-hour-old worker: the browser
            # dutifully revalidated and the CDN answered from its own copy.
            # A stale worker is the worst thing to cache — it keeps serving
            # the last release's CSS and JS from its own caches, which is why
            # a figure came up stretched and the Sign out mark did not draw on
            # the public host and never on the tailnet one.
            "CDN-Cache-Control": "no-store",
            "Cloudflare-CDN-Cache-Control": "no-store",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/health", include_in_schema=False)
def health():
    """A liveness probe, and only that.

    Orchestration and monitoring reach this unauthenticated by convention --
    gating it behind Auth broke the very startup checks in this test suite
    that poll it before a session exists. What it must not do instead is say
    anything about the deployment to whoever asks: the engine in use and the
    library's size used to come back to anyone, on an internet-facing
    instance same as a private one.
    """
    return {"ok": True}


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
