import pytest

from agentos.config import Config
from agentos.store import Store


@pytest.fixture
def setup(tmp_path):
    config = Config(input_dir=tmp_path / "in", data_dir=tmp_path / "data", retry_delay=0.01)
    config.prepare()
    return config, Store(config.data_dir / "agentos.sqlite3")
