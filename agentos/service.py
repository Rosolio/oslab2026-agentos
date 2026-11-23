"""Event delivery only queues paths; ingestion and processing happen off the observer thread."""

import fcntl
import logging
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .runtime import Runtime
from .store import Store
from .tools import PermanentError, TransientError, ingest

log = logging.getLogger(__name__)


@contextmanager
def worker_lock(config):
    # Prevent a second process from recovering/claiming the first worker's task.
    with (config.data_dir / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有后台工作进程运行，请勿重复启动") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Service:
    def __init__(self, config):
        config.prepare()
        self.config = config
        self.store = Store(config.data_dir / "agentos.sqlite3")
        self.runtime = Runtime(config, self.store)
        self.events = queue.Queue()
        self.stop = threading.Event()

    def scan(self):
        results = []
        for path in sorted(self.config.input_dir.rglob("*")):
            if path.suffix.lower() != ".pdf":
                continue
            try:
                results.append(ingest(self.config, self.store, path))
            except (OSError, PermanentError, TransientError) as exc:
                log.warning("跳过 %s: %s", path, exc)
        return results

    def drain(self):
        while (task := self.store.claim(self.config.signature)) is not None:
            self.runtime.run(task)

    def _ingestion_loop(self):
        pending = {}
        while not self.stop.wait(0.2):
            while True:
                try:
                    path = self.events.get_nowait()
                except queue.Empty:
                    break
                pending[path] = time.monotonic() + self.config.settle_seconds
            for path, deadline in list(pending.items()):
                if time.monotonic() < deadline:
                    continue
                del pending[path]
                try:
                    ingest(self.config, self.store, path)
                except (OSError, TransientError):
                    if Path(path).exists():
                        pending[path] = time.monotonic() + self.config.settle_seconds
                except PermanentError as exc:
                    log.warning("跳过 %s: %s", path, exc)

    def watch(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        events = self.events

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory or event.event_type not in {"created", "modified", "moved"}:
                    return
                path = getattr(event, "dest_path", "") or event.src_path
                if Path(path).suffix.lower() == ".pdf":
                    events.put(path)

        with worker_lock(self.config):
            self.store.recover()
            observer = Observer()
            observer.schedule(Handler(), str(self.config.input_dir), recursive=True)
            observer.start()
            ingestion = threading.Thread(target=self._ingestion_loop, daemon=True)
            ingestion.start()
            try:
                self.scan()
                log.info("监听目录: %s（provider=%s）", self.config.input_dir, self.config.provider)
                while not self.stop.is_set():
                    task = self.store.claim(self.config.signature)
                    if task:
                        self.runtime.run(task)
                    else:
                        self.stop.wait(0.25)
            except KeyboardInterrupt:
                log.info("停止监听；已保存的检查点将在下次启动时继续")
            finally:
                self.stop.set()
                observer.stop()
                observer.join()
                ingestion.join()
