"""Choice decisions keep the original labels, recover malformed responses and ignore old failure entries."""
from pathlib import Path
import sys
import json
import tempfile
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reader_cache import ReaderCache
from screen_worker import Model

choice="多选题\n1. 请选择符合条件的项。\nA. 第一项\nB. 第二项\nC. 以上两项"
model=Model()
model.complete=Mock(return_value=json.dumps({k:{"reason":"fixture","pick":k in "AC"} for k in "ABC"}))
assert model.answer(choice)=="A、C　〔模型原序作答〕"
assert model.complete.call_count==1
assert model.complete.call_args.args[0]["messages"][1]["content"]==choice
one=json.dumps({k:{"reason":"fixture","pick":k=="B"} for k in "ABC"})
two=json.dumps({k:{"reason":"fixture","pick":k in "BC"} for k in "ABC"})
for final, expected in [(two,"B、C"),(one,"B")]:
    model.complete=Mock(side_effect=[one,final])
    assert model.answer(choice)==expected+"　〔多选已复查〕"
    assert model.complete.call_count==2
model.complete=Mock(return_value=one)
assert model.answer(choice.replace("多选题","不定项选择题"))=="B　〔模型原序作答〕"
assert model.complete.call_count==1
single="单选题\n2. 请选择一项。\nA. 第一行\n续行\nB. 第二项"
model.complete=Mock(side_effect=['{"selected":["A","B"]}','{"selected":["A"]}'])
assert model.answer(single)=="A · 第一行 续行　〔模型原序作答〕"
assert model.complete.call_count==2
model.complete=Mock(side_effect=['not JSON','{"selected":["X"]}'])
try:
    model.answer(single)
except ValueError:
    pass
else:
    raise AssertionError("malformed answers must not become decisions")
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
    cache=ReaderCache(Path(directory)/"cache.sqlite")
    for old in ["待核对：交换选项顺序后答案不一致","待核对：选项解析失败"]:
        cache.put("q",old)
        assert cache.get_answer("q") is None
    cache.put("q","A、C　〔模型两次一致〕")
    assert cache.get_answer("q")=="A、C　〔模型两次一致〕"
    cache.put("q","B · 乙、丙　〔模型原序作答〕")
    assert cache.get_answer("q",choice) is None
    assert cache.get_answer("q",single)
    cache.put("q","B　〔多选已复查〕")
    assert cache.get_answer("q",choice)=="B　〔多选已复查〕"
    cache.close()
print("PASS original-order single and multiple choice, format recovery, legacy cache")

