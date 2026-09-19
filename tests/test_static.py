"""Static audit of server.py: find error-swallowing handlers and risky patterns.

Silently swallowed exceptions are exactly how the bugs in this project stayed
hidden (the file-lock cleanup and the discarded HTTP-400 body were both
`except: pass`). This lists every handler and whether it reports anything.
"""
import os
import sys
import ast
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

PATH = paths.SERVER_PY
src = open(PATH, encoding="utf-8").read()
tree = ast.parse(src)
lines = src.splitlines()


def classify(handler: ast.ExceptHandler) -> str:
    body = handler.body
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "SILENT (pass)"
    if len(body) == 1 and isinstance(body[0], ast.Continue):
        return "SILENT (continue)"
    if len(body) == 1 and isinstance(body[0], ast.Return):
        return "returns None silently"
    called = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name:
                called.append(name)
    loggers = sorted({c for c in called
                      if c in ("print", "print_exc", "warning", "error",
                               "exception", "debug", "raise_log", "warn")})
    if loggers:
        return "reports via " + ",".join(loggers)
    return "no log: " + ",".join(sorted(set(called))[:4])


print("=" * 78)
print("异常处理器审计")
print("=" * 78)
silent = []
for node in ast.walk(tree):
    if isinstance(node, ast.ExceptHandler):
        kind = classify(node)
        text = lines[node.lineno - 1].strip()
        flag = ""
        if "SILENT" in kind or kind.startswith("no log"):
            flag = "   <-- 静默，需修"
            silent.append(node.lineno)
        print("  L%-4d %-32s %s%s" % (node.lineno, text[:32], kind, flag))
print()
print("静默处理器: %d 处 -> %s" % (len(silent), silent))

print()
print("=" * 78)
print("其他风险模式")
print("=" * 78)
risks = []
for i, l in enumerate(lines, 1):
    s = l.strip()
    if s.startswith("#"):
        continue
    if s.startswith("def ") and ("=[]" in s.replace(" ", "")
                                 or "={}" in s.replace(" ", "")):
        risks.append((i, "可变默认参数", s))
    if re.match(r"^\s*\w+\s*=\s*open\(", l):
        risks.append((i, "open 未用 with", s))
    if re.search(r"/\s*0(?!\d)", s):
        risks.append((i, "可能除零", s))
if risks:
    for ln, kind, s in risks:
        print("  L%-4d [%s] %s" % (ln, kind, s[:66]))
else:
    print("  未发现")

print()
print("=" * 78)
print("编码与一致性")
print("=" * 78)
# ★ 这份清单要跟文件末尾"编码检查"那份一致：2026-09-18 加了 paths.py，它是新拆
#   出来的路径模块，之前完全没被任何审计扫过。
AUDITED = [PATH, paths.UI_HTML, os.path.join(paths.APP_DIR, 'cn_translate.py'),
           os.path.join(paths.APP_DIR, 'paths.py'), os.path.join(paths.APP_DIR, 'poisson_blend.py')]
for p in AUDITED:
    raw = open(p, "rb").read()
    try:
        raw.decode("utf-8")
        u = "UTF-8 OK"
    except Exception as e:
        u = "BAD: %s" % e
    print("  %-20s BOM=%-5s %s" % (p.split("\\")[-1], raw[:3] == b"\xef\xbb\xbf", u))

# every ComfyUI node type the server emits must exist
print()
print("=" * 78)
print("ComfyUI 节点类型引用检查")
print("=" * 78)
import json
import urllib.request
used = sorted(set(re.findall(r'"class_type":\s*"([^"]+)"', src)))
print("  server.py 使用了 %d 种节点: %s" % (len(used), ", ".join(used)))
missing = []
try:
    with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=120) as r:
        info = json.loads(r.read())
    missing = [u for u in used if u not in info]
    if missing:
        print("  !! 不存在的节点: %s" % missing)
    else:
        print("  OK - 全部节点在 ComfyUI 中可用")
except Exception as e:
    print("  无法查询 ComfyUI:", e)


# ---------------------------------------------------------------------------
# 汇总与退出码
# 之前这个脚本只打印、不判定，所以回归里它永远显示「没有汇总输出」——
# 一个不能通过也不能失败的脚本不是测试。下面把两项检查变成可判定的。
#
# 2026-09 再修一次：静默异常检查原来只匹配「单行 except X: pass」，
# 而现实里的写法是多行的：
#     except Exception:
#         pass
# 结果四种写法（单行 pass / 多行 pass / 只有 return / 裸 except）
# 全都判不出来 —— 我用已知坏样本逐条验证过，4 个全漏。
# 现在改用上面的 AST 分类结果，它天然覆盖多行写法。
# ---------------------------------------------------------------------------
fails = []

# 1) 静默异常处理：异常被吞掉且不留任何痕迹 = 真问题，判定为失败。
#    「无痕迹」= 只有 pass / continue，或只有 return，且函数体里没有
#    print / warn / 日志 / raise。
#
# 允许清单：这些位置「不上报」是正确的行为，逐条写明理由。
#
# 键用「所属函数名 + 该函数内第几个静默处理器」，不是行号。
# 第一版用行号做键，结果我只是往 server.py 里加了一段代码、行号平移 4 行，
# 这个测试立刻红了一次 —— 那种检查最终会被人忽略掉。
# 函数名不会因为增删行而变；同一函数里有两个静默处理器时用序号区分
# （is_readable_path 就是这种：realpath 失败、盘符不同）。
SILENT_ALLOWED = {
    # ⚠ 这个审计目前只覆盖 server.py。2026-09-18 起 _load_config 搬到了 paths.py，
    #   它的 FileNotFoundError（config.json 不存在属正常）现在不在这里管 ——
    #   paths.py 里那段已经就地写了理由。"扩展到多文件"记在待办里。
    ("_load_provenance", 0):      "溯源文件不存在是正常情况（首次运行）",
    ("resolve_seed", 0):          "非法种子本来就该静默换成随机",
    ("_safe_read_roots", 0):      "realpath 失败的根目录直接跳过",
    ("is_readable_path", 0):      "realpath 失败一律视为不可读（拒绝优先）",
    ("is_readable_path", 1):      "不同盘符时 commonpath 抛 ValueError，按不在根下处理",
    ("warn", 0):                  "warn() 自身兜底：连 stderr 都写不出去时无处可报",
    ("_log_exc", 0):              "_log_exc() 自身兜底，和 warn() 同一个道理："
                                  "它是**最后**一个报错出口，它再抛就没地方报了",
    ("humanize_comfy_error", 0):  "错误正文不是 JSON 就原样返回，属正常分支",
    ("object_info_choices", 0):   "拿不到 ComfyUI 节点信息时返回默认值",
    ("_read_safetensors_header", 0): "不是合法 safetensors 就返回 None，调用方判断",
    ("lora_category", 0):         "训练元数据读不出来就退回默认分类",
    ("lora_aliases", 0):          "命名表不存在是正常情况",
    ("_catalog_load", 0):         "LoRA 事实目录不存在 / 没权限是**正常情况**："
                                  "发布版用户第一次拿到包时它可能还没生成过，"
                                  "而且这个函数在每次 /api/capabilities 里都会走，"
                                  "报一次就够了（同一函数第 2 个处理器管的是"
                                  "\"文件在但内容是坏 JSON\"，那个会 warn）。"
                                  "读不到就是空目录，分类自动退回文件名/元数据",
    ("lora_details", 0):          "ComfyUI 不可用时返回已有结果，不阻断其余流程",
    ("capabilities", 0):          "探测可选依赖，没有就是没有",
    ("capabilities", 1):          "连不上 ComfyUI 是**预期情况**：用 reachable=False "
                                  "表达，并且会一路报到界面上的能力标签和\"连不上\""
                                  "提示，所以不是静默失败（加这条是为了让"
                                  "ComfyUI 没开时只探 1 次而不是 10 次 ——"
                                  "本机每次\"连不上\"要 2.05 秒）",
    ("parse_a1111_parameters", 0): "A1111 的 Size 写成非 AxB 时置 0，交给上层"
                                   "按「没有尺寸」处理，不影响提示词",
    ("upscale_target", 0):        "倍率传了非数字（JSON 里的 null）就退回 2×，"
                                  "和 resolve_seed 同一个道理：非法输入换默认值，"
                                  "不是错误",
    ("_port_free", 0):            "端口被占用正是这个函数要检测的情况，"
                                  "bind 失败就是答案，不是异常",
    # do_POST 里有多处静默处理器（好几个是"清理临时文件失败不影响结果"），
    # 这个序号按 AST 遍历顺序编号，改代码后序号可能变，测试会报出来让你核。
    # 2026-09-17：新增 /api/depth 处理器（它自己用 _json 上报，不算静默），
    # 但它插在这个处理器**前面**，把序号从 7 顶到了 8 —— 同一个处理器，不是新的。
    # ★ do_POST 里这个静默处理器用**代码锚点**登记，不用序号。
    #   序号键在这个函数里已经因为「前面插了个新端点」红过三次了：
    #   2026-09-17 加 /api/depth 时 7→8，2026-09-18 加 /api/read_meta 时 8→10，
    #   同一天加 /api/delete 又 10→11。端点只会越加越多，序号只会一直漂。
    #   锚点取的是 except 上面那一行代码（这个处理器是 os.remove(hard) 后面
    #   吞掉清理失败），它跟着代码走，不跟着顺序走。
    ("do_POST", "os.remove(hard)"): "清理临时文件失败不影响结果",
    ("is_in_recycle", 0):         "不同盘符（或相对/绝对混用）时 commonpath 抛 "
                                  "ValueError，按「不在回收站里」处理 —— 和 "
                                  "is_readable_path 第 2 条同一个道理",
}


def _enclosing_funcs(tree):
    """函数名 -> (起, 止) 行区间，用于判断某个行号属于哪个函数。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, node.end_lineno or node.lineno, node.name))
    return out


_FUNCS = _enclosing_funcs(tree)


def _owner(lineno: int) -> str:
    """最内层包含该行号的函数名（嵌套时取最靠内的那个）。"""
    best = None
    for start, end, name in _FUNCS:
        if start <= lineno <= end and (best is None or start > best[0]):
            best = (start, name)
    return best[1] if best else "(模块级)"


def _is_silent_kind(kind: str) -> bool:
    return (kind.startswith("SILENT") or kind.startswith("no log")
            or kind == "returns None silently")


# 「上报给前端」的处理器的判定，两种形式：
#   * 体内调用 _json / send_error（把错误写进 HTTP 响应）
#   * 体内 raise（重新抛出，由上层统一返回）
# 这类不是静默 —— 用户看得见错误。但它们**不写 stderr**，服务器控制台
# 上什么都不会出现，排查时容易漏。所以单独列出来提示，不判失败：
# 要不要补一行日志是产品选择，不是 bug。
def _reports_error(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in ("_json", "send_error"):
                return True
    return False


silent_hard, silent_ok, reported = [], [], []


def _anchor(node):
    """这个 except **上面那一行**代码，去空白。

    用作允许清单的键：序号会随"前面插了段代码"漂移，代码本身不会。
    """
    lines = src.splitlines()
    i = node.lineno - 2                       # 0-based：except 的上一行
    while i >= 0 and not lines[i].strip():
        i -= 1
    return lines[i].strip() if i >= 0 else ""


# 同一函数里可能有好几个静默处理器，按出现顺序编号，与允许清单的键对应
_seen_in_func = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.ExceptHandler):
        continue
    kind = classify(node)
    if not _is_silent_kind(kind):
        continue
    fn = _owner(node.lineno)
    idx = _seen_in_func.get(fn, 0)
    _seen_in_func[fn] = idx + 1
    # 两种键都认：老条目是 (函数名, 序号)，新条目可以是 (函数名, 代码锚点)
    matched = None
    for key in ((fn, idx), (fn, _anchor(node))):
        if key in SILENT_ALLOWED:
            matched = key
            break
    if matched is not None:
        silent_ok.append((matched, node.lineno, kind))
    elif _reports_error(node):
        reported.append(node.lineno)
    else:
        silent_hard.append((node.lineno, fn, idx, kind))

print()
print("  刻意静默的处理器：%d 处命中允许清单（共登记 %d 条）"
      % (len(silent_ok), len(SILENT_ALLOWED)))

# 允许清单里的键必须真的还对应一个静默处理器。不对应只有两种可能：
# 那个处理器被删了，或者改成会上报了 —— 两种情况都该更新清单。
# 键是「函数名 + 序号」或「函数名 + 上一行代码」，两种都不会因为纯行号平移而误报
# （第一版用行号做键，我只加了一段代码就让这个测试红了一次；序号键后来又因为
# "前面插了新端点"在 do_POST 里连续红过三次 —— 越容易漂的函数越该用锚点）。
stale = sorted(set(SILENT_ALLOWED) - {k for k, _ln, _kd in silent_ok})
for key in stale:
    where = ("第 %d 个静默处理器" % (key[1] + 1)) if isinstance(key[1], int) \
        else ("上一行是 %r 的那个处理器" % key[1])
    print("     ⚠ 允许清单 %s() %s 已不存在：%s"
          % (key[0], where, SILENT_ALLOWED[key]))
if stale:
    fails.append("允许清单有 %d 条失效（处理器被删或已改成会上报）" % len(stale))

if reported:
    print("  把错误返回给前端的处理器：%d 处（用户看得见，但控制台无记录）"
          % len(reported))
    print("     %s" % ", ".join("L%d" % x for x in sorted(reported)))

if silent_hard:
    print()
    print("  吞掉异常且不留痕迹的处理器 %d 处（未登记理由）:" % len(silent_hard))
    for ln, fn, idx, kind in silent_hard:
        line = src.splitlines()[ln - 1].strip()
        print("     L%-5d %s() 第%d个 %-24s %s"
              % (ln, fn, idx + 1, line[:24], kind))
    print("     （要放行就在 SILENT_ALLOWED 里加 (\"%s\", %d) 并写明理由）"
          % (silent_hard[0][1], silent_hard[0][2]))
    fails.append("%d 处异常被静默吞掉且未登记理由" % len(silent_hard))
else:
    print("  没有「未登记理由」的静默异常处理")

# 2) 节点类型必须都存在（ComfyUI 查不到时不算失败，环境问题不归代码）
if missing:
    fails.append("server.py 引用了不存在的节点: %s" % missing)

# 3) 文件编码必须是 UTF-8（清单与上面打印用的 AUDITED 一致）
for p in AUDITED:
    raw = open(p, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        fails.append("%s 含 BOM（会影响部分解析）" % p.split("\\")[-1])
    try:
        raw.decode("utf-8")
    except Exception as e:
        fails.append("%s 不是合法 UTF-8: %s" % (p.split("\\")[-1], e))

print()
print("=" * 78)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 78)
sys.exit(1 if fails else 0)
