# -*- coding: utf-8 -*-
"""端到端：真的放大一张图，量出结果的宽高比例。

为什么单测不够（这个项目上过当）：
    `test_upscale_size.py` 测的是 `upscale_target()` 的算术。但用户报的 bug 是
    "放大把尺寸拉伸了" —— 那是**整条链路**的结果：算目标尺寸 -> 建图
    （ImageScale + crop="disabled"，拉伸语义）-> ComfyUI 出图。
    算术对、图建错（比如 node 里写反了宽高）一样会拉伸。
    所以这里真的跑一次，量**成品 PNG 的像素**。

用合成图，不碰用户的产出：
    测试自己在 ComfyUI 的 input 目录里造一张 1344×768 的渐变图
    （16:9，正是踩坑的那组尺寸），放完就删；产出文件也按返回的路径删掉。

为什么用 1344×768 + 4×：
    旧代码是宽高**各自** min(..., 4096)：
        宽 1344*4=5376 -> 砍到 4096
        高  768*4=3072 -> 照给 3072
    比例从 1.75 变成 1.333，横向压扁 24%。用这组尺寸，正确实现和错误实现
    的结果**差得很远**，不会出现"两种写法碰巧都对"。

需要 ComfyUI 开着、放大模型已安装。服务没起或模型没装就 SKIP。

跑法：
    python tests/test_upscale_e2e.py            # 打 8765
    python tests/test_upscale_e2e.py http://127.0.0.1:8799
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

import paths  # noqa: E402
import serverguard  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
W0, H0 = 1344, 768            # 16:9，正是踩坑的那组
FACTOR = 4

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def post(path, body, timeout=600):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


print("=" * 74)
print("放大端到端：%d×%d @%d×" % (W0, H0, FACTOR))
print("=" * 74)

# 服务必须是本目录的（否则量的是别的安装的产出，结论不可信）
serverguard.require(BASE, "test_upscale_e2e")

# 放大模型在不在。
# ★ 键名是 `upscale_model`（单个默认模型的名字），不是 `upscalers`（没这个键）。
#   第一版写成 upscalers 拿到空列表，于是永远 SKIP —— 一个"永远跳过"的测试
#   比没有测试更糟：它看起来是绿的。
try:
    caps = serverguard.probe(BASE)
except Exception:
    caps = {}
ups_model = (caps or {}).get("upscale_model")
if not ups_model:
    print("SKIP: capabilities 里没有 upscale_model —— 没装放大模型？")
    print("      装一个 RealESRGAN_x4plus_anime_6B.pth 再跑")
    print("RESULT: SKIP")
    sys.exit(0)
print("  放大用: %s" % ups_model)
print("  可选的其他模型: %s"
      % ", ".join(list((caps.get("upscale_hint") or {}).keys())[:3]))

# ---------------------------------------------------------------- 造图
try:
    from PIL import Image
except ImportError:
    print("SKIP: 没装 Pillow")
    print("RESULT: SKIP")
    sys.exit(0)

src = os.path.join(paths.COMFY_INPUT, "_upscale_e2e_src.png")
want_ratio = W0 / H0
try:
    # 一张有细节的图（纯色放大看不出问题，但这里只量尺寸）
    im = Image.new("RGB", (W0, H0))
    px = im.load()
    for y in range(0, H0, 8):
        for x in range(0, W0, 8):
            px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
    im.save(src)
    print()
    print("【1】造了一张 %d×%d 的合成图放进 ComfyUI input" % (W0, H0))
    check(os.path.isfile(src), "合成图已就位")

    # ------------------------------------------------------------ 放大
    print()
    print("【2】调 /api/upscale（真的出图，可能要几十秒）")
    r = post("/api/upscale", {"path": src, "factor": FACTOR})
    if not r.get("ok"):
        check(False, "放大接口报错: %s" % r.get("error"))
        r = {}
    else:
        check(True, "接口返回 ok")
        frm, to = r.get("from"), r.get("to")
        print("      接口报的换算: %s×%s -> %s×%s"
              % (frm[0], frm[1], to[0], to[1]))
        check(frm == [W0, H0], "它读到的原图尺寸是 %s（期望 %s）" % (frm, [W0, H0]))
        # 接口自己报的目标尺寸就必须等比
        if to:
            got = to[0] / to[1]
            check(abs(got - want_ratio) < 0.01,
                  "接口报的目标比例 %.4f ≈ 原图 %.4f" % (got, want_ratio))
            check(max(to) <= 4096,
                  "最长边 %d 不超过上限 4096" % max(to))

    # ------------------------------------------------------------ 量成品
    print()
    print("【3】量成品 PNG 的真实像素（这才是用户看到的）")
    files = (r or {}).get("files") or []
    if not files:
        check(False, "没有产出文件，量不了")
        out_paths = []
    else:
        out_paths = [f.get("path") if isinstance(f, dict) else f for f in files]
        p = out_paths[0]
        check(os.path.isfile(p), "产出文件存在: %s" % os.path.basename(p))
        if os.path.isfile(p):
            with Image.open(p) as o:
                gw, gh = o.size
            print("      成品尺寸: %d×%d" % (gw, gh))
            ratio = gw / gh
            check(abs(ratio - want_ratio) < 0.01,
                  "成品比例 %.4f ≈ 原图 %.4f（差 %.2f%%）"
                  % (ratio, want_ratio, abs(ratio - want_ratio) / want_ratio * 100))
            check(max(gw, gh) <= 4096, "成品最长边 %d ≤ 4096" % max(gw, gh))
            check(gw > W0 and gh > H0, "确实放大了（比原图大）")
            # 旧代码在这组输入下会给出 4096×3072（比例 1.333）—— 明确排掉
            old_ratio = 4096 / 3072
            check(abs(ratio - old_ratio) > 0.05,
                  "不是旧代码那个被压扁的比例 %.3f（说明修复真的生效了）"
                  % old_ratio)
    # ------------------------------------------------------------ 清理
    print()
    print("【4】清理测试产物")
    n = 0
    for p in out_paths:
        try:
            if p and os.path.isfile(p):
                os.remove(p)
                n += 1
        except OSError:
            pass
    if n:
        check(True, "删掉了 %d 个测试产出的图（没留在你的产出目录里）" % n)
    else:
        check(True, "没有需要清理的产出")
finally:
    try:
        if os.path.isfile(src):
            os.remove(src)
    except OSError:
        pass

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m in FAILS:
        print("  FAIL %s" % m)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
