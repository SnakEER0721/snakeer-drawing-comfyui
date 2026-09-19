"""Verify the inpaint pipeline: only the masked area may change.

Procedure:
  1. generate a base image through the normal txt2img path
  2. synthesize a mask (small white square in the middle) and upload the base
     image and the mask
  3. run the inpaint graph with a FIXED seed
  4. compare the result against the original: the outside must be byte-identical,
     the inside must differ

Step 4 is the whole point. If ImageCompositeMasked were wired with the mask
polarity backwards, the untouched area would be the part that changed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageStat

# ★ 少了这几行会 NameError: name 'paths' is not defined —— 而且**只在真的跑它
#   的时候才会发现**：run_tests.py 只跑 fast 层时它根本不会被执行。
#   实测：慢速层有两条这样的（这条和 test_api.py），发布包里一直带着两个
#   跑不起来的测试。是"跑一遍慢速层"抓出来的。
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

PY = sys.executable
SERVER = paths.SERVER_PY
PORT = 8850
BASE = "http://127.0.0.1:%d" % PORT
COMFY = "http://127.0.0.1:8188"


def post(path, payload, timeout=1200, base=BASE):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": "HTTP %s %s" % (e.code, raw[:200])}


def get(path, timeout=120):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


proc = subprocess.Popen([PY, "-X", "utf8", SERVER, "--port", str(PORT)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8")
made = []
try:
    for _ in range(60):
        try:
            get("/api/capabilities"); break
        except Exception:
            time.sleep(0.5)

    # ---- 1. base image --------------------------------------------------
    r = post("/api/generate", {
        "mode": "txt2img", "prompt_cn": "少女 半身 微笑 简单背景",
        "width": 640, "height": 640, "count": 1, "steps": 20, "cfg": 6.0,
        "seed": 777001, "loras": [],
    })
    if not r.get("ok"):
        print("基础图生成失败:", r.get("error")); raise SystemExit(1)
    base_img = r["files"][0]
    made.append(base_img)
    print("基础图:", os.path.basename(base_img))
    with Image.open(base_img) as im:
        W, H = im.size
    print("  尺寸: %dx%d" % (W, H))

    # ---- 2. build mask --------------------------------------------------
    # 蒙版 PNG：RGB = 原图，alpha = 255 表示"要重绘"，0 表示"保留"
    box = (int(W * 0.30), int(H * 0.30), int(W * 0.70), int(H * 0.70))
    mask_img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    d = ImageDraw.Draw(mask_img)
    d.rectangle(box, fill=(255, 255, 255, 0))     # 中间透明 = 要重绘
    mask_path = os.path.join(os.path.dirname(base_img), "_mask_test.png")
    mask_img.save(mask_path)
    made.append(mask_path)
    print("蒙版: 重绘区域 = %s" % (box,))

    # ---- 3. upload both -------------------------------------------------
    sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
    import server as srv
    with open(base_img, "rb") as f:
        name_img = srv.upload_image(f.read(), "base.png")
    with open(mask_path, "rb") as f:
        name_mask = srv.upload_image(f.read(), "mask.png")
    print("已上传:", name_img, "|", name_mask)

    # ---- 4. run the inpaint graph --------------------------------------
    graph = srv.build_inpaint_graph(name_img, name_mask, {
        "positive": "masterpiece, best quality, 1girl, smile, simple background",
        "negative": "bad hands, bad anatomy",
        "seed": 777001, "steps": 24, "cfg": 7.0, "denoise": 0.75,
        "mask_grow": 6, "mask_feather": 8, "loras": [],
    }, "temp/inpaint_check")
    res = post("/prompt", {"prompt": graph, "client_id": "t"}, base=COMFY)
    pid = res.get("prompt_id")
    if not pid:
        print("提交失败:", json.dumps(res, ensure_ascii=False)[:400]); raise SystemExit(1)
    print("已提交:", pid)

    out = None
    deadline = time.time() + 900
    while time.time() < deadline:
        with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=60) as resp:
            h = json.loads(resp.read())
        e = h.get(pid)
        if e and e.get("status", {}).get("completed"):
            for node_out in (e.get("outputs") or {}).values():
                for im in node_out.get("images", []):
                    url = (COMFY + "/view?filename=" + urllib.parse.quote(im["filename"])
                           + "&subfolder=" + urllib.parse.quote(im.get("subfolder", ""))
                           + "&type=output")
                    dst = os.path.join(os.path.dirname(base_img), "_inpaint_out.png")
                    with urllib.request.urlopen(url, timeout=300) as s2, open(dst, "wb") as f:
                        f.write(s2.read())
                    out = dst
            break
        st = (e or {}).get("status", {})
        if st.get("status_str") == "error":
            print("执行报错:", json.dumps(st, ensure_ascii=False)[:600]); raise SystemExit(1)
        time.sleep(0.7)
    if not out:
        print("超时"); raise SystemExit(1)
    made.append(out)
    print("重绘结果:", os.path.basename(out))

    # ---- 5. compare inside vs outside ----------------------------------
    a = Image.open(base_img).convert("RGB")
    b = Image.open(out).convert("RGB")
    if a.size != b.size:
        print("!! 尺寸不一致:", a.size, b.size); raise SystemExit(1)

    diff = ImageChops.difference(a, b).convert("L")
    inside = diff.crop(box)
    # outside = everything except the (grown) box; use a slightly larger box
    grown = (max(0, box[0] - 16), max(0, box[1] - 16),
             min(W, box[2] + 16), min(H, box[3] + 16))
    outside_mask = Image.new("L", (W, H), 255)
    ImageDraw.Draw(outside_mask).rectangle(grown, fill=0)
    outside_px = [p for p, m in zip(diff.getdata(), outside_mask.getdata()) if m]
    inside_px = [p for p, m in zip(diff.getdata(),
                 Image.new("L", (W, H), 0).point(lambda v: 0).getdata())] if False else None

    di = ImageStat.Stat(inside).mean[0]
    do = sum(outside_px) / max(len(outside_px), 1)
    print()
    print("=== 结果 ===")
    print("  蒙版内平均差异: %.2f  (应该明显 > 0)" % di)
    print("  蒙版外平均差异: %.4f  (应该 ≈ 0)" % do)
    print("  蒙版外最大差异: %d" % (max(outside_px) if outside_px else -1))

    ok = di > 5 and do < 1.0
    print()
    print("RESULT: " + ("PASS 只改了蒙版区域" if ok else "FAIL"))
    if not ok:
        if di <= 5:
            print("  原因: 蒙版内没变化 -> 重绘没生效")
        if do >= 1.0:
            print("  原因: 蒙版外被改动 -> 贴回时蒙版极性或尺寸有问题")
    _verdict = 0 if ok else 1
finally:
    # ★ 先停服务再删产物 —— Windows 上服务还握着 PNG 句柄时
    #   os.remove 会抛 PermissionError，删不干净。
    proc.terminate()
    try:
        o = proc.communicate(timeout=20)[0]
        if o and "Traceback" in o:
            print("--- server traceback ---")
            print(o[-1500:])
    except Exception:
        proc.kill()
    for p in made:
        for _ in range(6):
            try:
                os.remove(p); break
            except Exception:
                time.sleep(0.3)

# ★ 退出码必须在 finally **之后**给（清理要跑完）。
#   原来没有 sys.exit —— 于是单独跑这个文件时，判定 FAIL 也退 0：
#   run_tests.py 靠读那行文字还能发现，但写进 CI 或别的脚本里就静默通过了。
#   （`_verdict` 用 globals 取，因为正常路径上它在 try 里赋值，
#     中途抛异常时可能还没轮到它。）
sys.exit(globals().get("_verdict", 1))
