"""Command line interface.

argparse rather than a CLI framework: one less dependency, and the command set
is small enough that it costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .document import Article, BlockKind, slugify
from .settings import get_settings


def _load(path_or_url: str, prefer: str | None = None) -> Article:
    from .ingest import parse_html
    from .ingest.newsletter import article_from_eml

    if path_or_url.startswith(("http://", "https://")):
        import requests

        response = requests.get(
            path_or_url,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; textcast/0.2)"},
        )
        response.raise_for_status()
        return parse_html(response.text, url=path_or_url, prefer=prefer)

    path = Path(path_or_url)
    raw = path.read_bytes()
    if path.suffix.lower() in (".eml", ".mbox", ".msg"):
        return article_from_eml(raw, url="")
    return parse_html(raw.decode("utf-8", errors="replace"), url="", prefer=prefer)


def cmd_engines(args: argparse.Namespace) -> int:
    from .tts import ENGINES, available

    have = available()
    for name, spec in ENGINES.items():
        mark = "installed" if have[name] else f"missing  (uv sync --extra {spec.extra})"
        print(f"  {name:<12} {mark:<38} {spec.description}")
    print(f"\nactive engine: {get_settings().engine}")
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    from .tts import get_engine

    settings = get_settings()
    engine = get_engine(args.engine or settings.engine, **settings.engine_options())
    for voice in engine.voices():
        print(f"  {voice.id:<14} {voice.gender or '':<8} {voice.lang}")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    article = _load(args.source, prefer=args.adapter)
    if args.json:
        print(json.dumps(article.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"title    {article.title}")
    if article.subtitle:
        print(f"subtitle {article.subtitle}")
    print(f"source   {article.source}   series={article.series or '-'}")
    print(f"words    {article.word_count}   sections={len(article.sections)}")
    for section in article.sections:
        kinds = ", ".join(
            f"{k}:{sum(1 for b in section.blocks if b.kind == k)}"
            for k in sorted({b.kind for b in section.blocks})
        )
        print(f"  [{section.idx}] {section.title[:58]:<58} {len(section.blocks):>3} blocks  ({kinds})")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from .audio import render_article
    from .tts import get_engine

    settings = get_settings()
    settings.ensure_dirs()

    article = _load(args.source, prefer=args.adapter)
    engine_name = args.engine or settings.engine
    engine = get_engine(engine_name, **settings.engine_options())
    voice = args.voice or settings.voice or _default_voice(engine)

    include = set(BlockKind)
    if args.no_footnotes:
        include.discard(BlockKind.FOOTNOTE)
    if args.no_summaries:
        include.discard(BlockKind.SUMMARY)

    out_dir = Path(args.out) if args.out else settings.media_dir / slugify(article.title)
    print(f"{article.title}  ({article.word_count} words, {len(article.sections)} sections)")
    print(f"engine {engine_name} voice {voice} -> {out_dir}")

    started = time.time()
    last = [0.0]

    def progress(done: int, total: int, block_id: str) -> None:
        now = time.time()
        if now - last[0] < 0.5 and done != total:
            return
        last[0] = now
        pct = 100 * done / total
        sys.stderr.write(f"\r  block {done}/{total}  {pct:5.1f}%  {block_id:<10}")
        sys.stderr.flush()

    manifest = render_article(
        article,
        engine,
        out_dir,
        voice=voice,
        quote_voice=args.quote_voice or settings.quote_voice or None,
        bitrate=settings.bitrate,
        gap_ms=settings.gap_ms,
        heading_gap_ms=settings.heading_gap_ms,
        include=include,
        cache_dir=settings.cache_dir,
        progress=progress,
    )
    sys.stderr.write("\r" + " " * 60 + "\r")

    (out_dir / "article.json").write_text(
        json.dumps(article.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    elapsed = time.time() - started
    audio_s = manifest.total_ms / 1000
    size = sum(f.stat().st_size for f in out_dir.glob("*.opus"))
    print(
        f"done  {audio_s / 60:.1f} min audio in {elapsed / 60:.1f} min "
        f"(RTF {elapsed / audio_s:.2f})  {size / 1e6:.1f} MB  {len(manifest.sections)} files"
    )
    return 0


def _default_voice(engine) -> str:
    from .tts import ENGINES

    spec = ENGINES.get(engine.name)
    if spec:
        return spec.default_voice
    voices = engine.voices()
    return voices[0].id if voices else ""


def cmd_add(args: argparse.Namespace) -> int:
    from . import db
    from .service import ingest

    settings = get_settings()
    settings.ensure_dirs()
    db.init(settings.db_path)

    source = args.source
    kwargs: dict = {"adapter": args.adapter, "build": not args.no_build}
    if source.startswith(("http://", "https://")):
        kwargs["url"] = source
    else:
        path = Path(source)
        raw = path.read_bytes()
        if path.suffix.lower() in (".eml", ".mbox", ".msg"):
            kwargs["eml"] = raw
        else:
            kwargs["html"] = raw.decode("utf-8", errors="replace")

    result = ingest(**kwargs)
    if result.duplicate:
        print(f"already stored as #{result.article_id} ({result.slug})")
        return 0

    series = f"  [{result.series}]" if result.series else ""
    print(f"#{result.article_id}  {result.title}{series}  {result.word_count} words")
    if result.job_id:
        print(f"queued build as job {result.job_id} — run `textcast worker` to process it")
    return 0


def _duration(ms: int) -> str:
    if not ms:
        return "-"
    minutes, seconds = divmod(round(ms / 1000), 60)
    return f"{minutes}:{seconds:02d}"


def cmd_library(args: argparse.Namespace) -> int:
    from . import db

    db.init(get_settings().db_path)
    conn = db.connect()

    if args.series:
        for row in db.list_series(conn):
            print(f"  {row['name']:<28} {row['issues']:>3} issues  {row['ready']:>3} ready  {_duration(row['audio_ms'] or 0):>7}")
        return 0

    rows = db.list_articles(conn, series=args.of, status=args.status, limit=args.limit)
    for row in rows:
        flag = "*" if row["starred"] else " "
        print(
            f"{flag}#{row['id']:<4} {row['status']:<9} {_duration(row['audio_ms']):>7}  "
            f"{(row['series'] or row['source'])[:16]:<17} {row['title'][:52]}"
        )
    summary = db.stats(conn)
    print(f"\n{summary['articles']} articles, {summary['ready']} ready, {_duration(summary['audio_ms'])} of audio, {summary['audio_bytes'] / 1e6:.0f} MB")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from . import db

    db.init(get_settings().db_path)
    hits = db.search(args.query, limit=args.limit)
    if not hits:
        print("no matches")
        return 0
    for hit in hits:
        where = f"{_duration(hit['start_ms'] or 0)}" if hit["start_ms"] is not None else "-"
        snippet = hit["snippet"].replace("<mark>", "[").replace("</mark>", "]")
        print(f"#{hit['article_id']} {hit['block_id']:<8} @{where:>7}  {hit['title'][:38]}")
        print(f"    {snippet}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    import logging

    from .jobs import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    settings.ensure_dirs()

    worker = Worker(settings)
    if args.once:
        from . import db

        db.init(settings.db_path)
        return 0 if worker.step() else 0

    worker.run()
    return 0


def cmd_mail(args: argparse.Namespace) -> int:
    import logging

    from .mail import fetch

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = fetch(limit=args.limit)
    print(result)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    get_settings().ensure_dirs()
    uvicorn.run(
        "textcast.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="textcast", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("engines", help="list TTS engines and whether they are installed").set_defaults(
        func=cmd_engines
    )

    voices = sub.add_parser("voices", help="list voices for an engine")
    voices.add_argument("--engine")
    voices.set_defaults(func=cmd_voices)

    parse = sub.add_parser("parse", help="parse a file, .eml or URL and show its structure")
    parse.add_argument("source")
    parse.add_argument("--adapter", help="force an adapter instead of auto-detecting")
    parse.add_argument("--json", action="store_true")
    parse.set_defaults(func=cmd_parse)

    build = sub.add_parser("build", help="parse and synthesise to Opus plus a timing map")
    build.add_argument("source")
    build.add_argument("--adapter")
    build.add_argument("--engine")
    build.add_argument("--voice")
    build.add_argument("--quote-voice", help="second voice for block quotes")
    build.add_argument("--out")
    build.add_argument("--no-footnotes", action="store_true")
    build.add_argument("--no-summaries", action="store_true")
    build.set_defaults(func=cmd_build)

    add = sub.add_parser("add", help="ingest a file, .eml or URL into the library and queue a build")
    add.add_argument("source")
    add.add_argument("--adapter")
    add.add_argument("--no-build", action="store_true", help="store it without queueing audio")
    add.set_defaults(func=cmd_add)

    library = sub.add_parser("library", help="list stored articles, newest first")
    library.add_argument("--series", action="store_true", help="list newsletters instead")
    library.add_argument("--of", help="only this series")
    library.add_argument("--status", choices=["new", "queued", "building", "ready", "failed"])
    library.add_argument("--limit", type=int, default=40)
    library.set_defaults(func=cmd_library)

    find = sub.add_parser("search", help="full-text search every article")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=20)
    find.set_defaults(func=cmd_search)

    worker = sub.add_parser("worker", help="process queued builds")
    worker.add_argument("--once", action="store_true", help="run a single job and exit")
    worker.set_defaults(func=cmd_worker)

    mail = sub.add_parser("mail", help="fetch unread newsletters from a mailbox over IMAP")
    mail.add_argument("--limit", type=int, default=50)
    mail.set_defaults(func=cmd_mail)

    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
