"""One-pass verification of every Chinese term in the vocabulary document.

Re-parses D:\\dsh\\webapp\\高频词表.txt and checks each term against the real
lookup chain, so the document's completeness claim is tested rather than
assumed. Multi-word phrases go through translate() (lookup() is single-term
only), and the "推荐写法" example lines are checked for leftover Chinese.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from cn_translate import Translator

DOC = paths.VOCAB
tr = Translator()
CJK = re.compile(r"[\u4e00-\u9fff]")

# 说明性章节：里面的箭头不是词条
# 「颜文字/符号」和「无法直接翻译」两节是符号与专名，不是中文词条，
# 不能被当成「中文 → 英文」的词汇行解析。
# 「画师串」同理：左边是画师的人名（炸虾人 / 八重樫南），不是可翻译的中文词，
# 右边是画师标签本身。它不是「中文 → 英文」映射，别拿 translate() 去要求它。
SKIP_SECTION = ("负向提示词", "权重语法", "参数速查", "LoRA 使用要点",
                "手动放大", "颜文字", "无法直接翻译", "画师串")

lines = open(DOC, encoding="utf-8").read().splitlines()
in_notes = False
terms, examples = [], []

for ln in lines:
    if "═══" in ln:
        in_notes = any(k in ln for k in SKIP_SECTION)
        continue
    if in_notes:
        continue
    if "推荐写法" in ln:
        m = re.search(r"推荐写法[:：]\s*(.+)$", ln)
        if m:
            examples.append(m.group(1).strip())
        continue
    if "→" not in ln:
        continue
    # A vocabulary line is "中文 → 英文" and nothing else: a short Chinese term
    # on the left, a short English mapping on the right. Long prose that merely
    # mentions a change with an arrow must not be treated as a term.
    if "：" in ln or ":" in ln.split("→")[0]:
        continue
    left, _, right = ln.partition("→")
    left, right = left.strip(), right.strip()
    if not left or not CJK.search(left):
        continue
    if len(left) > 22 or len(right) > 46:
        continue
    # measurement rows like "部位 + 特征 → 27.17" are data, not vocabulary
    if re.fullmatch(r"[\d\.\s%／/×x,，\-–—]+", right):
        continue
    if CJK.search(right):
        continue
    if left.startswith("【") or left.startswith("═") or left.startswith("★"):
        continue
    for piece in (x.strip() for x in left.split("/")):
        if piece and CJK.search(piece) and len(piece) <= 18:
            terms.append((piece, right))

seen, uniq = set(), []
for t, e in terms:
    if t not in seen:
        seen.add(t)
        uniq.append((t, e))

bad = []
for cn, expected in uniq:
    en, _, unknown = tr.translate(cn)
    if not en or unknown:
        bad.append((cn, expected, unknown))

print("文档中文字条: %d 个（去重后）" % len(uniq))
print("推荐写法示例: %d 条" % len(examples))
print("翻译失败    : %d 个" % len(bad))
for cn, exp, unk in bad:
    print("  ❌ %-16s 期望 %-30s 未收录=%s" % (cn, exp, unk))

print()
print("=== 推荐写法示例（不应残留中文）===")
for ex in examples:
    left = [p.strip() for p in ex.split(",") if p.strip() and CJK.search(p)]
    print("  %s %s" % ("OK " if not left else "BAD", ex[:74]))

print()
print("RESULT: " + ("词表 100% 可翻译" if not bad else "%d 个词查不到" % len(bad)))
sys.exit(0 if not bad else 1)
