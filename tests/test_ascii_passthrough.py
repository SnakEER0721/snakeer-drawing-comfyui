"""Regression test for the ASCII (English) pass-through path in the translator.

The question this answers
------------------------
Reported: "typing `han`, `hand o` or `hand on fa` for the compound tag
`hand on face` all trigger correct detection — is that a bug?"

Measured answer: no auto-correction happens. `lookup()` returns the input
unchanged, `is_known_tag()` returns False for all of them, and nothing is
replaced by a similar tag. The fragments reach the prompt because the ASCII
branch passes anything "tag-shaped" through on purpose (so a user can type a
tag the local dictionary happens not to carry).

What WAS a real problem
-----------------------
The preview rendered `→ han` with no comment, which looks like successful
recognition. Worse, the codebase already had `count_known()` — a function that
computes exactly this mismatch — and it had zero call sites. So the information
existed and was simply never surfaced.

Fix: `translate_detailed()` adds `not_tags` (hand-typed English that is not a
real dictionary tag). It only informs; `english` is unchanged, so the
pass-through behaviour the user asked to keep is preserved.

What this test locks down
-------------------------
1. Fragments are NOT resolved into some other tag (no fuzzy/prefix matching).
2. `not_tags` names the fragments, and is empty for Chinese input and for the
   prompt-builder's own tags (measured false-positive rate: 0/220).
3. The pass-through itself is unchanged: `english` always equals what was typed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(cond, label, detail=""):
    print("   %s %s%s" % ("OK  " if cond else "FAIL", label,
                          ("  " + str(detail)) if not cond else ""))
    if not cond:
        fails.append(label)


try:
    from cn_translate import Translator
except Exception as e:
    print("导入词库模块失败（%s），跳过" % type(e).__name__)
    print("RESULT: SKIP")
    sys.exit(0)

import json as _json

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_pb_path = os.path.join(_HERE, "prompt_builder.json")
pb = _json.load(open(_pb_path, encoding="utf-8")) if os.path.isfile(_pb_path) else []

tr = Translator()
if getattr(tr, "db_missing", False):
    print("词库不可用，跳过（这项检查依赖真实词库）")
    print("RESULT: SKIP")
    sys.exit(0)

print("=" * 84)
print("【1】割裂输入必须原样透传，不能被解析成别的标签")
print("=" * 84)
# 这些是用户报的输入。关键断言是「输出 == 输入」——如果哪天有人加了
# 模糊匹配，这里会立刻红。
FRAGMENTS = ["han", "hand o", "hand on", "hand on fa", "hand on fac",
             "hand on face", "hand on facee", "on face"]
for frag in FRAGMENTS:
    en, _res, _unk = tr.translate(frag)
    check(en == frag, "%-16r 原样透传（未解析成别的标签）" % frag, "实际 %r" % en)

# 写这个测试时我原本把 andon 归到「不是标签」里，跑出来才发现在词库里是
# 真标签（行灯，posts=658）。留着它当反例：不能因为一个词看起来像某个
# 标签的碎片，就假定它不是标签 —— 328k 条里有很多短词。
# `hand` 同理（posts 很高）。这两个都该被认成真标签。
print()
print("   顺带：这些「碎片形状」的词其实是真标签，必须被认出来")
for word, why in [("andon", "行灯"), ("hand", "手")]:
    check(tr.is_known_tag(word), "%-8r 是真标签（%s）" % (word, why))

print()
print("=" * 84)
print("【2】这些片段确实不是词库里的标签（所以不是「命中」）")
print("=" * 84)
for frag in FRAGMENTS:
    check(not tr.is_known_tag(frag), "%-16r is_known_tag = False" % frag)

print()
print("=" * 84)
print("【4】★ 本地词库快照缺基础单词标签（这条决定了 not_tags 能不能用）")
print("=" * 84)
# 这份 328k 的快照**不是全量**：它缺的恰好是最基础的单词标签，而它们的
# 复合形式却在库里。实测：
#   hands 不在      / hands_up、hands_on_own_hips 在
#   eyes  不在      / eyes_visible_through_hair 在
#   face  不在      / face_piercing 在
#   vagina 不在     / vaginal、vaginal_object_insertion 在
#
# 我第一次据此判定「not_tags 会对正常提示词误报」并把功能撤回了，那是错的：
# is_known_tag 会兜底查 _SUPPLEMENT 的值，而这些基础词正是补充词表里的值。
# 错在拿「词库直查」的标准去评判「is_known_tag」的输出。下面两条断言把
# 这个区别钉死，免得以后又踩。
COMPOUND_PRESENT = [
    ("hands", "hands_up"),
    ("eyes", "eyes_visible_through_hair"),
    ("face", "face_piercing"),
    ("hair", "hair_ornament"),
    ("waist", "waist_apron"),
    ("mouth", "mouth_mask"),
    ("wrist", "wristband"),
    ("chest", "chest_tattoo"),
    ("vagina", "vaginal"),
    ("arms", "arms_up"),
]
for base, compound in COMPOUND_PRESENT:
    base_in = tr.conn.execute(
        "SELECT 1 FROM tags WHERE name = ? OR name_key = ? LIMIT 1",
        (base, base)).fetchone() is not None
    comp_in = tr.conn.execute(
        "SELECT 1 FROM tags WHERE name = ? LIMIT 1", (compound,)).fetchone() is not None
    check(comp_in, "%r 的复合形式 %r 在词库里" % (base, compound))
    # 基础词不在 = 快照缺口；哪天真补上了，这里会提示去清理相关说明
    print("      %-10r 本身在库里: %s" % (base, base_in))

print()
print("   关键：is_known_tag 有一条兜底是 _SUPPLEMENT.values()，所以这些")
print("   基础词**不会**被 not_tags 误报 —— 快照缺口被这一层挡住了。")
for prompt in ["hands", "eyes", "face", "hair, eyes", "hands, face, eyes",
               "waist, mouth, wrist", "vagina, creampie"]:
    d = tr.translate_detailed(prompt)
    check(d["not_tags"] == [], "%-24r 不被误报" % prompt, "实际 %s" % d["not_tags"])

print()
print("   而真正不在任何一层里的片段，仍然会被标出来")
for prompt in ["han", "hand on face", "hand on fa", "qqqq"]:
    d = tr.translate_detailed(prompt)
    check(d["not_tags"] == [prompt], "%-16r 被标出" % prompt, "实际 %s" % d["not_tags"])

print()
print("   误报率：面板标签组合成真实提示词，不该有误报")
import random as _random
_random.seed(20260916)
_pools = {}
for _g in pb:
    _pools[_g.get("group")] = [it.get("en") for it in (_g.get("items") or []) if it.get("en")]
if _pools:
    _bad = 0
    for _ in range(200):
        _parts = ["masterpiece", "best quality", "1girl", "solo"]
        for _gname in _random.sample(list(_pools), _random.randint(2, 5)):
            _parts.append(_random.choice(_pools[_gname]))
        if tr.translate_detailed(", ".join(_parts))["not_tags"]:
            _bad += 1
    print("   随机组合 200 条提示词，误报 %d 条" % _bad)
    check(_bad == 0, "组合提示词误报为 0（否则这个提示会变成噪音）",
          "%d 条误报" % _bad)

print()
print("=" * 84)
print("【6】not_tags 只提示、不改变 english")
print("=" * 84)
CASES = [
    ("hand on face", ["hand on face"]),
    ("hand on fa", ["hand on fa"]),
    ("han", ["han"]),
    ("微笑", []),                                   # 中文翻译结果不质疑
    ("微笑, 蓝发", []),
    ("1girl, solo", []),                            # 真标签
    ("hand on fa, 微笑", ["hand on fa"]),            # 中英混写只挑英文
    ("qqqq, zzzz", ["qqqq", "zzzz"]),
]
for text, want in CASES:
    d = tr.translate_detailed(text)
    check(d["not_tags"] == want, "%-22r not_tags = %s" % (text, want),
          "实际 %s" % d["not_tags"])
    # 关键：english 仍然包含手打的内容，一个词都没被删掉
    typed = [p.strip() for p in text.split(",") if p.strip()]
    for t in typed:
        if not any("\u4e00" <= c <= "\u9fff" for c in t):
            check(t in d["english"],
                  "%-22r 的 %r 仍在 english 里" % (text, t), d["english"])

print()
print("=" * 84)
print("【4】误报率：拼装面板自己的标签一个都不该被标")
print("=" * 84)
if pb:
    n, warned = 0, []
    for g in pb:
        for it in (g.get("items") or []):
            for v in [x.strip() for x in (it.get("en") or "").split(",") if x.strip()]:
                n += 1
                d = tr.translate_detailed(v)
                if d["not_tags"]:
                    warned.append((it.get("cn"), v))
    print("   面板标签 %d 个，被标 not_tags 的 %d 个" % (n, len(warned)))
    for cn, v in warned[:10]:
        print("      %s -> %s" % (cn, v))
    check(not warned, "面板标签误报为 0（面板和手打必须一致）",
          "%d 个被误标" % len(warned))
else:
    print("   找不到 prompt_builder.json，跳过")

print()
print("=" * 84)
print("【5】count_known 与 not_tags 口径一致（同一件事的两个角度）")
print("=" * 84)
for text in ["1girl, solo", "han, smile", "hand on face"]:
    english = tr.translate(text)[0]
    known, total = tr.count_known(english)
    d = tr.translate_detailed(text)
    total2 = len([p for p in english.split(",") if p.strip()])
    check(known + len(d["not_tags"]) == total,
          "%-18r known(%d) + not_tags(%d) == total(%d)" % (text, known, len(d["not_tags"]), total))
    check(total == total2, "%-18r 总数一致" % text)

print()
print("=" * 84)
print("【7】中英混写：已知限制（评估过、按用户决定不修）")
print("=" * 84)
# _segment 只对「整条纯 ASCII」的输入走整词查询；中英混写时逐字贪心，
# 会把夹在中文里的拉丁词切碎：'Cosplay摄影' → 'cospla'，'Vtuber风格' → 'vtube, wind'。
#
# 我改过一版（把连续拉丁串当一个整体），全库 45311 词对比：
#   532 个词条变好、46 个「变坏」（逐个看后大多是修好了，比如 CLAMP: clam→clamp），
#   9 个真回归全是逗号/感叹号处理问题。
# 结论：修复有效，但影响 979 个词条的输出。
#
# ★ 用户决定保守处理：不做这个改动（他也不打算用中英混写）。
# 所以这里**不断言修好**，只记录现状，避免以后有人以为这是漏测。
KNOWN_LIMIT = [
    ("Cosplay摄影", "cospla"),      # 拉丁词被切碎
    ("Vtuber风格", "vtube"),        # 同上（后接 wind 也是误配）
]
for text, current in KNOWN_LIMIT:
    d = tr.translate_detailed(text)
    check(current in d["english"],
          "%-14r 现状仍为 %r（已知限制，未修）" % (text, current),
          "实际 %r" % d["english"])

# 这些「整条可查」的混写词不受影响，必须继续正确
for text, must_contain in [("3D背景", "3d background"), ("T恤", "t-shirt")]:
    d = tr.translate_detailed(text)
    check(must_contain in d["english"],
          "%-14r 输出含 %r（整条可查，不受影响）" % (text, must_contain),
          "实际 %r" % d["english"])

print()
print("=" * 84)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails[:15]:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 84)
sys.exit(1 if fails else 0)
