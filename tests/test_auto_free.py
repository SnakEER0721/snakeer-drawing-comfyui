"""自动释放显存：计数逻辑、触发点、以及「主体词不再自动补」的回归。

为什么有这个测试
================
用户反馈「app 打开太久容易卡死」。查下来根因是**我们从来不让 ComfyUI
卸载模型**：整个 server.py 里搜不到 free / unload 任何一处调用。
实测（RTX 4060 Laptop，8 GB 显存）出一张 512x512 的图就用掉 5.51 GB ——
第二张、或者换一次底模，就顶到天花板，显存不够就往系统内存挤
（实测 ComfyUI 那个进程挂到 7.5 GB），两头都满 → 卡死。

修法是每出 N 张图主动喊一次 ComfyUI 的 POST /free。

这个测试钉的是**改动之前会红**的东西，不是「跑一遍看看没报错」：

  第 1 节  计数逻辑      —— 阈值改成 3 之后，15 张必须**恰好**触发 5 次
  第 2 节  阈值 0 = 关掉 —— 关不掉就等于给用户留了一个永远在重载模型的开关
  第 3 节  真实释放路径  —— 队列非空时**必须不释放**（否则白白多一次 6.5 GB 读盘）
  第 4 节  真的喊了 /free —— 参数对不对、失败会不会往外抛
  第 5 节  触发点还在不在 —— 把 run_graph 末尾那句删掉，第 5 节必须变红
  第 6 节  主体词回归    —— 「窗边 逆光」不许再被补上 1girl, solo

注意：第 1/2 节是**临时改** `_AUTO_FREE_EVERY` 来测计数逻辑的，
所以它们验的不是"线上的阈值是几"。线上阈值是产品决定（现在是 5），
改它不该让这个测试变红 —— 但改计数逻辑本身必须变红。
"""
import ast
import io
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

import server as S  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("   %-4s %s%s" % ("OK" if ok else "FAIL", name,
                            ("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


print("=" * 74)
print("1. 计数逻辑：每 N 张触发一次")
print("=" * 74)

# 保存原值，最后还原 —— 这个测试不许把模块改得跟跑之前不一样
_orig_every = S._AUTO_FREE_EVERY
_orig_count = S._AUTO_FREE_COUNT
# ★ 必须在第 1 节替换它**之前**存。踩过的坑：先替换、再在 finally 里
#   `S.free_comfy_memory = _orig_free`，存下来的其实已经是那个假函数 ——
#   于是从第 3 节开始，所有"真的调用"都走假函数，全节误报 FAIL。
_orig_free = S.free_comfy_memory

try:
    S._AUTO_FREE_EVERY = 3
    S._AUTO_FREE_COUNT = 0
    fired = []
    S.free_comfy_memory = lambda force=False: (fired.append(force), True)[1]

    for i in range(1, 16):
        S.maybe_auto_free()

    check("阈值 3、出 15 张 → 恰好释放 5 次",
          len(fired) == 5, "实际 %d 次" % len(fired))
    check("计数器归零后继续累计（不是只触发一次）",
          S._AUTO_FREE_COUNT == 0, "计数=%d" % S._AUTO_FREE_COUNT)

    print()
    print("=" * 74)
    print("2. 阈值 0 = 关掉")
    print("=" * 74)
    S._AUTO_FREE_EVERY = 0
    S._AUTO_FREE_COUNT = 0
    fired.clear()
    for i in range(20):
        S.maybe_auto_free()
    check("阈值 0 时一次都不释放", not fired, "实际 %d 次" % len(fired))
    check("阈值 0 时计数器也不动", S._AUTO_FREE_COUNT == 0,
          "计数=%d" % S._AUTO_FREE_COUNT)

    print()
    print("=" * 74)
    print("3. 队列非空时不许释放（否则白白多一次 6.5 GB 读盘）")
    print("=" * 74)
    # 第 1/2 节把 free_comfy_memory 换成了假函数，这里必须换回真的，
    # 否则测的是假函数自己的行为，不是产品的。
    S.free_comfy_memory = _orig_free
    calls = []
    # ★ 这里必须是一个**可变容器**，而且改状态时只改它里面的值，
    #   绝不能 `state = {...}` 重新绑定。
    #   踩过的坑：第一版写成 state = {"running": [], "pending": []}，
    #   假 comfy_get 里用 dict(state) 快照 —— 看起来在改状态，其实
    #   `_fake_get` 闭包捕获的是**名字**没问题、但重新绑定后拿到的仍是
    #   旧 dict 的浅拷贝，于是三条断言全按"队列永远是空的"跑，
    #   报出「有排队任务 → 不释放 FAIL」。测试的输入错了，不是产品错了。
    queue = {"queue_running": [], "queue_pending": []}

    def _fake_get(path, timeout=60):
        return queue if path == "/queue" else {}

    def _fake_post(path, payload, timeout=60):
        calls.append((path, payload))
        return {}

    S.comfy_get = _fake_get
    S.comfy_post = _fake_post

    got = S.free_comfy_memory()
    check("队列空 → 释放", got is True and calls and calls[0][0] == "/free",
          "返回 %r, 调用 %d 次" % (got, len(calls)))

    calls.clear()
    queue["queue_pending"] = [{"1": {}}]
    got = S.free_comfy_memory()
    check("有排队任务 → 不释放", got is False and not calls,
          "返回 %r, 调用 %d 次" % (got, len(calls)))

    queue["queue_pending"] = []
    queue["queue_running"] = [{"1": {}}]
    got = S.free_comfy_memory()
    check("有正在跑的任务 → 不释放", got is False and not calls,
          "返回 %r, 调用 %d 次" % (got, len(calls)))

    print()
    print("=" * 74)
    print("4. 真的喊了 /free，且参数对；失败不许往外抛")
    print("=" * 74)
    queue["queue_running"] = []
    calls.clear()
    S.free_comfy_memory(force=True)
    check("/free 的 payload 要同时卸载模型 + 清缓存",
          calls and calls[0] == ("/free", {"unload_models": True,
                                           "free_memory": True}),
          repr(calls[0] if calls else None))

    # 队列查不到（ComfyUI 挂了）时也不许抛 —— 清理失败不能连累出图
    def _boom(path, timeout=60):
        raise RuntimeError("模拟 ComfyUI 掉线")

    S.comfy_get = _boom
    try:
        got = S.free_comfy_memory()
        check("ComfyUI 掉线时返回 False 而不是抛异常", got is False)
    except Exception as e:
        check("ComfyUI 掉线时返回 False 而不是抛异常", False, repr(e))
finally:
    S._AUTO_FREE_EVERY = _orig_every
    S._AUTO_FREE_COUNT = _orig_count
    S.free_comfy_memory = _orig_free

print()
print("=" * 74)
print("5. 触发点还在不在（删掉那句就等于这个功能没接上）")
print("=" * 74)

src = io.open(paths.SERVER_PY, encoding="utf-8").read()


def _find_call_in(path_to_src, func_name, callee):
    """在 func_name 里找 callee() 那个调用语句，返回 (语句, 它的父节点)。

    ★ 只走**直接子语句**，不要用 ast.walk 钻到底。
      踩过的坑：第一版用 ast.walk，它会钻进 while/if/try 的每一个后代，
      于是先撞到循环体里某个分支里的语句，把那个分支的父节点当成了目标，
      报出一句毫无意义的 "它在 run_graph 的最外层 FAIL"。
      要判断「它挂在哪」，就必须从函数体往下贴着走，而不是全树乱扫。
    """
    tree = ast.parse(path_to_src)
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == func_name):
            continue
        for parent in ast.walk(fn):          # 候选父节点
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, ast.Expr) and \
                        isinstance(child.value, ast.Call) and \
                        getattr(child.value.func, "id", None) == callee:
                    return child, parent
    return None, None


call_stmt, parent = _find_call_in(src, "run_graph", "maybe_auto_free")
check("run_graph 里有 maybe_auto_free() 调用", call_stmt is not None)

if call_stmt is not None:
    # 用 AST 的父子关系判断，**不要用「有没有 finally 关键字」**来猜。
    # 踩过的坑一：run_graph 里本来就有别的 try/except（清理中转文件那段），
    #   用关键字一扫必然误报。
    # 踩过的坑二：第一版用 ast.walk 找父节点，钻进了别的分支，结论完全错。
    check("它不在 try 里 —— 清理失败不会连累出图",
          not isinstance(parent, ast.Try), "父节点 %s" % type(parent).__name__)

    # 它的位置必须是：`if not saved: raise` 那个守卫**之后**、`return saved`
    # 之前。也就是说「出图成功了才释放」。
    # ★ 证据必须真的指向结论，不能只打印一行旁边的东西就下结论。
    #   第一版这里打印父节点的第一行（`if st.get("completed"):`），
    #   而断言说的是"在没出图就报错的守卫之后" —— 打印的东西压根不是那个
    #   守卫，看着像过了，其实没验到。改成直接在语句序列里找那两条。
    body_src = [ast.unparse(s) for s in parent.body]
    idx_call = next((i for i, s in enumerate(body_src)
                     if "maybe_auto_free()" in s), -1)
    idx_guard = next((i for i, s in enumerate(body_src)
                      if s.startswith("if not saved") and "raise" in s), -1)
    idx_ret = next((i for i, s in enumerate(body_src)
                    if s.startswith("return")), -1)

    check("在「没出图就报错」的守卫之后（只对成功的任务释放）",
          0 <= idx_guard < idx_call,
          "守卫@%d 调用@%d" % (idx_guard, idx_call))
    check("在 return 之前（不是死代码）",
          0 <= idx_call < idx_ret,
          "调用@%d 返回@%d" % (idx_call, idx_ret))
    check("是 return 前面那一条（成功路径的最后一步）",
          idx_ret == idx_call + 1,
          "调用@%d 返回@%d" % (idx_call, idx_ret))

print()
print("=" * 74)
print("6. 回归：主体词不再自动补 1girl, solo")
print("=" * 74)
check("SUBJECT_DEFAULT 已彻底删除（不只是没用）",
      not re.search(r"^\s*SUBJECT_DEFAULT\s*=", src, re.M))
check("生成路径里不再拼 subject",
      not re.search(r"\bsubject\s*=\s*\"\"\s*if\s+any", src))
check("positive 由 (quality, english) 拼成，没有第三个词",
      'x for x in (quality, english) if x)' in src)

print()
print("=" * 74)
print("7. 触发时必须留一行日志（否则「它到底跑没跑」只能靠猜）")
print("=" * 74)
# 为什么单独测这个：第一版成功时完全静默，实测时用户出了 6 张图，
# 我只能从"显存没满 + 两次出图隔了 30 秒"倒推它触发过。
# 一条查不到的链路 = 没有可观察性 = 哪天它不触发了也没人发现。
_orig_every2 = S._AUTO_FREE_EVERY
_orig_free2 = S.free_comfy_memory
try:
    S._AUTO_FREE_EVERY = 1
    S._AUTO_FREE_COUNT = 0
    S.free_comfy_memory = lambda force=False: True
    buf = io.StringIO()
    old_err = sys.stderr
    sys.stderr = buf
    try:
        S.maybe_auto_free()
    finally:
        sys.stderr = old_err
    out = buf.getvalue()
    check("触发时往 stderr 打了一行", "[清理]" in out, repr(out.strip()[:70]))
    check("这行日志说清了成功还是跳过",
          "成功" in out or "跳过" in out, repr(out.strip()[:70]))

    # 反过来：没到阈值时**不许**打日志（否则每张图刷一行，把日志淹了）
    S._AUTO_FREE_EVERY = 3
    S._AUTO_FREE_COUNT = 0
    buf2 = io.StringIO()
    old_err = sys.stderr
    sys.stderr = buf2
    try:
        S.maybe_auto_free()
        S.maybe_auto_free()
    finally:
        sys.stderr = old_err
    check("没到阈值时不打日志（不刷屏）", buf2.getvalue() == "",
          repr(buf2.getvalue()[:70]))
finally:
    S._AUTO_FREE_EVERY = _orig_every2
    S._AUTO_FREE_COUNT = 0
    S.free_comfy_memory = _orig_free2

print()
print("=" * 74)
print("8. 线上阈值确实是 5（上面几节临时改过它，这里验的是真实值）")
print("=" * 74)
# 这一条的作用：阈值是产品决定，有人手滑改大（比如改回 10）时这里会红，
# 提醒他「8 GB 卡出一张就吃 5.5 GB，留的余量必须小于 5 张」。
check("_AUTO_FREE_EVERY == 5", S._AUTO_FREE_EVERY == 5,
      "实际 %r" % (S._AUTO_FREE_EVERY,))
check("前面几节把改过的值还原了（否则这条测试会污染别的测试）",
      S._AUTO_FREE_COUNT == _orig_count,
      "计数 %r vs 原值 %r" % (S._AUTO_FREE_COUNT, _orig_count))

print()
if fails:
    print("RESULT: FAIL  失败 %d 项: %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("RESULT: ALL PASS")
