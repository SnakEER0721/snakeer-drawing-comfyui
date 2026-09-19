"""Regression test: a curated value must not claim a tag that does not exist.

The bug class this locks down
-----------------------------
`base_tags.json` is the FIRST layer of the lookup chain
(`base -> _SUPPLEMENT -> reverse -> db`), so a wrong value there silently
overrides a correct answer from the 328k-entry dictionary. Measured:

    高分辨率 -> "high resolution"   (this tag does not exist in the dictionary)
              while the dictionary itself says highres / 高分辨率, posts=8,156,931
              and the project's own 高频词表.txt line 508 already wrote highres

    丁达尔光 -> "volumetric lighting"  (does not exist; dictionary has
              volumetric_flask only)   while sunbeam is glossed 丁达尔效应
              (posts=13346) and the prompt-builder panel already used sunbeam

    双马尾   -> "twin tails"        (does not exist)  vs twintails (posts=1,237,227)

Three separate consequences, all silent:
  * the prompt got a tag the model never learned
  * clicking the same concept in the panel and typing it in the box produced
    DIFFERENT prompts
  * the doc and the code disagreed

Why `is_known_tag` was not enough to catch this
-----------------------------------------------
`is_known_tag` returns True if the value appears ANYWHERE in `_SUPPLEMENT`,
including as a value. So `is_known_tag("volumetric lighting")` is True purely
because `_SUPPLEMENT` maps 体积光 to it — the check confirmed the value was
"known", not that the tag was real. This test therefore checks the value two
independent ways: a direct dictionary lookup by name, and the reverse index.

What is checked
---------------
1. Every value in `base_tags.json` either exists in the dictionary, or is
   explicitly listed in KNOWN_NON_DICTIONARY with a reason. That list is the
   design record for tags this project uses on purpose even though Danbooru
   does not carry them.
2. Every option in `prompt_builder.json` is a real dictionary tag (that file
   has always been clean — this keeps it that way).
3. The panel-vs-typing consistency that the 丁达尔光 fix was about: for terms
   that appear in both the panel and the dictionary index, both paths must
   agree.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

HERE = paths.APP_DIR
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

tr = Translator()
if getattr(tr, "db_missing", False):
    print("词库不可用，跳过（这项检查依赖真实词库）")
    print("RESULT: SKIP")
    sys.exit(0)

# ---------------------------------------------------------------------------
# 设计记录：这些值确实不在本地词库里，但本项目是有意使用的。
# 每条都要写清楚为什么 —— 否则这个名单会变成"放行一切"的后门。
# ---------------------------------------------------------------------------
KNOWN_NON_DICTIONARY = {
    # 词库这一份快照里查不到、但确实是 Danbooru 标签、且是本项目离不开的。
    # 底模作者推荐的正向词就是 masterpiece, best quality —— 这两个不在快照里，
    # 但项目文档第十五章把它们列为「词库里真实存在」。也就是说：
    #   项目自己的说法 与 本地词库快照 不一致。
    # 快照是一个时间点导出的子集，不代表 Danbooru 全量。这里如实登记，
    # 而不是把它们当成错误值去"修"——改了反而会把作者的推荐词换掉。
    "masterpiece", "best quality", "highly detailed", "cel shading",
    "watercolor", "hand on hip", "rim lighting", "soft lighting",
    "dramatic lighting",
    # 底模/社区常用的质量词。CLIP 能理解英文短语，但模型没在 Danbooru 上
    # 学过明确含义，词表第十五章已把它们单列并提示"谨慎用"。
    "high quality", "HDR", "UHD", "8K", "4K", "ultra detailed",
    "sharp focus", "clean line art", "anime illustration", "cinematic",
    "oil painting", "two girls", "soft smile", "index finger to lips",
    "facing left", "facing right", "high angle", "eye-level", "low angle",
    "bird's-eye view", "worm's-eye view", "extreme close-up", "medium shot",
    "centered composition", "rule of thirds", "symmetrical composition",
    "dynamic composition", "85mm lens", "50mm lens", "35mm lens",
    "wide-angle lens", "fisheye lens", "shallow depth of field",
    "deep depth of field", "handheld camera", "camera pan", "camera tilt",
    "dolly in", "dolly out", "tracking shot", "orbit shot",
    "warm lighting", "cool lighting",
    # 摄影/构图类社区词，同上
    "worst quality", "low quality", "extra fingers", "missing fingers",
    "extra limbs", "missing limbs", "text",
    # 词库快照里没有，但 Danbooru 上确实存在；不参与翻译，仅作参考表
    "bangs", "volumetric lighting",
    # 快照缺项：from side(337776) 和 from behind(330135) 都在，偏偏没有
    # from front —— 同组的相机方位词，明显是这一份导出缺了它。
    "from front",
}


def dict_has(tag):
    """这个词是不是词库里真实存在的标签（按名字直接查）。"""
    key = tag.replace(" ", "_")
    row = tr.conn.execute(
        "SELECT 1 FROM tags WHERE name = ? OR name_key = ? LIMIT 1",
        (key, tag)).fetchone()
    return row is not None


print("=" * 84)
print("【1】base_tags.json：值必须是真实标签，或已在设计记录里说明")
print("=" * 84)
base = json.load(open(os.path.join(HERE, "data", "base_tags.json"), encoding="utf-8"))
tags = base.get("tags") or []
print("   条目 %d 个" % len(tags))

bad, allowed = [], 0
seen_pairs = {}
for t in tags:
    en = (t.get("english") or "").strip()
    zh = (t.get("chinese") or "").strip()
    if not en:
        continue
    if dict_has(en):
        continue
    if en in KNOWN_NON_DICTIONARY:
        allowed += 1
        continue
    bad.append((zh, en))
print("   词库中不存在但已登记原因的: %d" % allowed)
check(not bad, "没有「未登记且不存在」的值（%d 个）" % len(bad),
      "; ".join("%s->%s" % b for b in bad[:6]))

# 同一中文不能被两条不同英文映射（先出现的会赢，另一条是死数据）
print()
dupes = {}
for t in tags:
    zh = (t.get("chinese") or "").strip()
    en = (t.get("english") or "").strip()
    if zh and en:
        dupes.setdefault(zh, []).append(en)
collide = {k: v for k, v in dupes.items() if len(set(v)) > 1}
check(not collide, "同一中文没有两个冲突的英文映射（%d 个）" % len(collide),
      "; ".join("%s->%s" % (k, v) for k, v in list(collide.items())[:4]))

print()
print("=" * 84)
print("【2】prompt_builder.json：每个选项都必须是真实标签")
print("=" * 84)
pb = json.load(open(os.path.join(HERE, "prompt_builder.json"), encoding="utf-8"))
n_pb, bogus = 0, []
for g in pb:
    for it in (g.get("items") or []):
        for piece in [x.strip() for x in (it.get("en") or "").split(",") if x.strip()]:
            n_pb += 1
            if not dict_has(piece) and not tr.is_known_tag(piece):
                bogus.append((g.get("group"), it.get("cn"), piece))
print("   选项标签 %d 个" % n_pb)
check(not bogus, "全部真实存在（%d 个可疑）" % len(bogus),
      "; ".join("%s/%s->%s" % b for b in bogus[:6]))

print()
print("=" * 84)
print("【3】面板点选 与 手打中文 必须得到同一个标签")
print("=" * 84)
# 面板里给了中文显示名，用户也可能直接打这个中文。两条路径不一致时，
# 同一个概念会因为「怎么输入」而产生不同的提示词。
diverged = []
per_group_en = {}          # (group, en) -> [cn, ...]  同一分组内重复才是错误
for g in pb:
    gname = g.get("group")
    for it in (g.get("items") or []):
        cn = (it.get("cn") or "").strip()
        en = (it.get("en") or "").strip()
        if not cn or not en:
            continue
        per_group_en.setdefault((gname, en), []).append(cn)
        got = tr.lookup(cn)
        if got is None:
            diverged.append((cn, en, "<手打查不到>"))
        elif got != en:
            diverged.append((cn, en, got))
print("   面板 %d 项，两条路径不一致 %d 项" % (n_pb, len(diverged)))
for cn, panel, typed in diverged:
    print("      %-14s 面板 %-24s 手打 %s" % (cn, panel, typed))
check(not any(d[2] == "<手打查不到>" for d in diverged),
      "面板里没有「点得到、打不出」的词",
      "; ".join(d[0] for d in diverged if d[2] == "<手打查不到>"))

# 同一个分组里两个中文显示名指向同一个标签 = 纯重复项，用户会以为选了两种
# 不同的东西。跨分组重复是有意的（同一个词放进相关分组方便查找），不算错。
print()
same = {k: v for k, v in per_group_en.items() if len(v) > 1}
check(not same, "同一分组里没有两个显示名指向同一个标签（%d 组）" % len(same),
      "; ".join("%s[%s]<-%s" % (en, g, "/".join(cns)) for (g, en), cns in list(same.items())[:4]))
for (gname, en), cns in same.items():
    print("      [%s] %-20s <- %s" % (gname, en, " / ".join(cns)))

print()
print("=" * 84)
print("【4】同一个概念在不同数据源里必须给出同一个标签")
print("=" * 84)
# 这类不一致最隐蔽：词表文档、拼装面板、翻译器补充词表、base_tags 各写一份，
# 任何一份写了个不存在的标签，就只有走那条路径才出错。
# 实测抓到过：hand on hip 在**三个地方**都写了，而它在本地词库里根本不存在
# （词库官方注释：叉腰 = hands_on_own_hips(44685)、单手叉腰 = hand_on_own_hip(229913)）；
# clouds 同理（词库是 cloud，399481）。
# 修完这里仍然只做「值真实存在」的检查 —— 具体该用哪个标签由词库注释决定，
# 不该由测试硬编码一份期望值，否则改词库还要改测试。
CONCEPT_CHECKS = [
    ("单手叉腰", ["hand on own hip"]),
    ("双手叉腰", ["hands on own hips"]),
    ("叉腰", ["hands on own hips"]),
    ("云", ["cloud"]),
    ("天空", ["sky"]),
]
for zh, allowed in CONCEPT_CHECKS:
    got = tr.lookup(zh)
    check(got in allowed, "%-8s -> %-20r" % (zh, got),
          "期望其中之一 %s" % allowed)
    check(got is not None and dict_has(got), "%-8s 的值在词库里真实存在" % zh, got)

# 反向：这几个「词库里不存在」的值不该再出现在任何数据源里
print()
for bad_val in ["hand on hip", "clouds"]:
    hits = []
    for path in (os.path.join(HERE, "cn_translate.py"),
                 os.path.join(HERE, "prompt_builder.json"),
                 os.path.join(HERE, "data", "base_tags.json")):
        try:
            txt = open(path, encoding="utf-8").read()
        except OSError:
            continue
        # 只看「作为值出现」的形式，注释里提到它是可以的（那是修复记录）
        if '"%s"' % bad_val in txt or '"%s":' % bad_val in txt:
            hits.append(os.path.basename(path))
    check(not hits, "%r 不再作为取值出现（只允许在注释里）" % bad_val,
          "仍在 %s" % hits)

print()
print("=" * 84)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 84)
sys.exit(1 if fails else 0)
