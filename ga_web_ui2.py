import ast as _ast
import asyncio
import importlib.util as _il_util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

log = logging.getLogger('ga_web_ui')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

ROOT = Path(__file__).resolve().parent
TEMP_DIR = ROOT / 'temp'
AGENT = ROOT / 'agentmain.py'
INDEX_HTML = ROOT / 'ga_web_ui_frontend2.html'
PROJECT_CTX = ROOT / 'project_context.txt'
HISTORY_FILE = ROOT / 'prompt_history.json'
MYKEY_PY = ROOT / 'mykey.py'
HISTORY_MAX = 200

TOKEN_ESTIMATE_DIVISOR = 2.5  # 抄对方的经验值：chars/2.5 ≈ tokens
START_TIMEOUT = 30            # agent 启动 pid 抓取超时

FAST_PREFIX = (
    '【快速模式】直接执行，走最短路径。禁止以下行为：\n'
    '- 不要读 plan_sop / plan_sop.md 等任何 SOP 文件\n'
    '- 不要创建 plan_*/ 目录\n'
    '- 不要调用 update_working_checkpoint\n'
    '- 不要先"规划再执行"，能一步做完就一步做完\n'
    '- 不要反复 web_scan tabs_only，能直接切就直接切\n'
    '\n任务：\n'
)

_history_lock = threading.Lock()

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
    kind: str = 'new'
    task: str | None = None
    fast: bool = False


class HistoryDelPayload(BaseModel):
    ts: int
    text: str


class MykeyPayload(BaseModel):
    configs: list
    extras: dict = {}
    # passthrough 不走前端修改，保存时从磁盘 parse 结果合并回去


class FetchModelsPayload(BaseModel):
    format: str = 'oai_chat'
    apibase: str
    apikey: str = ''


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


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() else 0
    except Exception:
        return 0


def reply_pending(task_dir: Path) -> bool:
    try:
        qfile = task_dir / 'replies.jsonl'
        if qfile.exists() and qfile.stat().st_size > 0:
            return True
    except Exception:
        pass
    return (task_dir / 'reply.txt').exists()


def append_reply(task_dir: Path, reply: str) -> None:
    qfile = task_dir / 'replies.jsonl'
    line = json.dumps({'reply': reply}, ensure_ascii=False) + '\n'
    with qfile.open('a', encoding='utf-8') as f:
        f.write(line)


def append_prompt_turn(task_dir: Path, reply: str, ts: int | None = None) -> None:
    prompt_file = task_dir / 'prompt.txt'
    prev_prompt = prompt_file.read_text(encoding='utf-8') if prompt_file.exists() else ''
    ts = int(time.time()) if ts is None else int(ts)
    ts_str = time.strftime('%H:%M:%S', time.localtime(ts))
    sep = f'\n\n─── 续写 @ {ts_str} ───\n'
    new_prompt = (prev_prompt.rstrip() + sep + reply + '\n') if prev_prompt else (reply.rstrip() + '\n')
    prompt_file.write_text(new_prompt, encoding='utf-8')


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
    except Exception as e:
        log.warning('save_history failed: %s', e)


def push_history(text: str, kind: str = 'new', task: str | None = None, fast: bool = False):
    text = (text or '').strip()
    if not text:
        return
    with _history_lock:
        items = load_history()
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
    except Exception as e:
        log.warning('kill_pid(%s) failed: %s', pid, e)


def safe_task_name(name: str) -> str:
    name = (name or '').strip()
    if not name:
        raise HTTPException(400, 'empty task name')
    if len(name) > 64:
        raise HTTPException(400, 'task name too long (>64)')
    if '/' in name or '\\' in name or '..' in name or name.startswith('.'):
        raise HTTPException(400, 'bad task name')
    return name


def _spawn_agent(task_name: str, prompt: str) -> tuple[int, str]:
    """启动 agent 子进程，返回 (pid, stderr_or_reason)。
    pid == 0 表示启动失败，stderr 字段里是人能看懂的失败原因。"""
    cmd = [sys.executable, str(AGENT), '--task', task_name, '--input', prompt, '--bg']
    log.info('[spawn] task=%s prompt_len=%d', task_name, len(prompt))
    try:
        result = subprocess.run(
            cmd, cwd=ROOT,
            capture_output=True, text=True,
            encoding='utf-8', errors='ignore',
            timeout=START_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        log.warning('[spawn] timeout task=%s', task_name)
        return 0, f'spawn timeout after {START_TIMEOUT}s: {e}'
    except FileNotFoundError as e:
        log.warning('[spawn] agent not found: %s', AGENT)
        return 0, f'agent executable not found: {AGENT} ({e})'

    out = (result.stdout or '').strip()
    err = (result.stderr or '').strip()
    pid = 0
    for line in reversed(out.splitlines()):
        try:
            pid = int(line.strip())
            break
        except ValueError:
            continue

    if not pid:
        # 把真实原因拼出来，resume 失败时这段会落到 stderr.log 让前端看到
        reason_lines = [
            f'[spawn failed] task={task_name}',
            f'exit_code={result.returncode}',
            f'stdout_tail={out[-500:]!r}',
            f'stderr_tail={err[-1000:]!r}',
        ]
        reason = '\n'.join(reason_lines)
        log.warning('[spawn] no pid: %s', reason)
        return 0, reason

    log.info('[spawn] ok task=%s pid=%d', task_name, pid)
    return pid, err


# -------------------- token usage --------------------
def _estimate_tokens(text) -> int:
    try:
        return int(len(str(text or '')) / TOKEN_ESTIMATE_DIVISOR)
    except Exception:
        return 0


def _task_usage(task_dir: Path) -> dict:
    """按字节数粗估（O(1)，不读内容）。bytes ≈ chars（英文主导日志）。"""
    in_bytes = file_size(task_dir / 'prompt.txt')
    out_bytes = file_size(task_dir / 'output.txt') + file_size(task_dir / 'output1.txt')
    log_bytes = file_size(task_dir / 'stdout.log')
    return {
        'input': int(in_bytes / TOKEN_ESTIMATE_DIVISOR),
        'output': int(out_bytes / TOKEN_ESTIMATE_DIVISOR),
        'log': int(log_bytes / TOKEN_ESTIMATE_DIVISOR),
        'total': int((in_bytes + out_bytes) / TOKEN_ESTIMATE_DIVISOR),
        'divisor': TOKEN_ESTIMATE_DIVISOR,
    }


# -------------------- multi-turn chat split --------------------
TURN_SEP_RE = re.compile(r'\n\n─── 续写 @ \d{2}:\d{2}:\d{2} ───\n')
ARCHIVE_RE = re.compile(r'^archive_(\d+)__(.+)$')


def _strip_user_wrappers(text: str) -> tuple[str, bool, bool]:
    """剥掉 FAST_PREFIX 和项目上下文包装，返回 (纯用户输入, is_fast, has_ctx)。"""
    fast = False
    has_ctx = False
    if text.startswith('【快速模式】'):
        m = re.search(r'\n任务：\n', text)
        if m:
            text = text[m.end():]
            fast = True
    if text.startswith('【项目常驻上下文】'):
        m = re.search(r'\n【本次任务】\n', text)
        if m:
            text = text[m.end():]
            has_ctx = True
    return text, fast, has_ctx


def _split_turns(task_dir: Path) -> list[dict]:
    """把 task 目录拆成多轮对话。每次续写都会归档成 archive_{ts}__xxx，
    prompt.txt 里用分隔符标出续写点。运行中收到的 reply 也会同步写入 prompt.txt，
    因此未归档轮次里只有最后一轮绑定当前输出，其余仅保留 user 文本。"""
    prompt_path = task_dir / 'prompt.txt'
    try:
        raw = prompt_path.read_text(encoding='utf-8') if prompt_path.exists() else ''
    except Exception:
        raw = read_text(prompt_path)

    user_msgs = TURN_SEP_RE.split(raw) if raw else ['']

    # 搜集归档文件：{ts: {fname: path}}
    archives: dict[int, dict[str, Path]] = {}
    try:
        for p in task_dir.iterdir():
            m = ARCHIVE_RE.match(p.name)
            if m:
                ts = int(m.group(1))
                archives.setdefault(ts, {})[m.group(2)] = p
    except Exception as e:
        log.warning('scan archives failed: %s', e)
    sorted_ts = sorted(archives.keys())

    def _rd(p: Path | None) -> str:
        return read_text(p) if p and p.exists() else ''

    running = is_running(read_pid(task_dir))
    current_ts = int(task_dir.stat().st_mtime)
    last_idx = len(user_msgs) - 1
    curr_stdout = _rd(task_dir / 'stdout.log')
    curr_stderr = _rd(task_dir / 'stderr.log')

    def _live_output_by_idx(idx: int) -> str:
        name = 'output.txt' if idx == 0 else f'output{idx}.txt'
        return _rd(task_dir / name)

    turns = []
    for i, raw_user in enumerate(user_msgs):
        raw_user = (raw_user or '').strip()
        # 第一轮剥 wrapper（快速模式/上下文前缀）；续写轮是用户原样输入
        if i == 0:
            user, fast, has_ctx = _strip_user_wrappers(raw_user)
        else:
            user, fast, has_ctx = raw_user, False, False

        if i < len(sorted_ts):
            # 这是一个已归档的历史轮
            ts = sorted_ts[i]
            files = archives[ts]
            turns.append({
                'idx': i,
                'ts': ts,
                'user': user,
                'fast': fast,
                'has_ctx': has_ctx,
                'output': _rd(files.get('output.txt')),
                'output1': _rd(files.get('output1.txt')),
                'stdout': _rd(files.get('stdout.log')),
                'stderr': _rd(files.get('stderr.log')),
                'active': False,
                'running': False,
            })
        else:
            is_last = (i == last_idx)
            # 运行中 output 文件按轮次递增命名：首轮 output.txt，次轮 output1.txt，第三轮 output2.txt ...
            turns.append({
                'idx': i,
                'ts': current_ts,
                'user': user,
                'fast': fast,
                'has_ctx': has_ctx,
                'output': _live_output_by_idx(i),
                'output1': '',
                'stdout': curr_stdout if is_last else '',
                'stderr': curr_stderr if is_last else '',
                'active': is_last,
                'running': running if is_last else False,
            })
    return turns


# -------------------- mykey.py parse / serialize --------------------
# parse/serialize 核心抄自 dhdbv-cbs/genericagent-launcher，去掉 GUI 耦合。

EXTRA_KEYS = {
    'proxy',
    # 通讯渠道 token：UI 不暴露，但 parse/serialize 时必须保留，否则用户其它前端
    # 里填的 key 一走 UI 保存就没了
    'tg_bot_token', 'tg_allowed_users',
    'qq_app_id', 'qq_app_secret', 'qq_allowed_users',
    'fs_app_id', 'fs_app_secret', 'fs_allowed_users',
    'wecom_bot_id', 'wecom_secret', 'wecom_allowed_users', 'wecom_welcome_message',
    'dingtalk_client_id', 'dingtalk_client_secret', 'dingtalk_allowed_users',
}

KIND_LABEL = {
    'native_claude': '原生 Claude',
    'native_oai':    '原生 OpenAI',
    'mixin':         'Mixin 故障转移',
    'claude':        'Claude (文本协议)',
    'oai':           'OpenAI (文本协议)',
    'unknown':       '未知',
}

SIMPLE_FORMAT_RULES = {
    'oai_chat':      {'label': 'OpenAI Chat Completions', 'kind': 'oai',           'hint': '/v1/chat/completions'},
    'oai_responses': {'label': 'OpenAI Responses',        'kind': 'oai',           'hint': '/v1/responses'},
    'claude_text':   {'label': 'Claude (文本协议)',       'kind': 'claude',        'hint': '/v1/messages'},
    'native_oai':    {'label': 'OpenAI 原生',             'kind': 'native_oai',    'hint': '/v1'},
    'native_claude': {'label': 'Claude 原生',             'kind': 'native_claude', 'hint': '/v1'},
    'mixin':         {'label': 'Mixin（故障转移）',        'kind': 'mixin',         'hint': '按 llm_nos 顺序切换'},
}


def _classify_config_kind(var_name: str) -> str:
    n = (var_name or '').lower()
    if 'mixin' in n: return 'mixin'
    if 'native' in n and 'claude' in n: return 'native_claude'
    if 'native' in n and 'oai' in n: return 'native_oai'
    if 'claude' in n: return 'claude'
    if 'oai' in n: return 'oai'
    return 'unknown'


def _looks_like_config_name(name: str) -> bool:
    n = (name or '').lower()
    return any(x in n for x in ('api', 'config', 'cookie'))


def _is_config_var(name, value) -> bool:
    if name.startswith('_'): return False
    if not isinstance(value, dict): return False
    return (_looks_like_config_name(name) or
            ('apikey' in value) or ('llm_nos' in value) or
            ('apibase' in value) or ('model' in value))


def _is_passthrough_var(name, value) -> bool:
    if name.startswith('_'): return False
    return ('cookie' in (name or '').lower()) and not isinstance(value, dict)


def parse_mykey_py(path: Path) -> dict:
    """解析 mykey.py → {configs, extras, passthrough, error}"""
    out = {'configs': [], 'extras': {}, 'passthrough': [], 'error': None}
    if not path.exists():
        return out
    try:
        src = path.read_text(encoding='utf-8')
    except Exception as e:
        out['error'] = f'read failed: {e}'
        return out

    # 记录顶层赋值顺序（UI 展示稳定）
    order = []
    try:
        tree = _ast.parse(src)
        for node in tree.body:
            if isinstance(node, _ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], _ast.Name):
                order.append(node.targets[0].id)
    except Exception as e:
        out['error'] = f'syntax error: {e}'

    values = {}
    try:
        spec = _il_util.spec_from_loader('mykey_runtime', loader=None)
        mod = _il_util.module_from_spec(spec)
        exec(compile(src, str(path), 'exec'), mod.__dict__)
        for k, v in mod.__dict__.items():
            if k.startswith('__'): continue
            values[k] = v
    except Exception as e:
        out['error'] = f'exec failed: {e}'
        return out

    seen = set()
    for name in order:
        if name in seen: continue
        seen.add(name)
        if name in values and _is_config_var(name, values[name]):
            out['configs'].append({
                'var': name,
                'kind': _classify_config_kind(name),
                'data': dict(values[name]),
            })
        elif name in values and _is_passthrough_var(name, values[name]):
            out['passthrough'].append({'name': name, 'value': values[name]})
        elif name in values and name in EXTRA_KEYS:
            out['extras'][name] = values[name]

    for name, v in values.items():
        if name in seen: continue
        if _is_config_var(name, v):
            out['configs'].append({'var': name, 'kind': _classify_config_kind(name), 'data': dict(v)})
        elif _is_passthrough_var(name, v):
            out['passthrough'].append({'name': name, 'value': v})
        elif name in EXTRA_KEYS:
            out['extras'][name] = v
    return out


_FIELD_ORDER = [
    'name', 'apikey', 'apibase', 'model',
    'api_mode', 'fake_cc_system_prompt',
    'thinking_type', 'thinking_budget_tokens', 'reasoning_effort',
    'temperature', 'max_tokens',
    'stream',
    'max_retries', 'connect_timeout', 'read_timeout',
    'context_win', 'proxy',
    'llm_nos', 'base_delay', 'spring_back',
]


def _ordered_items(d: dict):
    idx = {k: i for i, k in enumerate(_FIELD_ORDER)}
    return sorted(d.items(), key=lambda kv: (idx.get(kv[0], 999), kv[0]))


def _fmt_dict(d: dict) -> str:
    if not d:
        return '{}'
    lines = ['{']
    for k, v in _ordered_items(d):
        lines.append(f'    {k!r}: {v!r},')
    lines.append('}')
    return '\n'.join(lines)


def serialize_mykey_py(configs: list, extras: dict, passthrough: list | None = None) -> str:
    """重新生成 mykey.py。变量名决定 Session 类型。"""
    passthrough = passthrough or []
    lines = [
        '# ═════════════════════════════════════════════════════════════════════',
        '# mykey.py — 由 ga_web_ui 的 API 面板生成。',
        '#',
        '# 变量名决定 Session 类型：',
        '#   含 native + claude → NativeClaudeSession',
        '#   含 native + oai    → NativeOAISession',
        '#   含 mixin           → MixinSession (按 llm_nos 顺序故障转移)',
        '#   含 claude          → Claude (文本协议)',
        '#   含 oai             → OpenAI (文本协议)',
        '#',
        '# cookie / proxy / 通讯渠道 token 会被 UI 保留原样，不覆盖。',
        '# ═════════════════════════════════════════════════════════════════════',
        '',
    ]
    for item in configs:
        var = item.get('var') or 'unnamed_cfg'
        data = item.get('data') or {}
        kind_label = KIND_LABEL.get(_classify_config_kind(var), '未知')
        lines.append(f'# {var}  →  {kind_label}')
        lines.append(f'{var} = {_fmt_dict(data)}')
        lines.append('')

    for item in passthrough:
        name = item.get('name')
        val = item.get('value')
        if not name:
            continue
        lines.append(f'{name} = {val!r}')
        lines.append('')

    if extras:
        lines.append('# ─── 额外变量 (proxy / 通讯渠道 token 等) ───')
        for k in sorted(extras):
            lines.append(f'{k} = {extras[k]!r}')
        lines.append('')

    return '\n'.join(lines)


# -------------------- model list fetching --------------------
def _strip_known_api_suffix(path: str) -> str:
    raw = (path or '').strip().rstrip('/')
    for suffix in (
        '/v1/chat/completions', '/chat/completions',
        '/v1/responses', '/responses',
        '/v1/messages', '/messages',
        '/v1/models', '/models',
        '/claude/office',
    ):
        if raw.endswith(suffix):
            return raw[:-len(suffix)] or '/'
    return raw


def _join_url(base: str, suffix: str) -> str:
    base = (base or '').rstrip('/')
    suffix = '/' + suffix.lstrip('/')
    return f'{base}{suffix}'


def _http_json(url: str, headers: dict | None = None, timeout: int = 12) -> dict:
    merged = {
        'User-Agent': 'GenericAgent/1.0',
        'Accept': 'application/json',
    }
    if headers:
        merged.update(headers)
    req = Request(url, headers=merged, method='GET')
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    return json.loads(raw) if raw.strip() else {}


def _extract_model_ids(payload) -> list:
    items = []
    if isinstance(payload, dict):
        for k in ('data', 'models', 'items'):
            if isinstance(payload.get(k), list):
                items = payload[k]
                break
    elif isinstance(payload, list):
        items = payload

    out, seen = [], set()
    for item in items:
        if isinstance(item, str):
            mid = item.strip()
        elif isinstance(item, dict):
            mid = str(item.get('id') or item.get('name') or item.get('model') or '').strip()
        else:
            mid = ''
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _oai_models_base(apibase: str) -> str:
    raw = (apibase or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    parsed = urlparse(raw)
    root = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
    path = _strip_known_api_suffix(parsed.path or '').rstrip('/')
    if not path:
        path = '/v1'
    if not path.startswith('/'):
        path = '/' + path
    return root + path


def _anthropic_models_candidates(apibase: str) -> list:
    raw = (apibase or '').strip()
    if not raw:
        return []
    if '://' not in raw:
        raw = 'https://' + raw
    parsed = urlparse(raw)
    root = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
    path = _strip_known_api_suffix(parsed.path or '').rstrip('/')
    out = []
    for candidate in (
        _join_url(root + path, '/v1/models'),
        _join_url(root + path, '/models'),
        _join_url(root, '/v1/models'),
        _join_url(root, '/models'),
    ):
        if candidate not in out:
            out.append(candidate)
    return out


def _fetch_remote_models(format_key: str, apibase: str, apikey: str) -> list:
    key = (apikey or '').strip()
    base = (apibase or '').strip()
    if not base:
        raise ValueError('请先填写 URL')
    fmt = SIMPLE_FORMAT_RULES.get(format_key, SIMPLE_FORMAT_RULES['oai_chat'])
    kind = fmt.get('kind', 'oai')

    # claude 路径
    if kind in ('native_claude', 'claude'):
        headers = {'anthropic-version': '2023-06-01'}
        if key.startswith('sk-ant-'):
            headers['x-api-key'] = key
        elif key:
            headers['Authorization'] = f'Bearer {key}'
        last_err = None
        for url in _anthropic_models_candidates(base):
            try:
                payload = _http_json(url, headers=headers)
                models = _extract_model_ids(payload)
                if models:
                    return models
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise ValueError('未拿到模型列表')

    # oai 路径（含 native_oai / oai_chat / oai_responses / mixin）
    headers = {}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    payload = _http_json(_join_url(_oai_models_base(base), '/models'), headers=headers)
    models = _extract_model_ids(payload)
    if models:
        return models
    raise ValueError('模型接口返回为空')


# -------------------- routes --------------------
@app.get('/')
def index():
    if not INDEX_HTML.exists():
        raise HTTPException(404, f'frontend missing: {INDEX_HTML.name}')
    return FileResponse(INDEX_HTML)


# --- context ---
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


# --- history ---
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


@app.post('/api/history/delete')
def delete_history_item(payload: HistoryDelPayload):
    with _history_lock:
        items = load_history()
        items = [x for x in items if not (x.get('ts') == payload.ts and x.get('text') == payload.text)]
        save_history(items)
    return {'ok': True}


# --- API 配置面板 ---
@app.get('/api/formats')
def get_formats():
    return {
        'formats': [{'key': k, **v} for k, v in SIMPLE_FORMAT_RULES.items()],
        'kind_labels': KIND_LABEL,
        'extras_keys': sorted(EXTRA_KEYS),
    }


@app.get('/api/mykey')
def get_mykey():
    parsed = parse_mykey_py(MYKEY_PY)
    return {
        'exists': MYKEY_PY.exists(),
        'path': str(MYKEY_PY),
        **parsed,
    }


@app.post('/api/mykey')
def save_mykey(payload: MykeyPayload):
    # parse 一遍拿到 passthrough（cookie 等），合并后再写
    existing = parse_mykey_py(MYKEY_PY)
    passthrough = existing.get('passthrough') or []

    # 清理 extras 里 None/空值，避免生成一堆 `proxy = None`
    extras = {k: v for k, v in (payload.extras or {}).items() if v not in (None, '')}

    src = serialize_mykey_py(payload.configs, extras, passthrough)
    # 先备份一次
    if MYKEY_PY.exists():
        try:
            shutil.copyfile(MYKEY_PY, MYKEY_PY.with_suffix('.py.bak'))
        except Exception as e:
            log.warning('backup mykey failed: %s', e)
    MYKEY_PY.write_text(src, encoding='utf-8')
    return {'ok': True, 'bytes': len(src.encode('utf-8')), 'path': str(MYKEY_PY)}


@app.post('/api/mykey/fetch_models')
def fetch_models(payload: FetchModelsPayload):
    try:
        models = _fetch_remote_models(payload.format, payload.apibase, payload.apikey)
        return {'ok': True, 'models': models}
    except HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', errors='replace').strip()
        except Exception:
            detail = ''
        msg = f'HTTPError {e.code}: {e.reason}'
        if detail:
            msg += f' | {detail[:500]}'
        raise HTTPException(400, msg)
    except Exception as e:
        raise HTTPException(400, f'{type(e).__name__}: {e}')


# --- 任务 ---
@app.post('/api/tasks')
def start_task(payload: StartTask):
    prompt = payload.prompt
    if not prompt.strip():
        raise HTTPException(400, 'empty prompt')

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

    pid, stderr = _spawn_agent(task_name, prompt)
    if pid:
        (task_dir / 'pid').write_text(str(pid), encoding='utf-8')
    return {'task': task_name, 'pid': pid, 'stderr': stderr}


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
            'usage': _task_usage(d),
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
        'reply_exists': reply_pending(task_dir),
        'running': is_running(pid),
        'pid': pid,
        'updated_at': int(task_dir.stat().st_mtime),
        'usage': _task_usage(task_dir),
    }


@app.get('/api/tasks/{task_name}/turns')
def get_turns(task_name: str):
    """多轮对话视图：把 task 目录拆成 N 轮 user/assistant 对。"""
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    pid = read_pid(task_dir)
    return {
        'name': task_name,
        'running': is_running(pid),
        'pid': pid,
        'reply_exists': reply_pending(task_dir),
        'updated_at': int(task_dir.stat().st_mtime),
        'usage': _task_usage(task_dir),
        'turns': _split_turns(task_dir),
    }


@app.get('/api/tasks/{task_name}/stream')
async def stream_task(task_name: str):
    """SSE：推 meta 信号（running + 文件大小），前端自己判断需不需要拉 turns。"""
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')

    async def gen():
        last_sig = None
        idle_ticks = 0
        keepalive = 0
        try:
            # 立即推一次初始状态
            while True:
                pid = read_pid(task_dir)
                running = is_running(pid)
                sizes = {
                    f: file_size(task_dir / f)
                    for f in ('output.txt', 'output1.txt', 'stdout.log', 'stderr.log')
                }
                try:
                    mtime = int(task_dir.stat().st_mtime)
                except Exception:
                    mtime = 0
                sig = (running, tuple(sizes.values()), mtime)

                if sig != last_sig:
                    payload = {
                        'running': running,
                        'pid': pid,
                        'sizes': sizes,
                        'updated_at': mtime,
                        'usage': _task_usage(task_dir),
                        'reply_exists': reply_pending(task_dir),
                    }
                    yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                    last_sig = sig
                    idle_ticks = 0
                else:
                    idle_ticks += 1

                # keepalive（避免代理断连）
                keepalive += 1
                if keepalive >= 30:  # ~15s
                    yield ': keepalive\n\n'
                    keepalive = 0

                # 停止条件：进程不活 且 连续 idle 10 次（~5s）
                if not running and idle_ticks >= 10:
                    yield 'event: done\ndata: {}\n\n'
                    return

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning('stream %s error: %s', task_name, e)
            return

    return StreamingResponse(
        gen(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',  # 关掉 nginx 缓冲
            'Connection': 'keep-alive',
        },
    )


@app.post('/api/tasks/{task_name}/reply')
def reply_task(task_name: str, payload: ReplyTask):
    task_name = safe_task_name(task_name)
    task_dir = TEMP_DIR / task_name
    if not task_dir.is_dir():
        raise HTTPException(404, 'task not found')
    if not payload.reply.strip():
        raise HTTPException(400, 'empty reply')

    push_history(payload.reply, kind='reply', task=task_name)

    pid = read_pid(task_dir)
    if is_running(pid):
        append_reply(task_dir, payload.reply)
        append_prompt_turn(task_dir, payload.reply)
        return {'ok': True, 'mode': 'reply', 'pid': pid}

    # agent 已结束：续写
    # 先保险地再 kill 一次，防止 pid 假死（Windows 下进程退出未彻底释放句柄）
    if pid > 0:
        kill_pid(pid)

    ts = int(time.time())
    ts_str = time.strftime('%H:%M:%S', time.localtime(ts))
    append_prompt_turn(task_dir, payload.reply, ts=ts)
    try:
        new_prompt = (task_dir / 'prompt.txt').read_text(encoding='utf-8')
    except Exception:
        new_prompt = read_text(task_dir / 'prompt.txt')

    # 归档上一轮（Windows 下 rename 可能因句柄未释放失败，兜底用 copy+truncate）
    for fname in ('output.txt', 'output1.txt', 'stdout.log', 'stderr.log', 'reply.txt', 'replies.jsonl'):
        src = task_dir / fname
        if not src.exists():
            continue
        dst = task_dir / f'archive_{ts}__{fname}'
        try:
            src.rename(dst)
        except Exception as e:
            log.warning('archive rename %s failed (%s), fallback to copy+truncate', src.name, e)
            try:
                shutil.copy2(src, dst)
                # 清空原文件，让新 agent 从零开始写
                with src.open('w', encoding='utf-8') as f:
                    f.truncate(0)
            except Exception as e2:
                log.warning('archive fallback %s also failed: %s', src.name, e2)

    new_pid, stderr = _spawn_agent(task_name, new_prompt)
    if new_pid:
        (task_dir / 'pid').write_text(str(new_pid), encoding='utf-8')
    else:
        # 启动失败 → 把原因写到 stderr.log，前端就能自动显示
        try:
            (task_dir / 'stderr.log').write_text(
                f'[resume failed @ {ts_str}]\n{stderr}\n',
                encoding='utf-8',
            )
            # 清一下 pid 文件，免得下次又被当成"有进程"
            try:
                (task_dir / 'pid').write_text('0', encoding='utf-8')
            except Exception:
                pass
        except Exception:
            pass
        log.warning('[resume] spawn failed task=%s', task_name)
    return {'ok': True, 'mode': 'resume', 'pid': new_pid, 'stderr': stderr}


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
    PORT = 8796
    print(f'GA 控制台: http://127.0.0.1:{PORT}')
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')