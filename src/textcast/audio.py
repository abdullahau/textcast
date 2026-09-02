"""Synthesis and encoding.

Audio is built one block at a time and encoded one file per *section*. Three
things follow from that: playback can start on section one while section four
is still rendering, a failed block re-renders in seconds, and every block's
duration comes back from the engine for free — which is the timing map the
read-along player needs, with no forced aligner.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .document import OPTIONAL_KINDS, Article, Block, BlockKind
from .tts import TTSEngine, silence

ProgressFn = Callable[[int, int, str], None]


class EncodeError(RuntimeError):
    pass


@dataclass
class BlockTiming:
    """Where one block sits inside its section's audio file.

    ``dur_ms`` includes the pause that follows the block, so the timeline has
    no gaps and the player can find the current block with one bisect.
    """

    id: str
    kind: str
    start_ms: int
    dur_ms: int
    speech_ms: int


@dataclass
class SectionAudio:
    idx: int
    title: str
    file: str
    duration_ms: int
    #: WebVTT metadata track carrying this section's block timings.
    track: str = ""
    blocks: list[BlockTiming] = field(default_factory=list)


@dataclass
class AudioManifest:
    engine: str
    voice: str
    sample_rate: int
    bitrate: str
    total_ms: int
    sections: list[SectionAudio] = field(default_factory=list)
    included_kinds: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise EncodeError("ffmpeg is not on PATH; it is required to encode Opus")
    return path


def encode_opus(samples: np.ndarray, sample_rate: int, out: Path, bitrate: str = "32k") -> None:
    """Encode mono float32 to Opus in an Ogg container, via a pipe."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", bitrate, "-vbr", "on",
        "-application", "audio", "-frame_duration", "60",
        str(out),
    ]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(samples, dtype=np.float32).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise EncodeError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")


def _cache_key(text: str, engine: str, voice: str, speed: float = 1.0) -> str:
    # Speed leaves the field empty at 1.0, so everything cached before the
    # setting existed is still a hit.
    rate = "" if speed == 1.0 else f"{speed:g}"
    h = hashlib.sha256(f"{engine}\x00{voice}\x00{rate}\x00{text}".encode())
    return h.hexdigest()


def _speak(
    engine: TTSEngine,
    text: str,
    voice: str,
    lang: str,
    cache_dir: Path | None,
    speed: float = 1.0,
) -> np.ndarray:
    """Synthesise one block, reusing a cached render when the text is unchanged.

    A 30-minute article takes minutes to build. Caching per block means a crash,
    a voice tweak on one section, or a re-run after an edit costs seconds.
    """
    if cache_dir is None:
        return engine.synthesize(text, voice=voice, speed=speed, lang=lang).samples

    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{_cache_key(text, engine.name, voice, speed)}.f32"
    if path.exists():
        try:
            return np.fromfile(path, dtype=np.float32)
        except OSError:
            pass

    samples = engine.synthesize(text, voice=voice, speed=speed, lang=lang).samples
    tmp = path.with_suffix(".part")
    samples.astype(np.float32).tofile(tmp)
    tmp.replace(path)
    return samples


#: Anything below this counts as silence. Kokoro's noise floor sits far under
#: it, and real speech sits far above.
SILENCE_LEVEL = 0.01

#: Left at each end after trimming, so the first consonant keeps its attack.
SILENCE_MARGIN_MS = 40


def trim_silence(
    samples: np.ndarray,
    sample_rate: int,
    level: float = SILENCE_LEVEL,
    margin_ms: int = SILENCE_MARGIN_MS,
) -> np.ndarray:
    """Cut the model's own lead-in and run-out off one block.

    Kokoro returns about 300 ms of silence before the first word and 500 ms
    after the last. Left in, they land inside the block's own timing: seeking
    to a block gave you a third of a second of dead air before it spoke, and
    the pause between two blocks was over a second when 350 ms was asked for.

    Trimming here rather than in the engine keeps the cache holding exactly
    what the model produced, so changing these numbers costs no synthesis.
    """
    if not len(samples):
        return samples
    loud = np.flatnonzero(np.abs(samples) > level)
    if not len(loud):
        # A block that is quiet all through is kept whole; better a long pause
        # than a block that vanishes.
        return samples
    margin = round(sample_rate * margin_ms / 1000)
    start = max(0, int(loud[0]) - margin)
    end = min(len(samples), int(loud[-1]) + margin + 1)
    return samples[start:end]


def vtt_timestamp(ms: int) -> str:
    hours, rest = divmod(max(0, ms), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def write_vtt(timings: list[BlockTiming], out: Path) -> None:
    """Write the timing map as a WebVTT metadata track.

    WebVTT is the web standard for text aligned to a media timeline, so the
    browser does the lookup itself and fires ``cuechange`` as each block starts.
    That removes the hand-rolled search the player would otherwise need, and
    with it a whole class of drift and off-by-one bugs.

    The cue id is the block's DOM id, so the player highlights with one lookup.
    """
    lines = ["WEBVTT", ""]
    for timing in timings:
        # A millisecond short of the next cue. Ending exactly where the next
        # begins makes the browser call both active at the boundary, and then
        # which one is "the" cue is a coin toss the player has to break.
        end = timing.start_ms + max(1, timing.dur_ms - 1)
        lines += [
            timing.id,
            f"{vtt_timestamp(timing.start_ms)} --> {vtt_timestamp(end)}",
            timing.id,
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")


def selected_blocks(article: Article, include: set[BlockKind]) -> Iterable[tuple[int, Block]]:
    for section in article.sections:
        for block in section.blocks:
            if block.kind in OPTIONAL_KINDS and block.kind not in include:
                continue
            yield section.idx, block


def render_article(
    article: Article,
    engine: TTSEngine,
    out_dir: Path,
    *,
    voice: str,
    pool: Sequence[TTSEngine] = (),
    quote_voice: str | None = None,
    speed: float = 1.0,
    bitrate: str = "32k",
    gap_ms: int = 350,
    heading_gap_ms: int = 700,
    include: set[BlockKind] | None = None,
    cache_dir: Path | None = None,
    progress: ProgressFn | None = None,
) -> AudioManifest:
    """Render an article to one Opus file per section, plus a timing map.

    ``speed`` is the reading pace baked into the audio, which is not the same
    thing as the player's speed control: this one changes the file.

    ``pool`` gives extra engine instances to synthesise blocks in parallel.
    Measured on 4 ARM cores, four single-threaded instances beat one
    four-threaded instance by about 11% — the model is bound more by memory
    bandwidth than by cores, so the gain is real but modest.
    """
    include = include if include is not None else set(BlockKind)
    engines = [engine, *pool]
    article.renumber()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Grouped once. Filtering the whole list per section was quadratic, which
    # a long article notices.
    by_section: dict[int, list[Block]] = {}
    total = 0
    for section_idx, block in selected_blocks(article, include):
        by_section.setdefault(section_idx, []).append(block)
        total += 1
    if not total:
        raise ValueError("Article has no blocks to synthesise")

    sample_rate = engine.sample_rate
    manifest = AudioManifest(
        engine=engine.name,
        voice=voice,
        sample_rate=sample_rate,
        bitrate=bitrate,
        total_ms=0,
        included_kinds=sorted(str(k) for k in include),
    )

    done = 0
    counter = threading.Lock()
    for section in article.sections:
        blocks = by_section.get(section.idx)
        if not blocks:
            continue

        def render_block(item: tuple[int, Block]) -> tuple[int, np.ndarray]:
            index, block = item
            # A quote in a second voice reads better than saying "start quote".
            use_quote_voice = block.kind is BlockKind.QUOTE and quote_voice
            block_voice = quote_voice if use_quote_voice else voice
            text = block.spoken(quote_markers=not use_quote_voice)

            # One engine per worker: the pipelines are not safe to share.
            worker = engines[index % len(engines)] if len(engines) > 1 else engine
            samples = _speak(worker, text, block_voice, article.lang, cache_dir, speed)
            # The block starts on its first word, so seeking to it does too.
            samples = trim_silence(samples, sample_rate)

            nonlocal done
            with counter:
                done += 1
                position = done
            if progress:
                progress(position, total, block.id)
            return index, samples

        work_items = list(enumerate(blocks))
        if len(engines) > 1 and len(work_items) > 1:
            with ThreadPoolExecutor(max_workers=len(engines)) as executor:
                rendered = dict(executor.map(render_block, work_items))
        else:
            rendered = dict(render_block(item) for item in work_items)

        # Lay the audio out first, then decide where the cues fall in it.
        chunks: list[np.ndarray] = []
        speech_at: list[int] = []
        speech_for: list[int] = []
        pause_after: list[int] = []
        cursor = 0
        for index, block in work_items:
            samples = rendered[index]
            speech_ms = round(len(samples) / sample_rate * 1000)
            pause = heading_gap_ms if block.kind is BlockKind.HEADING else gap_ms

            chunks.append(samples)
            chunks.append(silence(sample_rate, pause))
            speech_at.append(cursor)
            speech_for.append(speech_ms)
            pause_after.append(pause)
            cursor += speech_ms + pause

        # A block begins in the middle of the silence in front of it, not on
        # its first syllable. Landing exactly on the attack means anything
        # slow — a decoder still spinning up, a browser a frame behind — starts
        # you inside the first word. Half a gap of run-up costs nothing to
        # listen to and absorbs all of it.
        starts = [
            0 if i == 0 else speech_at[i] - pause_after[i - 1] // 2
            for i in range(len(work_items))
        ]
        timings: list[BlockTiming] = []
        for i, (_index, block) in enumerate(work_items):
            end = starts[i + 1] if i + 1 < len(starts) else cursor
            timings.append(
                BlockTiming(
                    id=block.id,
                    kind=str(block.kind),
                    start_ms=starts[i],
                    dur_ms=end - starts[i],
                    speech_ms=speech_for[i],
                )
            )

        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        stem = f"section-{section.idx:03d}"
        encode_opus(audio, sample_rate, out_dir / f"{stem}.opus", bitrate=bitrate)
        write_vtt(timings, out_dir / f"{stem}.vtt")

        manifest.sections.append(
            SectionAudio(
                idx=section.idx,
                title=section.title,
                file=f"{stem}.opus",
                track=f"{stem}.vtt",
                duration_ms=round(len(audio) / sample_rate * 1000),
                blocks=timings,
            )
        )

    manifest.total_ms = sum(s.duration_ms for s in manifest.sections)
    (out_dir / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def encode_opus_bytes(samples: np.ndarray, sample_rate: int, bitrate: str = "48k") -> bytes:
    """Encode to Opus in memory, for the pronunciation preview.

    Ogg streams to a pipe, so nothing has to touch the disk for a two-second
    sample.
    """
    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", bitrate, "-vbr", "on", "-application", "audio",
        "-f", "ogg", "pipe:1",
    ]
    proc = subprocess.run(
        cmd,
        input=np.ascontiguousarray(samples, dtype=np.float32).tobytes(),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise EncodeError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    return proc.stdout
