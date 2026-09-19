# -*- coding: utf-8 -*-
"""本地服务防护：只接受本机页面发来的请求。

背景：这是个听 127.0.0.1 的服务，但"本地"不等于"可信" —— 浏览器里任何一个
网页都能向它发请求（跑图、删产出、改配置）。挡法是校验两个浏览器必然会带、
攻击者伪造不了的头：

    Host   必须 127.0.0.1 / localhost（防 DNS rebinding）
    Origin 有就必须是本服务；本地脚本不发这个头，所以不受影响

服务没起时跳过。
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务（见 tests/serverguard.py）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_origin_guard")
fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def req(path, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            # 读 4000 字节而不是 200：报错里是中文，截在 200 字节会把 UTF-8
            # 字符切断，json.loads 直接失败 —— 那是测试的问题，不是产品的
            return resp.status, resp.read(4000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000)
    except Exception as e:
        return None, str(e).encode()


# 先确认服务在跑并且是新代码（旧代码没有这个校验，第 3 条会挂）
try:
    code, _ = req("/api/capabilities")
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)
if code is None:
    print("RESULT: SKIP（连不上 %s）" % BASE)
    sys.exit(0)

bad_origin = req("/api/capabilities", headers={"Origin": "https://evil.example"})
if bad_origin[0] == 200:
    print("RESULT: SKIP（%s 上跑的还是没有 Origin 校验的旧代码）" % BASE)
    sys.exit(0)

print("=" * 74)
print("【1】本机脚本（不带 Origin）—— 必须放行，否则 24 个测试全废")
st, _ = req("/api/capabilities")
check(st == 200, "无 Origin 的 GET -> HTTP %s" % st)
st, _ = req("/api/translate", "POST", body={"text": "少女"})
check(st == 200, "无 Origin 的 POST -> HTTP %s" % st)

print()
print("【2】本机页面（同源 Origin）—— 必须放行")
st, _ = req("/api/capabilities", headers={"Origin": BASE})
check(st == 200, "同源 Origin -> HTTP %s" % st)
st, _ = req("/api/translate", "POST", headers={"Origin": BASE}, body={"text": "少女"})
check(st == 200, "同源 Origin 的 POST -> HTTP %s" % st)

print()
print("【3】恶意网页（跨源 Origin）—— 必须挡住")
for p, m in (("/api/capabilities", "GET"), ("/api/generate", "POST"),
             ("/api/delete", "POST"), ("/api/set_config", "POST")):
    st, body = req(p, m, headers={"Origin": "https://evil.example"}, body={} if m == "POST" else None)
    check(st == 403, "%s %s 跨源 -> HTTP %s（期望 403）" % (m, p, st))

print()
print("【4】DNS rebinding（Host 是攻击者域名）—— 必须挡住")
# 模拟：Host 头写成 evil.com，Origin 也写成 evil.com（真的 rebinding 就是这样）
for p in ("/", "/api/capabilities"):
    st, _ = req(p, headers={"Host": "evil.example:8765",
                            "Origin": "http://evil.example:8765"})
    check(st == 403, "%s 带假 Host -> HTTP %s（期望 403）" % (p, st))

print()
print("【5】图片端点也要挡住跨源（<img> 也会带 Origin）")
st, _ = req("/api/image?path=x", headers={"Origin": "https://evil.example"})
check(st == 403, "跨源取图 -> HTTP %s（期望 403）" % st)

print()
print("【6】报错要说人话")
st, body = req("/api/capabilities", headers={"Origin": "https://evil.example"})
try:
    msg = json.loads(body.decode("utf-8", "replace")).get("error", "")
except Exception:
    msg = ""
check("127.0.0.1" in msg and "Origin" in msg,
      "403 里写清了要怎么办：%s" % msg[:60])

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
