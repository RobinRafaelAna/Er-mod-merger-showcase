"""ChatterboxEngine batch protocol and crash recovery, with a fake driver instead of the model.

The real driver (core/localisation/rvc_scripts/er_chatterbox.py) runs Chatterbox in its
own venv. Here a tiny script speaks the same stdout protocol, so the restart logic can
be tested without a GPU.
"""
import sys
from pathlib import Path

from core.localisation import chatterbox_engine as ce

FAKE_DRIVER = r'''
import json, sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("MODEL_LOADED|cpu", flush=True)
for job in manifest["jobs"]:
    if job["text"] == "CRASH":
        sys.exit(1)                      # hard crash, no RESULT line
    Path(job["out"]).write_bytes(b"RIFF")
    print(f"RESULT|ok|{job['out']}", flush=True)
'''


def make_engine(tmp_path, monkeypatch) -> ce.ChatterboxEngine:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "er_chatterbox.py").write_text(FAKE_DRIVER, encoding="utf-8")
    monkeypatch.setattr(ce, "_SCRIPTS_DIR", scripts)
    monkeypatch.setattr(ce.ChatterboxEngine, "_python", property(lambda self: Path(sys.executable)))
    engine = ce.ChatterboxEngine()
    engine._venv = tmp_path          # marks the toolchain as available
    return engine


def test_all_jobs_succeed_in_one_driver_run(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    jobs = [(f"line {i}", tmp_path / "ref.wav", tmp_path / f"out{i}.wav") for i in range(3)]
    results = engine.generate_batch(jobs)
    assert all(results.values()) and engine.restarts == 0


def test_a_line_that_kills_the_driver_is_retried_once_then_skipped(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    ok1, bad, ok2 = (tmp_path / n for n in ("a.wav", "bad.wav", "b.wav"))
    ref = tmp_path / "ref.wav"
    progress = []
    results = engine.generate_batch([("first", ref, ok1), ("CRASH", ref, bad), ("last", ref, ok2)],
                                    on_progress=lambda done, total: progress.append(done))
    assert results == {ok1: True, bad: False, ok2: True}
    assert engine.restarts == 2          # crash, retry crash, then the rest of the batch
    assert progress[-1] == 3


def test_reported_success_without_a_file_counts_as_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch)
    (tmp_path / "scripts" / "er_chatterbox.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "m=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "print('MODEL_LOADED|cpu')\n"
        "[print(f\"RESULT|ok|{j['out']}\") for j in m['jobs']]\n", encoding="utf-8")
    out = tmp_path / "never_written.wav"
    assert engine.generate_batch([("x", tmp_path / "ref.wav", out)]) == {out: False}
