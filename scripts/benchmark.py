"""Reproducible cache comparison. Uses mock unless the shell explicitly selects openai."""

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pymupdf

from agentos.config import Config
from agentos.service import Service


def run(config, label):
    service = Service(config)
    started = time.perf_counter()
    before = {c["id"] for task in service.store.tasks() for c in service.store.calls(task["id"])}
    submissions = service.scan()
    service.drain()
    calls = [
        c
        for t in service.store.tasks()
        for c in service.store.calls(t["id"])
        if c["id"] not in before and c["tool"].startswith("model:")
    ]
    return {
        "scenario": label,
        "seconds": round(time.perf_counter() - started, 4),
        "reused": sum(reused for _, reused in submissions),
        "model_calls": len(calls),
        "tokens": sum(c["tokens"] for c in calls),
        "completed": sum(t["state"] == "completed" for t in service.store.tasks()),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="agentos-benchmark-") as folder:
        root = Path(folder)
        materials = root / "materials"
        materials.mkdir()
        config = replace(Config.from_env(), input_dir=materials, data_dir=root / "cache")
        for index in range(3):
            with pymupdf.open() as doc:
                page = doc.new_page()
                page.insert_text((72, 72), f"Chapter {index}: FIFO, checkpoints, and recovery.")
                doc.save(materials / f"chapter-{index}.pdf")
        rows = [
            run(config, "initial"),
            run(config, "unchanged_cached"),
            run(replace(config, data_dir=root / "baseline"), "unchanged_full_baseline"),
        ]
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((72, 72), "Updated chapter: retry queues release the execution slot.")
            doc.save(materials / "chapter-1.pdf")
        rows.append(run(config, "one_modified"))
        print(
            json.dumps({"provider": config.provider, "results": rows}, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
