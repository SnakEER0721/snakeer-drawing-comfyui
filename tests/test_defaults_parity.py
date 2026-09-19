# -*- coding: utf-8 -*-
"""默认值一致性：界面控件的默认值 == server.py 的兜底默认值。

为什么必须有这个测试：同一个参数的默认值写在两个地方（ui.html 的控件 value=，
和 server.py 的 p.get(x, 默认)）。两边不一致时**不会报任何错** ——
界面显示 5.5、服务端兜底 5，用户以为自己在用 5.5。这个坑已经踩过一次。

现在 server.py 的兜底值统一取自 DEFAULTS 表，这个测试做四件事：
  1. DEFAULTS 里每个键，要么有对应的界面控件、要么在 NO_UI 里写明为什么没有；
  2. 有控件的那几个，两边的值必须相等；
  3. server.py 里不允许再出现写死数字的 p.get("x", 30) —— 必须引用 DEFAULTS；
  4. 每个出图请求都要把采样参数带上（局部重绘漏送过 steps/cfg/sampler/seed，
     结果那四个控件在修图里静默失效）。
"""
import os
import io
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from server import DEFAULTS  # noqa: E402

UI = paths.UI_HTML
SRV = paths.SERVER_PY

ui = io.open(UI, encoding="utf-8").read()
srv = io.open(SRV, encoding="utf-8").read()

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


# 界面控件 id -> DEFAULTS 的键
PAIRS = {
    "steps": "steps",
    "cfg": "cfg",
    "clipSkip": "clip_skip",
    "count": "count",
    "width": "width",
    "height": "height",
    "sketchStrength": "sketch_strength",
    "refStrength": "ref_strength",
    "depthStrength": "depth_strength",
    "inpDenoise": "inpaint_denoise",
    "inpGrow": "mask_grow",
    "inpFeather": "mask_feather",
    "inpCnStrength": "controlnet_strength",
}
# 采样器 / 调度器的默认值不在 HTML 上，在 fillCurated 的第三个参数里
CURATED = {"sampler": "sampler", "scheduler": "scheduler"}
# 没有对应控件的键 —— 每个都要写明为什么
NO_UI = {
    "gen_denoise": "文生图恒为 1.0（图生图由 #denoise 滑杆给 0.55），没有独立控件",
}


def html_value(ctrl_id):
    """控件在 HTML 里的默认值。range/text 看 value=，select 看它自己的第一个 option。

    ★ select 的 option 必须只看它**自己**标签内的内容：第一版从标签结束处往后
      全文找 <option>，于是 #width（选项由 JS 填、标签内是空的）被读成了文件里
      下一个 select 的 "1" —— 测试自己报了一个不存在的 bug。
    """
    for m in re.finditer(r"<(input|select)\b[^>]*>", ui):
        tag = m.group(0)
        if ('id="%s"' % ctrl_id) not in tag:
            continue
        v = re.search(r'value="([^"]*)"', tag)
        if v:
            return v.group(1)
        if m.group(1) == "select":
            end = ui.find("</select>", m.end())
            inner = ui[m.end():end if end > 0 else m.end()]
            o = re.search(r"<option[^>]*>([^<]*)</option>", inner)
            return o.group(1) if o else None      # 选项由 JS 填 -> None，往下走 js_number
        return None
    return None


def js_number(name, ctrl_id):
    """fillSelect($("width"), SIZES, 1024) / fillCurated(..., "dpmpp_2m") 的默认值。"""
    m = re.search(r"%s\(\$\(\"%s\"\)[^;]*?,\s*([0-9.]+|\"[^\"]*\")\s*\)\s*;" %
                  (name, re.escape(ctrl_id)), ui)
    return m.group(1).strip('"') if m else None


print("=" * 74)
print("【1】DEFAULTS 的每一个键都要有归属")
for k in DEFAULTS:
    if k in PAIRS.values() or k in CURATED.values():
        continue
    check(k in NO_UI, "DEFAULTS[%r] 有界面控件，或已在 NO_UI 里写明原因" % k)

print()
print("【2】界面默认值 == 服务端兜底值")
for ctrl, key in sorted(PAIRS.items()):
    got = html_value(ctrl)
    # 选项由 JS 填充的下拉框（#width/#height）默认值在 fillSelect 的第三个参数里
    if got is None:
        got = js_number("fillSelect", ctrl)
    want = DEFAULTS[key]
    same = got is not None and abs(float(got) - float(want)) < 1e-9
    check(same, "#%-16s = %-6s   DEFAULTS[%r] = %s" % (ctrl, got, key, want))
    if not same:
        print("        ^ 两边不一致：界面显示 %s，服务端兜底 %s" % (got, want))

for ctrl, key in sorted(CURATED.items()):
    got = js_number("fillCurated", ctrl)
    want = DEFAULTS[key]
    check(got == want, "fillCurated(#%s) = %-10s DEFAULTS[%r] = %s"
          % (ctrl, got, key, want))

print()
print("【3】server.py 里不许再写死兜底值（必须引用 DEFAULTS）")
hard = []
for m in re.finditer(r'p\.get\("([a-z_]+)",\s*([^)]+)\)', srv):
    key, val = m.group(1), m.group(2).strip()
    if key not in DEFAULTS:
        continue
    if not val.startswith("DEFAULTS["):
        hard.append((key, val))
check(not hard, "没有写死的兜底值" if not hard
      else "这些还在写死：%s" % hard)

print()
print("【4】每个出图请求都要带上采样参数")
# 局部重绘曾经漏送 steps/cfg/sampler/scheduler/seed —— 后端各自有兜底，
# 于是那四个控件在修图里静默失效，界面上完全看不出来。
# 出图请求的负载是一段单独的 JSON 字面量（fetch 那句里只有 JSON.stringify(payload)），
# 所以锚点用负载自己的字段，不能用 fetch 那一行。
ANCHORS = {"/api/generate": "prompt_cn:", "/api/inpaint": "path: maskState.path,"}
for api, need in (("/api/generate", ["steps", "cfg", "sampler", "scheduler"]),
                  ("/api/inpaint", ["steps", "cfg", "sampler", "scheduler", "seed"])):
    anchor = ANCHORS[api]
    idx = ui.find(anchor)
    if idx < 0:
        check(False, "找不到 %s 的请求负载（锚点 %r）" % (api, anchor))
        continue
    body = ui[idx:idx + 2200]
    missing = [k for k in need if not re.search(r"\b%s\s*:" % k, body)]
    check(not missing, "%s 送了 %s" % (api, "、".join(need))
          if not missing else "%s 漏送：%s" % (api, missing))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
