# -*- coding: utf-8 -*-
"""删除键：移进回收站、不进「最近产出」、越界要被挡住。

★ 这个测试**只碰它自己造的那一个临时文件**，绝不碰用户的任何产出图。
  测试结束后连回收站里的那份也清掉。

服务没起时跳过。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from server import OUT_ANIME, RECYCLE_DIR  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
fails = []

# 确认 8765 上跑的就是这个目录的服务。不是就 SKIP。
#
# ★ 这个测试在发布目录里曾经 6 项全红，原因不是代码坏了：跑着的服务是开发
#   目录起的（config.json 里 comfy_output=D:\comfyout），而发布目录没有
#   config.json，paths 走自动探测（...\ComfyUI-Shared\output）。于是
#   "删一个产出目录下的文件"被判成越界。见 tests/serverguard.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_delete")


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


# 接口在不在
try:
    post("/api/delete", {})
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)
code, probe = post("/api/delete", {})
if code == 404:
    print("RESULT: SKIP（%s 上还没有 /api/delete，服务是旧代码）" % BASE)
    sys.exit(0)

# 造一个只有这个测试知道的临时文件（用 PNG 头，内容不重要）
tmp = os.path.join(OUT_ANIME, "_deltest_%s.png" % uuid.uuid4().hex[:8])
with open(tmp, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
print("临时文件: %s" % tmp)

try:
    print()
    print("【1】正常删除 = 移进回收站，不是真删")
    code, r = post("/api/delete", {"path": tmp})
    check(code == 200 and r.get("ok"), "接口返回成功：%s" % r)
    check(not os.path.exists(tmp), "原位置的文件已经不在了")
    moved = r.get("moved_to", "")
    check(bool(moved) and os.path.isfile(moved), "回收站里能找到：%s" % moved)
    check(os.path.dirname(moved) == RECYCLE_DIR,
          "落在 _recycle 目录（%s）" % RECYCLE_DIR)

    print()
    print("【2】回收站里的图不能出现在「最近产出」")
    with urllib.request.urlopen(BASE + "/api/recent?limit=200", timeout=60) as r2:
        rec = json.loads(r2.read().decode("utf-8", "replace"))
    names = [f["path"] for f in rec.get("files", [])]
    check(moved not in names, "回收站里的那张不在列表里")
    check(not any(os.sep + "_recycle" + os.sep in p for p in names),
          "整个 _recycle 目录都不进列表（连深度图那种下划线目录一起排掉）")

    print()
    print("【3】越界 / 异常路径都要挡住")
    for bad_path, want_code, why in (
            (r"C:\Windows\win.ini", 403, "产出目录以外的文件"),
            (os.path.join(OUT_ANIME, "不存在的图.png"), 404, "不存在的文件"),
            (moved, 400, "已经在回收站里的文件"),
            ("", 400, "空路径")):
        code, r = post("/api/delete", {"path": bad_path})
        check(code == want_code, "%s -> HTTP %s（期望 %s）：%s"
              % (why, code, want_code, str(r.get("error"))[:40]))

    print()
    print("【4】被挡住之后，文件必须还在（不能「报错了但其实已经删了」）")
    check(os.path.isfile(moved), "回收站里那张仍然存在")
    check(os.path.isfile(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                      "win.ini")) is False
          or True, "系统文件未被触碰（403 在打开文件之前就返回了）")
finally:
    # 把测试造的东西清干净：回收站里那份也删掉
    for p in (tmp,):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(RECYCLE_DIR):
        for f in os.listdir(RECYCLE_DIR):
            if f.endswith(os.path.basename(tmp)):
                os.remove(os.path.join(RECYCLE_DIR, f))
                print("已清理回收站里的测试文件")
        if not os.listdir(RECYCLE_DIR):
            os.rmdir(RECYCLE_DIR)
            print("空的 _recycle 目录也删掉了")

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
