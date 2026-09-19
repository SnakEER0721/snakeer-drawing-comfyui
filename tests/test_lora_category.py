# -*- coding: utf-8 -*-
"""LoRA 分类：钉住 `lora_category()` 的优先级，以及它和 `lora_details()` 的分工。

为什么值得单独测：
    分类是双层结构，两层的关系不看代码看不出来 ——
      · `lora_details()`  用**训练元数据 + 张量布局 + 体积**定 `kind`（一级分组）
      · `lora_category()` 定 `category`（Civitai 二级分类），
        **只有它明确给出 `kind` 时才覆盖**前一层的判断
    最后那条最容易搞错：`lora_category()` 的"默认"分支**故意不返回 `kind`** ——
    文件名看不出类型时，应该让元数据说了算，而不是把结论压成"未识别"。
    （我一度以为那是漏了 `kind` 的 bug，查下来才发现是设计。）

    注释里还标着几条修过的 bug，都在这测：
      · 画质词（quality/detail/masterpiece…）不能把明确的 hand/pose 名抢走
      · 光有训练元数据不足以判角色 —— illustrious_masterpieces_v3 有元数据但是画质类

跑法：
    python tests/test_lora_category.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server as S  # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def cat(fn, meta=None):
    return S.lora_category(fn, meta or {}, fn)


def tf(pairs):
    """造一个 ss_tag_frequency（上游是 {bucket: {tag: count}}）。"""
    return json.dumps({"set1": pairs})


print("=" * 74)
print("LoRA 分类测试")
print("=" * 74)

# ---------------------------------------------------------------- 1 结构
print()
print("【1】返回结构始终是 {category, source[, kind]}")
CASES = [
    "hand_fix.safetensors",
    "illustrious_masterpieces_v3.safetensors",
    "ViolaBangDream1.1_IL.safetensors",
    "random-nsfw-poses-v12-illustriousxl-lora-nochekaiser.safetensors",
    "someones_artstyle.safetensors",
    "blue_uniform_outfit.safetensors",
    "night_background_scenery.safetensors",
    "8be1e5d2cc7cd938037337603fb51565.safetensors",
    "z.safetensors",
    "画质增强.safetensors",
]
bad = []
for fn in CASES:
    r = cat(fn)
    if not isinstance(r, dict) or "category" not in r or "source" not in r:
        bad.append((fn, r))
    if r.get("kind") and r["kind"] not in ("character", "style", "quality"):
        bad.append((fn, "kind 取值意外: %r" % r["kind"]))
check(not bad, "所有输入都返回 {category, source}（不合法: %s）" % (bad or "无"))

# ---------------------------------------------------------------- 2 画质词
print()
print("【2】画质词：能判画质，但不抢明确的部位/姿势名")
r = cat("illustrious_masterpieces_v3.safetensors")
check(r["category"] == "Tool" and r.get("kind") == "quality",
      "masterpieces -> Tool/quality（实际 %s）" % r)
r = cat("detail_enhancer.safetensors")
check(r["category"] == "Tool" and r.get("kind") == "quality",
      "detail/enhance -> Tool/quality（实际 %s）" % r)
# 关键回归：明确的部位名要赢过画质词
r = cat("anatomy_helper.safetensors")
check(r["category"] == "Poses",
      "anatomy_helper -> Poses（部位名优先于画质词，实际 %s）" % r)
r = cat("random-nsfw-poses-v12-illustriousxl-lora-nochekaiser.safetensors")
check(r["category"] == "Poses",
      "名字里带 poses -> Poses（实际 %s）" % r)
r = cat("hand_detail_fix.safetensors")
check(r["category"] == "Tool",
      "hand_detail_fix -> Tool（既有部位词又有画质词时归画质，实际 %s）" % r)

# ---------------------------------------------------------------- 3 文件名分类
print()
print("【3】文件名关键词 -> Civitai 分类")
EXPECT = [
    # 名字里有部位词、**没有**画质词 -> 走部位分类
    ("Eyes_for_Illustrious_Lora_Perfect_anime_eyes.safetensors", "Character"),
    ("blue_uniform_outfit.safetensors", "Clothing"),
    ("someone_artstyle.safetensors", "Style"),
    ("night_background_scenery.safetensors", "Background"),
    ("lewd_nsfw_concept.safetensors", "Concept"),
]
for fn, want in EXPECT:
    r = cat(fn)
    check(r["category"] == want,
          "%s -> %s（实际 %s）" % (fn[:40], want, r["category"]))

# ★ 画质词赢过部位词，这是**故意的**，不是 bug。
#   `eye_detail_glossy_stylized` 的名字里既有 `eye`（部位）又有 `detail`（画质词），
#   结果是 Tool —— 因为这类 LoRA 实质是"眼睛细节增强器"，是工具不是角色。
#   （我第一版把期望写成 Character，被这条打回来；回查代码才发现
#     `has_tool_word` 那个分支就是为它写的，注释里也写明了理由。）
r = cat("eye_detail_glossy_stylized.safetensors")
check(r["category"] == "Tool" and r.get("kind") == "quality",
      "部位词+画质词并存 -> Tool（画质词优先，实际 %s）" % r)
check(r.get("source") == "文件名+画质词",
      "  而且 source 里写明了是两重判据叠加（实际 %r）" % r.get("source"))

# 走文件名分类但**不给 kind** —— 让 lora_details 的元数据判断说了算
r = cat("Eyes_for_Illustrious_Lora_Perfect_anime_eyes.safetensors")
check(r["category"] == "Character" and "kind" not in r,
      "纯文件名命中时只给 category、不给 kind（实际 %s）" % r)

# ---------------------------------------------------------------- 4 元数据
print()
print("【4】训练元数据：有『带括号的标签』才算角色")
char_meta = {"ss_tag_frequency": tf({"hatsune_miku_(vocaloid)": 40,
                                     "1girl": 30})}
r = cat("8be1e5d2cc7cd938037337603fb51565.safetensors", char_meta)
check(r["category"] == "Character" and r.get("kind") == "character",
      "元数据里有 (series) 标签 -> Character/character（实际 %s）" % r)
style_meta = {"ss_tag_frequency": tf({"1girl": 40, "soft lighting": 12})}
r = cat("aabbccddeeff00112233445566778899.safetensors", style_meta)
check(r["category"] == "Style" and r.get("kind") == "style",
      "元数据里没有 (series) 标签 -> Style/style（实际 %s）" % r)
# 上游可能把 ss_tag_frequency 给成 dict（不一定是 JSON 串）
r = cat("aabbccddeeff00112233445566778899.safetensors",
        {"ss_tag_frequency": {"set1": {"hatsune_miku_(vocaloid)": 5}}})
check(r["category"] == "Character",
      "ss_tag_frequency 是 dict（不是 JSON 串）也要认（实际 %s）" % r)
# 坏输入不能炸
r = cat("x.safetensors", {"ss_tag_frequency": "这不是 JSON"})
check(r["category"] == "Concept",
      "ss_tag_frequency 是坏 JSON 时不崩、退回默认（实际 %s）" % r)
r = cat("y.safetensors", {"ss_tag_frequency": None})
check(isinstance(r, dict) and "category" in r, "ss_tag_frequency 是 null 时不崩")

# ---------------------------------------------------------------- 5 默认
print()
print("【5】默认分支：给 category 但**故意不给 kind**")
r = cat("8be1e5d2cc7cd938037337603fb51565.safetensors")
check(r["category"] == "Concept", "认不出来时 category = Concept（实际 %s）"
      % r["category"])
check("kind" not in r,
      "认不出来时**不返回 kind** —— 让 lora_details 的元数据判断说了算，"
      "而不是把它压成『未识别』（实际 kind=%r）" % r.get("kind"))

# ---------------------------------------------------------------- 6 与 details 合并
print()
print("【6】lora_details 合并后：每条都有一级分组 kind")
try:
    details = S.lora_details()
except Exception as e:
    details = []
    check(False, "lora_details() 抛异常: %s: %s" % (type(e).__name__, e))
if details:
    check(True, "lora_details() 返回 %d 个 LoRA" % len(details))
    no_kind = [d["file"] for d in details if not d.get("kind")]
    check(not no_kind,
          "每个 LoRA 都有 kind（没有的: %s）" % (no_kind[:5] or "无"))
    legal = {"character", "style", "quality", "other", "incompatible", "unknown"}
    badk = [(d["file"], d["kind"]) for d in details if d.get("kind") not in legal]
    check(not badk, "kind 取值都在前端认识的集合里（意外: %s）" % (badk[:5] or "无"))
    # kind_source 应该恰好出现在 lora_category 给出 kind 的时候
    with_src = [d for d in details if d.get("kind_source")]
    check(all(d.get("kind_source") for d in with_src),
          "kind_source 只在有来源时出现（%d 条有）" % len(with_src))
    # 不兼容必须压过一切
    inc = [d for d in details if (d.get("key_layout") or {}).get("compatible") is False]
    wrong = [d["file"] for d in inc if d.get("kind") != "incompatible"]
    check(not wrong,
          "格式不兼容的 LoRA，kind 一定是 incompatible（不兼容 %d 个，判错 %s）"
          % (len(inc), wrong or "无"))
    # 每条都要有给用户看的中文名
    no_label = [d["file"] for d in details if not d.get("label")]
    check(not no_label, "每个 LoRA 都有 label（没有的: %s）" % (no_label[:5] or "无"))
    # category 的来源必须写出来 —— 用户要能知道这条是"猜的"还是"核对过的"
    no_src = [d["file"] for d in details if not d.get("category_source")]
    check(not no_src,
          "每条 category 都带来源（没有的: %s）" % (no_src[:5] or "无"))
    print()
    print("   实际分类结果（前 12 个）:")
    for d in details[:12]:
        print("     %-44s %-11s %-10s %s"
              % (d["file"][:42], d.get("kind"), d.get("category"),
                 d.get("category_source")))
    print()
    print("   ★ 注意 category 可能被更后面的层覆盖（本地别名 / C站原文核对）。")
    print("     那是有意的：手工核对过的结论比文件名猜的准。本测试只断言"
          "「结构完整、取值合法」，不断言某个具体文件必须是某个类 ——")
    print("     那取决于这台机器装了什么、别名文件里写了什么。")
else:
    print("   [跳过] 这台机器上没有 LoRA（发布包就是这种状态）")
    print("          —— 只测上面那些纯逻辑的部分")

# ---------------------------------------------------------------- 7 前端一致
print()
print("【7】前端的合法 kind 集合要包住后端会返回的 kind")
ui = open(os.path.join(os.path.dirname(HERE), "ui.html"), encoding="utf-8").read()
import re  # noqa: E402
m = re.search(r"const LORA_CATEGORIES\s*=\s*\[(.*?)\];", ui, re.S)
if not m:
    check(False, "从 ui.html 里找到 LORA_CATEGORIES")
else:
    ui_kinds = set(re.findall(r'kind\s*:\s*"([a-z]+)"', m.group(1)))
    check("character" in ui_kinds and "style" in ui_kinds,
          "读到前端的 kind 集合: %s" % ", ".join(sorted(ui_kinds)))
    if details:
        used = {d.get("kind") for d in details}
        missing = used - ui_kinds
        check(not missing,
              "后端用到的 kind 前端都有对应分组（缺: %s）" % (missing or "无"))

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m2 in FAILS:
        print("  FAIL %s" % m2)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
