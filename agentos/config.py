import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "course-v1"


@dataclass(frozen=True)
class Config:
    input_dir: Path = Path("var/materials")
    data_dir: Path = Path("var/data")
    provider: str = "mock"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    question_count: int = 5
    timeout: float = 60
    max_retries: int = 3
    retry_delay: float = 5
    chunk_chars: int = 10000
    max_chars: int = 200000
    max_file_bytes: int = 50 * 1024 * 1024
    max_calls: int = 40
    max_output_tokens: int = 2000
    settle_seconds: float = 1

    def __post_init__(self):
        object.__setattr__(self, "input_dir", self.input_dir.resolve())
        object.__setattr__(self, "data_dir", self.data_dir.resolve())
        if self.provider not in {"mock", "openai"}:
            raise ValueError("AGENTOS_PROVIDER 必须为 mock 或 openai")
        if self.input_dir == self.data_dir or self.data_dir.is_relative_to(self.input_dir):
            raise ValueError("数据目录不能位于监听目录内")
        for name in (
            "question_count",
            "timeout",
            "chunk_chars",
            "max_chars",
            "max_calls",
            "max_file_bytes",
            "max_output_tokens",
            "retry_delay",
            "settle_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.max_retries < 0:
            raise ValueError("max_retries 不能为负数")

    @classmethod
    def from_env(cls):
        return cls(
            input_dir=Path(os.getenv("AGENTOS_INPUT", "var/materials")),
            data_dir=Path(os.getenv("AGENTOS_DATA", "var/data")),
            provider=os.getenv("AGENTOS_PROVIDER", "mock"),
            model=os.getenv("AGENTOS_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("OPENAI_API_KEY", ""),
            question_count=int(os.getenv("AGENTOS_QUESTION_COUNT", "5")),
            timeout=float(os.getenv("AGENTOS_TIMEOUT", "60")),
            max_retries=int(os.getenv("AGENTOS_MAX_RETRIES", "3")),
            retry_delay=float(os.getenv("AGENTOS_RETRY_DELAY", "5")),
        )

    def requirements(self):
        # Credentials and scheduling parameters do not affect generated content.
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "question_count": self.question_count,
            "prompt_version": PROMPT_VERSION,
            "chunk_chars": self.chunk_chars,
            "max_output_tokens": self.max_output_tokens,
        }

    @property
    def signature(self):
        return hashlib.sha256(json.dumps(self.requirements(), sort_keys=True).encode()).hexdigest()

    def prepare(self):
        self.input_dir.mkdir(parents=True, exist_ok=True)
        for folder in (self.data_dir, self.data_dir / "snapshots", self.data_dir / "results"):
            folder.mkdir(parents=True, exist_ok=True)
