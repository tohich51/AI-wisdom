"""Real local children prove byte bounds and cleanup; no provider calls."""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from kb.jobs import model_runner


def test_interleaved_small_writes_are_captured_without_deadlock():
    process = subprocess.Popen(  # noqa: S603 - fixed synthetic Python argv, no user input
        [
            sys.executable,
            "-c",
            "import os\nfor _ in range(4096):\n os.write(1,b'o'*16);os.write(2,b'e'*16)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = model_runner._capture_bounded(
            process, 5, stdout_limit=128 * 1024, stderr_limit=128 * 1024
        )
        assert (out, err) == (b"o" * 65536, b"e" * 65536)
        assert process.returncode == 0
    finally:
        model_runner._stop_cli(process)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("descriptor", [1, 2])
def test_run_cli_refuses_excess_bytes_and_reaps_child(tmp_path, monkeypatch, descriptor):
    launcher = tmp_path / "synthetic-launcher"
    marker = tmp_path / "pid"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        f"while True: os.write({descriptor},b'x'*2048)\n"
    )
    launcher.chmod(0o700)
    capture = model_runner._capture_bounded
    monkeypatch.setattr(
        model_runner,
        "_capture_bounded",
        lambda process, timeout: capture(process, timeout, stdout_limit=1024, stderr_limit=1024),
    )
    with pytest.raises(model_runner._RunnerFailure) as caught:
        model_runner._run_cli("Synthetic bounded source", [uuid4()], str(launcher))
    assert caught.value.reason == "output_too_large"
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_deadline_kills_descendant_even_after_leader_exits():
    code = """import os,signal,sys
rd,wr=os.pipe()
pid=os.fork()
if pid==0:
 os.close(rd)
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 os.write(wr,b'1');os.close(wr)
 signal.pause()
else:
 os.close(wr);os.read(rd,1);os.close(rd)
 print(pid,flush=True)
 sys.exit(0)
"""
    process = subprocess.Popen(  # noqa: S603 - fixed synthetic Python argv, no user input
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    child = int(process.stdout.readline())
    try:
        with pytest.raises(model_runner._RunnerFailure) as caught:
            model_runner._capture_bounded(process, 0.1)
        assert caught.value.reason == "model_unavailable"
    finally:
        model_runner._stop_cli(process)
        process.stdout.close()
        process.stderr.close()
    # A killed orphan may briefly be a zombie until PID 1 reaps it; it cannot run.
    deadline = time.monotonic() + 2
    while True:
        record = Path(f"/proc/{child}/stat")
        try:
            state = record.read_text().split(")", 1)[1].split()[0]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        assert time.monotonic() < deadline, "owned descendant survived cleanup"
        time.sleep(0.01)
