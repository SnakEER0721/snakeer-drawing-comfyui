"""End-to-end smoke test: load the real page and confirm every region actually fills.

Why this exists:
  Two silent failures shipped that all other tests missed.
    * loadCaps() lost its only call site -> every status pill stayed at 检测中…
    * pbData / pbSelected lost their declarations -> the prompt-builder panel
      opened but rendered zero options
  Both were invisible to syntax checks (the JS was valid) and to unit tests
  (the data layer was fine). The only thing that catches them is opening the
  page and looking at whether the regions contain anything.

This drives a real browser, opens every collapsible panel, and asserts each one
renders content. Skips cleanly if Playwright or the server is unavailable, so it
never becomes a false failure in a plain environment.

It launches through browser_helper, which gives the test run its own browser
profile. That matters for one reason: an earlier cleanup step killed every
msedge process with an empty MainWindowTitle, which is also how a normal
browser's renderer/GPU processes look - so it killed the user's browser
(RESULT_CODE_KILLED). With a dedicated profile the test's processes are
identifiable by an exact path and nothing else matches.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务（见 tests/serverguard.py）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_smoke_page")
fails = []

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过（这是端到端测试，需要浏览器）")
    print("RESULT: SKIP")
    sys.exit(0)

import browser_helper as bh

try:
    import urllib.request
    with urllib.request.urlopen(BASE + "/api/capabilities", timeout=20) as r:
        _caps0 = json.loads(r.read().decode("utf-8", "replace"))
except Exception as e:
    print("后端不可用（%s），跳过：这是端到端测试，需要服务在跑" % type(e).__name__)
    print("RESULT: SKIP")
    sys.exit(0)


def get_caps(path):
    """读一次能力接口（下面要拿它和界面显示的状态对照）。"""
    import urllib.request as _u
    try:
        with _u.urlopen(BASE + path, timeout=30) as r:
            return (json.loads(r.read().decode("utf-8", "replace")) or {}).get("data")
    except Exception:
        return {}


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def run_checks(br):
    pg = br.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("PAGEERROR: %s" % e))
    pg.on("console", lambda m: errs.append("CONSOLE: %s" % m.text)
          if m.type == "error" else None)
    pg.goto(BASE, wait_until="load")
    pg.wait_for_timeout(5000)

    print("=" * 78)
    print("【1】顶部状态区（loadCaps 是否真的跑了）")
    print("=" * 78)
    st = pg.evaluate("""() => {
      const c = document.getElementById('caps');
      return {text: c ? c.textContent.trim() : '',
              hasCaps: typeof caps !== 'undefined' && !!caps,
              online: (typeof caps !== 'undefined' && caps) ? caps.comfy_online : null};
    }""")
    print("   内容: %s" % st["text"][:100])
    check(st["hasCaps"], "caps 数据已加载（loadCaps 被调用且成功）")
    check("检测中" not in st["text"], "状态不再是「检测中…」")
    # ★ 别写死"必须在线"。ComfyUI 关着的时候页面**本来**就该显示 ✗ —— 那才是对的。
    #   原来这里断言 `caps.comfy_online is True`，于是维护者哪天没开 ComfyUI，
    #   fast 层就无缘无故红一条（实测：2026-09-19 就是这么红的）。
    #   现在改成**和真实状态对照**：服务端说 reachable 就必须 ✓，说不可达就必须 ✗。
    #   这比"跳过"更强 —— 两种状态下都验了界面反映得对不对。
    real_state = get_caps("/api/capabilities")
    real_reachable = bool((real_state or {}).get("comfy_reachable"))
    check(st["online"] is real_reachable,
          "ComfyUI 标签和真实状态一致（服务端说 reachable=%s，界面显示 %s）"
          % (real_reachable, st["online"]))
    if not real_reachable:
        print("   注：这台机器上 ComfyUI 没开着 —— 下面「面板是否填充」那几项"
              "按空状态检查（能填的必须填上，填不了的必须是空而不是坏）")

    print()
    print("=" * 78)
    print("【2】每个折叠面板展开后是否有内容")
    print("=" * 78)
    n = pg.evaluate("() => document.querySelectorAll('details').length")
    print("   页面共 %d 个折叠面板" % n)

    # 逐个展开，检查是否有子元素
    empties = []
    for i in range(n):
        info = pg.evaluate("""(idx) => {
          const d = [...document.querySelectorAll('details')][idx];
          const sum = d.querySelector('summary');
          const label = sum ? sum.textContent.trim().slice(0, 26) : '#' + idx;
          if(!d.open) d.open = true;
          return {label: label};
        }""", i)
        pg.wait_for_timeout(350)
        cnt = pg.evaluate("""(idx) => {
          const d = [...document.querySelectorAll('details')][idx];
          const body = d.querySelector('.body') || d;
          return {children: body.children.length,
                  text: body.textContent.trim().length};
        }""", i)
        if cnt["children"] == 0 and cnt["text"] < 10:
            empties.append(info["label"])
    if empties:
        for e in empties:
            print("   FAIL 面板「%s」展开后是空的" % e)
        fails.append("%d 个面板展开后为空" % len(empties))
    else:
        print("   OK   全部面板展开后都有内容")

    print()
    print("=" * 78)
    print("【3】提示词拼装面板（pbData 是否已声明）")
    print("=" * 78)
    pg.click("#pbBox > summary")     # 确保触发 toggle 加载
    pg.wait_for_timeout(3000)
    pb = pg.evaluate("""() => {
      const g = document.getElementById('pbGroups');
      return {groups: g ? g.children.length : -1,
              buttons: g ? g.querySelectorAll('button').length : -1};
    }""")
    check(pb["groups"] > 0, "拼装面板有分组（实际 %d）" % pb["groups"])
    check(pb["buttons"] > 50, "拼装面板有选项按钮（实际 %d）" % pb["buttons"])

    print()
    print("=" * 78)
    print("【4】最近产出（loadRecent + 日期分组）")
    print("=" * 78)
    # 先问后端实际有多少张 —— 输出目录为空是合法状态，
    # 无条件要求"必须渲染出卡片"会在清空目录后变成假失败。
    import urllib.request as _u
    try:
        with _u.urlopen(BASE + "/api/recent?limit=50", timeout=30) as r:
            api_n = len(json.loads(r.read().decode("utf-8", "replace")).get("files") or [])
    except Exception as e:
        api_n = -1
        print("   后端查询失败: %s" % e)
    pg.evaluate("() => loadRecent()")
    pg.wait_for_timeout(3000)
    rc = pg.evaluate("""() => {
      const b = document.getElementById('recent');
      return {groups: b.querySelectorAll('details.dayGroup').length,
              cards: b.querySelectorAll('.shot').length,
              text: b.textContent.trim().slice(0, 40)};
    }""")
    print("   后端报告 %d 张，页面渲染 %d 张（%d 组）" % (api_n, rc["cards"], rc["groups"]))
    if api_n == 0:
        check(rc["cards"] == 0, "输出目录为空时页面不渲染卡片")
        check(rc["cards"] > 0 or "还没有" in rc["text"] or rc["text"] != "",
              "空状态有提示文字")
    elif api_n > 0:
        check(rc["cards"] > 0, "有图片卡片（实际 %d / 后端 %d）" % (rc["cards"], api_n))
        check(rc["groups"] > 0, "有日期分组（实际 %d）" % rc["groups"])
    else:
        print("   （后端不可查，跳过这项）")

    print()
    print("=" * 78)
    print("【5】关键下拉框已填充")
    print("=" * 78)
    sel = pg.evaluate("""() => {
      const ids = ['width','height','sampler','scheduler','negPreset','count'];
      const out = {};
      ids.forEach(i => { const e = document.getElementById(i);
        out[i] = e && e.options ? e.options.length : -1; });
      return out;
    }""")
    for k, v in sel.items():
        check(v > 0, "%s 有 %d 个选项" % (k, v))

    print()
    print("=" * 78)
    print("【6】控制台错误")
    print("=" * 78)
    real = [e for e in errs if "favicon" not in e.lower()
            and "404" not in e and "Failed to load resource" not in e]
    if real:
        for e in real[:6]:
            print("   FAIL %s" % e[:110])
        fails.append("%d 条控制台错误" % len(real))
    else:
        print("   OK   无脚本错误")


with sync_playwright() as pw:
    br = bh.launch(pw, headless=True, viewport={"width": 1400, "height": 950})
    try:
        run_checks(br)
    finally:
        # 无论检查怎么结束，都只回收带测试 profile 标记的进程
        try:
            br.close()
        except Exception:
            pass
        _left = bh.reap_own_processes()
        if _left:
            print("   警告：测试浏览器残留 %d 个进程" % _left)

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
