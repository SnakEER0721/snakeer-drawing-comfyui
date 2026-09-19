# -*- coding: utf-8 -*-
r"""路径卫生：全仓不许再出现写死的绝对路径。

为什么必须有这条：2026-09-18 我清掉了 139 处硬编码（15 处 sys.path.insert +
82 处以开发目录开头的路径 + 42 处解释器/产出目录/仿冒路径）。**清完不锁，
下次写个新脚本又写回去了** —— 这个仓库已经用同样的手法锁过好几次
（test_defaults_parity 锁默认值、test_layout 锁卡片归属、check_chapter_sync
锁章节名），路径也该锁上。

判据：.py / .html / .bat 里出现盘符开头的字面量，或 /home/ /Users/ 这种
Unix 家目录路径。白名单只放两类：
  · C:\Windows、C:\Program Files —— 系统路径，本来就该写死
  · 注释/文档里解释"以前写死过什么"的文字

纯静态检查，不开浏览器、不连服务。
"""
import ast
import io
import os
import re
import sys
import tokenize

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

# 跳过不扫的目录。dev/ 是开发脚本，不进发布包；data/ 是二进制的词库。
# （原来这里还列了一个私人目录名，那是本机才有的东西 —— 发布包里不存在，
#   而且光是把名字写在这儿就等于把私人目录名发出去了，所以删掉。
#   那个目录里只有 .safetensors，本来也不会被这个测试扫到。）
SKIP_DIRS = {"__pycache__", "dev", "data", ".ruff_cache", ".git"}
EXTS = (".py", ".html", ".bat")

# ★ 路径里能带空格（C:\Program Files\nodejs\node.exe），所以不能按空白截断 ——
#   第一版就是截在空格上，"C:\Program Files\..." 变成了 "C:\Program"，
#   于是系统路径白名单也认不出来，一堆误报。
PAT = re.compile(r'(?<![\w/])(?:[A-Za-z]:\\{1,2}[^\n"\']*|/(?:home|Users)/[^\s"\')]*)')
TRAIL = " \t）)】，,、。；;:："

# 系统路径：本来就该是绝对路径，跟"移植性"无关
ALLOW = re.compile(r"^[A-Za-z]:\\{1,2}(?:Windows|Program Files|System32)", re.I)


def prose_lines(src: str) -> set:
    """哪些行是"说明文字"（文档字符串 / # 注释），不是可执行代码。

    为什么必须区分：这些文件里到处写着"以前写死过 D:\\... 这种路径"这类解释，
    那是历史记录，不是硬编码。第一版只按 line.startswith("#") 判，docstring 里
    的解释就全被判成了违规。

    ★ 注意：**不能把普通字符串也跳过** —— 硬编码路径本来就活在字符串字面量里。
      所以只跳过文档字符串（ast）和 # 注释（tokenize）。
    """
    out = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                    out.add(ln)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.add(tok.start[0])
    except Exception:
        pass
    return out


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


fails = []
self_bad = []
bad = []
scanned = 0
for dp, dns, fns in os.walk(paths.APP_DIR):
    dns[:] = [d for d in dns if d not in SKIP_DIRS]
    for f in fns:
        if not f.endswith(EXTS):
            continue
        if f == os.path.basename(__file__):
            continue    # 不扫自己：本文件里那几段"自检样例"本来就是硬编码字符串
        p = os.path.join(dp, f)
        rel = os.path.relpath(p, paths.APP_DIR)
        scanned += 1
        try:
            t = io.open(p, encoding="utf-8").read()
        except UnicodeDecodeError:
            t = open(p, "rb").read().decode("gbk", "replace")
        skip = prose_lines(t) if f.endswith(".py") else set()
        for m in PAT.finditer(t):
            v = m.group(0).rstrip(TRAIL)
            if ALLOW.match(v):
                continue
            ln = t[:m.start()].count("\n") + 1
            if ln in skip:
                continue
            line = t.splitlines()[ln - 1].strip()
            bad.append((rel, ln, v, line[:88]))

print("=" * 78)
print("扫了 %d 个文件（.py/.html/.bat，跳过 dev/ data/ __pycache__）" % scanned)
print("=" * 78)
if bad:
    print("写死的绝对路径 %d 处：" % len(bad))
    for rel, ln, v, line in bad:
        print("  %-30s :%-4d %s" % (rel, ln, v[:50]))
        print("      %s" % line)
else:
    print("没有写死的绝对路径 ✅")

print()
print("=" * 78)
print("自检：这个测试真的抓得住违规吗")
print("=" * 78)
# 一个只会说 ALL PASS 的测试等于没有测试（这个仓库里已经有过这种教训：
# test_static.py 早期只打印不判定，回归里永远"通过"）。这里用几段合成代码
# 验证判据本身有效。
SAMPLE_CODE = 'P = r"D:\\somewhere\\else\\app.py"\n'
# 样例里必须写成 \\dsh\\webapp（双反斜杠）：单反斜杠在 ast.parse 时会报
# "invalid escape sequence '\d'"，那不是被测代码的问题，是自己写法的问题。
SAMPLE_DOC = '"""说明：以前写死过 D:\\\\dsh\\\\webapp 这种路径。"""\n'
SAMPLE_SYS = 'EDGE = r"C:\\Program Files\\Microsoft\\Edge\\msedge.exe"\n'


def _hits(src, ext=".py"):
    skip = prose_lines(src) if ext == ".py" else set()
    out = []
    for m in PAT.finditer(src):
        v = m.group(0).rstrip(TRAIL)
        if ALLOW.match(v):
            continue
        if src[:m.start()].count("\n") + 1 in skip:
            continue
        out.append(v)
    return out


check(_hits(SAMPLE_CODE) == [r"D:\somewhere\else\app.py"],
      "抓得住代码里的硬编码：%s" % _hits(SAMPLE_CODE))
check(_hits(SAMPLE_DOC) == [], "文档字符串里的历史说明会跳过：%s" % _hits(SAMPLE_DOC))
check(_hits(SAMPLE_SYS) == [], "系统路径（C:\\Program Files）会放行：%s" % _hits(SAMPLE_SYS))
check(_hits('x = "/home/someone/app.py"') != [], "抓得住 Unix 家目录路径")

print()
if bad or self_bad:
    print("RESULT: FAIL（%d 处硬编码，%d 项自检失败）" % (len(bad), len(self_bad)))
    sys.exit(1)
print("RESULT: ALL PASS")
