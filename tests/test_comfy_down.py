# -*- coding: utf-8 -*-
"""ComfyUI 没开的时候，用户看到的是什么？

背景（实测，见 dev/probe_comfy_down.py）
========================================
把 ComfyUI 关掉、在界面上点「生成」，修之前返回的是：

    HTTP 500
    {"error": "URLError: <urlopen error [WinError 10061]
               由于目标计算机积极拒绝，无法连接。>"}

用户看到"目标计算机积极拒绝"，**既不知道说的是 ComfyUI，也不知道该怎么办**。
而这是最常发生的一种情况（忘了先开 ComfyUI）—— 正是「用户报错怎么修」要解决的。

修完是：

    HTTP 503
    {"error": "连不上 ComfyUI（http://127.0.0.1:8188）—— 它没开着，或者地址不对。
               先启动 ComfyUI、等它自己的界面出来，再回来点生成；……"}

这个测试钉四件事
================
  【1】真的对一个**没人听的端口**发请求，确认抛的是 ComfyUnreachable（不是 URLError）
  【2】那句消息必须点明 ComfyUI、并给出可执行的下一步（地址见 config.json）
  【3】超时和连不上要分开说（"没开"和"卡住了"是两回事）
  【4】**静态**：server.py 里所有打给 ComfyUI 的 urlopen 都必须走 comfy_open，
       而且 `f"{type(e).__name__}: {e}"` 只许出现在 `_err_text` 里面 ——
       漏一个入口，那个入口的用户看到的就又是 Python 原文，而这种事没人能一眼看出来

纯断言：不连 ComfyUI、不出图、不起服务、不改任何文件。
"""
import sys

# ★ 必须在 import paths / server **之前**。这个测试 import 了应用自己的模块，
#   从发布目录里单独跑它时（"模拟用户的机器上跑一遍"），Python 会在发布目录
#   写下 __pycache__/*.pyc —— 实测就是这么把发布目录从 74 个文件变成 78 个的，
#   然后 audit_release.py 和 audit_release_runtime.py 一起报红。
#   AGENTS.md 里那条"任何 import 发布目录里代码的脚本，开头加一行"适用于
#   **手工验证**也一样，不只是审计脚本。
sys.dont_write_bytecode = True

import os
import re
import socket
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
import server  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "   " + str(extra)))
    if not cond:
        fails.append(name)


def dead_port():
    """一个确定没人听的端口：绑上拿到号再关，然后确认连上去会被拒。"""
    for _ in range(10):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        c = socket.socket()
        c.settimeout(0.3)
        try:
            c.connect(("127.0.0.1", p))
        except Exception:
            return p            # 连不上 = 正是我们要的
        finally:
            c.close()
    raise RuntimeError("拿不到空闲端口")


# ---------------------------------------------------------------- 【1】真的连一次
print()
print("【1】对一个没人听的端口真的发一次请求")
DEAD = dead_port()
real_comfy = server.COMFY
server.COMFY = "http://127.0.0.1:%d" % DEAD
print("     假装 ComfyUI 在 127.0.0.1:%d（没人听）" % DEAD)
try:
    server.comfy_get("/object_info/KSampler", timeout=3)
    check("连不上时抛的是 ComfyUnreachable", False, "居然没抛异常？")
    msg = ""
except server.ComfyUnreachable as e:
    check("连不上时抛的是 ComfyUnreachable", True)
    msg = str(e)
except Exception as e:
    check("连不上时抛的是 ComfyUnreachable", False,
          "抛的是 %s: %s" % (type(e).__name__, e))
    msg = ""
finally:
    server.COMFY = real_comfy

print()
print("【2】那句话得能照着改")
print("     实际消息：%s" % msg)
check("点明了是 ComfyUI", "ComfyUI" in msg, msg)
check("说出了它现在的地址", "127.0.0.1:%d" % DEAD in msg, msg)
check("给了第一步（先启动 ComfyUI）", "启动" in msg, msg)
check("给了地址不对时怎么办（config.json / comfy_url）",
      "config.json" in msg and "comfy_url" in msg, msg)
check("没有 Python 味的东西漏出来",
      not any(x in msg for x in ("URLError", "WinError", "[Errno", "Traceback",
                                 "urlopen error", "10061")),
      msg)

# ---------------------------------------------------------------- 【3】超时
print()
print("【3】超时和连不上是两回事")
t_msg = str(server._comfy_error(socket.timeout("timed out")))
print("     超时的消息：%s" % t_msg)
check("超时说的是「没有响应」，不是「连不上」",
      "没有响应" in t_msg and "连不上" not in t_msg, t_msg)
check("超时也给了下一步（看一眼它的窗口 / 重启）",
      "重启" in t_msg, t_msg)

# 用真的 URLError 包一个 timeout，确认 reason 那条路径也对
wrapped = urllib.error.URLError(socket.timeout("timed out"))
check("URLError 里裹着 timeout 也识别成超时",
      "没有响应" in str(server._comfy_error(wrapped)),
      str(server._comfy_error(wrapped)))

# ---------------------------------------------------------------- 【4】给用户看 / 状态码
print()
print("【4】故意抛的异常：不要 Python 类型名前缀，状态码要分得清")
cu = server.ComfyUnreachable("连不上 ComfyUI（x）")
check("_err_text 不给我们自己的消息加前缀",
      server._err_text(cu) == "连不上 ComfyUI（x）", server._err_text(cu))
check("BadParam 也一样", server._err_text(server.BadParam("x 收到了 'a'")) == "x 收到了 'a'")
check("真 bug 仍然保留类型名（那是线索）",
      server._err_text(KeyError("k")) == "KeyError: 'k'",
      server._err_text(KeyError("k")))
check("ComfyUI 没开 = 503（上游不可用），不是 500",
      server._err_code(cu) == 503, server._err_code(cu))
check("参数错 = 400", server._err_code(server.BadParam("x")) == 400)
check("其他异常 = 500", server._err_code(ValueError("x")) == 500)

# ---------------------------------------------------------------- 【5】静态
print()
print("【5】静态检查：入口不能漏")
src = open(paths.SERVER_PY, encoding="utf-8").read()

# 所有打给 ComfyUI 的 urlopen 都要走 comfy_open
raw = []
for m in re.finditer(r"urllib\.request\.urlopen\(([^\n]*)", src):
    line = src[:m.start()].count("\n") + 1
    arg = m.group(1)
    # comfy_open 内部那一次是唯一允许的
    if "target" in arg:
        continue
    raw.append("第 %d 行: urlopen(%s" % (line, arg.strip()[:60]))
check("打给 ComfyUI 的 urlopen 全部走 comfy_open", not raw,
      "还有裸的：" + " / ".join(raw))

# `f"{type(e).__name__}: {e}"` 只许出现在 _err_text 里
#   （注释里提到它不算 —— 那是解释"为什么要这个函数"，不是代码）
LIT = 'f"{type(e).__name__}: {e}"'
lines = src.splitlines()
hits = [i + 1 for i, l in enumerate(lines) if LIT in l
        and not l.lstrip().startswith("#")]
in_helper = []
for ln in hits:
    # 简单判据：这一行往上，最近的 `def` 是不是 _err_text
    last_def = None
    for l in lines[:ln]:
        if re.match(r"\s*def \w+", l):
            last_def = l.strip()
    in_helper.append(last_def is not None and last_def.startswith("def _err_text"))
check('f"{type(e).__name__}: {e}" 只在 _err_text 里出现（handler 都用 _err_text(e)）',
      bool(hits) and all(in_helper),
      "第 %s 行不在 _err_text 里" % [h for h, ok in zip(hits, in_helper) if not ok])

check("handler 里用的是 _err_text/_err_code",
      src.count("_err_text(e)}, _err_code(e)") >= 10,
      "只找到 %d 处" % src.count("_err_text(e)}, _err_code(e)"))

probe = os.path.join(paths.APP_DIR, "dev", "probe_comfy_down.py")
# dev/ 不进发布包，所以在发布目录里跑时它不存在 —— 跳过，不算失败
# （和 test_version.py / test_bats.py 同一个处理；写死"必须有"会让
#   发布包里的测试自己变红，实测踩过）。
if os.path.isdir(os.path.join(paths.APP_DIR, "dev")):
    check("探针脚本还在（结论可复跑）", os.path.isfile(probe), probe)
else:
    print("   [跳过] 没有 dev/（发布包里本来就没有）")

print()
print("=" * 74)
if fails:
    print("RESULT: %d 项失败: %s" % (len(fails), ", ".join(fails)))
    print("=" * 74)
    sys.exit(1)
print("RESULT: ALL PASS")
print("=" * 74)
