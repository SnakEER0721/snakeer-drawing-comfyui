"""Lock in the ASCII tag handling rules.

Covers what was wrong and what is intentional:

  fixed   "v"    was rejected as 未收录 although v is a real tag (heat 238081)
  fixed   "V"    was echoed with its capital; Danbooru tags are lowercase
  fixed   "Heart Hands" kept mixed case instead of "heart hands"
  by design  "zzzz" passes - it really is a tag (an artist name in the index)
  by design  an unknown but well-shaped string passes, because the app lets users
             mix raw English tags into a prompt; rejecting those would break that
  guard   junk of the wrong shape ("q", "a", "xyzw!" fragments) still fails

The value of this test: the tag "v" is the sole correct spelling for 剪刀手, and a
rejection there is invisible until someone tries it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from cn_translate import Translator

tr = Translator()
fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


print("=" * 78)
print("【1】短标签必须可用（v 是剪刀手的唯一正确写法）")
print("=" * 78)
for s, want in (("v", "v"), ("ok", "ok"), ("zzz", "zzz")):
    got = tr.lookup(s)
    check(got == want, "%-6s -> %-10s (期望 %s)" % (repr(s), got or "未收录", want))

print()
print("=" * 78)
print("【2】ASCII 输入必须归一化为小写")
print("=" * 78)
for s, want in (("V", "v"), ("Heart Hands", "heart hands"),
                ("SPREAD LEGS", "spread legs"), ("Smile", "smile")):
    got = tr.lookup(s)
    check(got == want, "%-14s -> %-20s (期望 %s)" % (repr(s), got or "未收录", want))

print()
print("=" * 78)
print("【3】设计允许的放行（不是 bug）")
print("=" * 78)
# 这两个确实通过，理由明确，写下来避免以后被当成缺陷"修掉"
check(tr.lookup("zzzz") == "zzzz",
      "zzzz 放行（它确实是标签：画师名，在索引中）")
check(tr.lookup("xyzw") == "xyzw",
      "形状合法的未知词放行（允许用户直接混写英文标签）")

print()
print("=" * 78)
print("【4】形状不合法仍要拒绝")
print("=" * 78)
# 注意：'1' 和 '!' 确实是 Danbooru 标签（'!' 热度 57322），所以会被放行 ——
# 这是字典证据，不是缺陷。真正该被拒的是既非标签、形状也不像词的输入。
for s in ("q", "a", "x", "@"):
    got = tr.lookup(s)
    check(got is None, "%-6s 被拒（实际 %s）" % (repr(s), got or "未收录"))
for s in ("1", "!"):
    got = tr.lookup(s)
    check(got == s, "%-6s 放行（它确实是标签）" % repr(s))

print()
print("=" * 78)
print("【5】中文路径不受影响")
print("=" * 78)
for zh, want in (("剪刀手", "v"), ("比耶", "v"), ("双手比耶", "double v"),
                 ("比心", "heart hands"), ("微笑", "smile"),
                 ("双腿分开", "spread legs"), ("手放脸旁", "hand on own face"),
                 ("招手", "beckoning"), ("眨眼", "blinking")):
    got = tr.lookup(zh) or ""
    check(got.lower() == want.lower(), "%-8s -> %-20s (期望 %s)" % (zh, got, want))

print()
print("=" * 78)
print("【6】整句翻译里短标签不被丢")
print("=" * 78)
en, _p, unk = tr.translate("剪刀手 微笑")
check("v" in en, "「剪刀手 微笑」含 v（实际 %s）" % en)
check(not unk, "无未收录项（实际 %s）" % unk)

print()
print("=" * 78)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 78)
sys.exit(1 if fails else 0)
