import hashlib
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf
import pytest

from agentos.runtime import Runtime
from agentos.service import Service, worker_lock
from agentos.store import Store
from agentos.tools import PermanentError, Tools, TransientError, ingest


def pdf(config, name="course.pdf", text="FIFO scheduling saves task checkpoints."):
    path = config.input_dir / name
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        doc.save(path)
    return path


def test_full_pipeline_cache_and_versions(setup):
    config, store = setup
    source = pdf(config)
    task_id, reused = ingest(config, store, source)
    assert not reused
    # Same bytes at another path reuse the same TCB, without duplicate model calls.
    alias = config.input_dir / "copy.pdf"
    alias.write_bytes(source.read_bytes())
    assert ingest(config, store, alias) == (task_id, True)
    Runtime(config, store).run(store.claim(config.signature))
    task = store.get(task_id)
    assert task["state"] == "completed"
    assert len([c for c in store.calls(task_id) if c["tool"].startswith("model:")]) == 2
    assert ingest(config, store, source) == (task_id, True)
    result = json.loads(Path(task["result"]).read_text())
    assert result["content_hash"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(result["notes"]) == 1
    source.unlink()
    pdf(config, text="Round robin is another scheduling policy.")
    new_id, reused = ingest(config, store, source)
    assert new_id != task_id and not reused
    other = replace(config, question_count=8)
    assert ingest(other, store, source)[0] != new_id


def test_snapshot_is_independent_of_mutated_source(setup):
    config, store = setup
    source = pdf(config)
    task_id, _ = ingest(config, store, source)
    source.write_bytes(b"new invalid version")
    Runtime(config, store).run(store.claim(config.signature))
    assert store.get(task_id)["state"] == "completed"


def test_waiting_rejoins_fifo_tail_and_retry_limit(setup):
    config, store = setup
    ids = [ingest(config, store, pdf(config, f"{i}.pdf", f"Chapter {i}"))[0] for i in range(3)]
    task = store.claim(config.signature)
    assert task["id"] == ids[0]
    store.fail(ids[0], "timeout", True, config)
    due = store.get(ids[0])["retry_at"]
    assert store.claim(config.signature, due + 1)["id"] == ids[1]
    assert store.claim(config.signature, due + 1)["id"] == ids[2]
    assert store.claim(config.signature, due + 1)["id"] == ids[0]
    for _ in range(config.max_retries):
        store.fail(ids[0], "timeout", True, config)
    assert store.get(ids[0])["state"] == "failed"
    assert store.retry(ids[0])
    assert store.get(ids[0])["retries"] == 0


def test_crash_resumes_without_repeating_notes(setup):
    config, store = setup
    task_id, _ = ingest(config, store, pdf(config))

    class Crash(Tools):
        def generate(self, task_id, kind, text):
            if kind == "questions":
                raise KeyboardInterrupt("simulated hard exit")
            return super().generate(task_id, kind, text)

    with pytest.raises(KeyboardInterrupt):
        Runtime(config, store, Crash(config, store)).run(store.claim(config.signature))
    assert store.get(task_id)["state"] == "running"
    assert len(json.loads(store.get(task_id)["checkpoint"])["notes"]) == 1
    reopened = Store(store.path)
    reopened.recover()
    Runtime(config, reopened).run(reopened.claim(config.signature))
    assert reopened.get(task_id)["state"] == "completed"
    assert [c["tool"] for c in reopened.calls(task_id)].count("model:notes") == 1


def test_runtime_transient_error_preserves_checkpoint(setup):
    config, store = setup
    task_id, _ = ingest(config, store, pdf(config))

    class Timeout(Tools):
        def generate(self, task_id, kind, text):
            if kind == "questions":
                raise TransientError("timeout")
            return super().generate(task_id, kind, text)

    Runtime(config, store, Timeout(config, store)).run(store.claim(config.signature))
    task = store.get(task_id)
    assert task["state"] == "waiting" and task["stage"] == "questions"
    Runtime(config, store).run(store.claim(config.signature, task["retry_at"] + 1))
    assert store.get(task_id)["state"] == "completed"
    assert [c["tool"] for c in store.calls(task_id)].count("model:notes") == 1


def test_bad_input_bounds_and_budget(setup, tmp_path):
    config, store = setup
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"pdf")
    symlink = config.input_dir / "escape.pdf"
    symlink.symlink_to(outside)
    with pytest.raises(PermanentError, match="超出"):
        ingest(config, store, symlink)
    broken = config.input_dir / "bad.pdf"
    broken.write_bytes(b"not a pdf")
    task_id, _ = ingest(config, store, broken)
    Runtime(config, store).run(store.claim(config.signature))
    assert store.get(task_id)["state"] == "failed"
    assert store.get(task_id)["retries"] == 0
    limited = replace(config, max_calls=1)
    task_id, _ = ingest(limited, store, pdf(config))
    Runtime(limited, store).run(store.claim(limited.signature))
    assert store.get(task_id)["state"] == "failed"
    assert "预算" in store.get(task_id)["error"]


def test_single_worker_lock(setup):
    config, _ = setup
    with worker_lock(config), pytest.raises(RuntimeError, match="已有"), worker_lock(config):
        pass


def test_startup_scan_recovers_offline_additions(setup):
    config, store = setup
    pdf(config)
    service = Service(config)
    service.scan()
    service.scan()
    assert len(store.tasks()) == 1
    service.drain()
    assert store.tasks()[0]["state"] == "completed"


def test_real_http_adapter_success_and_retry(setup):
    config, store = setup
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if len(requests) == 1:
                self.send_response(429)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [{"message": {"content": "课程结果"}}],
                        "usage": {"total_tokens": 12},
                    }
                ).encode()
            )

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        real = replace(
            config,
            provider="openai",
            api_key="test-secret",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        task_id, _ = ingest(real, store, pdf(config))
        runtime = Runtime(real, store)
        runtime.run(store.claim(real.signature))
        task = store.get(task_id)
        assert task["state"] == "waiting"
        runtime.run(store.claim(real.signature, task["retry_at"] + 1))
        assert store.get(task_id)["state"] == "completed"
        assert sum(c["tokens"] for c in store.calls(task_id)) == 24
        assert "test-secret" not in str(store.calls(task_id))
        assert all(r["max_tokens"] == config.max_output_tokens for r in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_process_kill_and_restart(setup):
    import subprocess
    import sys

    config, store = setup
    task_id, _ = ingest(config, store, pdf(config))
    program = """
import os, signal, sys
from pathlib import Path
from agentos.config import Config
from agentos.store import Store
from agentos.runtime import Runtime
from agentos.tools import Tools
config = Config(input_dir=Path(sys.argv[1]), data_dir=Path(sys.argv[2]))
store = Store(config.data_dir / "agentos.sqlite3")
class Kill(Tools):
    def generate(self, task_id, kind, text):
        if kind == "questions":
            with self.record(task_id, "model:questions"):
                os.kill(os.getpid(), signal.SIGKILL)
        return super().generate(task_id, kind, text)
Runtime(config, store, Kill(config, store)).run(store.claim(config.signature))
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(config.input_dir), str(config.data_dir)],
        timeout=15,
        check=False,
    )
    assert child.returncode < 0
    assert store.get(task_id)["state"] == "running"
    store.recover()
    Runtime(config, store).run(store.claim(config.signature))
    assert store.get(task_id)["state"] == "completed"
    calls = store.calls(task_id)
    assert [c["tool"] for c in calls].count("model:notes") == 1
    assert any(c["status"] == "interrupted" for c in calls)


def test_watchdog_handles_live_addition(setup):
    import time

    config, store = setup
    service = Service(replace(config, settle_seconds=0.05))
    thread = threading.Thread(target=service.watch, daemon=True)
    thread.start()
    try:
        pdf(config, "live.pdf")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            tasks = store.tasks()
            if tasks and tasks[0]["state"] == "completed":
                break
            time.sleep(0.05)
        else:
            pytest.fail("监听器未自动完成新增 PDF")
        assert len(store.tasks()) == 1
    finally:
        service.stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
