"""Systematic API test suite for the web UI backend.

Covers every endpoint on both the happy path and the edge cases that have
historically broken in this project (bad input, missing files, repeated runs,
concurrency). Reports a PASS/FAIL table rather than stopping at the first error.
"""
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ★ 少了这几行会 NameError: name 'paths' is not defined —— 而且**只在真的跑它
#   的时候才会发现**：run_tests.py 只跑 fast 层时它根本不会被执行。
#   实测：慢速层有两条这样的（这条和 test_inpaint.py），发布包里一直带着两个
#   跑不起来的测试。是"跑一遍慢速层"抓出来的。
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

PY = sys.executable
SERVER = paths.SERVER_PY
PORT = 8820
BASE = "http://127.0.0.1:%d" % PORT
COMFY_INPUT = paths.COMFY_INPUT

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %-46s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail[:80]) if detail and not cond else ""))


def req(method, path, payload=None, timeout=900, raw=None):
    url = BASE + path
    data = None
    headers = {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/json"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body.decode("utf-8", "replace"))
            except Exception:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return e.code, body[:200]
    except Exception as e:
        return None, {"exception": "%s: %s" % (type(e).__name__, e)}


def get(path, timeout=120):
    return req("GET", path, timeout=timeout)


from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

proc = subprocess.Popen([PY, "-X", "utf8", SERVER, "--port", str(PORT)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8")
created = []
try:
    for _ in range(60):
        try:
            s, d = get("/api/capabilities", timeout=5)
            if s == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)

    print("=" * 78)
    print("A. 基础端点")
    print("=" * 78)
    s, _ = get("/")
    check("GET / 返回 200", s == 200, "status=%s" % s)
    s, d = get("/api/capabilities")
    check("GET /api/capabilities 返回 200", s == 200)
    check("  capabilities.ok 为真", d.get("ok") is True)
    caps = d.get("data", {})
    for k in ("comfy_online", "txt2img", "img2img", "sketch", "reference"):
        check("  capability %s 存在" % k, k in caps)
    check("  lora_details 是列表", isinstance(caps.get("lora_details"), list))
    check("  sketch_detail.controlnets 是列表",
          isinstance(caps.get("sketch_detail", {}).get("controlnets"), list))

    s, d = get("/api/recent?limit=5")
    check("GET /api/recent 返回 200", s == 200)
    check("  返回 files 列表", isinstance(d.get("files"), list))

    s, d = get("/api/last")
    check("GET /api/last 返回 200", s == 200)

    print()
    print("=" * 78)
    print("B. 错误处理（不该 500 的地方）")
    print("=" * 78)
    s, d = get("/api/nonexistent")
    check("未知路径返回 404", s == 404, "status=%s" % s)

    s, d = req("GET", "/api/image")
    # 以前这里是 404（send_error 的 HTML 错误页）。改成 JSON 错误 + 精确状态码：
    # 前端按 JSON 解析，拿到 HTML 会二次报错，看不清真正原因。
    check("GET /api/image 无参数 -> 400 且是 JSON",
          s == 400 and isinstance(d, dict) and "error" in d,
          "status=%s d=%s" % (s, str(d)[:60]))

    # ★ 这里要构造一个**真的在允许范围之外**的路径。
    #   原来用的是 `OUT_ANIME/_nope/no_such.png` —— 那是产出目录的**子目录**，
    #   而 is_readable_path 用 commonpath 比对根目录，子目录当然算在内。
    #   于是白名单过了、只是文件不存在，服务返 404 是**对的**，测试却期望 403。
    #   （这条一直没被发现，是因为这个文件此前根本跑不起来 —— 缺 import paths。）
    #   C:\ 根下的路径不在任何允许根里，才是真的越界。
    _outside = os.path.join(os.path.abspath(os.sep), "_not_allowed_", "no_such.png")
    s, d = req("GET", "/api/image?path=" + urllib.parse.quote(_outside))
    check("GET /api/image 路径不在白名单 -> 403",
          s == 403 and isinstance(d, dict), "status=%s d=%s" % (s, str(d)[:60]))

    # 白名单内的路径但文件不存在 -> 404（先判越界，再判存在）
    s, d = req("GET", "/api/image?path="
               + urllib.parse.quote(os.path.join(paths.OUT_ANIME, "no_such_file_xyz.png")))
    check("GET /api/image 白名单内但文件不存在 -> 404",
          s == 404 and isinstance(d, dict), "status=%s d=%s" % (s, str(d)[:60]))

    # 越权读取必须被挡住（回归：修复前 C:\Windows\win.ini 能整份读出来）
    s, d = req("GET", "/api/image?path=" + urllib.parse.quote(r"C:\Windows\win.ini"))
    check("GET /api/image 越权读系统文件 -> 403",
          s == 403, "status=%s d=%s" % (s, str(d)[:60]))

    s, d = req("POST", "/api/translate", raw=b"{not json")
    check("非法 JSON 体 -> 400 而不是 500", s == 400, "status=%s" % s)

    s, d = req("POST", "/api/translate", {})
    check("translate 空对象 -> 200 且 english 为空",
          s == 200 and d.get("english") == "", "status=%s d=%s" % (s, str(d)[:60]))

    s, d = req("POST", "/api/upload", {})
    check("upload 无 data -> 400/500 但要有 json 错误",
          s in (400, 500) and isinstance(d, dict) and "error" in d,
          "status=%s" % s)

    s, d = req("POST", "/api/upscale", {})
    check("upscale 无 path -> 400 而不是 500", s == 400, "status=%s" % s)

    # 同上：`OUT_ANIME/_nope/nope.png` 是产出目录的**子目录**，在白名单内，
    # 服务返 400"找不到这张图"是对的。改成真正越界的路径。
    s, d = req("POST", "/api/upscale",
               {"path": os.path.join(os.path.abspath(os.sep),
                                     "_not_allowed_", "nope.png")})
    # 先过白名单（403）再看文件在不在（400）—— 顺序是有意的：
    # 否则"文件不存在"和"不允许读"混在一起，探测时分不清。
    check("upscale 路径不在白名单 -> 403",
          s == 403, "status=%s d=%s" % (s, str(d)[:60]))

    s, d = req("POST", "/api/upscale",
               {"path": os.path.join(paths.OUT_ANIME, "no_such_file_xyz.png")})
    check("upscale 白名单内但文件不存在 -> 400",
          s == 400, "status=%s d=%s" % (s, str(d)[:60]))

    s, d = req("POST", "/api/generate", {"prompt_cn": ""})
    check("generate 空提示词 -> 400", s == 400, "status=%s" % s)

    s, d = req("POST", "/api/generate", {"prompt_cn": "zzzz不存在的词zzzz"})
    check("generate 全部未收录 -> 400 并列出 unknown",
          s == 400 and isinstance(d.get("unknown"), list), "status=%s" % s)

    print()
    print("=" * 78)
    print("C. 正常功能")
    print("=" * 78)
    s, d = req("POST", "/api/translate", {"text": "少女 半身 微笑"})
    check("translate 正常翻译", s == 200 and "1girl" in (d.get("english") or ""),
          str(d)[:70])

    # a real image for upload tests
    im = Image.new("RGB", (300, 300), (200, 120, 90))
    buf = io.BytesIO(); im.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    s, d = req("POST", "/api/upload", {"data": b64, "filename": "t.png"})
    check("upload 正常", s == 200 and d.get("ok"), str(d)[:70])
    upname = d.get("name")
    if upname:
        p = os.path.join(COMFY_INPUT, upname)
        check("  文件真的落到 input 目录", os.path.isfile(p))
        try:
            os.remove(p)
        except Exception:
            pass

    s, d = req("POST", "/api/generate", {
        "mode": "txt2img", "prompt_cn": "少女 半身 微笑",
        "width": 512, "height": 512, "count": 1, "steps": 8, "cfg": 6.0,
        "loras": [], "seed": 4242})
    check("generate 文生图", s == 200 and d.get("ok"), str(d.get("error"))[:70])
    if d.get("ok"):
        created.extend(d["files"])
        check("  产出文件存在", all(os.path.isfile(f) for f in d["files"]))
        check("  english 非空", bool(d.get("english")))

    print()
    print("=" * 78)
    print("D. 连续生成（历史上第二次必挂）")
    print("=" * 78)
    seq_ok = True
    for i in range(3):
        s, d = req("POST", "/api/generate", {
            "mode": "txt2img", "prompt_cn": "少女 微笑",
            "width": 512, "height": 512, "count": 1, "steps": 6, "cfg": 6.0,
            "loras": [], "seed": 500 + i})
        if not (s == 200 and d.get("ok")):
            seq_ok = False
            print("     第%d次失败: %s" % (i + 1, str(d.get("error"))[:90]))
        else:
            created.extend(d["files"])
    check("连续 3 次生成全部成功", seq_ok)

    print()
    print("=" * 78)
    print("E. 放大")
    print("=" * 78)
    if created:
        src = created[0]
        s, d = req("POST", "/api/upscale", {"path": src, "factor": 2}, timeout=600)
        check("upscale 2x", s == 200 and d.get("ok"), str(d.get("error"))[:70])
        if d.get("ok"):
            created.extend(d["files"])
            with Image.open(d["files"][0]) as o:
                check("  输出尺寸确实变大", o.width > 512, "%dx%d" % (o.size))

    print()
    print("=" * 78)
    print("F. 边界参数")
    print("=" * 78)
    s, d = req("POST", "/api/generate", {
        "mode": "txt2img", "prompt_cn": "少女 微笑",
        "width": 999999, "height": 999999, "count": 1, "steps": 6,
        "cfg": 6.0, "loras": [], "seed": 1})
    check("超大分辨率 -> 优雅失败（不崩溃服务）",
          s in (200, 400, 500) and isinstance(d, dict), "status=%s" % s)
    s2, _ = get("/api/capabilities")
    check("  服务仍存活", s2 == 200)

    s, d = req("POST", "/api/generate", {
        "mode": "img2img", "prompt_cn": "少女 微笑",
        "source_image": "definitely_missing.png",
        "width": 512, "height": 512, "count": 1, "steps": 6,
        "cfg": 6.0, "loras": [], "denoise": 0.5, "seed": 2})
    check("img2img 源图不存在 -> 优雅失败",
          isinstance(d, dict) and (d.get("ok") is False or s == 400),
          "status=%s" % s)
    s2, _ = get("/api/capabilities")
    check("  服务仍存活", s2 == 200)

    print()
    print("=" * 78)
    print("G. 并发（两个请求同时进来）")
    print("=" * 78)
    out = {}

    def worker(tag, seed):
        s, d = req("POST", "/api/generate", {
            "mode": "txt2img", "prompt_cn": "少女 微笑",
            "width": 512, "height": 512, "count": 1, "steps": 6, "cfg": 6.0,
            "loras": [], "seed": seed}, timeout=900)
        out[tag] = (s, d.get("ok"), d.get("files"))

    t1 = threading.Thread(target=worker, args=("a", 9001))
    t2 = threading.Thread(target=worker, args=("b", 9002))
    t1.start(); time.sleep(0.5); t2.start()
    t1.join(); t2.join()
    both = all(out.get(k, (None, False))[1] for k in ("a", "b"))
    check("并发两个生成都成功", both, str(out)[:120])
    for k in ("a", "b"):
        if out.get(k) and out[k][2]:
            created.extend(out[k][2])
    # files must not collide
    if both:
        fa = set(out["a"][2] or []); fb = set(out["b"][2] or [])
        check("  两次输出文件不冲突", not (fa & fb), str(fa & fb)[:80])

finally:
    # ★ 先停服务再删产物 —— Windows 上服务还握着 PNG 句柄时
    #   os.remove 会抛 PermissionError，删不干净。
    proc.terminate()
    try:
        out_txt = proc.communicate(timeout=20)[0]
        if out_txt and "Traceback" in out_txt:
            print()
            print("--- 服务端未捕获的 traceback ---")
            print(out_txt[-2500:])
    except Exception:
        proc.kill()
    for p in created:
        for _ in range(8):
            try:
                os.remove(p); break
            except Exception:
                time.sleep(0.4)

print()
print("=" * 78)
passed = sum(1 for _, o, _ in RESULTS if o)
failed = [(n, d) for n, o, d in RESULTS if not o]
print("结果: %d 通过 / %d 失败  (共 %d 项)" % (passed, len(failed), len(RESULTS)))
if failed:
    print()
    print("失败项:")
    for n, d in failed:
        print("   - %s   %s" % (n, d[:90]))
sys.exit(0 if not failed else 1)
