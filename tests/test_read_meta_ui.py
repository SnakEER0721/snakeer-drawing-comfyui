# -*- coding: utf-8 -*-
"""端到端（真浏览器）：把一张成品图丢进「从成品图读回参数」，看界面有没有真的填好。

为什么必须过浏览器：后端读得对，不代表界面接得上 —— 回填要写进 #promptCN、
要勾上质量词、要触发翻译预览、数值滑杆的标签还要跟着动。这些全是静态检查
看不见的东西。

服务没起 / 还跑着没带这个接口的旧代码时**跳过**（不是失败）。
用法：
    python server.py --port 8799 --no-browser
    python tests/test_read_meta_ui.py http://127.0.0.1:8799
"""
import base64
import glob
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务（见 tests/serverguard.py）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_read_meta_ui")
OUT = paths.OUT_ANIME
fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def num(v):
    """按**界面显示**的方式格式化数字。

    Python 的 str(5.0) 是 "5.0"，JavaScript 的 String(5.0) 是 "5" —— 直接拿
    str(cfg) 去页面上找，遇到 cfg 正好是整数就会假报失败（实测踩过）。
    """
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


# 接口在不在
try:
    req = urllib.request.Request(BASE + "/api/read_meta", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)
except urllib.error.HTTPError as e:
    if e.code == 404:
        print("RESULT: SKIP（%s 上没有 /api/read_meta，服务是旧代码）" % BASE)
        sys.exit(0)
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过（端到端测试需要浏览器）")
    print("RESULT: SKIP")
    sys.exit(0)

import browser_helper as bh  # noqa: E402

# 找一张真的带提示词的图。
# ★ 用应用自己的枚举（跳过 _recycle / _depth），不要裸 glob —— 裸 glob 会把
#   回收站里的图也算进来，可能挑中一张用户已经"删除"的。
from server import iter_output_images  # noqa: E402
_cand = [p for p, f in iter_output_images(OUT) if f.startswith("ui_")]
_cand.sort(key=os.path.getmtime)
cand = None
for p in reversed(_cand[-20:]):
    try:
        with open(p, "rb") as fh:
            req = urllib.request.Request(
                BASE + "/api/read_meta",
                data=json.dumps({"data": base64.b64encode(fh.read()).decode()}
                                ).encode(), headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception:
        continue
    if r.get("ok") and r.get("positive") and r.get("sampler", {}).get("steps"):
        cand = (p, r)
        break
if not cand:
    print("RESULT: SKIP（产出目录里没有可用作样本的图）")
    sys.exit(0)
path, meta = cand
print("样本图: %s" % os.path.basename(path))


def run(br):
    pg = br.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append("PAGEERROR: %s" % e))
    pg.on("console", lambda m: errs.append("CONSOLE: %s" % m.text)
          if m.type == "error" else None)
    pg.goto(BASE, wait_until="load")
    pg.wait_for_timeout(4000)

    print("=" * 78)
    print("【1】新面板在页面上")
    check(pg.locator("#metaBox").count() == 1, "「从成品图读回参数」折叠块存在")
    check(pg.locator("#dropMeta").count() == 1, "拖放区存在")
    pg.locator("#metaBox > summary").click()
    pg.wait_for_timeout(300)
    check(pg.locator("#dropMetaFile").count() == 1,
          "wireDrop 动态建的 file 控件带上了可预测 id（#dropMetaFile）")

    print()
    print("【2】丢一张成品图进去")
    before = pg.input_value("#promptCN")
    pg.set_input_files("#dropMetaFile", path)
    pg.wait_for_timeout(6000)
    after = pg.input_value("#promptCN")
    check(after.strip() != before.strip(), "正向提示词框被填上了")
    check(len(after) > 20, "填进去的内容不是空的（%d 字）" % len(after))
    check(meta["positive"].split(",")[-1].strip() in after
          or after.split(",")[-1].strip() in meta["positive"],
          "填的内容和图里的正向词一致（尾部标签对得上）")
    # 本应用会自动往正向词最前面加勾选的质量词。开头的质量词必须被摘出来
    # 变成勾选状态，否则图里那份会被重复加一遍。
    check(pg.locator("#qualityChips .chip.on").count() >= 1,
          "质量词被摘成勾选状态（不是硬塞进提示词框）")
    check(not after.lower().startswith("masterpiece"),
          "开头的 masterpiece 没有原样留在提示词框里")

    print()
    print("【3】读出来的信息有展示")
    out = pg.inner_text("#metaOut")
    check("底模" in out or "采样" in out, "参数摘要显示出来了")
    check(str(meta["sampler"]["steps"]) in out, "摘要里有步数 %s" % meta["sampler"]["steps"])
    check(num(meta["sampler"]["cfg"]) in out, "摘要里有 CFG %s" % meta["sampler"]["cfg"])
    if meta["loras"]:
        check(meta["loras"][0]["name"].split("/")[-1][:12] in out,
              "摘要里有 LoRA 名")

    print()
    print("【4】「填入负向词」按钮")
    if meta["negative"]:
        pg.get_by_role("button", name="填入负向词").click()
        pg.wait_for_timeout(500)
        neg = pg.input_value("#negative")
        check(neg.strip() == meta["negative"].strip(), "负向词框填对了")
    else:
        print("   [跳过] 样本图没有负向词")

    print()
    print("【5】「套用采样参数」按钮")
    pg.get_by_role("button", name="套用采样参数").click()
    pg.wait_for_timeout(600)
    check(pg.input_value("#seed").strip() == str(meta["sampler"]["seed"]),
          "种子套上了（%s）" % meta["sampler"]["seed"])
    check(abs(float(pg.input_value("#steps")) - float(meta["sampler"]["steps"])) < 0.01,
          "步数滑杆套上了")
    check(abs(float(pg.input_value("#cfg")) - float(meta["sampler"]["cfg"])) < 0.01,
          "CFG 滑杆套上了")
    # 注意用 text_content 不是 inner_text：#stVal 在默认折叠的「参数」区里，
    # inner_text 对不可见元素返回空串，会假报"标签没更新"。
    check(pg.text_content("#stVal").strip() == num(meta["sampler"]["steps"]),
          "步数的数字标签跟着更新了（没更新就是漏绑）")
    check(pg.input_value("#sampler") == meta["sampler"]["sampler_name"],
          "采样器选中了 %s" % meta["sampler"]["sampler_name"])
    check(pg.input_value("#width") == str(meta["size"][0])
          and pg.input_value("#height") == str(meta["size"][1]),
          "宽高套上了 %s×%s" % tuple(meta["size"]))
    st = pg.inner_text("#status")
    check("已套用" in st, "状态栏报告了套用结果：%s" % st[:60])

    print()
    print("【6】A1111 图：采样器名要翻译，认不出的绝不硬塞")
    # ★ 回归用户实报的错：A1111 的采样器名写进工作流会被 ComfyUI 拒掉
    #   （sampler_name: Value not in list）。界面必须在套用前就拦住。
    from PIL import Image, PngImagePlugin
    tmp = os.path.join(os.environ.get("TEMP", "."), "_meta_ui_sample.png")

    def write_a1111(sampler):
        im = Image.new("RGB", (64, 64), (30, 30, 30))
        info = PngImagePlugin.PngInfo()
        info.add_text("parameters", "1girl, solo\nNegative prompt: lowres\n"
                                    "Steps: 20, Sampler: %s, CFG scale: 7, "
                                    "Seed: 1, Size: 512x512" % sampler)
        im.save(tmp, pnginfo=info)

    write_a1111("Euler a")
    pg.set_input_files("#dropMetaFile", tmp)
    pg.wait_for_timeout(4000)
    pg.get_by_role("button", name="套用采样参数").click()
    pg.wait_for_timeout(600)
    check(pg.input_value("#sampler") == "euler_ancestral",
          "A1111 的「Euler a」翻成了 euler_ancestral（不是 euler_a）：%s"
          % pg.input_value("#sampler"))
    check(pg.input_value("#seed").strip() == "1", "A1111 的种子也套上了")

    write_a1111("SomeFutureSampler")
    pg.set_input_files("#dropMetaFile", tmp)
    pg.wait_for_timeout(4000)
    pg.get_by_role("button", name="套用采样参数").click()
    pg.wait_for_timeout(600)
    check(pg.input_value("#sampler") != "SomeFutureSampler",
          "认不出的采样器没有被塞进下拉框")
    check("没套用" in pg.inner_text("#status"), "状态栏说明了没套用：%s"
          % pg.inner_text("#status")[:70])

    print()
    print("【7】提交前的闸：下拉框里被塞了非法值时，压根不该发出去")
    # ★ 这一节**要求 ComfyUI 在线**：服务端是先问 ComfyUI 拿到合法取值表、
    #   再判断"这个 sampler 不是这台 ComfyUI 支持的"。ComfyUI 关着时它会先
    #   死在"连不上 ComfyUI"上，于是这两条断言测的就不是那道闸了。
    #   实测（2026-09-19）：维护者没开 ComfyUI 时 fast 层因此红两条 ——
    #   那是环境不具备，不是代码坏了（项目里其他端到端测试也都这么处理）。
    _ck = json.loads(urllib.request.urlopen(
        BASE + "/api/capabilities", timeout=30).read().decode("utf-8", "replace"))
    if not ((_ck.get("data") or {}).get("comfy_reachable")):
        print("   [跳过] ComfyUI 没开着 —— 这一节没法验（不是失败）")
    else:
        pg.fill("#promptCN", "1girl")
        pg.evaluate("""() => {
          const s = document.querySelector('#sampler');
          const o = document.createElement('option');
          o.value = 'bogus_sampler'; s.appendChild(o); s.value = 'bogus_sampler';
        }""")
        pg.click("#btnGen")
        pg.wait_for_timeout(1500)
        st = pg.inner_text("#status")
        check("不是这台 ComfyUI 支持的" in st, "被拦下来了：%s" % st[:80])
        check("bogus_sampler" in st, "报错里点名了是哪个值")
    os.remove(tmp)

    print()
    print("【8】控制台没有报错")
    # 和 test_smoke_page 同样的过滤：favicon / 静态资源 404 不是脚本错误
    real = [e for e in errs if "favicon" not in e.lower()
            and "404" not in e and "Failed to load resource" not in e]
    check(not real, "无 JS 错误" if not real else "JS 报错: %s" % real[:3])
    pg.close()


with sync_playwright() as pw:
    br = bh.launch(pw, headless=True, seed="readmeta")
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
