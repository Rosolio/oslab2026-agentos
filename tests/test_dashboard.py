from pathlib import Path

from streamlit.testing.v1 import AppTest
from test_workflow import pdf

from agentos.service import Service


def test_dashboard_empty_and_completed(setup, monkeypatch):
    config, _ = setup
    monkeypatch.setenv("AGENTOS_INPUT", str(config.input_dir))
    monkeypatch.setenv("AGENTOS_DATA", str(config.data_dir))
    monkeypatch.setenv("AGENTOS_PROVIDER", "mock")
    app = AppTest.from_file(Path("agentos/dashboard.py").resolve()).run(timeout=20)
    assert not app.exception
    assert any("尚无任务" in item.value for item in app.info)
    pdf(config)
    service = Service(config)
    service.scan()
    service.drain()
    app.run(timeout=20)
    assert not app.exception
    assert any(item.value == "摘要与知识点" for item in app.subheader)
    assert any("离线演示" in item.value for item in app.markdown)
