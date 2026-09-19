"""回归：`loras: []` 必须表达「一个 LoRA 都不要」，不能被旧字段静默顶回来。

被测的 bug（server.py 原 L952）：
    loras = p.get("loras") or ([{"name": lora, "strength": lora_strength}] if lora else [])

Python 里空列表是 falsy，所以前端发 `loras: []`（用户取消了所有 LoRA）会掉进
or 的右边，把遗留的旧版单数 `lora` 字段重新挂回节点图 —— 界面显示「已取消」，
后端却照样加载。这正是「改 A 层、B 层仍然生效」那一类静默失败。

修复：改成按「键是否存在」判断（见 server.resolve_lora_list）。
显式传了 loras（哪怕空数组）就是用户的最终意图。

这个测试直接读节点图，不看日志、不看返回值，因为「LoRA 有没有被挂上」
唯一可信的证据就是图里有没有 LoraLoader 节点。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server

fails = []


def check(cond, label, detail=""):
    print("   %s %s%s" % ("OK  " if cond else "FAIL", label,
                          ("  " + str(detail)) if not cond else ""))
    if not cond:
        fails.append(label)


LORA = "MSS_v2_IL.safetensors"
BASE = {"positive": "masterpiece, best quality, 1girl, solo",
        "negative": "lowres, bad quality",
        "seed": 1, "steps": 28, "cfg": 5.0,
        "sampler": "euler_ancestral", "scheduler": "normal",
        "width": 1024, "height": 1024, "count": 1,
        "mode": "txt2img",
        "checkpoint": "Illustrious-XL-v2.0.safetensors"}


def loras_in(g: dict) -> list:
    return [n["inputs"].get("lora_name") for n in g.values()
            if n.get("class_type") == "LoraLoader"]


def model_chain(g: dict) -> list:
    """KSampler.model 往回走到头，看链上挂了什么。"""
    cur = None
    for n in g.values():
        if n.get("class_type") == "KSampler":
            cur = n["inputs"].get("model")
    out, seen = [], set()
    while isinstance(cur, list) and cur:
        nid = str(cur[0])
        if nid in seen:
            break
        seen.add(nid)
        n = g.get(nid)
        if not n:
            break
        out.append(n["class_type"])
        cur = n["inputs"].get("model")
    return out


print("=" * 86)
print("【1】resolve_lora_list 的语义：键存在就用它，不做「空即回退」")
print("=" * 86)
CASES = [
    ({"loras": []}, 0, "空数组 = 一个都不要"),
    ({"loras": [], "lora": LORA, "lora_strength": 0.8}, 0,
     "★ 空数组 + 遗留旧字段：必须忽略旧字段"),
    ({"loras": [{"name": LORA, "strength": 0.8}]}, 1, "正常一个"),
    ({"loras": [{"name": LORA, "strength": 0.8}], "lora": "x.safetensors"}, 1,
     "两者都有时以 loras 为准"),
    ({"loras": None, "lora": LORA}, 0, "loras 显式为 None 也算「不要」"),
    ({"lora": LORA, "lora_strength": 0.8}, 1, "没传 loras 键：旧字段仍兼容"),
    ({"lora": ""}, 0, "只有空 lora 字段"),
    ({}, 0, "两者都没有"),
]
for p, want, label in CASES:
    got = len(server.resolve_lora_list(p))
    check(got == want, "%s（%d 个）" % (label, got), "期望 %d 实得 %d" % (want, got))

print()
print("=" * 86)
print("【2】build_graph：节点图里到底有没有 LoraLoader")
print("=" * 86)
for extra, want, label in [
    ({"loras": []}, 0, "loras: []"),
    ({"loras": [], "lora": LORA, "lora_strength": 0.8}, 0,
     "★ loras: [] + 旧版 lora 字段"),
    ({}, 0, "完全没有 loras 键"),
    ({"lora": LORA, "lora_strength": 0.8}, 1, "只有旧版 lora 字段"),
    ({"loras": [{"name": LORA, "strength": 0.8, "triggers": []}]}, 1, "正常挂一个"),
    ({"loras": [{"name": LORA, "strength": 0.0}]}, 0, "强度 0 不挂"),
]:
    g = server.build_graph({**BASE, **extra})
    got = loras_in(g)
    check(len(got) == want, "%-30s -> LoraLoader %d 个" % (label, len(got)),
          "实得 %s" % got)

print()
print("=" * 86)
print("【3】模型链必须真的不一样（防「节点没了但链没断」）")
print("=" * 86)
g_no = server.build_graph({**BASE, "loras": []})
g_yes = server.build_graph({**BASE, "loras": [{"name": LORA, "strength": 0.8}]})
c_no, c_yes = model_chain(g_no), model_chain(g_yes)
print("   不带 LoRA: %s" % " -> ".join(c_no))
print("   带 LoRA  : %s" % " -> ".join(c_yes))
check("LoraLoader" not in c_no, "不带 LoRA 的链上没有 LoraLoader")
check("LoraLoader" in c_yes, "带 LoRA 的链上有 LoraLoader")
check(c_no != c_yes, "两条链不同")

g_stale = server.build_graph({**BASE, "loras": [], "lora": LORA, "lora_strength": 0.8})
check(model_chain(g_stale) == c_no,
      "★ 旧字段不再污染模型链",
      "实得 %s" % " -> ".join(model_chain(g_stale)))

print()
print("=" * 86)
print("【4】局部重绘路径与主生成口径一致")
print("=" * 86)
for extra, want, label in [
    ({"loras": []}, 0, "loras: []"),
    ({"loras": [], "lora": LORA, "lora_strength": 0.8}, 0, "★ 空数组 + 旧字段"),
    ({"loras": [{"name": LORA, "strength": 0.8}]}, 1, "正常挂一个"),
]:
    p = {**BASE, **extra, "prompt": "x", "main_prompt": "", "quality": []}
    try:
        g = server.build_inpaint_graph("a.png", "b.png", p, prefix="temp/t")
        got = loras_in(g)
        check(len(got) == want, "%-24s -> LoraLoader %d 个" % (label, len(got)),
              "实得 %s" % got)
    except Exception as e:
        check(False, label, "抛异常 %r" % (e,))

print()
print("=" * 86)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 86)
sys.exit(1 if fails else 0)
