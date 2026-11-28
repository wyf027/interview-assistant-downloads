"""Run public input/output examples in a capability-limited WASI guest."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from answer_checks import samples_from

ROOT = Path(__file__).resolve().parent


def verify(question, response):
    cases = samples_from(question)
    if not cases:
        return "未验证：未取得完整的卷面样例", False
    blocks = re.findall(r"(?ims)^\s*~{3}(?:python3?|py)?[ \t]*\n(.*?)^\s*~{3}[ \t]*$", response.replace(chr(96)*3, "~~~"))
    if len(blocks)!=1:
        return "未验证：缺少唯一的完整 Python 程序", False
    if len(cases)>20 or len(blocks[0])>100000:
        return "未验证：样例或代码超出本地验证范围", False
    request=json.dumps({"code":blocks[0],"cases":cases},ensure_ascii=False).encode()
    env=dict(os.environ,PYTHONIOENCODING="utf-8",RAYON_NUM_THREADS="2")
    tmp=ROOT/"local"/"wasi-tmp"
    tmp.mkdir(exist_ok=True)
    env.update(TMP=str(tmp),TEMP=str(tmp))
    try:
        warm = any((ROOT/"local"/"wasm-cache").glob("*.cwasm"))
        timeout = (35 if warm else 200) + len(cases)*6
        result=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),"--guest"],
                              input=request,capture_output=True,timeout=timeout,
                              env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return "未验证：本地样例运行器启动失败", False
        outcome=json.loads(result.stdout.decode("utf-8"))
        return outcome["message"],outcome["passed"]
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return "未验证：本地样例运行器未返回结果", False


def run_cases():
    sys.path.insert(0,str(ROOT/"local"/"wasm-checker"))
    from wasi_python import run

    payload=json.loads(sys.stdin.buffer.read())
    for number,(data,expected) in enumerate(payload["cases"],1):
        result=run(["-c",payload["code"]],stdin=data.encode(),deadline_s=4,
                   output_bytes=65536,memory_bytes=192*1024*1024,
                   cache_dir=ROOT/"local"/"wasm-cache")
        prefix=f"样例 {number} 未通过"
        if result.trapped or result.truncated:
            return prefix+"：运行时间、内存或输出超限",False
        if result.exit_code:
            detail=result.stderr.decode("utf-8","replace").strip().splitlines()
            return prefix+"："+(detail[-1][:140] if detail else "程序异常退出"),False
        actual=result.stdout.decode("utf-8","replace").strip()
        if [line.split() for line in actual.splitlines()] != [line.split() for line in expected.splitlines()]:
            compact=lambda text:" / ".join(text.strip().splitlines())[:90]
            return f"{prefix}：期望 {compact(expected)}，实际 {compact(actual) or '无输出'}",False
    return f"卷面样例通过 {len(payload['cases'])}/{len(payload['cases'])}（未测隐藏用例）",True


if __name__=="__main__":
    message,passed=run_cases()
    sys.stdout.buffer.write(json.dumps({"message":message,"passed":passed}).encode())
