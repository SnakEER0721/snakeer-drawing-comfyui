# -*- coding: utf-8 -*-
"""浏览器关连接时，服务端不该往控制台吐 traceback。

为什么这条值得测
================
用户双击 `启动UI.bat` 之后，那个黑窗口就是**他唯一能看到的日志**。
而 `socketserver` 默认会把任何异常整个打成 traceback —— 包括
"页面上还在加载时按了刷新"、"关了标签页"、"取消一个还没画完的请求"。
那些都不是错误。

实测（2026-09）：本机 `%TEMP%` 里三个服务日志加起来 **1319 段 traceback**，
其中一份 **1075 段 / 1.36 MB 全是这个**。后果不是占空间，而是
**真正的错误被埋在中间** —— 用户来问"为什么出不了图"时，翻不到那句话。

这个测试**自带对照组**，所以它不可能空过：
  · 对照组：用 `ThreadingHTTPServer`（父类）+ 同一个 Handler 起一个服务，
    发同样的"中途断开"请求 -> 必须**打得出来** traceback
  · 被测：用 `QuietServer` -> 必须**一段都不打**
如果哪天机器上"断开"根本没触发到（比如响应太小、写不失败），
对照组会先变成 0 —— 那时这个测试会报红，而不是假装通过。

纯断言：不连 ComfyUI、不出图、不占 GPU，起的是本地回环上的临时端口。
"""
import io
import os
import socket
import struct
import sys
import threading
import time
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer

# ★ 必须在 import paths / server **之前**（见 AGENTS.md）
sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
import server  # noqa: E402

fails = []
TRACEBACK = b"Traceback (most recent call last)"


def check(name, cond, extra=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "   " + str(extra)))
    if not cond:
        fails.append(name)


def abort_requests(port, times=4):
    """发 N 个"中途断开"的请求：SO_LINGER=0 强制发 RST，不等响应。"""
    for _ in range(times):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
        except Exception:
            return 0
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
            s.sendall(("GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                       % port).encode())
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
        time.sleep(0.05)
    return times


def run_against(server_cls, label):
    """起一个服务、发断开的请求、返回 (traceback 段数, 提示)。"""
    try:
        srv = server_cls(("127.0.0.1", 0), server.Handler)
    except Exception as e:
        return -1, "起不来: %s" % e
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            # 先确认服务是活的（用一次正常请求），免得后面数出来的是"根本没连上"
            import urllib.request
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/" % port,
                                            timeout=10) as r:
                    n = len(r.read())
            except Exception as e:
                return -1, "正常请求都失败: %s" % e
            sent = abort_requests(port)
            time.sleep(1.2)              # 等错误处理线程把日志写完
    finally:
        srv.shutdown()
        srv.server_close()
    text = buf.getvalue()
    tb = text.count(TRACEBACK.decode("utf-8", "replace"))
    return tb, "页面 %d 字节 / 断开 %d 次 / stderr %d 字节" % (n, sent, len(text))


print()
print("【1】对照组：父类 ThreadingHTTPServer —— 必须打得出来")
tb_plain, info_plain = run_against(ThreadingHTTPServer, "父类")
print("     %s" % info_plain)
check("对照组确实产生了 traceback（证明本机的「断开」真的触发了）",
      tb_plain > 0, "对照组的 traceback 段数 = %s" % tb_plain)

print()
print("【2】被测：QuietServer —— 一段都不许打")
tb_quiet, info_quiet = run_against(server.QuietServer, "QuietServer")
print("     %s" % info_quiet)
check("客户端断开时 0 段 traceback", tb_quiet == 0,
      "打了 %s 段" % tb_quiet)

print()
print("【3】真错误仍然要打（别把报错一起关掉了）")
# handle_error 只在"处理器抛了异常"时被调用。造一个必然抛异常的假处理器，
# 用它替代 Handler —— 真 bug 的 traceback 必须照旧出来。
buf = io.StringIO()


class Boom(server.Handler):
    def do_GET(self):
        raise RuntimeError("故意炸一个，用来证明真错误的 traceback 还在")


srv = server.QuietServer(("127.0.0.1", 0), Boom)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    with redirect_stderr(buf):
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % port,
                                   timeout=10).read()
        except Exception:
            pass
        time.sleep(1.0)
finally:
    srv.shutdown()
    srv.server_close()
text = buf.getvalue()
check("非连接类异常照旧打 traceback",
      "Traceback" in text and "故意炸一个" in text,
      "stderr: %s" % text[:200])

print()
print("=" * 74)
if fails:
    print("RESULT: %d 项失败: %s" % (len(fails), ", ".join(fails)))
    print("=" * 74)
    sys.exit(1)
print("RESULT: ALL PASS")
print("=" * 74)
