"""Run with: streamlit run agentos/dashboard.py"""

import json
import sys
from pathlib import Path

# Streamlit executes this file as a script rather than a package module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from agentos.config import Config
from agentos.store import Store
from agentos.tools import contained

st.set_page_config(page_title="课程资料 AgentOS", layout="wide")
st.title("课程资料 AgentOS")
config = Config.from_env()
config.prepare()
store = Store(config.data_dir / "agentos.sqlite3")
st.caption(f"资料目录：{config.input_dir} · 生成模式：{config.provider}")
if config.provider == "mock":
    st.info("当前为离线演示模式。演示内容用于验证系统流程，正式整理请配置模型接口。")
st.caption("后台运行 agentos watch 后自动处理资料；页面每 2 秒刷新。")


@st.fragment(run_every="2s")
def monitor():
    tasks = store.tasks()
    labels = {
        "ready": "就绪",
        "running": "运行",
        "waiting": "等待重试",
        "completed": "完成",
        "failed": "失败",
    }
    columns = st.columns(5)
    for column, (state, label) in zip(columns, labels.items()):
        column.metric(label, sum(t["state"] == state for t in tasks))
    if not tasks:
        st.info("尚无任务。将文本型 PDF 放入资料目录，然后启动后台监听。")
        return
    st.dataframe(
        [
            {
                "任务编号": t["id"],
                "资料": Path(t["source"]).name,
                "状态": labels[t["state"]],
                "阶段": t["stage"],
                "重试次数": t["retries"],
                "排队序号": t["queue_order"],
                "错误": t["error"],
            }
            for t in tasks
        ],
        use_container_width=True,
        hide_index=True,
    )
    task_id = st.selectbox(
        "查看任务",
        [t["id"] for t in tasks],
        format_func=lambda value: next(
            f"{Path(t['source']).name} · {value[:8]}" for t in tasks if t["id"] == value
        ),
    )
    task = store.get(task_id)
    st.caption(f"内容版本：{task['content_hash']} · 处理配置：{task['signature'][:12]}")
    if task["signature"] != config.signature:
        st.warning("该任务使用其他处理配置；请使用创建任务时的配置启动工作进程。")
    calls = store.calls(task_id)
    st.write(
        f"模型调用次数：{sum(c['tool'].startswith('model:') for c in calls)} · "
        f"Token 用量：{sum(c['tokens'] for c in calls)} · "
        f"工具累计耗时：{sum(c['duration'] or 0 for c in calls):.2f} 秒"
    )
    if task["error"]:
        st.error(task["error"])
    if task["state"] == "failed" and st.button("从检查点重试", key=task_id):
        store.retry(task_id)
        st.rerun()
    if task["state"] == "completed":
        try:
            path = contained(task["result"], config.data_dir / "results")
            raw = path.read_text()
            result = json.loads(raw)
            st.subheader("摘要与知识点")
            for index, note in enumerate(result["notes"], 1):
                st.markdown(f"**第 {index} 部分**")
                st.markdown(note)
            st.subheader("复习题与参考答案")
            st.markdown(result["questions"])
            st.download_button("下载整理结果 JSON", raw, f"{task_id}.json", "application/json")
        except (OSError, ValueError) as exc:
            st.error(f"结果读取失败：{exc}")
    with st.expander("工具调用记录"):
        st.dataframe(calls, use_container_width=True, hide_index=True)


monitor()
