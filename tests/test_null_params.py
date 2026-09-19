# -*- coding: utf-8 -*-
"""null 参数必须被当成"没传"，不能抛 TypeError、也不能悄悄进图。

为什么单独立一条：
    `dict.get(k, 默认值)` 挡不住 null —— 键存在、值是 None 时直接返回 None，
    默认值不生效。这个项目被同一类咬过两次（resolve_seed、checkpoint），
    都是事后单点修的。实测扫描发现建图函数上还有 16 处。

    真实触发路径不是"有人手搓 API"：**前端数字框被清空时 `parseFloat("")`
    是 NaN，而 `JSON.stringify({steps: NaN})` 会变成 `null`** ——
    把步数那一格删干净再点生成就能踩到。

    **纯函数测试，不出图、不连服务、不用 GPU。**
    直接调 build_graph / build_inpaint_graph 并检查图里有没有 None。

跑法：
    python tests/test_null_params.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server as S  # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def find_none(obj, path=""):
    """递归找图里的 None。比在 repr() 里搜 'None' 靠谱（那会误报字符串）。"""
    out = []
    if obj is None:
        out.append(path or "(root)")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out += find_none(v, "%s.%s" % (path, k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out += find_none(v, "%s[%d]" % (path, i))
    return out


print("=" * 74)
print("null 参数处理")
print("=" * 74)

# ---------------------------------------------------------------- 1 归一化本身
print()
print("【1】drop_null_params 本身")
cases = [
    ({"a": 1, "b": None}, {"a": 1}, "null 被删掉"),
    ({"steps": None}, {}, "steps=null 被删掉"),
    ({"steps": ""}, {}, "数值参数的空串也删掉（= 没填）"),
    ({"negative": ""}, {"negative": ""}, "文本字段的空串**保留**（有语义）"),
    ({"a": None, "b": "x"}, {"b": "x"}, "只删 null，别的键不动"),
    ({"sampler": None, "scheduler": None}, {}, "sampler/scheduler 的 null 删掉"),
    ({"count": 0}, {"count": 0}, "0 不能被当成「没传」（它是合法值）"),
    ({"count": False}, {"count": False}, "False 不能被当成「没传」"),
    ({"steps": "abc"}, {"steps": "abc"}, "乱字符串不在这里管（留给下游报错）"),
    ([], {}, "不是 dict 时返回空 dict（别崩）"),
    (None, {}, "None 时返回空 dict（别崩）"),
]
for src, want, why in cases:
    got = S.drop_null_params(src)
    check(got == want, "%s  →  %r" % (why, got))

# ---------------------------------------------------------------- 2 图里没有 None
print()
print("【2】建图：null 参数不能让 None 进图")
GEN = {"positive": "1girl, solo", "negative": "", "seed": 1}
GEN_KEYS = ["steps", "cfg", "width", "height", "count", "clip_skip",
            "denoise", "sampler", "scheduler", "checkpoint"]
bad = []
for k in GEN_KEYS:
    p = S.drop_null_params(dict(GEN, **{k: None}))
    try:
        g = S.build_graph(p)
        n = find_none(g)
        if n:
            bad.append((k, "图里有 None: %s" % ", ".join(n[:2])))
    except Exception as e:
        bad.append((k, "%s: %s" % (type(e).__name__, e)))
check(not bad, "文生图：%d 个参数全部正常（问题: %s）" % (len(GEN_KEYS), bad or "无"))

INP = {"positive": "1girl", "negative": "", "seed": 1}
INP_KEYS = ["steps", "cfg", "denoise", "mask_grow", "mask_feather",
            "clip_skip", "controlnet_strength", "sampler", "scheduler",
            "checkpoint"]
bad = []
for k in INP_KEYS:
    p = S.drop_null_params(dict(INP, **{k: None}))
    try:
        g = S.build_inpaint_graph("t.png", "t_mask.png", p, "temp/t")
        n = find_none(g)
        if n:
            bad.append((k, "图里有 None: %s" % ", ".join(n[:2])))
    except Exception as e:
        bad.append((k, "%s: %s" % (type(e).__name__, e)))
check(not bad, "局部重绘：%d 个参数全部正常（问题: %s）" % (len(INP_KEYS), bad or "无"))

# ---------------------------------------------------------------- 3 值回落到默认
print()
print("【3】null 之后必须用 DEFAULTS 里的值，不是随便一个数")
g = S.build_graph(S.drop_null_params(dict(GEN, steps=None, cfg=None,
                                          width=None, height=None)))
# 从图里把 KSampler 和 EmptyLatentImage 的参数捞出来比对
vals = {}
for nid, node in g.items():
    if not isinstance(node, dict):
        continue
    ins = node.get("inputs") or {}
    for k in ("steps", "cfg", "width", "height"):
        if k in ins:
            vals.setdefault(k, []).append(ins[k])
for k, dflt in (("steps", S.DEFAULTS["steps"]), ("cfg", S.DEFAULTS["cfg"]),
                ("width", S.DEFAULTS["width"]),
                ("height", S.DEFAULTS["height"])):
    got = vals.get(k, [])
    check(got and all(v == dflt for v in got),
          "%s = %s（图里实际 %s）" % (k, dflt, got))
# 采样器名字要回落到默认字符串，不能是 None
names = [v for v in vals.get("sampler_name", [])]
check(not names or all(n and n != "None" for n in names),
      "sampler_name 不是 None")

# ---------------------------------------------------------------- 4 别把合法值改掉
print()
print("【4】不能误伤合法值（改了这里会把用户设置吃掉）")
p = S.drop_null_params({"steps": 0, "cfg": 0.0, "count": 1, "negative": "",
                        "sampler": "euler", "checkpoint": "x.safetensors"})
check(p.get("steps") == 0, "steps=0 保留（0 是合法值，不是「没传」）")
check(p.get("cfg") == 0.0, "cfg=0.0 保留")
check(p.get("count") == 1, "count=1 保留")
check(p.get("negative") == "", "negative='' 保留")
check(p.get("sampler") == "euler", "sampler 保留")
check(p.get("checkpoint") == "x.safetensors", "checkpoint 保留")
check(S.drop_null_params({"x": False}) == {"x": False}, "False 保留（不是 null）")

# ---------------------------------------------------------------- 5 入口真的接了
print()
print("【5】入口确实接上了（不然上面测的全是空转）")
import inspect  # noqa: E402
src = inspect.getsource(S.Handler._read_body)
check("drop_null_params" in src,
      "_read_body() 里调了 drop_null_params —— 所有 POST 接口都覆盖到")

# ---------------------------------------------------------------- 6 坏字符串
print()
print("【6】「值不是数字」也要给出可照做的提示（不是 Python 味的 500）")
# 直接 int("abc") 抛的是 "invalid literal for int() with base 10: 'abc'"，
# 而且会被通用 except 兜成 HTTP 500 —— 用户按 500 去查服务日志，找不到原因。
for key, val, cast, why in (
        ("factor", "abc", float, "手搓 API 传了字符串"),
        ("factor", "2x", float, "复制粘贴带了尾巴"),
        ("width", "abc", int, "宽度传了字符串"),
        ("steps", "abc", int, "步数传了字符串"),
):
    body = {key: val}
    try:
        got = S.num_param(body, key, 999, cast)
        check(False, "%s=%r 应该抛 BadParam，实际返回 %r" % (key, val, got))
    except S.BadParam as e:
        msg = str(e)
        check(key in msg and repr(val) in msg and "应该" in msg,
              "%s=%r → BadParam（%s）: %s" % (key, val, why, msg))
    except Exception as e:
        check(False, "%s=%r 抛的是 %s（应该是 BadParam）: %s"
              % (key, val, type(e).__name__, e))

# 合法值不能被误伤
check(S.num_param({"factor": 4}, "factor", 2.0, float) == 4.0, "factor=4 正常")
check(S.num_param({"factor": "4"}, "factor", 2.0, float) == 4.0,
      "factor='4'（数字字符串）正常 —— 浏览器表单有时就送字符串")
check(S.num_param({}, "factor", 2.0, float) == 2.0, "没传时用默认值")
check(S.num_param({"factor": ""}, "factor", 2.0, float) == 2.0,
      "空串 = 没填，用默认值（int('') 会抛 ValueError）")
check(S.num_param({"factor": 0}, "factor", 2.0, float) == 0.0,
      "0 保留（是合法值）")

# 服务端要把 BadParam 变成 400，不是 500
src_api = inspect.getsource(S.Handler.do_POST)
check("BadParam" in src_api and "400" in src_api,
      "do_POST 里把 BadParam 转成了 400（不是 500）")

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m in FAILS:
        print("  FAIL %s" % m)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
