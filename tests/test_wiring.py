"""Catch dead startup wiring: functions that populate the UI but are never called.

Real incident: while rewriting loadRecent(), the replacement slice ran one line
too far and deleted the trailing `loadCaps();` - the only call to it in the
file. Nothing errored; the page simply showed 检测中… forever and every
capability looked unchecked, while the backend and API were fine. A silent
failure, and no existing test covered "is this even called".

This checks that every function responsible for filling a visible region has at
least one call site, and that the essential boot calls are present.
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

# 这些函数负责填充界面或绑定事件，没有被调用就意味着界面某块是空的/死的
MUST_BE_CALLED = [
    "loadCaps",             # 顶部能力状态
    "loadRecent",           # 最近产出
    "renderQualityChips",   # 质量词勾选
    "renderLoraGroups",     # LoRA 分组
    "bindMaskUI",           # 蒙版编辑器事件
    "bindCompareUI",        # 对比弹窗事件
]

print("=" * 76)
print("【1】启动函数是否有调用点")
print("=" * 76)
# 两种定义方式都要认：
#   function foo(){}
#   const foo = (...) => {}
defined = set(re.findall(r"^(?:async )?function (\w+)", t, re.M))
defined |= set(re.findall(r"^\s*(?:const|let|var) (\w+)\s*=\s*(?:\(|async)", t, re.M))
for fn in MUST_BE_CALLED:
    if fn not in defined:
        fails.append("%s 未定义" % fn)
        print("   FAIL %-22s 未定义" % fn)
        continue
    n = len(re.findall(r"\b%s\s*\(" % re.escape(fn), t)) - 1
    ok = n > 0
    print("   %s %-22s 调用点 %d 个" % ("OK  " if ok else "FAIL", fn, n))
    if not ok:
        fails.append("%s 定义了但从不被调用" % fn)

print()
print("=" * 76)
print("【2】关键启动调用必须存在")
print("=" * 76)
# loadCaps 是页面唯一的启动入口，必须在脚本末尾被调用
boot = re.findall(r"^\s*loadCaps\(\);\s*$", t, re.M)
ok = len(boot) >= 1
print("   %s loadCaps() 启动调用 %d 处" % ("OK  " if ok else "FAIL", len(boot)))
if not ok:
    fails.append("缺少 loadCaps() 启动调用（状态区会一直显示「检测中…」）")

print()
print("=" * 76)
print("【3】每个被调用的函数都有定义（防运行时报错）")
print("=" * 76)
# 只查本项目自己的函数命名前缀，避免误报浏览器内置
called = set(re.findall(r"\b((?:pb|mask|render|load|bind|draw|open|cmp)\w*)\s*\(", t))
undef = sorted(c for c in called if c not in defined
               and c not in ("load", "render", "open", "maskApplyView"))
# 过滤掉对象方法式调用（如 x.load(...)）
real_undef = []
for c in undef:
    if re.search(r"\.\s*%s\s*\(" % re.escape(c), t):
        continue
    real_undef.append(c)
if real_undef:
    for c in real_undef[:10]:
        print("   FAIL %s 被调用但未定义" % c)
    fails.append("%d 个函数被调用但未定义" % len(real_undef))
else:
    print("   OK   全部有定义")

print()
print("=" * 76)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 76)
sys.exit(1 if fails else 0)
