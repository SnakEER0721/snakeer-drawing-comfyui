"""Detect dead controls: form fields the request path never reads.

Why this test exists: #negative (main view) and #negExtra (parameters) both
looked like the negative-prompt input, but composedNegative() only ever read
#negExtra. A user could type into the other one and have it silently discarded -
and a later pass even gave it a helpful placeholder, which made it look more
functional, not less.

The check: every textarea/select/input with an id must either be read somewhere
in the script (appear inside a $("id") or getElementById lookup) or be declared
purely presentational. Anything else is a field that accepts input and drops it.
"""
import os
import io
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

P = paths.UI_HTML
t = io.open(P, encoding="utf-8").read()

# 只检查可见的输入控件（checkbox 之类由 chips 生成的不算）
tags = re.findall(r"<(textarea|select|input)\b([^>]*)>", t, re.I)
ids = []
for tag, attrs in tags:
    if tag.lower() == "input":
        it = re.search(r'type="([^"]+)"', attrs, re.I)
        if it and it.group(1).lower() in ("hidden", "range", "checkbox", "radio"):
            continue          # range 由 bind() 处理，checkbox 由 chips 生成
    m = re.search(r'id="([^"]+)"', attrs)
    if m:
        ids.append((m.group(1), tag.lower()))

print("可见输入控件: %d 个" % len(ids))
print()

dead = []
for i, tag in ids:
    # 该 id 是否在脚本里被读取
    read = ('$("%s")' % i) in t or ("getElementById('%s')" % i) in t \
        or ('getElementById("%s")' % i) in t
    if not read:
        dead.append((i, tag))

if dead:
    print("未被脚本读取的控件（输入会被丢弃）:")
    for i, tag in dead:
        print("   %-18s <%s>" % (i, tag))
else:
    print("全部输入控件都会被读取")

# 额外检查：两个框指向同一设置时，必须同步
print()
n_neg = t.count('id="negative"')
n_ext = t.count('id="negExtra"')
print("negative 元素: %d   negExtra 元素: %d" % (n_neg, n_ext))
synced = "negSync" in t
if n_neg and n_ext and not synced:
    print("   FAIL 两个负向词框同时存在但没有同步")
else:
    print("   OK   负向词框已同步（或只存在一个）")

# ---------------------------------------------------------------------------
# 下面两块是补上的盲区。原来这个测试只检查 textarea/select/input，
# 于是两类真 bug 全都漏过去了：
#   * cmpFlip 是个 <button>，有样式能点，但从来没有绑定 → 点了没反应
#   * cnVal 是数值标签，滑块那边绑没绑它没人查 → 数字永远停在默认值
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("按钮：有没有存在但没绑事件的")
print("=" * 70)
btns = re.findall(r"<button\b([^>]*)>", t, re.I)
btn_ids = []
for a in btns:
    m = re.search(r'id="([^"]+)"', a)
    if m:
        btn_ids.append(m.group(1))
print("带 id 的按钮: %d 个" % len(btn_ids))

# 事件有两种挂法，都要认：
#   1) 直接挂：        $("cmpFlip").onclick = ...
#   2) 经变量别名挂：  const a = $("pbAddBtn");  ...  a.onclick = ...
# 只认第 1 种会误报 6 个。但也不能只放宽正则到"出现过就算" —— 那样
# cmpFlip 这种一次都没出现的会被放过，检查器就白写了。
#
# 别名匹配必须带「就近」限制。踩过的坑：全文件按变量名匹配时，
#   const f = $("pbFill"), c = $("pbClear"), a = $("pbAddBtn");
# 声明里的 `c` 会和文件别处（渲染循环里的 `const c = ...`，以及
# 卡片里的 `c.onclick = ...`）撞名，于是 pbClear 的绑定即使被删掉，
# 检查器也认为"有绑定" —— 假阴性，比误报危险得多。
# 因为它要能找出「绑定被删掉」这种情况，所以这里只在别名声明处往后
# 2500 字符内找绑定，跨过这个距离就不算同一个变量了。
ALIAS_WINDOW = 2500
alias_spans = {}                   # 按钮 id -> [(变量名, 声明位置)]
for m in re.finditer(r"(?:const|let|var)\s+([^;\n]+)", t):
    decl = m.group(1)
    for var, bid in re.findall(r'([A-Za-z_$][\w$]*)\s*=\s*\$\("([^"]+)"\)', decl):
        alias_spans.setdefault(bid, []).append((var, m.start()))


def handler_forms(bid):
    """这个按钮所有可能被挂事件的写法，返回匹配到的那些。"""
    hit = []
    for evt in ("onclick", "oninput", "onchange", "addEventListener"):
        if re.search(r'\$\("%s"\)\s*\.\s*%s' % (re.escape(bid), evt), t):
            hit.append('$("%s").%s' % (bid, evt))
    for var, pos in alias_spans.get(bid) or []:
        window = t[pos:pos + ALIAS_WINDOW]
        for evt in ("onclick", "oninput", "onchange", "addEventListener"):
            if re.search(r"\b%s\s*\.\s*%s" % (re.escape(var), evt), window):
                hit.append("%s.%s（别名，距声明 %d 字符）"
                           % (var, evt, window.find(var + ".")))
    if re.search(r'\bbind\w*\(\s*"%s"' % re.escape(bid), t):
        hit.append('bind("%s",...)' % bid)
    return hit


dead_btn = [i for i in btn_ids if not handler_forms(i)]
if dead_btn:
    print("   FAIL 这些按钮点了不会有任何反应:")
    for i in dead_btn:
        print("      %s" % i)
else:
    print("   全部按钮都有事件绑定（直接挂或用别名挂）")

print()
print("=" * 70)
print("滑块数值显示：每个 <span class=val> 是否有 bind/bindV 绑定")
print("=" * 70)
spans = re.findall(r'<span class="val" id="([^"]+)"[^>]*>([^<]*)</span>', t)
bound_displays = {d for _s, d in re.findall(r'\bbindV?\(\s*"([^"]+)"\s*,\s*"([^"]+)"', t)}
# maskZoomVal 由 maskApplyView() 直接写 textContent，不走 bind
DIRECT = {"maskZoomVal"}
print("数值显示 span: %d 个" % len(spans))
unbound = []
for sid, default in spans:
    if sid in bound_displays or sid in DIRECT:
        continue
    unbound.append((sid, default))
if unbound:
    print("   FAIL 这些数字标签没有更新来源（拖动滑块它不会变）:")
    for sid, d in unbound:
        print("      %-16s 默认显示 %r" % (sid, d))
else:
    print("   全部数值标签都有更新来源")

print()
if dead or dead_btn or unbound or (n_neg and n_ext and not synced):
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: ALL PASS")
