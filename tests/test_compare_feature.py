"""End-to-end check of the new features.

  1. inpaint with the ControlNet enabled actually reaches the graph
  2. /api/recent reports the source image for a repaired result, which is what
     the UI needs to offer a before/after comparison
  3. the fetched before/after pair is loadable at the same size (so the canvas
     can overlay them)
"""
import sys
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

PY = sys.executable
SERVER = paths.SERVER_PY
PORT = 8882
BASE = "http://127.0.0.1:%d" % PORT
CN = "noobaiInpainting_v10.fp16.safetensors"


def post(path, payload, timeout=1800):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": "HTTP %s %s" % (e.code, raw[:300])}


def get(path, timeout=180):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


fails = []
def check(name, cond, extra=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "   " + str(extra)))
    if not cond:
        fails.append(name)


proc = subprocess.Popen([PY, "-X", "utf8", SERVER, "--port", str(PORT)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8")
made = []


def recycle_files():
    """回收站里现在有哪些文件。"""
    try:
        return set(os.listdir(paths.RECYCLE_DIR))
    except Exception:
        return set()


recycle_before = recycle_files()
try:
    for _ in range(60):
        try:
            get("/api/capabilities"); break
        except Exception:
            time.sleep(0.5)

    caps = get("/api/capabilities")["data"]
    check("能力接口报告了 inpainting 模型",
          CN in (caps.get("inpaint_controlnets") or []),
          caps.get("inpaint_controlnets"))

    W = H = 640
    r = post("/api/generate", {
        "mode": "txt2img", "prompt_cn": "少女 脸部特写 微笑 简单背景",
        "width": W, "height": H, "count": 1, "steps": 22, "cfg": 6.0,
        "seed": 918273, "loras": []})
    base_img = r["files"][0]; made.append(base_img)

    mi = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    ImageDraw.Draw(mi).rectangle((180, 190, 460, 420), fill=(255, 255, 255, 0))
    mp = base_img.replace(".png", "_m.png"); mi.save(mp); made.append(mp)
    with open(mp, "rb") as f:
        mask_url = "data:image/png;base64," + base64.b64encode(f.read()).decode()

    print()
    print("=== 1. 挂 ControlNet 的局部重绘 ===")
    # ★ 出图之前记一份「画廊里有什么」。重绘的**普通合成结果**是中间产物，
    #   服务端会把它收进回收站（_recycle/），前端只拿到融合版 —— 用户点一次
    #   修图不该在画廊里多出两张。这条是用户能直接看到的行为，以前没有任何
    #   测试钉它，所以下面用「最近产出」的前后差集来断言。
    def gallery():
        return {os.path.normcase(f["path"])
                for f in get("/api/recent?limit=200").get("files", [])}
    gal_before = gallery()
    r2 = post("/api/inpaint", {
        "path": base_img, "mask": mask_url,
        "prompt": "masterpiece, 1girl, face, detailed face, green eyes, smile",
        "negative": "bad anatomy, blurry, jpeg artifacts", "quality": [],
        "seed": 555, "steps": 24, "cfg": 6.5, "denoise": 0.8,
        "mask_grow": 12, "mask_feather": 12, "loras": [],
        "inpaint_controlnet": CN, "controlnet_strength": 0.8,
        "blend": "poisson"})
    check("带 ControlNet 的重绘成功", r2.get("ok"), r2.get("error"))
    if not r2.get("ok"):
        raise SystemExit(1)
    result = r2["files"][0]; made.append(result)
    print("      产物:", os.path.basename(result))
    print("      用的提示词:", (r2.get("used_prompt") or "")[:70])

    gal_new = gallery() - gal_before
    check("画廊里只多了重绘结果这一张（普通合成结果没留在画廊里）",
          gal_new == {os.path.normcase(result)},
          [os.path.basename(p) for p in gal_new])

    print()
    print("=== 2. 溯源：/api/recent 是否带出原图 ===")
    rec = get("/api/recent?limit=20")
    hit = None
    for f in rec.get("files", []):
        if os.path.normcase(f["path"]) == os.path.normcase(result):
            hit = f; break
    check("重绘结果出现在最近产出里", hit is not None)
    if hit:
        check("带上了 prev（原图路径）", bool(hit.get("prev")), hit)
        check("origin 标记为 inpaint", hit.get("origin") == "inpaint", hit.get("origin"))
        if hit.get("prev"):
            check("prev 指向的确实是原图",
                  os.path.normcase(hit["prev"]) == os.path.normcase(base_img),
                  hit["prev"])

    print()
    print("=== 3. 前后两张图能否叠加（尺寸一致）===")
    if hit and hit.get("prev"):
        # ★ 必须 with 关掉。PIL 是**惰性打开**的：Image.open() 只读文件头，
        #   句柄一直留着，直到 .load()/.close() 或 GC。
        #   原来这两行没关，于是 Windows 上这两个文件被本进程锁住，
        #   finally 里的 os.remove 死活删不掉 —— 每次跑都往用户的产出目录
        #   留 2 张图。（这正是那 2 个"服务停了还占着"的文件的真正原因，
        #   不是服务占的，是测试自己占的。）
        with Image.open(hit["prev"]) as ia, Image.open(hit["path"]) as ib:
            check("两张图尺寸一致", ia.size == ib.size, (ia.size, ib.size))
            check("尺寸不为零", ia.size[0] > 0 and ia.size[1] > 0, ia.size)

    print()
    print("=== 4. 对比接口能取到图 ===")
    for label, p in (("原图", base_img), ("修后", result)):
        try:
            with urllib.request.urlopen(
                    BASE + "/api/image?path=" + urllib.parse.quote(p),
                    timeout=60) as rr:
                n = len(rr.read())
            check("能取到 %s 的字节" % label, n > 1000, n)
        except Exception as e:
            check("能取到 %s 的字节" % label, False, e)

    print()
    print("RESULT: " + ("ALL PASS" if not fails else "%d 项失败: %s"
                        % (len(fails), ", ".join(fails))))
finally:
    # ★ 顺序反了会漏文件：必须**先停服务，再删产物**。
    #   原来先 os.remove 再 terminate —— 而 Windows 上服务进程还握着刚写的
    #   PNG 句柄，os.remove 抛 PermissionError，重试 6 次（共 1.8 秒）也往往
    #   等不到它释放。结果每次跑都往用户的产出目录里留 3 张图。
    #   实测：跑了 3 遍慢速层，产出目录里多了 9 个文件（3×(ui_ + inp_ + inp__blend0)），
    #   而且它们把 test_read_meta.py 的「最近 40 张」挤变了，害那个快测变红。
    proc.terminate()
    try:
        o = proc.communicate(timeout=20)[0]
        if o and "Traceback" in o:
            print("--- server ---"); print(o[-1200:])
    except Exception:
        proc.kill()
    left = []
    for p in made:
        for _ in range(10):
            try:
                os.remove(p); break
            except Exception:
                time.sleep(0.3)
        else:
            left.append(p)
    if left:
        print("!! 有 %d 个测试产物没能删掉（服务停了还占着？）：" % len(left))
        for p in left:
            print("     %s" % p)
    # ★ 服务端自己收进回收站的中间产物也要清。它不是"用户的产出"（画廊里
    #   看不到），但确实落在产出目录底下 —— 不管它，每跑一次慢速层就在用户的
    #   回收站里多一个 `inp_*.png`。用前后差集认，不猜文件名。
    for name in sorted(recycle_files() - recycle_before):
        try:
            os.remove(os.path.join(paths.RECYCLE_DIR, name))
            print("   已清回收站里的中间产物: %s" % name)
        except Exception as e:
            print("!! 回收站里的 %s 没删掉: %s" % (name, e))

# ★ 退出码必须在 finally **之后**给（清理要跑完）。
#   原来没有 sys.exit —— 于是单独跑这个文件时，判定失败也退 0：
#   run_tests.py 靠读那行文字还能发现，但写进 CI 或别的脚本里就静默通过了。
#   （`fails` 在 try 之前就初始化过，所以这里一定能取到。）
sys.exit(0 if not fails else 1)
