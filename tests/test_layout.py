# -*- coding: utf-8 -*-
"""三栏布局的结构回归：哪张卡在哪一栏、窄屏降级规则还在不在。

为什么要测：三栏是靠 CSS 的 grid-template-areas 定位的，DOM 顺序**不**决定
位置。所以有人把一张卡挪到错误的 .col 里，宽屏下可能"看着还行"，窄屏降级或
DOM 顺序就错了 —— 而且不会有任何报错。这里把归属钉死。

纯静态检查，不开浏览器。
"""
import os
import io
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

UI = paths.UI_HTML
t = io.open(UI, encoding="utf-8").read()
fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def col_span(name):
    """返回 .col-<name> 这个 div 的内容区间 [起, 止)。"""
    m = re.search(r'<div class="col col-%s">' % name, t)
    if not m:
        return None
    i, depth = m.end(), 1
    for d in re.finditer(r"<div\b|</div>", t[m.end():]):
        depth += 1 if d.group(0) == "<div" else -1
        if depth == 0:
            return (m.end(), m.end() + d.start())
        i = d.end()
    return None


print("=" * 70)
print("【1】三栏都在")
spans = {}
for name in ("left", "mid", "right"):
    s = col_span(name)
    spans[name] = s
    check(s is not None, ".col-%s 存在" % name)

print()
print("【2】每张卡在该在的栏里")
# (控件 id 或标记, 应该在哪一栏, 说明)
BELONG = [
    ("id=\"promptCN\"", "left", "正向提示词框"),
    ("id=\"dropSource\"", "left", "源图（图生图）"),
    ("id=\"negBox\"", "left", "负向提示词"),
    ("id=\"metaBox\"", "left", "从成品图读回参数"),
    ("id=\"pbBox\"", "left", "提示词拼装"),
    ("id=\"sketchBox\"", "left", "草图构图"),
    ("id=\"refBox\"", "left", "参考图 / IP-Adapter"),
    ("id=\"depthBox\"", "left", "构图迁移（深度图）"),
    ("id=\"btnGen\"", "mid", "生成按钮"),
    ("id=\"recent\"", "mid", "最近产出"),
    ("id=\"lastEnglish\"", "mid", "本次使用的英文提示词"),
    ("id=\"steps\"", "right", "步数"),
    ("id=\"cfg\"", "right", "CFG"),
    ("id=\"sampler\"", "right", "采样器"),
    ("id=\"loraGroups\"", "right", "LoRA 分组"),
    ("id=\"sampler\"", "right", "采样器下拉"),
]
for needle, want, label in BELONG:
    where = None
    for name, sp in spans.items():
        if sp and sp[0] <= t.find(needle) < sp[1]:
            where = name
            break
    check(where == want, "%-22s 在 %s 栏（实际 %s）" % (label, want, where))

print()
print("【3】窄屏降级规则还在")
check(re.search(r"@media \(max-width:1240px\)\s*\{\s*\.wrap\{[^}]*"
                r'grid-template-areas:"left mid" "right mid"', t) is not None,
      "≤1240px 退回两栏，参数掉到左栏下面，中栏跨两行")
check(re.search(r"@media \(max-width:900px\)\s*\{\s*\.wrap\{[^}]*"
                r'grid-template-areas:"left" "mid" "right"', t) is not None,
      "≤900px 退回单栏，顺序 提示词 → 出图 → 参数")
check('grid-template-areas:"left mid right"' in t, "宽屏是三栏")

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
