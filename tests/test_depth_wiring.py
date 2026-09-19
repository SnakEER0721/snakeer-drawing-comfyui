# -*- coding: utf-8 -*-
"""验证 build_graph 在勾了「构图迁移」时，确实把 depth 那条链接了进去。

纯图构造检查，不出图、不加载模型。但 build_graph 会向 ComfyUI 查一次
object_info（取 LoRA/模型清单），所以 ComfyUI 没跑时这条测不了 —— 明确跳过，
不要伪装成通过。
"""
import os
import json
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

try:
    urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=10).read()
except Exception as e:
    print("SKIP: ComfyUI 没在跑，depth 接线检查跳过（%s）" % e)
    sys.exit(0)

import server as S  # noqa: E402

body = {
    "positive": "1girl, solo",
    "prompt_cn": "少女",
    "width": 896, "height": 1152, "steps": 30, "cfg": 5.5,
    "depth_image": "ui_927109c3b3.png",     # 上一步回传的深度图
    "use_depth": True,
    "depth_strength": 0.7,
}

g = S.build_graph(body)
types = [v["class_type"] for v in g.values()]
print("节点总数:", len(g))
print("含 depth 相关节点:")
for k, v in sorted(g.items(), key=lambda x: int(x[0])):
    if v["class_type"] in ("ControlNetLoader", "SetUnionControlNetType",
                           "ControlNetApplyAdvanced", "LoadImage"):
        print("   #%-3s %-24s %s" % (k, v["class_type"],
                                     json.dumps(v["inputs"], ensure_ascii=False)[:160]))

n_union = types.count("SetUnionControlNetType")
n_apply = types.count("ControlNetApplyAdvanced")
print()
print("SetUnionControlNetType: %d 个（应为 1）" % n_union)
print("ControlNetApplyAdvanced: %d 个（应为 1）" % n_apply)
fails = []
if n_union != 1 or n_apply != 1:
    fails.append("depth 链路没接上")

# 关掉开关时必须完全不出现，否则会白加载 2.4G 模型
body["use_depth"] = False
g2 = S.build_graph(body)
t2 = [v["class_type"] for v in g2.values()]
print("关掉开关后 SetUnionControlNetType: %d（应为 0）" % t2.count("SetUnionControlNetType"))
if t2.count("SetUnionControlNetType") != 0:
    fails.append("关掉开关仍然接了 depth")

# 没解析出深度图时也不能接
body["use_depth"] = True
body["depth_image"] = None
g3 = S.build_graph(body)
t3 = [v["class_type"] for v in g3.values()]
print("没有深度图时 SetUnionControlNetType: %d（应为 0）" % t3.count("SetUnionControlNetType"))
if t3.count("SetUnionControlNetType") != 0:
    fails.append("没有深度图仍然接了 depth")

print()
if fails:
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: ALL PASS")
