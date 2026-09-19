# -*- coding: utf-8 -*-
"""端到端打一次 /api/read_meta。

默认打正在跑的 8765。服务没起、或者还跑着没带这个接口的旧代码时**跳过**
（不是失败）—— 这和 test_depth_wiring 的处理一致：环境不具备时不该报红，
但也不能假装通过。

自己起临时实例跑：python server.py --port 8799 --no-browser
               然后 python tests/test_read_meta_e2e.py http://127.0.0.1:8799
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
OUT = paths.OUT_ANIME

# 确认 8765 上跑的就是这个目录的服务。不是就 SKIP —— 否则会拿"另一个安装"
# 的产出目录来判断，报出一堆和被测代码无关的失败。见 tests/serverguard.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_read_meta_e2e")


def _load(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, _load(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _load(e.read())


# 先探一下接口在不在
try:
    code, probe = post("/api/read_meta", {})
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)
if code == 404:
    print("RESULT: SKIP（%s 上还没有 /api/read_meta —— 服务是旧代码，重启后再跑）" % BASE)
    sys.exit(0)


# 找一张带提示词的真图。
# ★ 用应用自己的枚举（会跳过 _recycle / _depth），不要裸 glob ——
#   裸 glob 会连回收站一起扫，可能挑中一张用户已经"删除"的图。
from server import iter_output_images  # noqa: E402
files = [p for p, f in iter_output_images(OUT) if f.startswith("ui_")]
files.sort(key=os.path.getmtime)
files = files[-20:]
pick = None
for p in reversed(files):
    code, r = post("/api/read_meta", {"data": base64.b64encode(open(p, "rb").read()).decode()})
    if code == 200 and r.get("ok") and r.get("positive") and r.get("sampler", {}).get("steps"):
        pick = (p, r)
        break
if not pick:
    print("FAIL 最近 20 张里没有一张能读出完整参数")
    sys.exit(1)
p, r = pick
print("图: %s" % os.path.basename(p))
print("kind=%s  nodes=%s" % (r["kind"], r.get("node_count")))
print("正向: %s" % r["positive"][:120])
print("负向: %s" % r["negative"][:120])
print("底模: %s" % r["checkpoint"])
print("LoRA: %s" % r["loras"])
print("采样: %s" % r["sampler"])
print("尺寸: %s 批次 %s" % (r["size"], r["batch"]))
print("CN  : %s" % r["controlnet"])
print("meta_keys=%s" % r["meta_keys"])

bad = []
if not r["positive"] or not r["negative"]:
    bad.append("正/负向为空")
if r["positive"] == r["negative"]:
    bad.append("正负向相同")
if not r["sampler"].get("steps"):
    bad.append("没有 steps")
if not r["size"][0]:
    bad.append("没有尺寸")

print("\n[错误路径]")
code, e = post("/api/read_meta", {"data": base64.b64encode(b"not an image" * 50).decode()})
print("  非图片 -> HTTP %s  %s" % (code, e.get("error", "")[:70]))
if code == 200 and e.get("ok"):
    bad.append("坏图居然返回成功")
code, e = post("/api/read_meta", {})
print("  空请求 -> HTTP %s  %s" % (code, e.get("error", "")[:70]))
if code == 200 and e.get("ok"):
    bad.append("空请求居然返回成功")

print("\nRESULT: %s" % ("ALL PASS" if not bad else "FAIL " + "；".join(bad)))
sys.exit(1 if bad else 0)
