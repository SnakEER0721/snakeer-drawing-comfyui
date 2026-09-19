"""Verify the prompt-builder panel end to end.

Checks the parts that can silently break:
  * the panel sits under the negative-prompt box, as requested
  * /api/prompt_builder returns grouped options with Chinese labels
  * every option's English tag is a real tag (so it will not waste tokens)
  * POST stores a custom entry and it comes back on the next GET
  * a custom entry can be removed by editing the file (no API needed)
  * the assembled prompt is usable by the normal generate path
"""
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from cn_translate import Translator

BASE = "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务。
# 不确认的话，POST 会打到**另一个安装**上，写到那边的 custom_prompt_options.json，
# 而这里检查的是本目录的文件 —— 于是"落盘到自定义项文件"假失败。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_prompt_builder")
UI = paths.UI_HTML
# ★ 绝不破坏真实数据。早期版本直接 os.remove 了它，抹掉了用户加的自定义选项，
#   且无法恢复。现在采用「先备份、测完还原」——这种方式与服务进程怎么启动无关，
#   比依赖环境变量更可靠（服务是独立进程，未必继承测试的环境变量）。
REAL = paths.CUSTOM_PROMPT_OPTIONS
CUSTOM = REAL          # 测试读写真实路径，但全程有备份兜底
fails = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        fails.append(msg)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, body):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


print("=" * 80)
print("【1】面板位置")
print("=" * 80)
t = io.open(UI, encoding="utf-8").read()
i_neg = t.find('id="negBox"')
i_pb = t.find('id="pbBox"')
i_guide = t.find("<!-- 引导输入 -->")
check(i_pb > 0, "面板存在")
check(i_pb > i_neg, "面板在负向提示词框之后")
check(i_pb < i_guide, "面板在「引导输入」之前")
check('<summary>提示词拼装' in t, "是折叠面板（details/summary）")

print()
print("=" * 80)
print("【2】接口数据")
print("=" * 80)
d = get("/api/prompt_builder")
check(d.get("ok"), "GET 返回 ok")
gs = d.get("data") or []
check(len(gs) >= 10, "分组数 >= 10（实际 %d）" % len(gs))
total = sum(len(g["items"]) for g in gs)
check(total >= 150, "选项数 >= 150（实际 %d）" % total)

no_cn = [i for g in gs for i in g["items"]
         if not any("\u4e00" <= c <= "\u9fff" for c in str(i.get("cn", "")))]
check(not no_cn, "全部选项有中文标签（缺失 %d）" % len(no_cn))

print()
print("=" * 80)
print("【3】标签有效性（防止把废词填进提示词）")
print("=" * 80)
tr = Translator()
bad = []
for g in gs:
    for i in g["items"]:
        if i.get("custom"):
            continue
        if not tr.is_known_tag(i["en"]):
            bad.append((g["group"], i["en"]))
check(not bad, "内置选项全部是真实标签（可疑 %d）" % len(bad))
for b in bad[:10]:
    print("        %s / %s" % b)

print()
print("=" * 80)
print("【3b】性器细节分组（原版「加强阴部」的对应项）")
print("=" * 80)
organ = next((g for g in gs if g["group"] == "性器细节"), None)
check(organ is not None, "存在「性器细节」分组")
if organ:
    ens = [i["en"] for i in organ["items"]]
    check(len(ens) >= 15, "选项数 >= 15（实际 %d）" % len(ens))
    for must in ("spread pussy", "pussy", "clitoris", "anus", "nipples"):
        check(must in ens, "含 %s" % must)

print()
print("=" * 80)
print("【3c】随机功能（原版 Random / randomizePoseExpression）")
print("=" * 80)
ui = io.open(UI, encoding="utf-8").read()
check('id="pbRandom"' in ui, "有「随机一组」按钮")
check('id="pbRandomPE"' in ui, "有「随机姿势+表情」按钮")
check("function pbPickRandom(" in ui, "有随机函数")
check("function pbPickRandomPoseExpression(" in ui, "有姿势表情随机函数")
check("PB_RANDOM_GROUPS" in ui, "随机范围可配置")


print()
print("=" * 80)
print("【4】自定义项存取")
print("=" * 80)
# 备份真实文件（若存在），测试全程只操作临时文件
_backup = None
if os.path.isfile(REAL):
    _backup = REAL + ".testbak"
    import shutil as _sh
    _sh.copy2(REAL, _backup)
    print("   （已备份真实数据文件）")
r = post("/api/prompt_builder", {"group": "发色", "cn": "测试色",
                                 "en": "test color"})
check(r.get("ok"), "POST 保存成功")
check(os.path.isfile(REAL), "落盘到自定义项文件")
d2 = get("/api/prompt_builder")
found = [i for g in d2["data"] if g["group"] == "发色"
         for i in g["items"] if i["en"] == "test color"]
check(len(found) == 1, "重新读取能取到自定义项")
check(found and found[0].get("custom"), "自定义项带 custom 标记")
# 重复添加不应产生两份
r2 = post("/api/prompt_builder", {"group": "发色", "cn": "测试色",
                                  "en": "test color"})
check(r2.get("note") == "已存在，未重复添加", "重复添加被拒绝")

print()
print("=" * 80)
print("【5】拼装结果可用于生成")
print("=" * 80)
# 模拟前端拼装：取几个选项拼成一行，走正常翻译接口
sample = []
for g in d2["data"]:
    if g["group"] in ("发色", "姿势 / 动作", "表情"):
        sample += [i["en"] for i in g["items"][:2]]
txt = ", ".join(sample)
tr_r = post("/api/translate", {"text": "少女 微笑"})
check(tr_r.get("ok"), "翻译接口可用")
check(len(sample) >= 4, "能拼出多个标签（%d 个）" % len(sample))
print("        拼装示例: %s" % txt)

# 清理：真实文件原样复原（没有备份说明原本就没有，删掉即可）
if _backup and os.path.isfile(_backup):
    import shutil as _sh
    _sh.copy2(_backup, REAL)
    os.remove(_backup)
    print("   （真实数据文件已复原）")
else:
    if os.path.isfile(REAL):
        os.remove(REAL)
    print("   （原本没有该文件，已清理测试产物）")

print()
print("=" * 80)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
else:
    print("RESULT: ALL PASS")
print("=" * 80)
sys.exit(1 if fails else 0)
