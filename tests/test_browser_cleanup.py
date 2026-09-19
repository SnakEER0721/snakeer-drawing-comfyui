# -*- coding: utf-8 -*-
"""守 browser_helper 的浏览器清理 —— 它曾经**一直是个空转**，而且没人发现。

抓到它的经过（用户视角实测）：
    跑完一遍完整回归，紧接着再跑 `--tier fast`，4 个浏览器测试**全部**
    在 1–2 秒内失败：
        TargetClosedError: ... Target page, context or browser has been closed
        [pid=...] <process did exit: exitCode=21>
    可单独跑每一条又都是好的。查下来：
      · Chromium 对同一个 --user-data-dir 只允许一个实例，第二个直接退出
      · 上一次留下的 headless Edge 还握着那个 profile 目录
      · `reap_own_processes()` 本来就该清掉它，可它**匹配不到任何进程** ——
        路径被 replace("\\\\", "\\\\\\\\") 转义成双反斜杠，而用的是 PowerShell 的
        `-like`（通配符，不是正则）。量出来的对照：
            转义后的 marker -> 匹配 0 个
            不转义的路径    -> 匹配 9 个
    于是"连跑两次 fast"这种最普通的动作，第二次会看到 4 个假失败。

这一条测的就三件事（都要真的起进程，不能靠读代码）：
  【1】起一个带测试 profile 的 headless Edge -> 必须知道它在
  【2】reap_own_processes() -> 必须真的清掉（这是**曾经空转**的那一句）
  【3】清完之后 launch() 必须能起来（残留不该把下一轮搞红）

⚠️ 全程只用测试 profile 那个目录做判据，**不碰用户自己的浏览器**，
   并且在结尾断言"用户的 Edge 数量一个没少"。
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import browser_helper as bh  # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  [%s] %s" % ("OK" if cond else "FAIL", msg))


def _spawn_stray():
    """起一个"上一轮没清干净"的实例（headless、带测试 profile）。"""
    edge = bh._find_edge()
    if not edge:
        return False
    os.makedirs(bh.PROFILE_DIR, exist_ok=True)
    subprocess.Popen(
        [edge, "--headless", "--no-first-run", "--no-default-browser-check",
         "--user-data-dir=%s" % bh.PROFILE_DIR, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def main():
    if not bh._find_edge():
        print("SKIP: 这台机器上没有 Edge，浏览器清理测不了")
        return 0

    user_before = bh.count_user_processes()
    print("用户的 Edge 进程（不属于测试 profile）：%d 个" % user_before)
    if bh.count_test_processes() > 0:
        print("开跑前有残留，先清掉（正常应为 0）：%d 个"
              % bh.count_test_processes())
        bh.reap_own_processes()
        time.sleep(2)

    print()
    print("【1】人为制造一个残留实例")
    if not _spawn_stray():
        print("SKIP: 起不了探针进程")
        return 0
    got = 0
    for _ in range(20):
        time.sleep(0.5)
        got = bh.count_test_processes()
        if got > 0:
            break
    check(got > 0, "带测试 profile 的实例能被认出来（count_test_processes=%d）" % got)
    if got <= 0:
        print("  起不来探针，后面两条没法测了")
        return 1

    print()
    print("【2】reap_own_processes() 真的清得掉吗（它曾经是空转）")
    left = bh.reap_own_processes()
    for _ in range(20):
        if bh.count_test_processes() == 0:
            break
        time.sleep(0.5)
    after = bh.count_test_processes()
    check(after == 0,
          "清理之后残留为 0（实际的 after=%s、返回值=%s）—— 空转的话这里会是 %d"
          % (after, left, got))
    check(left == 0 or left > 0,
          "reap_own_processes 返回剩余数（%s），不是 None" % left)

    print()
    print("【3】清干净之后 launch() 必须起得来（残留不该把下一轮搞红）")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = bh.launch(pw, headless=True)
            pg = ctx.new_page()
            # 真的开一个页面，确认不是"对象建出来了但浏览器是死的"
            pg.set_content("<title>probe</title><p id=x>ok</p>")
            check(pg.inner_text("#x") == "ok", "新起的浏览器能真的打开页面")
            ctx.close()
    except Exception as e:
        check(False, "launch 失败：%s: %s" % (type(e).__name__, str(e)[:120]))

    # 收尾：清干净，并确认没误伤用户的浏览器
    bh.reap_own_processes()
    time.sleep(1)
    user_after = bh.count_user_processes()
    check(user_after >= user_before,
          "用户的 Edge 一个没少（跑前 %d -> 跑后 %d）" % (user_before, user_after))
    check(bh.count_test_processes() == 0, "结束时没有留下测试浏览器")

    print()
    print("=" * 74)
    print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL: %s" % f)
        print("RESULT: FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
