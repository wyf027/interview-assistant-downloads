from pathlib import Path
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from page_text import question_text
from screen_worker import Model

def fill(lines, editors):
    return question_text({"kind":"填空题","lines":[{"text":line} for line in lines],"blankEditors":editors})

q = fill(["2.", "请填两项。", "【空1】", "【空2】"], 2)
assert q.count("【空") == 2
model = Model()
model.complete = Mock(side_effect=['{"1":"甲"}','{"2":"乙","1":"甲"}'])
assert model.answer(q) == "第1空　甲\n第2空　乙"
assert model.complete.call_count == 2
implicit = fill(["3.", "求输入为 5 时的余数。"], 1)
assert "5" in implicit and implicit.endswith("待填：【空1】")
model.complete = Mock(return_value='{"1":0}')
assert model.answer(implicit) == "0"
model.complete = Mock(return_value='{"1":"甲","2":""}')
try:
    model.answer(q)
except ValueError:
    pass
else:
    raise AssertionError("An empty slot must not be displayed as a complete answer")
print("PASS explicit and implicit blanks, count validation, ordering and numeric zero")
