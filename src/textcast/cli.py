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
