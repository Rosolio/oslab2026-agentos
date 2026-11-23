import json

from .tools import Tools, TransientError


class Runtime:
    def __init__(self, config, store, tools=None):
        self.config = config
        self.store = store
        self.tools = tools or Tools(config, store)

    def run(self, task):
        task_id = task["id"]
        data = json.loads(task["checkpoint"])
        try:
            # Progress is inferred from durable artifacts, not only the stage label.
            if "text" not in data:
                data["text"] = self.tools.extract(task)
                self.store.checkpoint(task_id, "notes", data)
            chunks = [
                data["text"][i : i + self.config.chunk_chars]
                for i in range(0, len(data["text"]), self.config.chunk_chars)
            ]
            notes = data.setdefault("notes", [])
            for chunk in chunks[len(notes) :]:
                notes.append(self.tools.generate(task_id, "notes", chunk))
                self.store.checkpoint(task_id, "notes", data)
            if "questions" not in data:
                self.store.checkpoint(task_id, "questions", data)
                # Distribute the context budget evenly across all chunk notes.
                budget = self.config.chunk_chars // max(1, len(notes))
                context = "\n\n".join(note[:budget] for note in notes)
                data["questions"] = self.tools.generate(task_id, "questions", context)
                self.store.checkpoint(task_id, "publish", data)
            path = self.tools.publish(task, data)
            self.store.finish(task_id, path)
        except Exception as exc:  # noqa: BLE001 -- contain failures at the task boundary
            self.store.fail(task_id, exc, isinstance(exc, (TransientError, OSError)), self.config)
