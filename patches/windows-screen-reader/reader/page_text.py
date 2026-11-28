"""Reconstruct lines from the browser's accessible text and layout boxes."""
import re
import unicodedata


MATH = {c: unicodedata.normalize("NFKC", chr(c)) for c in range(0x1D400, 0x1D800)}


def question_text(page):
    choices = page["kind"] in {"单选题", "多选题", "不定项选择题", "判断题"}
    lines = []
    previous = base = None
    script = None
    for node in page["lines"]:
        raw = node["text"].translate(MATH).replace("\r\n", "\n")
        value = raw.strip()
        if not value:
            continue
        rect = node.get("rect")
        join = False
        level = None
        if rect and previous and base and "\n" not in value:
            x, y, width, height = rect
            bx, by, bw, bh = base
            join = (x >= previous[0] and x >= previous[0] + previous[2] - max(height*.4, 3)
                    and abs((y+height/2) - (by+bh/2)) < bh*.73)
            if join and height < bh*.84 and value not in {"′", "’", "‘", "'"}:
                level = "_" if y + height/2 > by + bh*.56 else "^"
        if join:
            if level:
                if level == script:
                    lines[-1] = lines[-1][:-1] + value + "}"
                else:
                    lines[-1] += level + "{" + value + "}"
            elif choices and re.fullmatch(r"[A-H]", lines[-1]):
                lines[-1] += ". " + value
            elif len(lines) <= 3 and re.fullmatch(r"\d+[.．]", lines[-1]):
                lines[-1] += " " + value
            else:
                space = " " if raw[0].isspace() or (previous and x-previous[0]-previous[2] > height*.35) else ""
                lines[-1] += space + value
        else:
            lines.append(value)
        if not level:
            base = rect
        script, previous = level, rect
    # Some sites position the option badge on a separate typographic baseline.
    merged = []
    for line in lines:
        if merged and ((choices and re.fullmatch(r"[A-H]", merged[-1])) or
                       (len(merged) <= 3 and re.fullmatch(r"\d+\.", merged[-1]))):
            merged[-1] += (". " if merged[-1].isalpha() else " ") + line
        else:
            merged.append(line)
    text = page["kind"] + "\n" + "\n".join(merged)
    if page["kind"] == "填空题" and page.get("blankEditors") and not re.search(r"【空\d+】", text):
        text += "\n待填：" + " ".join(f"【空{i+1}】" for i in range(page["blankEditors"]))
    return text
