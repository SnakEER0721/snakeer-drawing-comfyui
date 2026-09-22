# -*- coding: utf-8 -*-
"""参数范围 / NaN / Infinity 必须在建图之前被拦住。

为什么单独立一条（上游报告 2026-09-20 的 P1-2 / P1-3 / P1-4，我都实测复现过）：

  ① 服务端原本**零范围校验**。直接调 build_graph（纯函数，零副作用）实测：
     10 个参数里 9 个原样透传 —— width=999999 就真写 999999、width=-100 就真写
     -100、count=99999 就真写 batch_size 99999、steps=0 就真写 0。

  ② 顺带更正报告一处说法：报告写「ComfyUI 侧宽高/批量不挡」，但打真实 HTTP 时
     width/height/batch_size/clip_skip **都被它拒了**。所以问题不是"会崩"，
     而是**这道防线不在我们手里**：报错是 ComfyUI 的文案、走 HTTP 500
     （"服务器挂了"）而不是 400（"你参数写错了"）。

  ③ `cfg=NaN` **静默出图成功（HTTP 200）**，没有任何提示。这条最危险 ——
     报错至少能被发现，一张"看起来正常但参数无意义"的产物会被当正常结果用下去。
     实测过 `{"cfg":NaN}` 真的返回 200 并写出一张 PNG。

  `NaN` / `Infinity` 是 Python `json.loads` **默认就收**的（不是标准 JSON），
  而 `float("nan")`/`float("inf")` 又不抛异常 —— 所以两处都得堵。

**纯函数测试：不出图、不连服务、不用 GPU、不碰用户产出目录。**


⚠️ 那条"因为错误理由而变绿"的教训（这个脚本第一版踩过，写进 HTTP 探针里）：
   探针最初用 `prompt_cn: "t"` 发请求，9 条越界请求全部返回 `400 提示词为空` ——
   状态码对了，**理由完全不对**：那些 400 来自更早的"提示词为空"那道门，
   范围校验压根没执行。只看状态码会报"全拦住"，实际漏了 9 条。
   所以下面凡是要验证"被拦住"的，都连**报错信息里有没有那个参数名**一起断言。

跑法：
    python tests/test_param_ranges.py
"""
import io
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import server as S  # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def try_build(**over):
    """建一次图，返回 None（放行）或 BadParam 的消息（拦住了）。"""
    p = dict(S.DEFAULTS)
    p.update({"prompt_cn": "test", "positive": "1girl, solo"})
    p.update(over)
    try:
        S.build_graph(p)
        return None
    except S.BadParam as e:
        return str(e)


def try_inpaint(**over):
    p = dict(S.DEFAULTS)
    p.update({"positive": "1girl, solo"})
    p.update(over)
    try:
        S.build_inpaint_graph("img.png", "mask.png", p, "stem")
        return None
    except S.BadParam as e:
        return str(e)


print("=" * 74)
print("【1】越界参数必须被拦住，而且报错里要出现**那个参数名**")
print("=" * 74)
# (参数, 值) —— 全是"界面送不出来"的值（界面 steps 是 10~50 的 range、
# count 只有 1/2/4、宽高是下拉框），所以拦它们不影响任何正常用法。
BAD = [
    ("width", 999999), ("width", -100), ("width", 0),
    ("height", 999999), ("height", -100),
    ("steps", 0), ("steps", -5), ("steps", 100000),
    ("count", 0), ("count", -1), ("count", 99999),
    ("clip_skip", 999), ("clip_skip", -1),
    ("cfg", 999), ("cfg", 0), ("cfg", -1),
    ("mask_grow", 99999), ("mask_feather", -1),
]
missed = []
for key, val in BAD:
    msg = try_build(**{key: val})
    # ★ 两条一起判：拦住了，**并且**报错里点了这个参数的名。
    #   少了后半条，任何一道 BadParam 都能冒充通过。
    ok = msg is not None and key in msg
    if not ok:
        missed.append("%s=%s" % (key, val))
    check(ok, "%-12s %-8s -> %s" % (key, val, (msg or "❌ 没拦住")[:48]))

check(not missed, "以上越界全部被拦住（漏了 %d 个：%s）"
      % (len(missed), missed or "无"))

print()
print("=" * 74)
print("【2】NaN / Infinity 必须被拦住（float('nan') 不抛异常，别指望 except）")
print("=" * 74)
for key, val, label in [
    ("cfg", float("nan"), "NaN"),
    ("cfg", float("inf"), "Infinity"),
    ("cfg", float("-inf"), "-Infinity"),
    ("width", float("nan"), "NaN"),
    ("width", float("inf"), "Infinity"),
    ("steps", float("nan"), "NaN"),
    ("height", float("inf"), "Infinity"),
    ("count", float("nan"), "NaN"),
    ("clip_skip", float("inf"), "Infinity"),
    ("seed", float("nan"), "NaN"),
]:
    msg = try_build(**{key: val})
    ok = msg is not None and key in msg
    check(ok, "%-12s %-10s -> %s" % (key, label, (msg or "❌ 没拦住")[:46]))

print()
print("=" * 74)
print("【3】界面上真会送的值一律必须放行（加校验最容易误伤这里）")
print("=" * 74)
# 来源：ui.html —— steps range 10~50、cfg range 1~14、clipSkip range 0~4、
# count 只有 1/2/4、SIZES = [768, 832, 864, 896, 1024, 1152, 1344, 1360]
GOOD = [
    ("steps", 10), ("steps", 30), ("steps", 50),
    ("cfg", 1), ("cfg", 5.5), ("cfg", 14),
    ("clip_skip", 0), ("clip_skip", 2), ("clip_skip", 4),
    ("count", 1), ("count", 2), ("count", 4),
    ("width", 768), ("width", 864), ("width", 1024), ("width", 1360),
    ("height", 768), ("height", 1024), ("height", 1360),
    ("seed", 0), ("seed", -1),
    ("denoise", 1.0), ("denoise", 0.55),
    ("mask_grow", 0), ("mask_grow", 12), ("mask_feather", 12),
]
broke = []
for key, val in GOOD:
    msg = try_build(**{key: val})
    if msg is not None:
        broke.append("%s=%s(%s)" % (key, val, msg[:26]))
    check(msg is None, "%-12s %-8s -> %s"
          % (key, val, "放行" if msg is None else "❌ 被误挡: " + msg[:34]))

check(not broke, "界面真实取值全部放行（误挡了 %d 个）" % len(broke))

print()
print("=" * 74)
print("【4】范围边界本身：正好等于上下限要放行，差一点点要拦住")
print("=" * 74)
for key, lo, hi, _unit in S.PARAM_RANGES:
    m_lo = try_build(**{key: lo})
    m_hi = try_build(**{key: hi})
    check(m_lo is None and m_hi is None,
          "%-12s 边界 [%s, %s] 两端都放行" % (key, lo, hi))
# 只对整数参数试"越界一步" —— cfg 是浮点，越界一步要按它的粒度走
for key, out in [("width", 63), ("width", 2049), ("steps", 201),
                 ("count", 9), ("clip_skip", 13)]:
    msg = try_build(**{key: out})
    check(msg is not None and key in msg,
          "%-12s %-6s 越过边界 -> %s" % (key, out, (msg or "❌ 没拦住")[:40]))

print()
print("=" * 74)
print("【5】局部重绘入口不许和文生图分叉（AGENTS.md「6 处」那条的另一半）")
print("=" * 74)
for key, val in [("steps", 0), ("steps", 100000), ("cfg", 999),
                 ("mask_grow", 99999), ("clip_skip", 999)]:
    msg = try_inpaint(**{key: val})
    check(msg is not None and key in msg,
          "重绘 %-12s %-8s -> %s" % (key, val, (msg or "❌ 没拦住")[:40]))
for key, val in [("steps", 30), ("cfg", 5.5), ("mask_grow", 12)]:
    msg = try_inpaint(**{key: val})
    check(msg is None, "重绘 %-12s %-8s 正常值放行" % (key, val))

print()
print("=" * 74)
print("【6】num_param 也要挡 NaN/Infinity（/api/upscale 走它，不经过建图函数）")
print("=" * 74)
# 这个函数原来只抓 TypeError/ValueError —— 而 float("inf")/float("nan")
# **不抛异常**，于是 inf 会被原样返回。
for val, label in [(float("inf"), "Infinity"), (float("-inf"), "-Infinity"),
                   (float("nan"), "NaN")]:
    try:
        got = S.num_param({"factor": val}, "factor", 2.0, float)
        check(False, "factor=%-10s -> ❌ 原样返回了 %r" % (label, got))
    except S.BadParam as e:
        check("factor" in str(e), "factor=%-10s -> 拦住：%s" % (label, str(e)[:44]))
# 正常值不能被误伤
for val, want in [("2.5", 2.5), (2, 2.0), (None, 2.0), ("", 2.0)]:
    try:
        got = S.num_param({"factor": val}, "factor", 2.0, float)
        check(got == want, "factor=%-6r -> %r（期望 %r）" % (val, got, want))
    except Exception as e:
        check(False, "factor=%-6r -> ❌ 抛了 %s: %s" % (val, type(e).__name__, e))

print()
print("=" * 74)
print("【7】裸 JSON 里的 NaN/Infinity 在**入口**就被拒（含 json.loads 的位置信息那条）")
print("=" * 74)


class _FakeHeaders:
    def __init__(self, n):
        self._n = n

    def get(self, k):
        return str(self._n) if k == "Content-Length" else None


def read_body(raw: bytes):
    """直接调 _read_body，不需要真起服务。

    _read_body 只用到 self.headers.get 和 self.rfile.read —— 用一个最小替身就够。
    比"起个服务再打 HTTP"轻得多，而且能进 fast 层（HTTP 那条在 slow 层）。
    """
    h = object.__new__(S.Handler)       # 不走 __init__，免得去连 socket
    h.headers = _FakeHeaders(len(raw))
    h.rfile = io.BytesIO(raw)
    return h._read_body()


if not hasattr(S, "Handler"):
    # 宿主类改名会让下面整节静默失去意义 —— 宁可显式失败，也不要"跳过即通过"
    check(False, "server.Handler 不存在，第【7】节无法执行（不是通过）")
else:
    # 裸字节发，json.dumps 发不出 NaN
    for raw, label in [
        (b'{"steps":NaN}', "steps=NaN"),
        (b'{"width":Infinity}', "width=Infinity"),
        (b'{"cfg":NaN}', "cfg=NaN"),
        (b'{"width":-Infinity}', "width=-Infinity"),
    ]:
        try:
            read_body(raw)
            check(False, "%-18s -> ❌ 放过去了" % label)
        except S.BadParam as e:
            hit = ("NaN" in str(e)) or ("Infinity" in str(e))
            check(hit, "%-18s -> 拦住：%s" % (label, str(e)[:42]))
        except Exception as e:
            check(False, "%-18s -> ❌ 抛的不是 BadParam 而是 %s: %s"
                  % (label, type(e).__name__, e))

    # 合法 JSON 要照常过
    try:
        d = read_body('{"prompt_cn":"一个女孩","steps":30}'.encode("utf-8"))
        check(d.get("steps") == 30 and d.get("prompt_cn") == "一个女孩",
              "合法 JSON 正常解析：%s" % d)
    except Exception as e:
        check(False, "合法 JSON 被拒了：%s: %s" % (type(e).__name__, e))

    # 坏 JSON -> BadParam（400），不是 500
    try:
        read_body(b'{"prompt_cn":')
        check(False, "坏 JSON -> ❌ 放过去了")
    except S.BadParam as e:
        check("JSON" in str(e), "坏 JSON -> BadParam：%s" % str(e)[:50])
    except Exception as e:
        check(False, "坏 JSON -> ❌ 抛的是 %s（该是 BadParam）" % type(e).__name__)

    # null 仍然按"没传"处理（不能因为新加的入口校验把这个老行为弄坏）
    try:
        d = read_body(b'{"steps":null,"cfg":""}')
        check("steps" not in d and "cfg" not in d,
              "null / 空串仍然被 drop_null_params 删掉：%s" % d)
    except Exception as e:
        check(False, "null 处理被破坏了：%s: %s" % (type(e).__name__, e))

print()
print("=" * 74)
print("【8】_err_code 要把 BadParam 报成 400、ComfyUI 连不上报成 503")
print("=" * 74)
check(S._err_code(S.BadParam("x")) == 400, "BadParam -> 400")
check(S._err_code(S.ComfyUnreachable("x")) == 503, "ComfyUnreachable -> 503")
check(S._err_code(ValueError("x")) == 500, "其他异常 -> 500")
check(S._err_text(S.BadParam("width 收到了 999")) == "width 收到了 999",
      "BadParam 的消息不加类型名前缀（是写给用户看的）")
check(S._err_text(ValueError("boom")).startswith("ValueError"),
      "真 bug 保留类型名（那是排查线索）")

print()
print("=" * 74)
if FAILS:
    print("RESULT: FAIL —— %d 项失败 / 共 %d 项" % (len(FAILS), len(FAILS) + len(OKS)))
    for f in FAILS:
        print("   FAIL: %s" % f)
    sys.exit(1)
print("RESULT: ALL PASS（%d 项）" % len(OKS))
sys.exit(0)
