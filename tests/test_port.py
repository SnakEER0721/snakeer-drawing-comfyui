# -*- coding: utf-8 -*-
"""验证端口被占用时会自动往上找空闲端口。

为什么必须测：别人机器上 8765 完全可能被占。写死端口的话，用户双击启动只见
一句 "Address already in use"，不知道怎么办。这个测试把"换个端口也要起得来"
钉死。
"""
import socket
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


print("=" * 70)
print("【1】config.json 不给 port 时，默认是 8765")
check(paths.DEFAULT_PORT == 8765, "DEFAULT_PORT = %d" % paths.DEFAULT_PORT)
check(isinstance(paths.PORT, int) and paths.PORT > 0,
      "PORT = %d（来自 config.json 或默认值）" % paths.PORT)

print()
print("【2】端口被占用时，bind_server 往上找")
# 先自己占住一个端口（用 0 让系统挑，避免和真实服务打架）
squat = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
squat.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
squat.bind(("127.0.0.1", 0))
busy = squat.getsockname()[1]
squat.listen(1)
print("   已占用端口 %d 作为测试目标" % busy)

import server  # noqa: E402  （要在 sys.path 设好之后）

try:
    srv, got = server.bind_server(busy, tries=3)
    check(got != busy, "没有用被占用的端口 %d，实际用了 %d" % (busy, got))
    check(got <= busy + 3, "在允许的尝试范围内（%d ≤ %d）" % (got, busy + 3))
    # 真的能服务吗 —— bind 只是绑上，还得 serve_forever 才会应答。
    # （第一版忘了这句，测出来是"超时"，看着像产品 bug，其实是测试写漏了。）
    import threading
    import urllib.request
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with urllib.request.urlopen("http://127.0.0.1:%d/" % got, timeout=15) as r:
        body = r.read(400).decode("utf-8", "replace")
    check(r.status == 200 and "Snakeer" in body,
          "换到的端口真的能返回页面（HTTP %d）" % r.status)
    srv.shutdown()
    srv.server_close()
except Exception as e:
    check(False, "bind_server 抛异常: %s: %s" % (type(e).__name__, e))
finally:
    squat.close()

print()
print("【3】只监听 127.0.0.1，不暴露到局域网")
src = open(os.path.join(paths.APP_DIR, "server.py"), encoding="utf-8").read()
# ★ 不要盯着**服务器类的名字**去匹配。原来是
#   `'ThreadingHTTPServer(("127.0.0.1"' in src` —— 后来 bind_server 改成返回
#   QuietServer（"客户端断开不打 traceback"那个子类），这行立刻误报。
#   要盯的是**绑定地址**：不管用哪个类，地址都必须是 127.0.0.1。
check(re.search(r'\(\("127\.0\.0\.1", p\), \w+\)', src) is not None,
      "绑定地址写死是 127.0.0.1（这个服务没有鉴权，不能听外网）")
check("0.0.0.0" not in src, "源码里没有 0.0.0.0")
# 行为上也验一次：真绑一个，看它落在哪个地址上（文本匹配会被重命名绕过去）
try:
    srv, port = server.bind_server(0)
    check(srv.server_address[0] == "127.0.0.1",
          "真绑一次，确实只落在 127.0.0.1（实际 %s）" % (srv.server_address[0],))
    srv.server_close()
except Exception as e:
    check(False, "起服务验绑定地址失败: %s: %s" % (type(e).__name__, e))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
