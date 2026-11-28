"""Read explicitly paired examples from the current question."""
import re

_MARK = re.compile(
    r"(?im)^[ \t]*(?P<kind>输入例子|样例输入|输入样例|输出例子|样例输出|输出样例|"
    r"sample[ \t]+(?:input|output)|example[ \t]+(?:input|output)|输入|输出|input|output|"
    r"示例|样例|example|例子说明|样例说明|解释|说明|备注|数据范围|提示|constraints|explanation|"
    r"ACM 模式|单题模式)(?:[ \t]*#?[ \t]*(?P<index>\d+))?[ \t]*[:：]?[ \t]*\r?$")
_INPUTS = {"输入例子","样例输入","输入样例","sample input","example input"}
_OUTPUTS = {"输出例子","样例输出","输出样例","sample output","example output"}


def samples_from(question):
    marks = list(_MARK.finditer(question))
    cases = []
    example = False
    pending = None
    for i, mark in enumerate(marks):
        label = " ".join(mark["kind"].lower().split())
        body = question[mark.end():marks[i+1].start() if i+1<len(marks) else len(question)].strip("\r\n")
        if label in {"示例", "样例", "example"}:
            if pending is not None:
                return []
            example = True
        elif label in _INPUTS or (example and label in {"输入","input"}):
            if pending is not None:
                return []
            pending = (body + "\n", mark["index"])
        elif label in _OUTPUTS or (example and label in {"输出","output"}):
            if pending is None or not body.strip():
                return []
            data, index = pending
            if index and mark["index"] and index != mark["index"]:
                return []
            cases.append((data,body))
            pending = None
        else:
            if pending is not None:
                return []
            example = False
    return cases if pending is None else []
