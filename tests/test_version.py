# -*- coding: utf-8 -*-
"""版本号管道：VERSION 文件 -> /api/capabilities -> 页头 -> 发布目录。

为什么专门测：版本号散在多处迟早对不上（CHANGELOG 写 0.1.0、页头显示 0.0.0）。
这个项目在"同一个值写两个地方"上已经吃过一次亏（CFG 默认值），所以钉住。

不需要 ComfyUI；页头那部分需要服务在跑，没跑就跳过。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


print("=" * 74)
print("【1】VERSION 文件存在且长得像个版本号")
check(os.path.isfile(paths.VERSION_FILE), "VERSION 文件在：%s" % paths.VERSION_FILE)
check(re.fullmatch(r"\d+\.\d+\.\d+", paths.VERSION or "") is not None,
      "内容是 semver（读到 %r）" % paths.VERSION)
raw = io_open = None
with open(paths.VERSION_FILE, encoding="utf-8") as fh:
    raw = fh.read()
check(raw.endswith("\n") and raw.strip() == raw.strip("\n"),
      "文件格式干净（一行 + 结尾换行）")

print()
print("【2】paths / server / 发布脚本用的是同一个来源")
import server  # noqa: E402
check(server.VERSION == paths.VERSION,
      "server.VERSION == paths.VERSION（%s）" % server.VERSION)
# dev/ 不进发布包，所以在发布目录里跑时这个文件不存在 —— 跳过，不算失败。
_rel = os.path.join(paths.APP_DIR, "dev", "make_release.py")
if os.path.isfile(_rel):
    rel_src = open(_rel, encoding="utf-8").read()
    check('"VERSION"' in rel_src, "发布脚本会把 VERSION 复制过去")
else:
    print("   [跳过] 没有 dev/make_release.py（发布包里本来就没有 dev/）")

print()
print("【3】没有别处再写死版本号")
bad = []
for f in ("server.py", "ui.html", "paths.py"):
    t = open(os.path.join(paths.APP_DIR, f), encoding="utf-8").read()
    for m in re.finditer(r'["\']v?(\d+\.\d+\.\d+)["\']', t):
        bad.append((f, m.group(1)))
check(not bad, "源码里没有硬编码的版本号" if not bad else "这些地方写死了：%s" % bad)

print()
print("【4】服务把版本报给前端（服务没起、或不是本目录的服务就跳过）")
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
#
# ★ 这里用 ok() 而不是 require()。
#   require() 会直接 exit(0) —— 那会把上面 1~3 节的检查结果（包括失败）
#   一起吞掉。只有"整个测试都依赖服务"的文件才该在开头用 require()。
if not serverguard.ok(BASE):
    print("   [跳过] %s" % serverguard.check(BASE)[1])
else:
    try:
        with urllib.request.urlopen(BASE + "/api/capabilities", timeout=20) as r:
            caps = json.loads(r.read().decode("utf-8", "replace"))
        check((caps.get("data") or {}).get("version") == paths.VERSION,
              "/api/capabilities 里的 version = %s"
              % (caps.get("data") or {}).get("version"))
        with urllib.request.urlopen(BASE + "/", timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'id="buildTag"[^>]*>([^<]*)<', html)
        shown = m.group(1) if m else ""
        check(paths.VERSION in shown,
              "页头构建戳里有版本号：%r" % shown[:60])
        check("<!--BUILD-->" not in html, "占位符已被替换")
    except urllib.error.URLError as e:
        print("   [跳过] 服务没起：%s" % e)

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
