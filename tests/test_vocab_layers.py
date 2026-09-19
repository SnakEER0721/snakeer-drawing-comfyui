"""词库分层一致性审计：不抽查，全部枚举。

为什么需要这个测试
------------------
翻译器的查找链是 base_tags.json -> _SUPPLEMENT -> 倒排索引 -> 词库。
四层都可以给同一个中文定义英文值，**高优先级的那层会静默覆盖低优先级**。

实际踩过的坑（都是这个结构造成的）：
  * 在 _SUPPLEMENT 里把「两个女孩」修成 2girls，但 base_tags.json 的
    别名里也写着「两个女孩」，它优先级更高 —— 修正被静默覆盖，
    界面给出不存在的 two girls。
  * 在 _SUPPLEMENT 里把「平涂」修成 flat color，但 base_tags 里一条
    「赛璐璐上色 -> cel shading」的别名也叫「平涂」。
  * 「浅笑」被 base 的别名指向不存在的 soft smile。

这些都是「改 A 层、B 层仍然生效」的静默失败，靠抽查发现不了。
所以这个测试做**全枚举**：

    1. base_tags.json 的每个值，必须在词库里真实存在（或已登记为例外）
    2. 每一层的每个键，如果它指向的值在词库里不存在、而另一层有存在的值
       —— 这是「高优先级层给出了坏值」，判失败（这正是上面三个坑）
    3. 词表文档里每一条「中文 → 值」，中文必须真的翻出该值

第 2 条是核心：它把「静默覆盖」变成可见的失败。
"""
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(cond, label, detail=""):
    print("   %s %s%s" % ("OK  " if cond else "FAIL", label,
                          ("  " + str(detail)) if not cond else ""))
    if not cond:
        fails.append(label)


try:
    import cn_translate as CT
    from cn_translate import Translator
except Exception as e:
    print("导入词库模块失败（%s），跳过" % type(e).__name__)
    print("RESULT: SKIP")
    sys.exit(0)

tr = Translator()
if getattr(tr, "db_missing", False):
    print("词库不可用，跳过")
    print("RESULT: SKIP")
    sys.exit(0)


def db_has(value):
    """该值是不是词库里真实存在的标签（按名字直接查）。"""
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    return tr.conn.execute(
        "SELECT 1 FROM tags WHERE name = ? OR name_key = ? LIMIT 1",
        (v.replace(" ", "_"), v)).fetchone() is not None


# ---------------------------------------------------------------------------
# 设计记录：确实不在本地词库、但本项目有意使用的值。
# 每条都要有理由 —— 否则这个名单会变成「放行一切」的后门。
# 详见 tests/test_tag_values_real.py 的 KNOWN_NON_DICTIONARY。
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(HERE, "tests"))
KNOWN_NON_DICTIONARY = set()
try:
    src = open(os.path.join(HERE, "tests", "test_tag_values_real.py"),
               encoding="utf-8").read()
    blk = src.split("KNOWN_NON_DICTIONARY = {")[1].split("\n}")[0]
    KNOWN_NON_DICTIONARY = set(re.findall(r'"([^"]+)"', blk))
except Exception as e:
    print("（读不到已知例外清单，将按严格模式检查：%s）" % type(e).__name__)

print("=" * 86)
print("【1】base_tags.json：值必须在词库里真实存在")
print("=" * 86)
base = json.load(open(os.path.join(HERE, "data", "base_tags.json"), encoding="utf-8"))
tags = base.get("tags") or []

# 建立各层索引
supp = CT._SUPPLEMENT
reverse = tr.reverse
base_map = {}                    # 中文键 -> 值（含别名）
for t in tags:
    en = (t.get("english") or "").strip()
    if not en:
        continue
    for k in [t.get("chinese")] + list(t.get("aliases") or []):
        if k:
            base_map.setdefault(str(k).strip(), en)

bad_base, known_exc = [], 0
for t in tags:
    en = (t.get("english") or "").strip()
    if not en:
        continue
    if db_has(en):
        continue
    if en in KNOWN_NON_DICTIONARY:
        known_exc += 1
        continue
    bad_base.append((t.get("chinese"), en))
print("   条目 %d 个，值不在词库且未登记的: %d" % (len(tags), len(bad_base)))
check(not bad_base, "没有「值不存在且未登记」的条目",
      "; ".join("%s->%s" % b for b in bad_base[:6]))
print("   （已在设计记录里登记为例外的: %d 个）" % known_exc)

print()
print("=" * 86)
print("【2】★ 静默覆盖：高优先级层给出坏值，而低优先级层有好的")
print("=" * 86)
# 对每个出现在多层里的键，找出各层给的值。
# 若最高层（base > _SUPPLEMENT > reverse）的值不存在，而更低层存在 —— 这是覆盖错误。
shadowed = []
all_keys = set(base_map) | set(supp) | set(reverse)
for k in sorted(all_keys):
    layers = []
    if k in base_map:
        layers.append(("base_tags", base_map[k]))
    if k in supp:
        layers.append(("_SUPPLEMENT", supp[k]))
    if k in reverse:
        layers.append(("倒排索引", reverse[k]))
    if len(layers) < 2:
        continue
    top_name, top_val = layers[0]              # 列表顺序即优先级
    if db_has(top_val):
        continue                               # 最高层的值有效，没有覆盖问题
    # 最高层值无效：看有没有更低层给了有效值
    better = [(n, v) for n, v in layers[1:] if db_has(v)]
    if better:
        shadowed.append((k, top_name, top_val, better[0]))

print("   被静默覆盖的键: %d 个" % len(shadowed))
for k, top_name, top_val, (low_name, low_val) in shadowed[:25]:
    print("      %-14s %s 给 %r（词库无） 但 %s 有 %r"
          % (k, top_name, top_val, low_name, low_val))
check(not shadowed,
      "没有键被高优先级层的坏值覆盖（%d 个）" % len(shadowed),
      "; ".join("%s(%s)" % (k, t) for k, t, _, _ in shadowed[:5]))

print()
print("=" * 86)
print("【3】词表文档：每条「中文 → 值」都要真的翻译成该值")
print("=" * 86)
DOC = os.path.join(HERE, "高频词表.txt")
CJK = re.compile(r"[\u4e00-\u9fff]")
SKIP_SECTION = ("负向提示词", "权重语法", "参数速查", "LoRA 使用要点",
                "手动放大", "颜文字", "无法直接翻译", "画师串")
# 经用户明确决定「暂不改动」的词条。它们词表写一个值、翻译器给另一个值，
# 两边都是合法标签（或者都是社区俗称），属已知差异，登记在此不再报。
# 加进来必须写原因，否则这个名单会变成「放行一切」的后门。
KNOWN_DOC_DIFF = {
    "轮廓光": "用户决定不动（词表写 rim light，翻译器给 rim lighting，两者曾判为可疑项）",
    "面对面": "用户决定不动（词表写 face to face，翻译器给 face-to-face）",
    "咬唇":   "用户决定不动（词表写 biting lip，翻译器给 biting own lip）",
}
lines = open(DOC, encoding="utf-8").read().splitlines()
in_notes, mismatch, n_rows, exempted = False, [], 0, []
for ln in lines:
    if "═══" in ln:
        in_notes = any(k in ln for k in SKIP_SECTION)
        continue
    if in_notes or "→" not in ln:
        continue
    left, _, right = ln.partition("→")
    left, right = left.strip(), right.strip()
    if not left or not right or "：" in left or ":" in left:
        continue
    if not CJK.search(left) or CJK.search(right):
        continue
    if right.startswith("◆") or re.fullmatch(r"[\d\.\s%／/×x,，\-–—]+", right):
        continue
    for piece in (x.strip() for x in left.split("/")):
        if not piece or not CJK.search(piece) or len(piece) > 22:
            continue
        n_rows += 1
        got = tr.lookup(piece)
        # 词表可以写多个候选，写成 "a / b"
        opts = [x.strip() for x in right.split("/") if x.strip()]
        if got not in opts:
            if piece in KNOWN_DOC_DIFF:
                exempted.append(piece)
                continue
            mismatch.append((piece, right, got))
print("   检查词条 %d 行，不匹配 %d 行，已登记豁免 %d 行"
      % (n_rows, len(mismatch), len(exempted)))
if exempted:
    print("   豁免: %s" % "、".join(sorted(set(exempted))))
for piece, want, got in mismatch[:25]:
    print("      %-16s 写的是 %-26s 实际 %r" % (piece, want, got))
check(not mismatch, "词表文档与翻译器一致（%d 处不一致）" % len(mismatch),
      "; ".join("%s" % m[0] for m in mismatch[:5]))

print()
print("=" * 86)
print("【4】_SUPPLEMENT 内部：重复键、空值，以及「永远匹配不上」的死键")
print("=" * 86)
src = open(os.path.join(HERE, "cn_translate.py"), encoding="utf-8").read()
tree = ast.parse(src)
dup_keys, empty_vals = [], []
for node in ast.walk(tree):
    if not isinstance(node, ast.Dict):
        continue
    seen = set()
    for k, v in zip(node.keys, node.values):
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            continue
        if k.value in seen:
            dup_keys.append((k.value, k.lineno))
        seen.add(k.value)
        if isinstance(v, ast.Constant) and isinstance(v.value, str) and not v.value.strip():
            empty_vals.append(k.value)
check(not dup_keys, "没有重复键（%d 个）" % len(dup_keys),
      "; ".join("%s@L%d" % d for d in dup_keys[:6]))
check(not empty_vals, "没有空值（%d 个）" % len(empty_vals),
      "; ".join(empty_vals[:6]))

# ★ 死键：键本身含 " / " 或空格。_segment 先按分隔符切、再逐段查，
# 所以这种长键永远匹配不上 —— 映射看着存在，实际是死的。
# 实测踩过：8 个形如「新艺术 / 穆夏风格」的键，打字进去得到 'wind' 这种垃圾。
dead = []
for cn in supp:
    if " / " in cn or (" " in cn and re.search(r"[\u4e00-\u9fff]", cn)):
        got = tr.translate(cn)[0]
        if got != supp[cn]:
            dead.append((cn, supp[cn], got))
check(not dead,
      "没有「永远匹配不上」的长键（%d 个）" % len(dead),
      "; ".join("%s->%r" % (c, g) for c, _, g in dead[:5]))
if dead:
    for cn, want, got in dead[:10]:
        print("      %-24s 期望 %-22s 实跑 %r" % (cn, want, got))

# 全量实跑一遍：每条映射都要真的从 translate() 拿到指定值
mismatch_all = []
for cn, want in supp.items():
    got = tr.translate(cn)[0] if re.search(r"[\u4e00-\u9fff]", cn) else tr.lookup(cn)
    if got != want:
        mismatch_all.append((cn, want, got))
check(not mismatch_all,
      "_SUPPLEMENT 全量实跑一致（%d 条中 %d 条不符）" % (len(supp), len(mismatch_all)),
      "; ".join("%s->%r" % (c, g) for c, _, g in mismatch_all[:5]))
print("   _SUPPLEMENT 条目: %d，全部实跑" % len(supp))

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
