# -*- coding: utf-8 -*-
"""真起一个服务，用真 HTTP 打这些越界请求 —— 端到端确认用户看到的是 400 而不是 500。

为什么在 slow 层单独立一条（`test_param_ranges.py` 已经用纯函数验过一遍）：

  ★ 纯函数验不了**入口那一层**。裸 JSON 里的 `NaN` / `Infinity` 是
    `json.loads` 在**解析阶段**就吞进去的（它们不是标准 JSON），
    还没走到任何建图函数。所以"400 还是 500"只有真打 HTTP 才知道。

  ★ 也验不了**用户看到的文案**。实测过这个坑：探针用 `prompt_cn: "t"` 发越界请求，
    9 条全部返回 `400 提示词为空` —— 状态码对了、**理由完全不对**，
    那些 400 来自更早的"提示词为空"那道门，范围校验压根没执行。
    所以下面每一条都**连报错里有没有那个参数名一起判**，
    只判状态码的话它会报"全拦住"而实际漏掉 9 条。

★ 依赖外部状态的地方跟着状态走（AGENTS.md 那条硬规矩）：
  ComfyUI 关着时**不判失败**，只打印理由。但"该拦住的"那部分与 ComfyUI 无关，
  无论开关都必须全绿 —— 拦不住的 bug 不该被环境掩盖。

跑法：
    python tests/test_param_http.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True          # 别在 tests/ 里留 __pycache__
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import paths  # noqa: E402

PORT = 8821
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def post(path, raw, timeout=120):
    req = urllib.request.Request(BASE + path, data=raw,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def recent_paths():
    """产出目录里现在有哪些图。键是 **files**，不是 items。"""
    try:
        with urllib.request.urlopen(BASE + "/api/recent", timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return set()
    return {it.get("path") for it in (d.get("files") or [])
            if isinstance(it, dict)}


def recycle_files():
    """回收站里现在有哪些文件。用前后差集认自己的产物，**不猜文件名**。"""
    try:
        return set(os.listdir(paths.RECYCLE_DIR))
    except Exception:
        return set()


def wait_up(proc, secs=40):
    for _ in range(secs * 2):
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(BASE + "/api/capabilities", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


proc = subprocess.Popen([sys.executable, "-X", "utf8", paths.SERVER_PY,
                         "--port", str(PORT)],
                        cwd=ROOT, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
started = time.time()
try:
    if not wait_up(proc):
        print("RESULT: FAIL —— 服务没起来（端口 %d），这条测试什么都没验到" % PORT)
        sys.exit(1)

    caps = json.loads(urllib.request.urlopen(
        BASE + "/api/capabilities", timeout=10).read().decode("utf-8"))
    # ⚠️ `comfy_reachable` 是**嵌在 data 里的**，不是顶层键：
    #       {"ok": true, "data": {"comfy_reachable": true, ...}}
    #    我第一版写成 `caps.get("comfy_reachable")` —— 恒为 None，
    #    于是这条测试**永远走 skip 分支**，"能出图"那条断言永远不执行。
    #    这和 `test_upscale_e2e.py` 从没存在的 `upscalers` 键读值是**同一个坑**：
    #    测试看起来是绿的，其实什么都没验。
    #    所以这里键不在就**判失败**，不静默当成"不可达"。
    data = caps.get("data")
    check(isinstance(data, dict) and "comfy_reachable" in data,
          "/api/capabilities 里有 data.comfy_reachable（键名/层级要对得上，"
          "否则下面那条出图断言会永远跳过）")
    reachable = bool((data or {}).get("comfy_reachable"))
    print("  服务已起（端口 %d，用了 %.1fs）；ComfyUI 可达 = %s"
          % (PORT, time.time() - started, reachable))
    print()

    print("=" * 76)
    print("【1】该拦住的：HTTP 必须 400，**而且报错里要点出那个参数**")
    print("=" * 76)

    def body(fields):
        return ('{"prompt_cn":"一个女孩",' + fields + "}").encode("utf-8")

    # (说明, 原始请求体, 报错里必须出现的字样, 为什么是这组判据)
    BAD = [
        ("width=999999", body('"width":999999'), "width", "范围"),
        ("width=-100", body('"width":-100'), "width", "范围"),
        ("height=999999", body('"height":999999'), "height", "范围"),
        ("steps=0", body('"steps":0'), "steps", "范围"),
        ("steps=100000", body('"steps":100000'), "steps", "范围"),
        ("count=99999", body('"count":99999'), "count", "范围"),
        ("clip_skip=999", body('"clip_skip":999'), "clip_skip", "范围"),
        ("cfg=999", body('"cfg":999'), "cfg", "范围"),
        ("steps=NaN", body('"steps":NaN'), "NaN", "入口（json.loads 阶段）"),
        ("cfg=NaN", body('"cfg":NaN'), "NaN",
         "★ 改前这条**静默出图成功（HTTP 200）**，是最危险的一条"),
        ("width=Infinity", body('"width":Infinity'), "Infinity", "入口"),
        ("cfg=Infinity", body('"cfg":Infinity'), "Infinity", "入口"),
        ("width=-Infinity", body('"width":-Infinity'), "-Infinity", "入口"),
        ("坏 JSON", b'{"prompt_cn":', "合法 JSON", "入口"),
    ]
    before = recent_paths()
    for name, raw, needle, why in BAD:
        code, txt = post("/api/generate", raw)
        try:
            msg = str(json.loads(txt).get("error", txt))
        except Exception:
            msg = txt
        # ★ 两条一起判。"只看状态码"就是"因为错误理由而变绿"的入口。
        ok = (code == 400 and needle in msg)
        check(ok, "%-16s [%s] HTTP %-4s %s"
              % (name, why, code, msg[:46]))

    print()
    print("=" * 76)
    print("【2】cfg=NaN 那次不许真的落一张图到用户产出目录")
    print("=" * 76)
    time.sleep(0.5)
    new = recent_paths() - before
    check(not new, "上面 %d 条全被拒之后，产出目录新增 %d 张（必须是 0；"
                   "改前 cfg=NaN 那条会多 1 张）" % (len(BAD), len(new)))
    for p in sorted(new)[:4]:
        print("     ❌ %s" % p)

    print()
    print("=" * 76)
    print("【3】正常请求照常工作（加校验最容易误伤这里）")
    print("=" * 76)
    if not reachable:
        # 跟着真实状态走：ComfyUI 关着时"能不能出图"本来就无从验证，
        # 但**要说清理由**，不能默默跳过。
        print("  skip ComfyUI 连不上（comfy_reachable=False），"
              "出图那两条无从验证 —— 这不是通过")
        print("       被拒那两节的断言与 ComfyUI 无关，仍然必须全绿")
    else:
        # 只跑最小的一次（1 张），避免这条测试给用户产出目录添一堆图
        good = {"prompt_cn": "一个女孩", "steps": 10, "width": 768,
                "height": 768, "count": 1, "cfg": 1.0, "clip_skip": 0,
                "denoise": 1.0}
        code, txt = post("/api/generate", json.dumps(good).encode("utf-8"),
                         timeout=900)
        try:
            d = json.loads(txt)
            files = d.get("files") or []
            ok = (code == 200 and d.get("ok") and files)
        except Exception:
            files, ok = [], False
        check(ok, "界面能选的最小值（steps=10 cfg=1.0 768×768）能出图：HTTP %d，%d 张"
              % (code, len(files)))
        # 出完立刻清掉，不给用户产出目录留东西。
        #
        # ⚠️ /api/delete 是**移进回收站**，不是真删。所以只断言"产出目录里没了"
        #   是不够的 —— 原来这里就是这么写的（assert not os.path.isfile(p)），
        #   它一直是绿的，而**每跑一次这个测试，用户的回收站就多一个 ui_*.png**，
        #   没有任何信号。实测：连跑两次，回收站 92 -> 93 -> 94，净增 2。
        #   这和 AGENTS.md 里记的那次事故是同一个病（"中间产物落进 _recycle 就
        #   看不见了，于是一次次累积"），只是换了个测试。
        #   修法同 test_compare_feature.py：用回收站**前后差集**认出自己的产物，
        #   并且把"确实是移进回收站"这件事**断言出来**（比只断言目录里没了更强：
        #   万一有人把 move_to_recycle 改成 os.remove，这条会变红）。
        rx_before = recycle_files()
        for p in files:
            post("/api/delete", json.dumps({"path": p}).encode("utf-8"))
        check(all(not os.path.isfile(p) for p in files),
              "为验证产出的 %d 张已从产出目录清掉" % len(files))
        rx_new = recycle_files() - rx_before
        check(len(rx_new) == len(files),
              "/api/delete 把它们移进了回收站而不是直接删掉："
              "回收站新增 %d 条（期望 %d）" % (len(rx_new), len(files)))
        for name in sorted(rx_new):
            try:
                os.remove(os.path.join(paths.RECYCLE_DIR, name))
            except Exception:
                pass
        check(not (recycle_files() - rx_before),
              "测试自己的产物已从回收站清掉（不给用户的回收站留东西）")

    print()
    print("=" * 76)
    print("【4】/api/upscale 的 factor 也走 num_param，NaN 不该变成 500")
    print("=" * 76)
    code, txt = post("/api/upscale",
                     b'{"path":"x.png","factor":NaN}')
    try:
        msg = str(json.loads(txt).get("error", txt))
    except Exception:
        msg = txt
    check(code == 400, "factor=NaN -> HTTP %d（该 400，不是 500）：%s"
          % (code, msg[:50]))

finally:
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

print()
print("=" * 76)
if FAILS:
    print("RESULT: FAIL —— %d 项失败 / 共 %d 项" % (len(FAILS), len(FAILS) + len(OKS)))
    for f in FAILS:
        print("   FAIL: %s" % f)
    sys.exit(1)
print("RESULT: ALL PASS（%d 项）" % len(OKS))
sys.exit(0)
