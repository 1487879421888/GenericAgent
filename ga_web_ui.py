import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

ROOT = Path(__file__).resolve().parent
TEMP_DIR = ROOT / 'temp'
AGENT = ROOT / 'agentmain.py'
INDEX_HTML = ROOT / 'ga_web_ui_frontend.html'
PROJECT_CTX = ROOT / 'project_context.txt'
HISTORY_FILE = ROOT / 'prompt_history.json'
HISTORY_MAX = 200  # 后端存 200 条，比 localStorage 更持久

FAST_PREFIX = (
    '【快速模式】直接执行，走最短路径。禁止以下行为：\n'
    '- 不要读 plan_sop / plan_sop.md 等任何 SOP 文件\n'
    '- 不要创建 plan_*/ 目录\n'
    '- 不要调用 update_working_checkpoint\n'
    '- 不要先"规划再执行"，能一步做完就一步做完\n'
    '- 不要反复 web_scan tabs_only，能直接切就直接切\n'
    '\n任务：\n'
)

app = FastAPI()


# -------------------- pydantic --------------------
class StartTask(BaseModel):
    name: str | None = None
    prompt: str
    fast: bool = False
    use_context: bool = True


class ReplyTask(BaseModel):
    reply: str


class CtxPayload(BaseModel):
    content: str


class HistoryPayload(BaseModel):
    text: str
    kind: str = 'new'    # 'new' | 'reply' | 'skill' | 'other'
    task: str | None = None
    fast: bool = False


# -------------------- helpers --------------------
def read_text(path: Path, tail_bytes: int = 200_000) -> str:
    if not path.exists():
        return ''
    try:
        size = path.stat().st_size
        with path.open('rb') as f:
            if size > tail_bytes:
                f.seek(-tail_bytes, os.SEEK_END)
                data = b'...[truncated]...\n' + f.read()
            else:
                data = f.read()
        return data.decode('utf-8', errors='ignore')
    except Exception:
        return ''


def load_project_context() -> str:
    if not PROJECT_CTX.exists():
        return ''
    try:
        return PROJECT_CTX.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(items: list) -> None:
    items = items[:HISTORY_MAX]
    try:
        HISTORY_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=0),
            encoding='utf-8',
        )
    except Exception:
        pass


def push_history(text: str, kind: str = 'new', task: str | None = None, fast: bool = False):
    text = (text or '').strip()
    if not text:
        return
    items = load_history()
    # 同文本去重（保留最新）
    items = [x for x in items if x.get('text') != text]
    items.insert(0, {
        'text': text,
        'kind': kind,
        'task': task,
        'fast': bool(fast),
        'ts': int(time.time()),
    })
    save_history(items)


def is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == 'win32':
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def read_pid(task_dir: Path) -> int:
    p = task_dir / 'pid'
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding='utf-8').strip())
    except Exception:
        return 0


def kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def safe_task_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(400, 'empty task name')
    if '/' in name or '\\' in name or '..' in name or name.startswith('.'):
        raise HTTPException(400, 'bad task name')
    return name


# -------------------- routes --------------------
@app.get('/')
def index():
    if not INDEX_HTML.exists():
        raise HTTPException(404, f'frontend missing: {INDEX_HTML.name}')
    return FileResponse(INDEX_HTML)


@app.get('/api/context')
def get_context():
    return {'content': load_project_context(), 'exists': PROJECT_CTX.exists()}


@app.post('/api/context')
def set_context(payload: CtxPayload):
    content = payload.content
    if not content.strip():
        if PROJECT_CTX.exists():
            try:
                PROJECT_CTX.unlink()
            except Exception:
                pass
        return {'ok': True, 'cleared': True}
    PROJECT_CTX.write_text(content, encoding='utf-8')
    return {'ok': True, 'bytes': len(content.encode('utf-8'))}


@app.get('/api/history')
def get_history():
    return {'items': load_history(), 'max': HISTORY_MAX}


@app.post('/api/history')
def add_history(payload: HistoryPayload):
    push_history(payload.text, payload.kind, payload.task, payload.fast)
    return {'ok': True, 'count': len(load_history())}


@app.delete('/api/history')
def clear_history():
    if HISTORY_FILE.exists():
        try:
            HISTORY_FILE.unlink()
        except Exception:
            pass
    return {'ok': True}


class HistoryDelPayload(BaseModel):
    ts: int
    text: str


@app.post('/api/history/delete')
def delete_history_item(payload: HistoryDelPayload):
    items = load_history()
    items = [x for x in items if not (x.get('ts') == payload.ts and x.get('text') == payload.text)]
    save_history(items)
    return {'ok': True}


@app.post('/api/tasks')
def start_task(payload: StartTask):
    prompt = payload.prompt
    if not prompt.strip():
        raise HTTPException(400, 'empty prompt')

    # 原始输入先入历史（保存的是用户真正打的字，不含 context/fast 前缀）
    push_history(prompt, kind='new', fast=payload.fast)

    if payload.use_context:
        ctx = load_project_context()
        if ctx:
            prompt = f'【项目常驻上下文】\n{ctx}\n\n【本次任务】\n{prompt}'

    if payload.fast:
        prompt = FAST_PREFIX + prompt

    task_name = safe_task_name(payload.name or f'task_{int(time.time())}')
    task_dir = TEMP_DIR / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')

    cmd = [sys.executable, str(AGENT), '--task', task_name, '--input', prompt, '--bg']
    result = subprocess.run(
        cmd, cwd=ROOT,
        capture_output=True, text=True,
        encoding='utf-8', errors='ignore',
    )
    out = (result.stdout or '').strip()
    pid = 0
    for line in reversed(out.splitlines()):
        try:
            pid = int(line.strip())
            break
        except ValueError:
            continue
    if pid:
        (task_dir / 'pid').write_text(str(pid), encoding='utf-8')
    return {
        'task': task_name,
        'pid': pid,
        'stderr': result.stderr,
    }


@app.get('/api/tasks')
def list_tasks():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tasks = []
    for d in sorted(TEMP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        pid = read_pid(d)
        tasks.append({
            'name': d.name,
            'updated_at': int(d.stat().st_mtime),
            'running': is_running(pid),
            'pid': pid,
        })
    return tasks


@app.get('/api/tasks/{task_name}')
def get_task(task_name: str):
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    pid = read_pid(task_dir)
    return {
        'name': task_name,
        'prompt': read_text(task_dir / 'prompt.txt'),
        'stdout': read_text(task_dir / 'stdout.log'),
        'stderr': read_text(task_dir / 'stderr.log'),
        'output': read_text(task_dir / 'output.txt'),
        'output1': read_text(task_dir / 'output1.txt'),
        'reply_exists': (task_dir / 'reply.txt').exists(),
        'running': is_running(pid),
        'pid': pid,
        'updated_at': int(task_dir.stat().st_mtime),
    }


@app.post('/api/tasks/{task_name}/reply')
def reply_task(task_name: str, payload: ReplyTask):
    """智能 reply:
       - agent 还在运行 → 写 reply.txt（给 loop-style agent）
       - agent 已退出   → 把 reply 当作新一轮，追加到 prompt.txt 并重启 agent
    """
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    if not payload.reply.strip():
        raise HTTPException(400, 'empty reply')

    push_history(payload.reply, kind='reply', task=task_name)

    pid = read_pid(task_dir)
    if is_running(pid):
        # Agent 在跑，走 reply.txt 通道
        (task_dir / 'reply.txt').write_text(payload.reply, encoding='utf-8')
        return {'ok': True, 'mode': 'reply', 'pid': pid}

    # Agent 已结束：续写并重启
    prompt_file = task_dir / 'prompt.txt'
    prev_prompt = prompt_file.read_text(encoding='utf-8') if prompt_file.exists() else ''

    ts = int(time.time())
    ts_str = time.strftime('%H:%M:%S', time.localtime(ts))
    sep = f'\n\n─── 续写 @ {ts_str} ───\n'
    new_prompt = (prev_prompt.rstrip() + sep + payload.reply + '\n') if prev_prompt else payload.reply
    prompt_file.write_text(new_prompt, encoding='utf-8')

    # 把上一轮 output/logs 存档（archive_{ts}__xxx），新一轮用干净文件
    for fname in ('output.txt', 'output1.txt', 'stdout.log', 'stderr.log', 'reply.txt'):
        src = task_dir / fname
        if src.exists():
            dst = task_dir / f'archive_{ts}__{fname}'
            try:
                src.rename(dst)
            except Exception:
                pass

    # 启动新一轮
    cmd = [sys.executable, str(AGENT), '--task', task_name, '--input', new_prompt, '--bg']
    result = subprocess.run(
        cmd, cwd=ROOT,
        capture_output=True, text=True,
        encoding='utf-8', errors='ignore',
    )
    out = (result.stdout or '').strip()
    new_pid = 0
    for line in reversed(out.splitlines()):
        try:
            new_pid = int(line.strip())
            break
        except ValueError:
            continue
    if new_pid:
        (task_dir / 'pid').write_text(str(new_pid), encoding='utf-8')
    return {
        'ok': True,
        'mode': 'resume',
        'pid': new_pid,
        'stderr': result.stderr,
    }


@app.post('/api/tasks/{task_name}/kill')
def kill_task(task_name: str):
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    pid = read_pid(task_dir)
    if not is_running(pid):
        return {'ok': True, 'note': 'not running'}
    kill_pid(pid)
    return {'ok': True}


@app.delete('/api/tasks/{task_name}')
def delete_task(task_name: str):
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    if is_running(read_pid(task_dir)):
        raise HTTPException(400, 'task still running, kill it first')
    shutil.rmtree(task_dir, ignore_errors=True)
    return {'ok': True}


if __name__ == '__main__':
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    print(f'GA 控制台: http://127.0.0.1:8799')
    uvicorn.run(app, host='127.0.0.1', port=8799, log_level='warning')