import argparse
import json
import logging

from .config import Config
from .service import Service, worker_lock


def main():
    parser = argparse.ArgumentParser(description="课程资料 AgentOS")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch", help="启动监听与后台调度")
    sub.add_parser("run", help="扫描并处理当前就绪任务，等待重试的任务留待后续运行")
    sub.add_parser("status", help="显示任务控制块")
    retry = sub.add_parser("retry", help="将失败任务重新加入队尾")
    retry.add_argument("task_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = Service(Config.from_env())
    if args.command == "watch":
        service.watch()
    elif args.command == "run":
        with worker_lock(service.config):
            service.store.recover()
            service.scan()
            service.drain()
        print(
            json.dumps(
                [
                    {
                        key: task[key]
                        for key in ("id", "source", "state", "stage", "retries", "error")
                    }
                    for task in service.store.tasks()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "status":
        for task in service.store.tasks():
            print(
                f"{task['id']}  {task['state']:10} {task['stage']:10} "
                f"retries={task['retries']}  {task['source']}"
            )
    elif args.command == "retry":
        if not service.store.retry(args.task_id):
            parser.error("任务不存在或不处于失败状态")
        print("已重新入队；启动 watch 或 run 执行。")


if __name__ == "__main__":
    main()
