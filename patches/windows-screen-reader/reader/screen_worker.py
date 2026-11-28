"""Read a selected screen region and answer locally. Standard output carries JSON lines."""
import ast
import base64
import concurrent.futures
import ctypes
from functools import lru_cache
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

from answer_checks import samples_from
from page_text import question_text
from reader_cache import ReaderCache, fingerprint
from provider_bridge import AppProvider
from sample_check import verify

from PIL import Image, ImageChops, ImageGrab, ImageOps

HOME = Path(__file__).resolve().parent
MODELS = HOME / "local"
MODEL = MODELS / "Qwen3.5-4B-Q4_K_M.gguf"
END = threading.Event()
OUTPUT_LOCK = threading.Lock()
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def emit(kind, **data):
    with OUTPUT_LOCK:
        print(json.dumps(dict(kind=kind, **data), ensure_ascii=False))


@lru_cache(maxsize=1)
def local_image_reader():
    packages = MODELS / "image-ocr"
    if not packages.is_dir():
        return None
    sys.path.append(str(packages))
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR(use_angle_cls=False, width_height_ratio=-1,
                        det_limit_type="max", det_limit_side_len=1920, det_model_path=None)
    except (ImportError, OSError, RuntimeError, ValueError):
        return None


def read_image_blocks(data):
    engine = local_image_reader()
    if engine is not None:
        try:
            blocks, _ = engine(data)
            if blocks:
                rows = []
                for corners, value, _ in blocks:
                    ys = [point[1] for point in corners]
                    center = (max(ys) + min(ys)) / 2
                    height = max(ys) - min(ys)
                    rows.append({"center": center, "height": height,
                                 "x": min(point[0] for point in corners), "text": value})
                return join_ocr_rows(rows)
        except (OSError, RuntimeError, ValueError):
            pass
    return ""


def read_question_image(bitmap):
    buffer = io.BytesIO()
    bitmap.save(buffer, format="PNG")
    try:
        child = subprocess.run([sys.executable, "-B", __file__, "--ocr-image"],
                               input=buffer.getvalue(), capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        if child.returncode == 0 and child.stdout.strip():
            return child.stdout.decode("utf-8").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return recognize_block(bitmap, layout=True)


def join_ocr_rows(boxes):
    rows = []
    for box in sorted(boxes, key=lambda part: part["center"]):
        if not rows or box["center"] - rows[-1][0]["center"] > min(rows[-1][0]["height"], box["height"]) * .55:
            rows.append([])
        rows[-1].append(box)
    return "\n".join("  ".join(part["text"] for part in sorted(row, key=lambda part: part["x"])) for row in rows)


def recognize_block(image, layout=False):
    scale = min(1.7, 2460 / max(image.size))
    image = image.resize(tuple(max(1, round(v*scale)) for v in image.size), Image.Resampling.LANCZOS)
    image = ImageOps.expand(image, border=12, fill=image.getpixel((0, 0)))
    png = io.BytesIO()
    image.save(png, "PNG")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(HOME/"ocr.ps1")]
    if layout:
        args.append("-Layout")
    result = subprocess.run(args,
                            input=base64.b64encode(png.getvalue()), capture_output=True, timeout=25,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[:240])
    text = result.stdout.decode("utf-8-sig").strip()
    if layout:
        text = join_ocr_rows(json.loads(text))
    return re.sub(r"(?<=[\u3400-\u9fff])[^\S\n]+(?=[\u3400-\u9fff])", "", text.replace("\r", ""))


def recognize(image):
    text = recognize_block(image)
    gray = image.convert("L")
    background = gray.getpixel((0, 0))
    mask = gray.point(lambda p: 255 if abs(p-background) > 95 else 0)
    bands = []
    for y in range(mask.height):
        if not mask.crop((0, y, mask.width, y+1)).getbbox():
            continue
        if bands and y-bands[-1][1] <= 3:
            bands[-1][1] = y
        else:
            bands.append([y,y])
    # The recognizer may discard isolated, very short answer choices. Give the
    # same recognizer a horizontal line containing the trailing short rows.
    tails = []
    for top,bottom in reversed(bands):
        row = mask.crop((0,top,mask.width,bottom+1)).getbbox()
        if len(tails) == 8 or row[2]-row[0] > mask.width*.48 or bottom-top < 6:
            break
        tails.append(image.crop((row[0],top,row[2],bottom+1)))
    tails.reverse()
    if len(tails) < 2 or len(re.findall(r"(?im)^\s*[A-H]\s*[.、．:：]", text)) >= len(tails):
        return text
    height = max(row.height for row in tails)
    gap = max(10,round(height*0.62))
    strip = Image.new("RGB",(sum(row.width+gap for row in tails)+gap,height+20),
                      image.getpixel((0,0)))
    x = 8
    for row in tails:
        strip.paste(row,(x,10)); x += row.width+gap
    # Normalize the printed character size across Windows display scales.
    scale = min(1.5, 27/height)
    strip = strip.resize(tuple(max(1,round(v*scale)) for v in strip.size),Image.Resampling.LANCZOS)
    choices = recognize_block(strip)
    split = re.split(r"(?i)(?<![a-z])([A-H])\s*[.、．:：]\s*",choices)
    letters = [s.upper() for s in split[1::2]]
    if len(letters) == len(tails) and letters == list("ABCDEFGH"[:len(letters)]):
        base = re.split(r"(?im)^\s*A\s*[.、．:：]",text)[0].strip()
        opts = [f"{label}. {value.strip()}" for label,value in zip(letters,split[2::2])]
        return "\n".join([base,*opts])
    return text


def inspect_page(foreground, preferred):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                             str(HOME/"visible_question.ps1"), str(foreground),
                             str(preferred), getattr(browser_question,"target_id","")],
                            capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    return None if result.returncode else json.loads(result.stdout.decode("utf-8-sig") or "null")


def browser_question(cache=None):
    browser_question.upcoming = ""
    foreground = ctypes.windll.user32.GetForegroundWindow()
    page = inspect_page(foreground, getattr(browser_question,"window",0))
    if not page:
        return ""
    browser_question.window = page["window"]
    browser_question.target_id = page.get("targetID","")
    browser_question.paper = page.get("url") or page.get("docID") or str(page["window"])
    location = urlsplit(browser_question.paper)
    if location.hostname in {"nowcoder.com","www.nowcoder.com"} and location.path.startswith(("/exam/test/","/test/")):
        pid = parse_qs(location.query).get("pid", [""])[0]
        if pid.isdigit():
            browser_question.paper = "nowcoder-paper/" + pid
    following = page.get("next")
    if following and following.get("lines"):
        preview = question_text(following)
        if (following["kind"] == "简答题" and len(preview.strip()) > 32 or
                following["kind"] in {"单选题","多选题","不定项选择题","判断题"} and
                len(re.findall(r"(?m)^[A-H][.．、]\s*\S", preview)) >= 2):
            browser_question.upcoming = preview
    left, top, right, bottom = page["view"]
    for node in page["lines"]:
        rect = node.get("rect")
        if rect and top <= rect[1] < bottom-8 and left <= rect[0] < right and len(node["text"].strip()) >= 4:
            browser_question.anchor = (round(rect[0]), round(rect[1]))
            break
    else:
        browser_question.anchor = None
    text = question_text(page)
    if page["kind"] != "简答题" or not re.fullmatch(r"简答题\s+\d+[.．、]?", text.strip()):
        return text
    # An image-only stem may expose only its number in Chromium's accessibility tree.
    # Crop only the fully visible space between that number and the answer editor.
    rect = page["lines"][-1].get("rect")
    end = page.get("imageBottom")
    identity = (browser_question.paper, page.get("url"), page.get("browserStart"), page.get("docID"), page.get("nodeID"), page["window"], text)
    last = getattr(browser_question, "last_image", None)
    previous_text = last[2] if last and last[0] == identity else ""
    alias = fingerprint("live-image-node-v3", identity)
    if not previous_text and cache and page.get("nodeID"):
        known_image = cache.get(alias)
        known_text = cache.get(known_image) if known_image else None
        if known_text:
            previous_text = text + "\n题面图片（本机文字识别）\n" + known_text
    if previous_text and page.get("nodeID") and browser_question.paper.startswith("nowcoder-paper/"):
        if rect and top <= rect[1] < bottom:
            browser_question.anchor = (round(rect[0]), round(rect[1]))
        return previous_text
    if not rect or end is None or page["window"] != foreground:
        return previous_text
    x, y = rect[:2]
    if y < top-2 or end > bottom+2 or end-y < 32:
        return previous_text
    xmax = min(right, page.get("imageRight") or right)
    region = tuple(map(round,(max(left,x-2),max(top,y),xmax,end-2)))
    if region[2]-region[0]<60 or region[3]<=region[1]:
        return ""
    bitmap = ImageGrab.grab(bbox=region, all_screens=True)
    if ctypes.windll.user32.GetForegroundWindow() != foreground:
        return ""
    gray = bitmap.convert("L")
    ink = ImageChops.difference(gray, Image.new("L", gray.size, gray.getpixel((0,0)))).getbbox()
    if ink:
        gray = gray.crop(ink)
    signature = fingerprint("ocr-v3", browser_question.paper, text, gray.size, hashlib.blake2s(gray.tobytes()).hexdigest())
    browser_question.anchor = (round(x), round(max(top, y)))
    if last and last[:2] == (identity, signature):
        return last[2]
    extracted = cache.get(signature) if cache else None
    if not extracted:
        settled = inspect_page(foreground, page["window"])
        geometry = ("window","docID","nodeID","lines","view","imageBottom","imageRight")
        if not settled or any(page.get(k) != settled.get(k) for k in geometry):
            return ""
        extracted = read_question_image(bitmap)
        if not re.match(r"^\s*"+re.escape(re.search(r"\d+",text)[0])+r"\s*[.．、·]",extracted):
            # The accessible number precedes the bitmap. If OCR drops it, ensure
            # the captured viewport did not move to another question meanwhile.
            again = ImageGrab.grab(bbox=region, all_screens=True)
            if ctypes.windll.user32.GetForegroundWindow()!=foreground or again.tobytes()!=bitmap.tobytes():
                return ""
        if cache and len(re.sub(r"\s","",extracted)) >= 8:
            cache.put(signature,extracted)
    if len(re.sub(r"\s", "", extracted)) < 8:
        return ""
    result = text + "\n题面图片（本机文字识别）\n" + extracted
    browser_question.last_image = identity, signature, result
    if cache and page.get("nodeID") and cache.get(alias) != signature:
        cache.put(alias,signature)
    return result


class Model:
    def __init__(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.url = "http://127.0.0.1:" + str(sock.getsockname()[1])
        self.token = secrets.token_urlsafe(24)
        self.proc = None
        self.cancel = threading.Event()

    def launch(self, layers):
        server = next((MODELS/"runtime").rglob("llama-server.exe"))
        args = [str(server), "-m", str(MODEL), "--host", "127.0.0.1", "--port", self.url.split(":")[-1],
                "--reasoning-budget", "1200", "--api-key", self.token, "-c", "6144", "-b", "64", "-ub", "32", "--cache-ram", "0", "-np", "1", "-ngl", layers, "-t", "4"]
        if layers != "0":
            args.extend(["--device", "Vulkan0", "--split-mode", "none"])
        self.proc = subprocess.Popen(args, cwd=server.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic()+100
        while not END.wait(.3) and self.proc.poll() is None and time.monotonic()<deadline:
            try:
                req = urllib.request.Request(self.url+"/health", headers={"Authorization":"Bearer "+self.token})
                with HTTP.open(req, timeout=2) as response:
                    if response.status == 200:
                        return True
            except OSError:
                pass
        self.close()
        return False

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)

    def answer(self, question):
        coding = question.startswith("编程题")
        if question.startswith("填空题\n"):
            blanks = list(dict.fromkeys(re.findall(r"【空(\d+)】", question)))
            if blanks:
                return self.fill_answers(question, blanks)
        if question.startswith("简答题\n"):
            return self.complete({"messages":[{"role":"system","content":
                "直接交付题目要求的答案。要求脚本、代码或 SQL 时，先给完整代码块；其余问答先给结论，再按要求分点。"
                "若题面标明来自图片识别，请把无法确认的精确字符或定位符标在相关行。简洁中文，不复述题目。"},
                {"role":"user","content":question}],"max_tokens":1200,"temperature":0.1,
                "chat_template_kwargs":{"enable_thinking":False}})
        if coding:
            return self.program_answer(question)
        options = dict(re.findall(r"(?ms)^([A-H])[.．、][ \t]*(.*?)(?=^[A-H][.．、]|\Z)", question))
        if len(options) < 2:
            return self.complete({"messages":[{"role":"system","content":"直接给出这道题的简明答案。"},
                                  {"role":"user","content":question}], "temperature":0, "max_tokens":450,
                                  "chat_template_kwargs":{"enable_thinking":False}})
        single = question.startswith(("单选题","单项选择题","判断题"))
        prompt = ("按原题的选项字母作答，注意题干要求选正确还是错误。逐项判断，"
                  "在 basis 中用一句话写依据，在 selected 中列出最终选项。"
                  + ("只选一项。" if single else "选出所有符合题意的项。"))
        data = {"messages":[{"role":"system","content":prompt},{"role":"user","content":question}],
                "temperature":0, "max_tokens":300, "chat_template_kwargs":{"enable_thinking":False},
                "response_format":{"type":"json_schema","json_schema":{"name":"decision","schema":{
                    "type":"object","properties":{"basis":{"type":"string"},
                    "selected":{"type":"array","items":{"type":"string","enum":list(options)},"minItems":1,
                    "maxItems":1 if single else len(options)}},"required":["basis","selected"],"additionalProperties":False}}}}
        if not single:
            data["messages"][0]["content"] = (
                "按题面原来的顺序独立判断每一项。先用简明事实说明这一项是否成立，"
                "对绝对化表述检查反例，对有多个条件的表述检查每个条件。"
                "pick 表示该项是否满足题干的选择要求；题干要求选错误项时应选不成立的项。"
                "输出 JSON，以原选项字母为键，每项含 reason 和布尔值 pick。")
            verdict = {"type":"object","properties":{"reason":{"type":"string"},"pick":{"type":"boolean"}},
                       "required":["reason","pick"],"additionalProperties":False}
            data["response_format"]["json_schema"]["schema"] = {
                "type":"object","properties":dict.fromkeys(options,verdict),
                "required":list(options),"additionalProperties":False}
            data["max_tokens"] = 100 * len(options) + 100
        for attempt in range(2):
            if attempt:
                data["messages"].append({"role":"user","content":
                    "请严格按前述 JSON 格式重新作答，至少选一项。"})
            response = self.complete(data)
            selected = self.letters(response, options) if single else self.verdicts(response, options)
            if selected and (not single or len(selected) == 1):
                reviewed = False
                if len(selected) == 1 and question.startswith(("多选题\n","多项选择题\n")):
                    review = dict(data, messages=[*data["messages"][:2], {"role":"user","content":
                        "这是多选题。请专门检查漏选：分别判断哪些选项还有独立成立的依据，"
                        "留意限定词、概念层次和题干问的是正确还是错误。重新给出所有选项的 reason 和 pick。"
                        "以内容为准，不按数量硬凑。"}])
                    selected = self.verdicts(self.complete(review), options)
                    if not selected:
                        continue
                    reviewed = True
                label = "、".join(selected)
                if len(selected) == 1 and single:
                    label += " · " + " ".join(options[selected[0]].splitlines()).strip()
                return label + ("　〔多选已复查〕" if reviewed else "　〔模型原序作答〕")
        raise ValueError("模型连续两次未返回有效选项")

    def fill_answers(self, question, blanks):
        keys = "、".join(blanks)
        prompt = ("按标注逐空作答。返回 JSON 对象，键为这些空号：" + keys +
                  "。每个键的值是该空应填的简短答案字符串。不能漏空，也不要把空号当作答案。"
                  "若答案取决于题干未指定的平台或约定，请只在受影响的那一空简短列明各情况。")
        schema = {"type":"object","properties":{k:{"type":"string"} for k in blanks},
                  "required":blanks,"additionalProperties":False}
        data = {"messages":[{"role":"system","content":prompt},{"role":"user","content":question}],
                "response_format":{"type":"json_schema","json_schema":{"name":"fill_answers","schema":schema}},
                "temperature":0,"max_tokens":min(2600,160+100*len(blanks)),
                "chat_template_kwargs":{"enable_thinking":False}}
        for attempt in range(2):
            if attempt:
                data["messages"].append({"role":"user","content":"上一份结果格式或空数有误。请填写全部空号："+keys+"，每空一个非空答案。"})
            response = self.complete(data)
            try:
                values = json.loads(response)
            except ValueError:
                continue
            if isinstance(values, dict) and set(values) == set(blanks):
                answers = {k:str(v).strip() for k,v in values.items()
                           if isinstance(v,str) or type(v) is int}
                if len(answers) == len(blanks) and all(answers.values()):
                    if len(blanks) == 1:
                        return answers[blanks[0]]
                    return "\n".join(f"第{k}空　{answers[k]}" for k in blanks)
        raise ValueError("模型未给出完整的逐空答案")

    def program_answer(self, question):
        cases = samples_from(question)
        has_io = bool(cases or re.search(r"(?im)^\s*(输入描述|输入格式|输入说明|Input(?: Format| Specification)?)(?:[ \t]*[:：]|[ \t]*$)", question))
        interface = ("按题面输入格式，从标准输入读取并输出结果，给完整可执行程序。" if has_io else
                     "当前页只有题干。写一个名为 solve 的 Python 函数，用参数接收题意需要的数据，返回结果。不要猜测标准输入格式。")
        prompt = (interface + "使用 Python 3 标准库。只输出一个简短、完整且高效的 Python 3 代码块，"
                  "闭合代码块后立即结束。不写推导或示例。")
        data = {"messages":[{"role":"system","content":prompt},{"role":"user","content":question}],
                "temperature":0,"seed":42,"max_tokens":3000,"stop_after_code":True,
                "chat_template_kwargs":{"enable_thinking":False}}
        fence = chr(96)*3
        problem = ""
        for attempt in range(2):
            text = self.complete(data)
            blocks = re.findall(r"(?ms)^" + fence + r"(?:python3?|py)?[ \t]*\r?\n(.*?)^" + fence + r"[ \t]*\r?$", text)
            source = (blocks[0] if len(blocks)==1 else text if fence not in text else "").strip("\n")
            try:
                if not source.strip():
                    raise ValueError("缺少完整的 Python 代码块")
                tree = ast.parse(source)
                compile(tree, "<model>", "exec")
                if not has_io and not any(isinstance(n, ast.FunctionDef) and n.name=="solve" for n in tree.body):
                    raise ValueError("缺少按参数调用的 solve 函数")
            except (SyntaxError, ValueError) as exc:
                problem = "Python 代码检查失败：" + str(exc)[:160]
            else:
                solution = f"{fence}python\n{source}\n{fence}"
                if not cases:
                    note = "本页无样例，仅通过语法检查" if has_io else "函数解法 · 本页未给输入格式或样例，仅通过语法检查"
                    return note + "\n" + solution
                problem, passed = verify(question, solution)
                if passed:
                    return problem + "\n" + solution
            if attempt == 0 and not END.is_set():
                data["messages"].extend([{"role":"assistant","content":text},
                    {"role":"user","content":problem+"。定位原因并修正，返回完整的通用 Python 3 代码。"}])
            else:
                break
        return "模型未能给出通过检查的程序。" + problem

    @staticmethod
    def verdicts(text, options):
        try:
            verdicts = json.loads(text)
            if set(verdicts) != set(options) or any(type(verdicts[k]["pick"]) is not bool for k in options):
                return []
            return [k for k in options if verdicts[k]["pick"]]
        except (ValueError, KeyError, TypeError):
            return []

    @staticmethod
    def letters(text,options):
        try:
            raw=json.loads(text)["selected"]
            if not isinstance(raw,list) or any(not isinstance(k,str) for k in raw):
                return []
            chosen={k.strip().upper() for k in raw}
        except (ValueError, KeyError, TypeError):
            return []
        return [key for key in options if key in chosen] if chosen.issubset(options) else []

    def complete(self,data):
        if self.cancel.is_set() or END.is_set():
            raise concurrent.futures.CancelledError()
        payload = json.dumps(dict({k:v for k,v in data.items() if k!="stop_after_code"}, stream=True)).encode()
        req = urllib.request.Request(self.url+"/v1/chat/completions", data=payload,
                    headers={"Content-Type":"application/json", "Authorization":"Bearer "+self.token})
        parts, reason = [], None
        with HTTP.open(req, timeout=120) as response:
            for line in response:
                if self.cancel.is_set() or END.is_set():
                    raise concurrent.futures.CancelledError()
                if not line.startswith(b"data:"):
                    continue
                event = line[5:].strip()
                if event == b"[DONE]":
                    break
                chunk = json.loads(event)
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"])[:120])
                for choice in chunk.get("choices", []):
                    content = choice.get("delta", {}).get("content")
                    if content:
                        parts.append(content)
                        if data.get("stop_after_code") and chr(96) in content:
                            assembled = "".join(parts)
                            fenced = re.search(r"(?ms)^" + chr(96)*3 + r"(?:python3?|py)?[ \t]*\n.+?^" + chr(96)*3, assembled)
                            if fenced:
                                return fenced[0]
                    reason = choice.get("finish_reason") or reason
        text = "".join(parts).strip()
        if reason != "stop":
            raise RuntimeError("生成未完整结束：" + str(reason))
        if not text:
            raise RuntimeError("本地模型返回空答案")
        return text


def watch_interaction():
    previous = None
    while not END.is_set():
        pressed = bool(ctypes.windll.user32.GetAsyncKeyState(0x12) & 0x8000)
        if pressed != previous:
            emit("interaction", enabled=pressed)
            previous = pressed
        END.wait(.05)


def link_child_process_lifetime():
    # Windows can terminate the reader before Python reaches its finally block.
    # Children inherit this job, whose last handle lives in the reader itself.
    from ctypes import wintypes
    size = ctypes.c_size_t
    class Limits(ctypes.Structure):
        _fields_ = [("user_time",ctypes.c_longlong),("job_time",ctypes.c_longlong),
                    ("flags",wintypes.DWORD),("working_min",size),("working_max",size),
                    ("active",wintypes.DWORD),("affinity",size),("priority",wintypes.DWORD),
                    ("scheduling",wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("limits",Limits),("io",ctypes.c_ulonglong*6),("memory",size*4)]
    kernel = ctypes.windll.kernel32
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if job:
        handle = wintypes.HANDLE(job)
        limits = Extended()
        limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not (kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits))
                and kernel.AssignProcessToJobObject(handle,wintypes.HANDLE(-1))):
            kernel.CloseHandle(handle)


def run(box, api=False):
    threading.Thread(target=watch_interaction, daemon=True).start()
    model = Model()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    cache = ReaderCache(MODELS / "reader-cache.sqlite3")
    profile = ("api", os.environ.get("MC_READER_MODEL_SCOPE","")) if api else ("local",MODEL.name,MODEL.stat().st_mtime_ns)

    def answer_key(paper, question):
        content = "\n".join(line.rstrip() for line in question.splitlines()).strip()
        version = "reader-fill-v1" if content.startswith("填空题\n") and "【空" in content else "reader-answer-v2"
        return fingerprint(version, profile, paper, content)

    def generate(question):
        if not api and (model.proc is None or model.proc.poll() is not None):
            if not model.launch("14") and not END.is_set() and not model.launch("0"):
                raise RuntimeError("无法启动本地模型")
        if model.cancel.is_set() or END.is_set():
            raise concurrent.futures.CancelledError()
        return model.answer(question)

    try:
        if api:
            model.complete = AppProvider(emit, END, model.cancel).complete
        ctypes.windll.user32.SetProcessDPIAware()
        digest = shown = pending_key = None
        current = None
        pending = None
        current_key = None
        blank_since = None
        attempted_preview = None
        emit("status", text="读屏中，等待题目…")
        previous_anchor = None
        while not END.is_set():
            if box:
                image = ImageGrab.grab(bbox=box, all_screens=True)
                signature = hashlib.blake2s(image.convert("L").tobytes()).digest()
                candidate = recognize(image) if signature != digest else current
                digest = signature
            else:
                foreground_pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(
                    ctypes.windll.user32.GetForegroundWindow(), ctypes.byref(foreground_pid))
                if current and foreground_pid.value == os.getppid():
                    candidate = current
                else:
                    try:
                        candidate = browser_question(cache)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        candidate = ""
            anchor = box[:2] if box else getattr(browser_question, "anchor", None)
            if candidate and anchor and (anchor != previous_anchor or candidate != current):
                emit("anchor", x=anchor[0], y=anchor[1])
                previous_anchor = anchor
            if not candidate:
                if blank_since is None:
                    blank_since = time.monotonic()
                if current and time.monotonic()-blank_since < 1.8:
                    END.wait(.15)
                    continue
            else:
                blank_since = None
            paper = "region" if box else getattr(browser_question,"paper","")
            key = answer_key(paper,candidate) if candidate else None
            if key != current_key:
                current, current_key, shown = candidate, key, None
                saved = cache.get_answer(key,current) if key else None
                if pending and pending_key != key and not saved:
                    model.cancel.set()
                if saved:
                    emit("answer", question=current, text=saved, cached=True)
                    shown = key
                else:
                    emit("page", text="正在解答当前题…" if current else "等待浏览器题目页面…")
            if pending and pending.done():
                try:
                    result = pending.result()
                except concurrent.futures.CancelledError:
                    result = None
                except (ValueError, OSError, RuntimeError) as exc:
                    result = "作答失败：" + str(exc)[:100]
                if result and not model.cancel.is_set():
                    if not result.startswith("作答失败："):
                        cache.put(pending_key,result)
                    if current_key == pending_key:
                        emit("answer", question=current, text=result)
                        shown = current_key
                pending = None
            if current and shown != current_key and not pending:
                emit("status", text="已读题，API 正在回答…" if api else "已读题，本地模型正在回答…")
                model.cancel.clear()
                pending_key = current_key
                pending = pool.submit(generate, current)
            elif current and shown == current_key and not pending and not api and not box:
                preview = getattr(browser_question, "upcoming", "")
                if preview:
                    next_key = answer_key(paper, preview)
                    if next_key not in (current_key, attempted_preview) and cache.get_answer(next_key,preview) is None:
                        attempted_preview = pending_key = next_key
                        model.cancel.clear()
                        pending = pool.submit(generate, preview)
            END.wait(.65)
    finally:
        model.cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
        model.close()
        cache.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--ocr-image"]:
        print(read_image_blocks(sys.stdin.buffer.read()))
        sys.exit(0)
    link_child_process_lifetime()
    def watch_parent():
        os.read(0, 1)
        END.set()
    api = "--app-api" in sys.argv[1:]
    if not api:
        threading.Thread(target=watch_parent, daemon=True).start()
    try:
        run(tuple(int(x) for x in sys.argv[1:] if x!="--app-api"), api)
    except Exception as exc:
        if not END.is_set():
            emit("error", text=str(exc)[:240])
