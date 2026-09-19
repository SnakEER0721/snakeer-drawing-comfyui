"""Make the vocabulary test compare VALUES, not just check translatability.

The test only asserted that each Chinese term resolves to something:
    en, _, unknown = tr.translate(cn)
    if not en or unknown: bad.append(...)
It never compared against the value the document actually claims. So a row like
    "手放脸旁 / 比心 → hand on face"
passed even though "hand on face" is not a Danbooru tag and the two actions
differ - the Chinese resolved fine, which was all that was checked.

That is the fourth time in this project that a check verified the wrong thing
(can-load vs has-content, hash-found vs file-present, translatable vs correct).
This adds the missing half.

Also filters out lines that are not vocabulary rows (troubleshooting advice such
as "图糊、细节少 → steps 调到 32"), which would otherwise be reported as
untranslatable prose.
"""
import os
import io
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from cn_translate import Translator

DOC = paths.VOCAB
UI = paths.UI_HTML
tr = Translator()
CJK = re.compile(r"[\u4e00-\u9fff]")
fails = []

# 说明性章节里的箭头不是词条
# 「颜文字/符号」和「无法直接翻译」两节是符号与专名，不是中文词条，
# 不能被当成「中文 → 英文」的词汇行解析。
# 「画师串」同理：左边是画师人名，右边是画师标签本身，不是翻译映射。
SKIP_SECTION = ("负向提示词", "权重语法", "参数速查", "LoRA 使用要点",
                "手动放大", "颜文字", "无法直接翻译", "画师串")


def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", str(s).lower()).split()


lines = io.open(DOC, encoding="utf-8").read().splitlines()
rows = []
in_notes = False
for ln in lines:
    if "═══" in ln:
        in_notes = any(k in ln for k in SKIP_SECTION)
        continue
    if in_notes or "→" not in ln:
        continue
    left, _, right = ln.partition("→")
    left, right = left.strip(), right.strip()
    # 排除：含冒号的说明行、过长的散文、右侧含中文（故障排查建议）
    if "：" in left or ":" in left:
        continue
    if len(left) > 22 or len(right) > 46:
        continue
    if not CJK.search(left) or CJK.search(right):
        continue
    if re.fullmatch(r"[\d\.\s%／/×x,，\-–—]+", right):
        continue
    if left.startswith(("【", "═", "★")):
        continue
    for piece in (x.strip() for x in left.split("/")):
        if piece and CJK.search(piece) and len(piece) <= 18:
            rows.append((piece, right))

seen, uniq = set(), []
for a, b in rows:
    if a not in seen:
        seen.add(a)
        uniq.append((a, b))

print("=" * 78)
print("【1】每个中文词都能翻译")
print("=" * 78)
bad_trans = []
for cn, stated in uniq:
    en, _, unknown = tr.translate(cn)
    if not en or unknown:
        bad_trans.append((cn, stated, unknown))
print("   词条 %d 个，翻译失败 %d 个" % (len(uniq), len(bad_trans)))
for cn, st, unk in bad_trans:
    print("      ❌ %-14s 写的是 %-24s 未收录=%s" % (cn, st[:24], unk))
if bad_trans:
    fails.append("%d 个词查不到" % len(bad_trans))

print()
print("=" * 78)
print("【2】词表写的值 与 程序实际翻译 是否一致")
print("=" * 78)
mismatch = []
for cn, stated in uniq:
    got = tr.lookup(cn)
    if not got:
        continue
    # 词表可能写多个候选，写成 "a / b"；只要命中了其中任何一个就算一致
    options = [x.strip() for x in stated.split("/") if x.strip()]
    g = norm(got)
    if not g:
        continue
    ok = False
    for o in options:
        n = norm(o)
        if n and n[0] == g[0]:
            ok = True
            break
    if not ok:
        mismatch.append((cn, stated, got))

print("   不一致 %d 条" % len(mismatch))
for cn, stated, got in mismatch:
    print("      %-14s 词表写 %-26s 程序给 %-26s" % (cn, stated[:26], got[:26]))
    fails.append("%s: 词表写 %s，实际 %s" % (cn, stated[:20], got[:20]))

print()
print("=" * 78)
print("【3】提到的标签是否真实存在")
print("=" * 78)
tag_lines = []
in_notes = False
for ln in lines:
    if "═══" in ln:
        in_notes = any(k in ln for k in SKIP_SECTION)
        continue
    if in_notes or "→" not in ln:
        continue
    right = ln.partition("→")[2].strip()
    if CJK.search(right) or len(right) > 46 or not right:
        continue
    for v in right.split("/"):
        v = v.strip()
        if v and re.match(r"^[a-z]", v) and len(v) < 34:
            tag_lines.append(v)
tag_lines = sorted(set(tag_lines))
unknown = [v for v in tag_lines if not tr.is_known_tag(v)]
print("   出现 %d 个标签，其中词库不认的 %d 个" % (len(tag_lines), len(unknown)))
for v in unknown:
    print("      ⚠️ %s" % v)

print()
print("=" * 78)
if fails:
    print("FAILED %d 项" % len(fails))
    for f in fails[:20]:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 78)
sys.exit(1 if fails else 0)
