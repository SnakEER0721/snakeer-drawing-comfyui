# -*- coding: utf-8 -*-
"""端到端测试的守卫：确认「我连的服务」就是「我这个安装」。

为什么需要它：
    端到端测试都是去敲 127.0.0.1:8765。但 8765 上跑的**可能是另一个目录**的
    实例 —— 例如开发目录和发布目录各有一份，或者你改了 config.json 的
    comfy_output 之后没重启。这时测试拿到的路径和它自己从 paths.py 算出来的
    不一样，于是报出一堆和被测代码毫无关系的失败：

        FAIL  落在 _recycle 目录（...\\ComfyUI-Shared\\output\\anime\\_recycle）
        FAIL  接口返回成功：{'ok': False, 'error': '只能删除产出目录…下的文件'}

    实测：在发布目录里跑 fast 层，7 个端到端测试全红，而开发目录里全绿 ——
    因为跑着的那个服务是开发目录起的（产出目录 D:\\comfyout），
    而发布目录没有 config.json，走自动探测（...\\ComfyUI-Shared\\output）。

判据：`/api/capabilities` 回的 `app_dir` 必须等于本测试的所在目录。
不相等、连不上、或者服务太旧没有这个字段 —— 一律**打印 SKIP 并 exit 0**。
这是项目约定的做法：环境不具备是 SKIP，不是 FAIL。假失败比真失败更贵。

用法（放在测试文件靠近顶部、BASE 定义之后）：

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import serverguard

    # ① 整个测试都依赖服务 → 在开头 require()，不满足就 SKIP 退出
    serverguard.require(BASE, "test_delete")

    # ② 只有一部分检查依赖服务 → 用 ok()，别用 require()
    #
    #   ★ require() 会 exit(0)，会把已经记录的失败一起吞掉。
    #     实测踩过：test_version.py 的 1~3 节不依赖服务，却在第 4 节
    #     调了 require()，于是服务不可用时整个测试报 SKIP ——
    #     前面查出来的失败全没了，比不测还糟。
    if serverguard.ok(BASE):
        ...敲服务...
    else:
        print("   [跳过] %s" % serverguard.check(BASE)[1])

★ 文件名不要加下划线前缀：dev/make_release.py 的 is_junk() 会把 tests/_*.py
  当"一次性脚本"跳过，那样它在发布包里就不存在，端到端测试会 import 失败。
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)


def probe(base, timeout=20):
    """取 /api/capabilities 里的 data。

    失败返回 {'__err__': 原因}。

    ★ 这个接口的返回是包在信封里的：{"ok": true, "data": {...}}。
      第一版直接对最外层取 app_dir，永远取不到 —— 于是把所有服务都误判成
      "旧版 server.py"，9 个 e2e 测试全部 SKIP（看起来像"环境不具备"，
      实际是守卫自己坏了）。这种"守卫坏了但不报错"最难发现。
    """
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/capabilities",
                                    timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"__err__": "%s: %s" % (type(e).__name__, str(e)[:120])}
    if not isinstance(payload, dict):
        return {"__err__": "返回的不是 JSON 对象（%r）" % (str(payload)[:80],)}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if payload.get("ok") is False:
        return {"__err__": "接口报错: %s" % str(payload.get("error"))[:120]}
    return {"__err__": "返回里没有 data 字段（键: %s）"
                       % ", ".join(list(payload)[:6])}


def check(base, timeout=20):
    """返回 (能用?, 原因)。原因只在不能用时有意义。"""
    caps = probe(base, timeout)
    if "__err__" in caps:
        return False, "连不上服务（%s）" % caps["__err__"]
    got = caps.get("app_dir")
    if got is None:
        return False, ("服务报了 data，但里面没有 app_dir 字段 —— "
                       "说明它跑的是旧版 server.py（这个字段是后加的）。"
                       "重启一次服务再跑测试。")
    if os.path.normcase(os.path.abspath(got)) != \
            os.path.normcase(os.path.abspath(APP)):
        return False, (
            "8765 上跑的是「另一个目录」的服务：\n"
            "        服务在   %s\n"
            "        本测试在 %s\n"
            "      两边的 config.json 不同（产出目录/模型目录可能不一样），\n"
            "      跑出来的失败和被测代码无关。\n"
            "      要么停掉那个服务、从这个目录起一个，要么用参数指定地址：\n"
            "          python tests/xxx.py http://127.0.0.1:<端口>"
            % (got, APP))
    return True, ""


def ok(base, timeout=20):
    return check(base, timeout)[0]


def require(base, who="", timeout=20):
    """不是「我自己的服务」就打印 SKIP 并 exit 0。"""
    good, why = check(base, timeout)
    if good:
        return
    print("=" * 74)
    print("SKIP: %s 需要 8765 上跑着本目录的服务" % (who or "本测试"))
    print("=" * 74)
    print("  原因: %s" % why)
    print()
    print("  （环境不具备，不是测试失败 —— 和「没装浏览器」同一类。）")
    print("RESULT: SKIP")
    print("=" * 74)
    sys.exit(0)
