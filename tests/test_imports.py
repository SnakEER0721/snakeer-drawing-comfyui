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
# 默认扫 tests/；`py tests/test_imports.py <目录>` 可以改扫别的目录。
# 这是给 A/B 用的：在**临时目录**造一个"先用后 import"的样本，喂给下面**同一份
# 逻辑**，证明它真的会报红 —— 光看正式目录是绿的，分不清"检查通过"和
# "检查器压根没查"（对齐 dev/check_alias_kind.py --file 的做法）。
if len(sys.argv) > 1:
    D = sys.argv[1]
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
    # ★ 模块名 -> 最早 import 它的行号。原来只记"出现过没有"，不看位置，
    #   于是"用了才 import"（一样是 NameError）全部漏掉。实测 3 个文件：
    #     tests/_verify_zoom_paint.py  第 17 行用 paths.OUT_ANIME，第 35 行才 import
    #     tests/exp_lora_effect.py     第 26 行用 paths.SERVER_PY，第 57 行才 import
    #     tests/exp_multi_character.py 第 20 行用 paths.SERVER_PY，第 27 行才 import
    #   真跑证据（改前）：py tests/exp_multi_character.py → 第 20 行 NameError: name 'paths' is not defined
    #   而同一时刻本测试打印 RESULT: ALL PASS。
    import_line = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add((a.asname or a.name).split(".")[0])
                # 取真模块名（`import x as y` 的真名是 x），别名不算"已 import x"。
                # ★ 取**最早**那一处：函数内部先 import 再用的写法是合法的
                #   （test_client_abort.py 第 89 行 import urllib.request、
                #    第 91 行才用 —— 取最后一处会把它误报成"先用后 import"）。
                nm = a.name.split(".")[0]
                import_line[nm] = min(import_line.get(nm, n.lineno), n.lineno)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported.add(a.asname or a.name)
            if n.module:
                nm = n.module.split(".")[0]
                imported.add(nm)
                import_line[nm] = min(import_line.get(nm, n.lineno), n.lineno)
    # ★ 只在**代码**里找 "模块名."，不要在注释和字符串里找。
    #   踩过的坑：test_static.py 的允许清单里存了一个代码锚点 "os.remove(hard)"
    #   当键，那是字符串不是调用，却让这个测试报了"用了 os. 但未 import"。
    #   字符串/注释里提到某个模块名，本来就不该算使用。
    code_only = []
    use_line = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            code_only.append("%s." % n.value.id)
            # ast.walk 是广度优先，不按行号走 —— 所以这里取最小值，不是 setdefault
            use_line[n.value.id] = min(use_line.get(n.value.id, n.lineno), n.lineno)
    code_only = "\n".join(code_only)
    for mod in CHECK:
        if re.search(r"\b%s\." % mod, code_only) and mod not in imported:
            problems.append("%s: 用了 %s. 但未 import" % (f, mod))
        # ★ 顺序也要查：首次使用在该模块 import 之前 = 运行时 NameError。
        #   只看"出现过没有"是不够的 —— 那正是上面 3 个文件漏掉的原因。
        elif mod in import_line and mod in use_line \
                and use_line[mod] < import_line[mod]:
            problems.append("%s: 第 %d 行就用了 %s.，但到第 %d 行才 import（先用后 import）"
                            % (f, use_line[mod], mod, import_line[mod]))
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

# ★ 必须给退出码。这个文件长期只打印 RESULT、从不 sys.exit —— 于是单独跑它、
#   或写进 CI 时，**报出 FAIL 也仍然是 exit 0**，等于静默通过。
#   而它守的正是「用了没 import / 先用后 import」这类运行时 NameError，
#   旁边 3 个坏文件（_verify_zoom_paint / exp_lora_effect / exp_multi_character）
#   就是它自己漏掉的。讽刺的是它查的规则里有一条正是"调了 sys.exit 却没 import sys" ——
#   它规定了别人要有退出码，自己却没有。
#   （这条是被另一路审计 agent 独立发现的：全文件 4 处 `sys.exit` 全是字符串和注释。）
sys.exit(1 if problems else 0)
