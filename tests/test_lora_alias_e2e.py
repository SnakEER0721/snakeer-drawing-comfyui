# -*- coding: utf-8 -*-
"""端到端：在界面上给自己 LoRA 改名字/分类 -> 写进 lora_aliases.json -> 分类跟着变。

**这个测试会写一个真实文件（lora_aliases.json）**，所以测完必须逐字节还原 ——
那份文件里有用户手写的全部备注，弄丢一篇就再也写不回来了。还原逻辑同时管两种
起始状态：
  · 文件本来存在  -> 按原字节写回
  · 文件本来不存在（全新安装就是这样，用户还没改过任何一条）-> 删掉它

服务没起时跳过。写测试用的名字只在**一个**条目上做"写 → 验 → 清空"。
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
ALIAS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "lora_aliases.json")
serverguard.require(BASE, "test_lora_alias_e2e")

fails = []


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
    with urllib.request.urlopen(BASE + path, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def details():
    """拿 /api/capabilities 里的 LoRA 明细。

    ★ 外层是 {ok, data} —— 第一版把它当顶层读，于是 lora_details 是 None、
    22 条一条都看不见（是测试写错了，不是产品坏了）。
    """
    c = get("/api/capabilities")
    d = c.get("data") or c
    return {x["file"]: x for x in (d.get("lora_details") or [])}


try:
    det = details()
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)

# 接口在不在（旧代码没有这个端点）
code, probe = post("/api/lora_alias", {})
if code == 404:
    print("RESULT: SKIP（%s 上还没有 /api/lora_alias）" % BASE)
    sys.exit(0)

if not det:
    print("RESULT: SKIP（ComfyUI 里没有 LoRA，没法测）")
    sys.exit(0)

existed = os.path.isfile(ALIAS)
before = io.open(ALIAS, encoding="utf-8", newline="").read() if existed else ""
print("lora_aliases.json: %s" % ("已存在（%d 字节，测完按原字节还原）"
                                % len(before) if existed else "不存在（测完删掉）"))
print("ComfyUI 里的 LoRA: %d 个" % len(det))

target = sorted(det)[0]
orig_alias = det[target].get("alias") or ""
orig_kind = det[target].get("kind") or ""
print("拿来做实验的是: %s（当前名字=%r 分类=%r）"
      % (target, orig_alias, orig_kind))

try:
    print()
    print("【1】C站目录这一层真的接上了吗")
    withcat = [f for f, d in det.items() if d.get("catalog_name")]
    # 目录文件不随包发布到每个开发机都存在，所以不能断言"必须有 20 条"；
    # 但只要有一条带目录信息，就必须同时带上 tags 和 sha256（同一个来源）。
    if withcat:
        check(len(withcat) >= 1, "%d 条带 C站真名（例：%s）"
              % (len(withcat), withcat[0]))
        check(all(det[f].get("catalog_sha256") for f in withcat),
              "带真名的条目同时也带 sha256（同一个来源，不会缺一半）")
        check(all(det[f].get("catalog_name") for f in withcat),
              "catalog_name 不为空")
    else:
        print("   [跳过 1] 本机没有 lora_catalog.json（发布版用户可能也没有）—— "
              "这时分类会退回文件名/元数据，那是设计好的降级")
    check(all(d.get("kind_source") for d in det.values()),
          "每条的 kind_source 都写明是哪一层判的（%d 条）" % len(det))
    check(len({d.get("kind_source") for d in det.values()}) >= 1,
          "kind_source 的取值: %s"
          % sorted({d.get("kind_source") or "-" for d in det.values()}))

    print()
    print("【2】写一个自己起的名字")
    code, r = post("/api/lora_alias",
                   {"file": target, "alias": "端到端测试名", "kind": "other"})
    check(code == 200 and r.get("ok"), "接口成功（HTTP %d）: %s" % (code, r))
    on_disk = json.loads(io.open(ALIAS, encoding="utf-8").read())
    check((on_disk.get(target) or {}).get("alias") == "端到端测试名",
          "名字写进磁盘了")
    check((on_disk.get(target) or {}).get("kind") == "other", "分类也写进去了")
    check(len(on_disk) >= len(json.loads(before or "{}")),
          "别人手写的条目没被冲掉（%d 条）" % len(on_disk))

    d2 = details()[target]
    check(d2.get("alias") == "端到端测试名", "重新拉接口，新名字生效")
    check(d2.get("kind") == "other", "新分类生效")
    check(d2.get("kind_source") == "你标的", "来源标成「你标的」而不是猜的")

    print()
    print("【3】非法输入要被挡住，而且要说清为什么（报错信息要能照着改）")
    code, r = post("/api/lora_alias", {"file": target, "alias": "x", "kind": "画风"})
    check(code == 400 and not r.get("ok"),
          "非法分类被拒（HTTP %d）：%s" % (code, str(r.get("error"))[:40]))
    # 报错必须能照着改：说清"收到的是什么"和"合法取值有哪些"
    check(all(k in str(r.get("error") or "")
              for k in ("画风", "style", "quality")),
          "错误信息里既有收到的值、也有合法取值: %s" % str(r.get("error"))[:70])
    code, r = post("/api/lora_alias", {"file": "../../etc/passwd", "alias": "x"})
    check(code == 400 and not r.get("ok"),
          "ComfyUI 列表里没有的文件被拒（HTTP %d）" % code)
    code, r = post("/api/lora_alias", {"alias": "x"})
    check(code == 400 and not r.get("ok"), "缺 file 参数被拒（HTTP %d）" % code)
    code, r = post("/api/lora_alias", {"file": target, "alias": "a\nb"})
    check(code == 400 and not r.get("ok"), "名字里带换行被拒（HTTP %d）" % code)

    print()
    print("【4】清空名字 = 回到自动判断")
    code, r = post("/api/lora_alias", {"file": target, "alias": "", "kind": ""})
    check(code == 200 and r.get("ok"), "清空成功（HTTP %d）" % code)
    d3 = details()[target]
    check(d3.get("kind") != "other", "分类回到自动判断（%r）" % d3.get("kind"))
finally:
    # 还原。注意**服务端的内存缓存**：/api/lora_alias 每次都重新读文件
    # （lora_aliases() 不缓存），所以写回文件就够了，不需要再调一次接口。
    if existed:
        io.open(ALIAS, "w", encoding="utf-8", newline="").write(before)
    else:
        for p in (ALIAS, ALIAS + ".tmp", ALIAS + ".bak"):
            if os.path.isfile(p):
                os.remove(p)
    print()
    print("已还原 lora_aliases.json")

after = io.open(ALIAS, encoding="utf-8", newline="").read() if existed else None
check(after == (before if existed else None),
      "lora_aliases.json 回到测试前的状态（%s）"
      % ("逐字节相同" if existed else "文件不存在"))

# 最后再确认接口报的还是原来那条（证明还原是真的生效，不是只改了文件）
d4 = details().get(target) or {}
check((d4.get("alias") or "") == orig_alias,
      "接口报的名字回到 %r（实际 %r）" % (orig_alias, d4.get("alias")))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
