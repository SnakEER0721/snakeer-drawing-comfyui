"""Audit test scripts for missing imports before running them.

Caught a real instance: test_static.py called sys.exit() without importing sys,
so it crashed at the final line after printing "ALL PASS" - the worst place for
a failure, since the output looked successful while the exit code was 1.

Written as a file rather than `python -c`: inline snippets have repeatedly
broken on this machine (quoting, and here, a missing import in the snippet
itself).
"""
import ast
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

D = paths.TESTS_DIR
STDLIB = ("sys", "os", "re", "json", "time", "shutil", "base64", "sqlite3",
          "subprocess", "hashlib", "struct", "urllib", "random", "math",
          "tempfile", "traceback", "importlib", "threading", "uuid", "glob")
# ★ 项目自己的模块也要查。
#
#   实测漏过：test_api.py / test_inpaint.py / exp_poisson.py 都用了
#   `paths.SERVER_PY` 却从来没 import paths，**一跑就 NameError**。
#   这个审计脚本当时看不见 —— 因为它只遍历上面那张 STDLIB 表，
#   而 `paths` 是项目模块，不在表里。于是发布包里一直带着三个跑不起来的脚本，
#   直到真的去跑一遍慢速层才发现。
#
#   教训：审计的"覆盖面"本身也要被审。写死一张表就等于只查那张表。
PROJECT = ("paths", "server", "cn_translate", "poisson_blend",
           "install_models", "browser_helper", "serverguard")
CHECK = STDLIB + PROJECT

problems = []
for f in sorted(os.listdir(D)):
    # ★ 原来只查 test_*.py，跳过了 exp_*.py 和两个辅助模块（browser_helper /
    #   serverguard）—— exp_poisson.py 的 bug 就是这么漏掉的。
    if not f.endswith(".py"):
        continue
    if f == os.path.basename(__file__):
        continue          # 不审计自己（自身源码里的字符串会被误判）
    p = os.path.join(D, f)
    src = io.open(p, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        problems.append("%s: 语法错误 %s" % (f, e))
        continue
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported.add(a.asname or a.name)
            if n.module:
                imported.add(n.module.split(".")[0])
    # ★ 只在**代码**里找 "模块名."，不要在注释和字符串里找。
    #   踩过的坑：test_static.py 的允许清单里存了一个代码锚点 "os.remove(hard)"
    #   当键，那是字符串不是调用，却让这个测试报了"用了 os. 但未 import"。
    #   字符串/注释里提到某个模块名，本来就不该算使用。
    code_only = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            code_only.append("%s." % n.value.id)
    code_only = "\n".join(code_only)
    for mod in CHECK:
        if re.search(r"\b%s\." % mod, code_only) and mod not in imported:
            problems.append("%s: 用了 %s. 但未 import" % (f, mod))
    # 调用了 sys.exit 但没有 import sys 的情况最危险：输出看着成功、退出码却是 1
    if "sys.exit(" in code_only and "sys" not in imported:
        problems.append("%s: 调用了 sys.exit 但未 import sys（退出码会错）" % f)

print("=" * 74)
print("测试脚本导入审计")
print("=" * 74)
print("  扫了 tests/ 下所有 .py（含 exp_*.py 和辅助模块）")
print("  检查的模块: 标准库 %d 个 + 项目模块 %d 个（%s）"
      % (len(STDLIB), len(PROJECT), ", ".join(PROJECT)))
if problems:
    for x in problems:
        print("   ❌ %s" % x)
    print()
    print("RESULT: FAIL  %d 个问题" % len(problems))
else:
    print("   ✅ 全部测试脚本导入完整")
    print()
    print("RESULT: ALL PASS")
print("=" * 74)
