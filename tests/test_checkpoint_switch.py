# -*- coding: utf-8 -*-
"""底模切换：界面上选哪个，工作流里就得是哪个 —— 包括「没选」的情况。

为什么专门测：这条链上有两个静默失败的坑，都是本项目踩过的同类：
  1. 前端不送 checkpoint 时，后端用自己的兜底值 —— 界面换了底模却没生效；
  2. 前端送 `checkpoint: null` 时，`p.get("checkpoint", CHECKPOINT)` **不会**用
     默认值（键存在、值是 None），None 直接进工作流，ComfyUI 报 value_not_in_list。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
import server  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def ckpt_of(graph):
    """从工作流里把 CheckpointLoaderSimple 的 ckpt_name 挖出来。"""
    for node in graph.values():
        if isinstance(node, dict) and node.get("class_type") == "CheckpointLoaderSimple":
            return node["inputs"].get("ckpt_name")
    return None


BASE = {
    # build_graph 要的是已经翻译好的正向词（positive），不是中文原文
    "positive": "1girl, solo", "negative": "lowres",
    "width": 1024, "height": 1024, "steps": 20, "cfg": 6.0,
    "seed": 1, "loras": [], "count": 1,
}

print("=" * 74)
print("【1】build_graph：选了哪个就用哪个")
g = server.build_graph(dict(BASE, checkpoint="myModel.safetensors"))
check(ckpt_of(g) == "myModel.safetensors",
      "正向图用的是 myModel：%r" % ckpt_of(g))

print()
print("【2】build_graph：不送 / 送 null，都要退回 CHECKPOINT")
check(ckpt_of(server.build_graph(dict(BASE))) == server.CHECKPOINT,
      "不送 -> %r" % ckpt_of(server.build_graph(dict(BASE))))
check(ckpt_of(server.build_graph(dict(BASE, checkpoint=None))) == server.CHECKPOINT,
      "送 null -> %r（不能是 None）"
      % ckpt_of(server.build_graph(dict(BASE, checkpoint=None))))
check(ckpt_of(server.build_graph(dict(BASE, checkpoint=""))) == server.CHECKPOINT,
      "送空串 -> %r" % ckpt_of(server.build_graph(dict(BASE, checkpoint=""))))

print()
print("【3】build_inpaint_graph：同三条规则")
INP = dict(BASE, checkpoint="myModel.safetensors")
g = server.build_inpaint_graph("in.png", "mask.png", INP, "temp/x")
check(ckpt_of(g) == "myModel.safetensors", "修图图用的是 myModel：%r" % ckpt_of(g))
for sent, label in ((None, "送 null"), ("", "送空串")):
    p = dict(BASE)
    p["checkpoint"] = sent
    got = ckpt_of(server.build_inpaint_graph("in.png", "mask.png", p, "temp/x"))
    check(got == server.CHECKPOINT, "%s -> %r" % (label, got))
p = dict(BASE)
got = ckpt_of(server.build_inpaint_graph("in.png", "mask.png", p, "temp/x"))
check(got == server.CHECKPOINT, "不送 -> %r" % got)

print()
print("【4】两个入口的兜底值必须是同一个")
check(server.CHECKPOINT == paths.CHECKPOINT,
      "server 的 CHECKPOINT 来自 paths：%r" % server.CHECKPOINT)

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
