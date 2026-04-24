"""
GA mykey 加载诊断脚本 (Python 3.11 兼容版)
放在 GenericAgent 文件夹里跑: python diag.py
"""
import os
import sys
import traceback

print("=" * 70)
print("[1] 环境信息")
print("=" * 70)
print("Python 版本:", sys.version)
print("Python 路径:", sys.executable)
print("当前工作目录:", os.getcwd())
print("脚本所在目录:", os.path.dirname(os.path.abspath(__file__)))
print("sys.path[0]:", sys.path[0] if sys.path else "(空)")

print()
print("=" * 70)
print("[2] mykey.py 文件检查")
print("=" * 70)
script_dir = os.path.dirname(os.path.abspath(__file__))
mykey_path = os.path.join(script_dir, "mykey.py")
print("期望路径:", mykey_path)
print("文件存在:", os.path.exists(mykey_path))

if os.path.exists(mykey_path):
    size = os.path.getsize(mykey_path)
    print("文件大小:", size, "字节")

    with open(mykey_path, "rb") as f:
        raw = f.read()

    print("前 8 字节(hex):", raw[:8].hex())

    if raw.startswith(b"\xef\xbb\xbf"):
        print("!!! 文件开头有 UTF-8 BOM,可能影响 Python 解析")

    crlf_marker = b"\r\n"
    if crlf_marker in raw:
        newline_type = "CRLF"
    else:
        newline_type = "LF"
    print("换行符类型:", newline_type)

    print()
    print("--- 文件内容 ---")
    try:
        content = raw.decode("utf-8")
        print(content)
    except UnicodeDecodeError as e:
        print("UTF-8 解码失败:", e)
        print("尝试 GBK:")
        try:
            print(raw.decode("gbk"))
        except Exception as e2:
            print("GBK 也失败:", e2)
    print("--- 内容结束 ---")

print()
print("=" * 70)
print("[3] 直接 import mykey 测试")
print("=" * 70)

if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

try:
    import mykey
    print("OK: import mykey 成功")
    print("  模块路径:", mykey.__file__)
    attrs = [k for k in vars(mykey) if not k.startswith("_")]
    print("  模块变量:", attrs)
    if hasattr(mykey, "oai_config"):
        cfg = mykey.oai_config
        print("  oai_config.name:", cfg.get("name"))
        print("  oai_config.apibase:", cfg.get("apibase"))
        print("  oai_config.model:", cfg.get("model"))
        apikey = cfg.get("apikey", "")
        print("  oai_config.apikey(前12位):", apikey[:12] + "...")
except Exception as e:
    print("FAIL: import mykey 失败:", type(e).__name__, ":", e)
    traceback.print_exc()

print()
print("=" * 70)
print("[4] llmcore._load_mykeys 测试(复现 GA 启动时的调用)")
print("=" * 70)

for mod in list(sys.modules.keys()):
    if mod in ("mykey", "mykey_template", "llmcore"):
        del sys.modules[mod]

try:
    from llmcore import _load_mykeys
    result = _load_mykeys()
    print("OK: _load_mykeys 返回类型:", type(result).__name__)
    if isinstance(result, dict):
        print("  键列表:", list(result.keys()))
        print("  键数量:", len(result))
except Exception as e:
    print("FAIL: _load_mykeys 失败:", type(e).__name__, ":", e)
    traceback.print_exc()

print()
print("=" * 70)
print("[5] llmcore.py 源码前 30 行(看加载逻辑)")
print("=" * 70)
llmcore_path = os.path.join(script_dir, "llmcore.py")
if os.path.exists(llmcore_path):
    with open(llmcore_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i > 30:
                break
            print("%3d| %s" % (i, line.rstrip()))
else:
    print("FAIL: llmcore.py 不存在")

print()
print("=" * 70)
print("诊断完成,把以上所有输出贴给 Claude")
print("=" * 70)