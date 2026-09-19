# -*- coding: utf-8 -*-
"""端到端（真浏览器）：点缩略图在**站内**打开，左右键能翻页，Esc 能关。

为什么要专门测这个：用户实报"点开一张图直接另起一个浏览器标签页，没法像系统
照片应用那样左右切换"。新标签页这种行为本身断言不了（Playwright 里点开的新
标签是另一个 page），所以这里断言的是**新的**行为：浮层出现、页码变化、
到边界不越界、Esc 关闭、底下没有新开标签。

服务没起时跳过（和别的端到端测试一致）。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务（见 tests/serverguard.py）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_viewer_ui")
fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


try:
    with urllib.request.urlopen(BASE + "/api/capabilities", timeout=20) as r:
        json.loads(r.read().decode("utf-8", "replace"))
except Exception as e:
    print("RESULT: SKIP（后端不可用：%s）" % type(e).__name__)
    sys.exit(0)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过（端到端测试需要浏览器）")
    print("RESULT: SKIP")
    sys.exit(0)

import browser_helper as bh  # noqa: E402


def run(br):
    pg = br.new_page()
    errs = []
    pages = []
    pg.on("pageerror", lambda e: errs.append("PAGEERROR: %s" % e))
    pg.on("console", lambda m: errs.append("CONSOLE: %s" % m.text)
          if m.type == "error" else None)
    pg.context.on("page", lambda p: pages.append(p))
    pg.goto(BASE, wait_until="load")
    pg.wait_for_timeout(4000)
    # 「最近产出」不是自动加载的（页面上写着「点刷新列表加载」），先点一下
    pg.get_by_role("button", name="刷新列表").click()
    pg.wait_for_timeout(5000)

    print("=" * 74)
    print("【1】缩略图存在且点得开")
    shots = pg.locator("#recent .shot img")
    n = shots.count()
    check(n > 0, "最近产出里有 %d 张缩略图" % n)
    if not n:
        return
    before_pages = len(pg.context.pages)
    shots.first.click()
    pg.wait_for_timeout(800)
    check(pg.locator("#viewModal.on").count() == 1, "站内浮层打开了")
    check(len(pg.context.pages) == before_pages,
          "没有另开浏览器标签页（page 数 %d → %d）"
          % (before_pages, len(pg.context.pages)))
    check(pg.locator("#viewImg").count() == 1, "浮层里有图片")
    src0 = pg.get_attribute("#viewImg", "src")
    check(bool(src0) and "/api/image" in src0, "图片走的是 /api/image：%s" % str(src0)[:50])
    idx0 = pg.inner_text("#viewIdx")
    check("/" in idx0, "显示了页码：%s" % idx0)

    print()
    print("【2】→ 翻页")
    if n > 1:
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(500)
        check(pg.inner_text("#viewIdx") != idx0, "页码变了：%s" % pg.inner_text("#viewIdx"))
        check(pg.get_attribute("#viewImg", "src") != src0, "图片换了")
        pg.keyboard.press("ArrowLeft")
        pg.wait_for_timeout(500)
        check(pg.inner_text("#viewIdx") == idx0, "← 能翻回上一张")
    else:
        print("   [跳过] 只有一张图")

    print()
    print("【3】边界不越界")
    if n > 1:
        for _ in range(n + 5):
            pg.keyboard.press("ArrowLeft")
        pg.wait_for_timeout(500)
        check(pg.inner_text("#viewIdx").startswith("1 /"),
              "连按左键停在第一张：%s" % pg.inner_text("#viewIdx"))
        check(pg.locator("#viewPrev").is_disabled(), "到头的按钮置灰")
        for _ in range(n + 5):
            pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(500)
        check(pg.inner_text("#viewIdx").startswith("%d /" % n),
              "连按右键停在最后一张：%s" % pg.inner_text("#viewIdx"))

    print()
    print("【4】Esc 关闭 / 点背景关闭")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(400)
    check(pg.locator("#viewModal.on").count() == 0, "Esc 关掉了")
    shots.first.click()
    pg.wait_for_timeout(500)
    check(pg.locator("#viewModal.on").count() == 1, "又能打开")
    pg.mouse.click(5, 5)               # 浮层外的角落
    pg.wait_for_timeout(400)
    check(pg.locator("#viewModal.on").count() == 0, "点背景关掉了")

    print()
    print("【5】浮层开着时左右键始终翻页（焦点残留也不能失效）")
    # 点缩略图不会把焦点从提示词框拿走（img 不可聚焦），所以打开浮层前光标
    # 可能还在 textarea 里。早期版本判断"焦点在输入框就放行"，结果看着像
    # 按键没反应 —— 浮层是模态的，这种情况就该翻页。
    pg.fill("#promptCN", "测试")
    pg.click("#promptCN")
    shots.first.click()
    pg.wait_for_timeout(600)
    i_before = pg.inner_text("#viewIdx")
    check(pg.evaluate("document.activeElement.tagName") in ("TEXTAREA", "BODY"),
          "打开浮层后焦点确实可能还留在输入框/body：%s"
          % pg.evaluate("document.activeElement.tagName"))
    pg.keyboard.press("ArrowRight")
    pg.wait_for_timeout(400)
    check(pg.inner_text("#viewIdx") != i_before,
          "左右键照样翻页：%s → %s" % (i_before[:8], pg.inner_text("#viewIdx")[:8]))
    check(pg.input_value("#promptCN") == "测试",
          "提示词框的内容没被改动：%r" % pg.input_value("#promptCN"))

    print()
    print("【6】控制台没有报错")
    real = [e for e in errs if "favicon" not in e.lower()
            and "404" not in e and "Failed to load resource" not in e]
    check(not real, "无 JS 错误" if not real else "JS 报错: %s" % real[:3])
    pg.close()


with sync_playwright() as pw:
    br = bh.launch(pw, headless=True, viewport={"width": 1500, "height": 1000},
                   seed="viewer")
    try:
        run(br)
    finally:
        br.close()
        bh.cleanup_profile()

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
