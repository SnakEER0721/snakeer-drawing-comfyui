"""Regression test: a browser test run must never touch the user's own browser.

The bug this locks down:
  A cleanup step used
      Get-Process msedge | Where-Object { $_.MainWindowTitle -eq '' } | Stop-Process
  meaning to reap headless test instances. But in a Chromium browser every
  normal session also runs dozens of child processes with an empty window
  title - renderers, GPU, network, utility. So that filter matched the user's
  real browser and killed it, which the user saw as
  "网页总会被清理 / RESULT_CODE_KILLED".

The fix is a dedicated --user-data-dir for test browsers, matched by exact
path. This test proves the property instead of assuming it:

  1. count the user's Edge processes (those NOT carrying the test profile)
  2. launch a test browser through browser_helper and load the app
  3. assert the user's count did not move
  4. close, reap, and assert only zero test-marked processes remain
  5. assert the user's count is still intact after cleanup

Runs headless and skips cleanly when Playwright or the server is absent.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import browser_helper as bh

BASE = "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务（见 tests/serverguard.py）。
import serverguard  # noqa: E402
serverguard.require(BASE, "test_browser_isolation")

fails = []
TOLERANCE = 2      # 浏览器自身会回收/新建少量子进程，允许小幅波动


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过")
    print("RESULT: SKIP")
    sys.exit(0)

try:
    import urllib.request
    with urllib.request.urlopen(BASE + "/api/capabilities", timeout=20):
        pass
except Exception as e:
    print("后端不可用（%s），跳过" % type(e).__name__)
    print("RESULT: SKIP")
    sys.exit(0)


print("=" * 78)
print("【0】测试目录隔离")
print("=" * 78)
print("   测试用 profile: %s" % bh.PROFILE_DIR)
check("dsh_ui_test_profile" in bh.PROFILE_DIR,
      "测试 profile 是专用目录，不是用户的默认 profile")

before = bh.count_user_processes()
print("   测试前用户自己的 msedge 进程: %d" % before)

print()
print("=" * 78)
print("【1】启动测试浏览器（独立 profile）")
print("=" * 78)
with sync_playwright() as pw:
    br = bh.launch(pw, headless=True, viewport={"width": 900, "height": 700})
    try:
        pg = br.new_page()
        pg.goto(BASE, wait_until="load")
        pg.wait_for_timeout(2000)
        title = pg.title()
        print("   页面标题: %s" % title)
        check(title.strip() != "", "测试浏览器确实打开了应用页面")

        during = bh.count_user_processes()
        print("   测试运行中用户自己的进程: %d" % during)
        check(abs(during - before) <= TOLERANCE,
              "启动测试浏览器没有影响用户的浏览器（%d -> %d）" % (before, during))
    finally:
        try:
            br.close()
        except Exception:
            pass

print()
print("=" * 78)
print("【2】清理只针对测试 profile")
print("=" * 78)
left = bh.reap_own_processes()
print("   清理后带测试标记的残留: %d" % left)
check(left == 0, "测试进程已全部回收")

after = bh.count_user_processes()
print("   清理后用户自己的进程: %d" % after)
check(abs(after - before) <= TOLERANCE,
      "清理没有误杀用户浏览器（%d -> %d）" % (before, after))

bh.cleanup_profile()

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
