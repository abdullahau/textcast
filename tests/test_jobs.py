"""The build worker: the two job kinds, and what runs on which thread."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import numpy as np
import pytest

from textcast import db
from textcast.jobs import Worker
from textcast.service import ingest
from textcast.tts.base import Clip, Voice


class FakeEngine:
    name = "fake"
    sample_rate = 24000

    def voices(self):
        return [Voice(id="v1", name="One")]

    def synthesize(self, text, voice=None, speed=1.0, lang="en"):
        n = max(1, len(text) // 10) * self.sample_rate
        return Clip(samples=np.zeros(n, dtype=np.float32), sample_rate=self.sample_rate)


LONG_NOTE = "# One\n\n" + "\n\n".join(f"Paragraph number {n} of the first section." for n in range(8))


def stub_pool(worker: Worker, count: int = 3) -> None:
    """Replace the model with a pool of cheap fakes."""
    worker.engines_for = lambda name: [FakeEngine() for _ in range(count)]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_parallel_build_can_report_its_progress(conn, settings):
    """Progress is written from the render pool, not from the worker thread.

    A sqlite3 connection may only be used by the thread that opened it, so
    passing the worker's connection into the callback failed every build that
    ran more than one engine.
    """
    stored = ingest(text=LONG_NOTE, title="A long note")
    worker = Worker(settings)
    stub_pool(worker)

    assert worker.step() is True

    job = db.get_job(stored.job_id, conn)
    assert job["state"] == "done", job["error"]
    assert db.get_article(stored.article_id, conn)["status"] == "ready"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_summarise_job_writes_the_blocks_and_stops_there(conn, settings, monkeypatch):
    from textcast import summarize
    from textcast.document import BlockKind

    summarize.save_credential("K", provider="gemini", api_key="k", conn=conn)
    summarize.save_config(conn, credential_name="K", model="stub-1")
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="In short."))])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply))
    )
    monkeypatch.setattr(summarize, "_client", lambda cfg: client)

    stored = ingest(text=LONG_NOTE, title="A long note", options={"summarize": True})
    worker = Worker(settings)
    stub_pool(worker)

    assert worker.step() is True, "the summarise job"
    article = db.load_article(stored.article_id, conn)
    assert article.sections[0].blocks[0].kind is BlockKind.SUMMARY

    # Building is a separate decision, made on the article page.
    assert worker.step() is False, "no build was queued behind it"
    assert db.active_jobs(conn) == []
    assert db.get_article(stored.article_id, conn)["status"] == "new"


def test_a_partly_summarised_article_keeps_what_arrived(conn, settings, monkeypatch):
    """One refused call used to throw away the summaries that had landed
    beside it, and the only record was a line in the worker's log."""
    from textcast import summarize
    from textcast.document import BlockKind

    summarize.save_credential("K", provider="gemini", api_key="k", conn=conn)
    summarize.save_config(conn, credential_name="K", model="stub-1")

    def create(model, messages):
        if "second section" in messages[0]["content"]:
            raise RuntimeError("429 rate limit exceeded")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="In short."))]
        )

    monkeypatch.setattr(
        summarize,
        "_client",
        lambda cfg: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    text = "# One\n\nA paragraph of the first section.\n\n# Two\n\nA paragraph of the second section."
    stored = ingest(text=text, title="Half of it", build=False)
    db.enqueue(stored.article_id, kind="summarise", conn=conn)

    Worker(settings).step(("summarise",))

    article = db.load_article(stored.article_id, conn)
    assert article.sections[0].blocks[0].kind is BlockKind.SUMMARY, "it landed"
    assert article.sections[1].blocks[0].kind is not BlockKind.SUMMARY

    job = conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 1").fetchone()
    assert job["state"] == "failed", "a partial pass is not a clean one"
    assert "1 of 2 sections summarised" in job["error"]
    assert "429" in job["error"], "the reason reaches the page, not just the log"
    assert db.get_article(stored.article_id, conn)["status"] == "new"


def test_an_article_asking_for_a_retired_engine_still_builds(conn, settings, caplog):
    """A build option naming an engine that no longer ships must not be fatal."""
    stored = ingest(text=LONG_NOTE, title="A long note", build=False)
    db.set_build_options(stored.article_id, {"engine": "gone"}, conn)
    db.enqueue(stored.article_id, conn=conn)

    worker = Worker(settings)
    stub_pool(worker, count=1)
    worker.step()

    assert db.get_article(stored.article_id, conn)["status"] == "ready"
    assert "gone" in caplog.text


def test_a_summary_and_a_build_run_at_the_same_time(conn, settings):
    """They use different machines — the network and the CPU — so queueing one
    behind the other bought nothing."""
    first = ingest(text=LONG_NOTE, title="Being built")
    second = ingest(text=LONG_NOTE + "\n\nDifferent.", title="Being summarised", build=False)
    db.enqueue(second.article_id, kind="summarise", conn=conn)

    build = db.claim_job(conn, ("build",))
    summary = db.claim_job(conn, ("summarise",))

    assert build["article_id"] == first.article_id
    assert summary["article_id"] == second.article_id, "the second lane did not wait"


def test_the_two_kinds_never_meet_on_one_article(conn, settings):
    """A summary rewrites the very blocks a build is rendering."""
    stored = ingest(text=LONG_NOTE, title="One article")
    db.enqueue(stored.article_id, kind="summarise", conn=conn)

    db.claim_job(conn, ("summarise",))

    assert db.claim_job(conn, ("build",)) is None, "the build waited its turn"


def test_a_failed_summary_does_not_mark_the_audio_failed(conn, settings, monkeypatch):
    from textcast import summarize

    summarize.save_credential("K", provider="gemini", api_key="k", conn=conn)
    summarize.save_config(conn, credential_name="K", model="stub")
    monkeypatch.setattr(summarize, "_client", lambda cfg: (_ for _ in ()).throw(RuntimeError("no")))
    stored = ingest(text=LONG_NOTE, title="Summary will fail", build=False)
    db.enqueue(stored.article_id, kind="summarise", conn=conn)

    Worker(settings).step(("summarise",))

    assert db.get_article(stored.article_id, conn)["status"] == "new", "the article is as it was"
    job = conn.execute("SELECT state, kind FROM job ORDER BY id DESC LIMIT 1").fetchone()
    assert (job["kind"], job["state"]) == ("summarise", "failed")


# --- the engine pool, and when it is given back --------------------------


def pooled(settings, monkeypatch, count=4):
    """A worker whose pool is cheap fakes, built through the real code path."""
    from textcast import jobs, tts

    # A private slot per test: publishing into the real one leaks a fake
    # engine into every test that runs after.
    monkeypatch.setattr(tts, "_shared", {})
    monkeypatch.setattr(jobs, "get_engine", lambda name, **options: FakeEngine())
    monkeypatch.setattr(settings, "concurrency", count)
    return Worker(settings)


def test_releasing_leaves_an_engine_somebody_else_published(settings, monkeypatch):
    """A web process that built its own must not lose it to the worker."""
    from textcast import tts

    theirs, mine = FakeEngine(), FakeEngine()
    monkeypatch.setattr(tts, "_shared", {"fake": theirs})

    assert tts.release_shared(mine) is False, "it was not ours to give up"
    assert tts.loaded_engine("fake") is theirs


# --- builds run in a child process ---------------------------------------


class FakeChild:
    """A build process that has already finished."""

    def __init__(self, exitcode: int = 0) -> None:
        self.exitcode = exitcode
        self.joined = False
        self.terminated = False

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return not self.joined

    def terminate(self) -> None:
        self.terminated = True


def watched(worker: Worker, child: FakeChild) -> list[tuple]:
    """Hand the worker a child it does not have to spawn."""
    started: list[tuple] = []

    def start(kinds):
        started.append(kinds)
        return child

    worker._start_job_process = start
    return started


def test_nothing_is_spawned_while_the_queue_is_empty(conn, settings):
    """The parent polls a table. Spawning per poll would be a fork bomb."""
    worker = Worker(settings)
    started = watched(worker, FakeChild())

    assert worker._run_in_child(("build",)) is False, "there was nothing to build"
    assert worker._run_in_child(("summarise",)) is False, "nor to summarise"
    assert started == [], "and so nothing was started"


@pytest.mark.parametrize("kind", ["build", "summarise"])
def test_a_queued_job_is_handed_to_a_child_process(conn, settings, kind):
    """The parent imports neither torch nor openai. The children do, and exit."""
    stored = ingest(text=LONG_NOTE, title=f"For the {kind} child", build=False)
    db.enqueue(stored.article_id, kind=kind, conn=conn)
    worker = Worker(settings)
    child = FakeChild()
    started = watched(worker, child)

    assert worker._run_in_child((kind,)) is True, "there was work, so do not sleep"
    assert started == [(kind,)], "one child, for this lane only"
    assert child.joined, "and the lane waited for it"


def test_a_lane_ignores_the_other_lane_s_queue(conn, settings):
    """Two lanes, two children. A summary must not wake the build lane."""
    stored = ingest(text=LONG_NOTE, title="Only a summary", build=False)
    db.enqueue(stored.article_id, kind="summarise", conn=conn)
    worker = Worker(settings)
    started = watched(worker, FakeChild())

    assert worker._run_in_child(("build",)) is False, "nothing to build"
    assert started == [], "so the build lane started nothing"


def test_a_build_process_that_was_killed_leaves_no_job_running(conn, settings):
    """The child marks its own failures. This is the other kind: OOM, SIGKILL."""
    one = ingest(text=LONG_NOTE, title="Killed", build=False)
    two = ingest(text=LONG_NOTE, title="Still queued", build=False)
    db.enqueue(one.article_id, kind="build", conn=conn)
    db.enqueue(two.article_id, kind="build", conn=conn)
    db.claim_job(conn, ("build",))  # the first is now 'running'
    worker = Worker(settings)
    watched(worker, FakeChild(exitcode=-9))

    worker._run_in_child(("build",))

    running = conn.execute("SELECT COUNT(*) c FROM job WHERE state = 'running'").fetchone()
    assert running["c"] == 0, "the killed job went back to the queue"


def test_stopping_takes_the_build_process_with_it(conn, settings):
    """watchfiles restarts this process on every edit to src."""
    worker = Worker(settings)
    child = FakeChild()
    worker._children["build"] = child

    worker.stop(timeout=0.1)

    assert child.terminated, "an orphaned build would hold the model and keep writing"


def switchable(settings, monkeypatch, count=2) -> Worker:
    """A worker whose fakes answer to whatever engine name is asked for."""
    from textcast import jobs, tts

    monkeypatch.setattr(tts, "_shared", {})
    monkeypatch.setattr(settings, "concurrency", count)

    def named(name, **options):
        engine = FakeEngine()
        engine.name = name
        return engine

    monkeypatch.setattr(jobs, "get_engine", named)
    return Worker(settings)


def test_asking_for_the_same_engine_again_reuses_the_pool(settings, monkeypatch):
    """Loading it costs seconds, which is the only reason the pool exists."""
    worker = switchable(settings, monkeypatch)

    assert worker.engines_for("kokoro") is worker.engines_for("kokoro")


def test_a_second_engine_is_refused_inside_one_process(settings, monkeypatch):
    """One process, one engine. Loading the second beside the first is 7.5 GB.

    Measured on a queue that switched engine mid-drain: the child sat at
    1.8 GB on ONNX and reached 3.5 GB the moment the kokoro pool finished
    loading. `step` puts the other engine's jobs back before it ever gets
    here, so this is the backstop, not the mechanism.
    """
    worker = switchable(settings, monkeypatch)
    worker.engines_for("kokoro-onnx")

    with pytest.raises(RuntimeError, match="another one"):
        worker.engines_for("kokoro")


def test_a_job_for_the_other_engine_is_put_back_untouched(conn, settings, monkeypatch):
    """A build process is bound to the engine of the first job it takes."""
    from textcast import db as database

    theirs = ingest(text=LONG_NOTE, title="Theirs", build=False)
    other = database.enqueue(
        theirs.article_id, kind="build", options={"engine": "kokoro-onnx"}, conn=conn
    )
    worker = switchable(settings, monkeypatch)
    worker._engine_name = "kokoro"
    skip: set[int] = set()

    assert worker.step(("build",), skip) is True, "there is work, just not ours"

    row = conn.execute("SELECT state, started_at FROM job WHERE id = ?", (other,)).fetchone()
    assert row["state"] == "queued", "put back for a process loaded with that engine"
    assert row["started_at"] is None, "and left as it was found"
    assert skip == {other}, "and passed over, or it would be claimed for ever"
    article = conn.execute(
        "SELECT status FROM article WHERE id = ?", (theirs.article_id,)
    ).fetchone()
    assert article["status"] == "queued", "the article never started building"


def test_a_released_job_does_not_block_the_ones_behind_it(conn, settings, monkeypatch):
    """`claim_job` takes the oldest, so a skipped job would stall the lane."""
    from textcast import db as database

    theirs = ingest(text=LONG_NOTE, title="Theirs, and first", build=False)
    ours = ingest(text=LONG_NOTE, title="Ours, and second", build=False)
    other = database.enqueue(
        theirs.article_id, kind="build", options={"engine": "kokoro-onnx"}, conn=conn
    )
    database.enqueue(ours.article_id, kind="build", options={"engine": "kokoro"}, conn=conn)
    worker = switchable(settings, monkeypatch)
    worker._engine_name = "kokoro"
    monkeypatch.setattr(worker, "_build", lambda conn, job: None)
    skip: set[int] = set()

    worker.step(("build",), skip)   # the ONNX job, put back
    worker.step(("build",), skip)   # ours, behind it

    ran = conn.execute(
        "SELECT state FROM job WHERE article_id = ?", (ours.article_id,)
    ).fetchone()
    assert ran["state"] == "done", "the job behind the skipped one still ran"
    assert other in skip


def test_a_finished_build_collects_what_it_orphaned(conn, settings):
    """A build is the only thing that makes an orphan.

    An edited paragraph, a re-parse, a new rule or a changed voice all reach
    the audio through a rebuild, so the moment it finishes is the moment the
    old keys become garbage. No timer, and nothing to remember.
    """
    from textcast.audio import CACHE_SUFFIX

    result = ingest(text=LONG_NOTE, title="Sweeps after", build=False)
    db.enqueue(result.article_id, kind="build", conn=conn)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    orphan = settings.cache_dir / f"{'a' * 64}{CACHE_SUFFIX}"
    orphan.write_bytes(b"\x00" * 8)

    worker = Worker(settings)
    watched(worker, FakeChild(exitcode=0))
    worker._run_in_child(("build",))

    assert not orphan.exists()


def test_a_build_that_died_is_not_followed_by_a_sweep(conn, settings):
    """Its jobs go back on the queue, so their renders are still wanted."""
    from textcast.audio import CACHE_SUFFIX

    result = ingest(text=LONG_NOTE, title="Killed mid-build", build=False)
    db.enqueue(result.article_id, kind="build", conn=conn)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    orphan = settings.cache_dir / f"{'b' * 64}{CACHE_SUFFIX}"
    orphan.write_bytes(b"\x00" * 8)

    worker = Worker(settings)
    watched(worker, FakeChild(exitcode=-9))
    worker._run_in_child(("build",))

    assert orphan.exists(), "a killed build is a repair, not a collection"


def test_a_sweep_that_fails_does_not_fail_the_build(conn, settings, monkeypatch):
    """The audio is already written. Losing the collection is not losing it."""
    from textcast import jobs

    result = ingest(text=LONG_NOTE, title="Sweep explodes", build=False)
    db.enqueue(result.article_id, kind="build", conn=conn)

    def boom(*args, **kwargs):
        raise OSError("the disk went away")

    monkeypatch.setattr(jobs, "sweep_cache", boom)
    worker = Worker(settings)
    watched(worker, FakeChild(exitcode=0))

    assert worker._run_in_child(("build",)) is True


def test_a_dying_build_child_leaves_the_summary_beside_it_alone(conn, settings):
    """The two lanes run side by side, and requeueing swept both.

    A build child that was killed put *every* running job back, so the
    summary running next to it started again from the top -- and its article
    was stamped `queued`, which claims audio is on the way for an article
    nobody asked to build. `status` describes the audio and nothing else.
    """
    worker = Worker(settings)
    building = ingest(text="# Building\n\nA paragraph.", title="Building", build=False)
    summarising = ingest(text="# Summarising\n\nA paragraph.", title="Summarising", build=False)

    db.enqueue(building.article_id, kind="build", conn=conn)
    db.enqueue(summarising.article_id, kind="summarise", conn=conn)
    build_job = db.claim_job(conn, ("build",))
    summary_job = db.claim_job(conn, ("summarise",))
    assert build_job and summary_job

    worker._requeue_orphans(("build",))

    assert db.get_job(build_job["id"], conn)["state"] == "queued"
    assert db.get_job(summary_job["id"], conn)["state"] == "running", (
        "the other lane is still working"
    )
    assert db.get_article(summarising.article_id, conn)["status"] != "queued", (
        "a summary does not claim the audio is coming"
    )
