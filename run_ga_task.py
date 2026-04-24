import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMP_DIR = ROOT / 'temp'
AGENT = ROOT / 'agentmain.py'


def stream_file(path: Path, stop_event: threading.Event, label: str):
    pos = 0
    while not stop_event.is_set():
        if path.exists():
            with path.open('r', encoding='utf-8', errors='ignore') as f:
                f.seek(pos)
                data = f.read()
                pos = f.tell()
            if data:
                print(f'\n[{label}]\n{data}', end='')
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Run GenericAgent task with live logs')
    parser.add_argument('--name', help='task name')
    parser.add_argument('--input', help='task prompt')
    parser.add_argument('--follow', action='store_true', help='follow logs after start')
    args = parser.parse_args()

    task_name = args.name or f'task_{int(time.time())}'
    prompt = args.input
    if not prompt:
        print('请输入任务内容，结束请按回车：')
        prompt = input('> ').strip()
    if not prompt:
        print('任务为空，退出。')
        return

    task_dir = TEMP_DIR / task_name
    task_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(AGENT), '--task', task_name, '--input', prompt, '--bg']
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    print('Task directory:', task_dir)
    print('PID:', result.stdout.strip())

    if not args.follow:
        print('后台已启动。加 --follow 可持续看日志。')
        return

    stop_event = threading.Event()
    watchers = [
        threading.Thread(target=stream_file, args=(task_dir / 'stdout.log', stop_event, 'stdout'), daemon=True),
        threading.Thread(target=stream_file, args=(task_dir / 'stderr.log', stop_event, 'stderr'), daemon=True),
        threading.Thread(target=stream_file, args=(task_dir / 'output.txt', stop_event, 'output'), daemon=True),
        threading.Thread(target=stream_file, args=(task_dir / 'output1.txt', stop_event, 'output1'), daemon=True),
    ]
    for w in watchers:
        w.start()

    print("已进入跟踪模式。输入内容并回车可写入 reply.txt 继续下一轮，输入 /exit 退出跟踪。")
    try:
        while True:
            user = input('\nreply> ').strip()
            if user == '/exit':
                break
            if user:
                (task_dir / 'reply.txt').write_text(user, encoding='utf-8')
                print('已写入 reply.txt')
    finally:
        stop_event.set()


if __name__ == '__main__':
    main()
