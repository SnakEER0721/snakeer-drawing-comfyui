# -*- coding: utf-8 -*-
"""端到端：在界面上换底模 -> 写回 config.json -> 真的用新模型出图（不发图）。

服务没起时跳过。**这个测试会临时改 config.json 的 checkpoint，测完还原** ——
还原要同时管两处：文件，以及运行中服务的**内存**那份（只写文件的话服务会一直
用测试切过去的那个底模，用户开着界面跑一次测试就被悄悄换了默认底模）。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
fails = []

# 这个测试要**读写 config.json**。全新安装时它还不存在（发布包只带
# config.example.json，config.json 由 安装模型.bat 或界面上换底模时生成），
# 所以没有就跳过 —— 那是环境不具备，不是失败。
if not os.path.isfile(paths.CONFIG_PATH):
    print("=" * 74)
    print("SKIP: test_set_config_e2e 需要 config.json")
    print("=" * 74)
    print("  %s 不存在。" % paths.CONFIG_PATH)
    print("  全新安装时它是被 安装模型.bat（或界面上换底模）创建出来的；")
    print("  也可以手动复制 config.example.json 改名成 config.json。")
    print("RESULT: SKIP")
    print("=" * 74)
    sys.exit(0)

# 确认 8765 上跑的就是这个目录的服务。见 tests/serverguard.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_set_config_e2e")


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
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


try:
    caps = get("/api/capabilities")
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)

# 接口在不在（旧代码没有这个端点）
code, probe = post("/api/set_config", {})
if code == 404:
    print("RESULT: SKIP（%s 上还没有 /api/set_config）" % BASE)
    sys.exit(0)

list_ = (caps.get("data") or {}).get("checkpoints") or []
cur = (caps.get("data") or {}).get("checkpoint")
print("ComfyUI 里的底模: %s" % list_)
print("当前默认: %s" % cur)

# 真实 config.json 的原始内容，测完还原
raw_before = io.open(paths.CONFIG_PATH, encoding="utf-8", newline="").read()

try:
    print()
    print("【1】白名单：不允许改别的键")
    for key, why in (("comfy_output", "产出目录"), ("", "空键"), ("__proto__", "怪键")):
        code, r = post("/api/set_config", {"key": key, "value": "x"})
        check(code == 400 and not r.get("ok"),
              "%s 被拒绝（HTTP %d）：%s" % (why, code, str(r.get("error"))[:40]))

    print()
    print("【2】不允许写 ComfyUI 里没有的模型")
    code, r = post("/api/set_config", {"key": "checkpoint", "value": "no_such_model.ckpt"})
    check(code == 400 and not r.get("ok"),
          "写不存在的模型被拒：%s" % str(r.get("error"))[:50])

    if len(list_) < 2:
        print()
        print("   [跳过 3/4] ComfyUI 里只装了一个底模，没法测切换")
    else:
        other = next(m for m in list_ if m != cur)
        print()
        print("【3】切到另一个底模 -> 写进 config.json")
        code, r = post("/api/set_config", {"key": "checkpoint", "value": other})
        check(code == 200 and r.get("ok"), "接口返回成功：%s" % r)
        saved = json.loads(io.open(paths.CONFIG_PATH, encoding="utf-8").read())
        check(saved.get("checkpoint") == other,
              "config.json 里已经变成 %s" % other)
        check(io.open(paths.CONFIG_PATH, encoding="utf-8", newline="").read()
              .count("\r\n") == raw_before.count("\r\n"),
              "换行风格没变（CRLF 行数一致）")
        caps2 = get("/api/capabilities")
        check((caps2.get("data") or {}).get("checkpoint") == other,
              "重启前 /api/capabilities 也认新值：%s"
              % (caps2.get("data") or {}).get("checkpoint"))
finally:
    # 还原分两步，缺一不可：
    #
    # ① 先用**接口**把运行中的服务改回原值。为什么必须有这一步：
    #    /api/set_config 改的是「文件 + 进程内存里的那份配置」，而只把文件写
    #    回去**不会**让服务重新读文件 —— 服务会一直用【3】里切过去的那个底模，
    #    直到重启。实测（用户视角）：跑完一遍 fast 层之后，config.json 里明明
    #    写着 rinFlanimeIllustrious_v50，而 /api/capabilities 报的是
    #    Illustrious-XL-v2.0 —— 用户开着界面跑一次测试，默认底模就被悄悄换掉。
    # ② 再用原字节写回文件：接口那条会把 json 重排（键顺序/缩进），不能只靠它。
    if cur:
        try:
            code, r = post("/api/set_config", {"key": "checkpoint", "value": cur})
            print("已把运行中的服务改回 %s（HTTP %s）" % (cur, code))
        except Exception as e:
            print("★ 改回运行中的服务失败（%s）—— 要重启服务才生效" % e)
    io.open(paths.CONFIG_PATH, "w", encoding="utf-8", newline="").write(raw_before)
    print()
    print("已还原 config.json")

# 还原后再确认**两件它真正该负责的事**：文件逐字节回去、运行中的服务回到原值。
#
# ★ 这里**不能**断言「文件和服务的值相等」。两者不一致是**合法状态**：
#   config.json 是用户会手改的文件（README 的 FAQ 里就有「改了 config.json 却
#   没生效」这一条 —— 改文件不会让运行中的服务重新读它，重启才生效）。
#   本机实测：跑之前文件=rinFlanime、服务=v2.0（上一次测试遗留的漂移），
#   硬拿一个值去要求两处相等，报出来的"失败"和被测代码毫无关系。
after = json.loads(io.open(paths.CONFIG_PATH, encoding="utf-8").read())
print("还原后 checkpoint = %s" % after.get("checkpoint"))
check(io.open(paths.CONFIG_PATH, encoding="utf-8", newline="").read() == raw_before,
      "config.json 逐字节还原（%s）" % after.get("checkpoint"))
if cur:
    live = (get("/api/capabilities").get("data") or {}).get("checkpoint")
    check(live == cur, "运行中的服务还原成测试前的值 %s（实际 %s）" % (cur, live))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
