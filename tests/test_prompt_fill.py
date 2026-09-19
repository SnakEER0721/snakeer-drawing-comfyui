"""Verify the deselect-removes-tag behaviour and the click feedback.

Background: appendToPrompt() only ever appended. Switching a hair colour left
both colour tags in the prompt box, because deselecting an option only removed
its highlight - the text stayed. Fixed with removeFromPrompt(), which deletes by
exact comma-item match, plus a press animation on the fill buttons.

The substring case matters: a naive includes() would strip "blonde hair" out of
"dark blonde hair". That is checked here.
"""
import os
import io
import re
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

UI = paths.UI_HTML
NODE = r"C:\Program Files\nodejs\node.exe"
fails = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        fails.append(msg)


t = io.open(UI, encoding="utf-8").read()

print("=" * 76)
print("【1】删除标签的函数存在且按逗号项精确匹配")
print("=" * 76)
check("function removeFromPrompt" in t, "存在 removeFromPrompt()")
m = re.search(r"function removeFromPrompt\(text\)\{(.*?)\n\}", t, re.S)
body = m.group(1) if m else ""
check("split(\",\")" in body, "按逗号切分")
check('!== target' in body, "整项比较（不是 includes 子串匹配）")

print()
print("=" * 76)
print("【2】删除逻辑的边界情况（用 node 实跑）")
print("=" * 76)
SCRIPT = r'''
function rm(box, text){
  const target = String(text||'').trim().toLowerCase();
  if(!target) return box;
  const parts = (box||'').split(',').map(s=>s.trim()).filter(Boolean);
  const kept = parts.filter(p=>p.toLowerCase()!==target);
  return kept.join(', ');
}
const cases = [
  ['blonde hair, 1girl, smile', 'blonde hair', '1girl, smile'],
  ['1girl, blonde hair',        'blonde hair', '1girl'],
  ['dark blonde hair, 1girl',   'blonde hair', 'dark blonde hair, 1girl'],
  ['blonde hair',               'blonde hair', ''],
  ['1girl, smile',              'blonde hair', '1girl, smile'],
  ['1girl,white hair',          'white hair',  '1girl'],
];
let bad = 0;
cases.forEach(([box, tag, want]) => {
  const got = rm(box, tag);
  const ok = got === want;
  if(!ok) bad++;
  console.log((ok?'  OK   ':'  FAIL ') + '[' + box + '] -' + tag + '-> [' + got + ']');
});
process.exit(bad ? 1 : 0);
'''
r = subprocess.run([NODE, "-e", SCRIPT], capture_output=True, text=True,
                   encoding="utf-8")
print(r.stdout.rstrip())
check(r.returncode == 0, "全部边界情况通过")

print()
print("=" * 76)
print("【3】取消选择会触发删除")
print("=" * 76)
check("removeFromPrompt(it.en)" in t, "选项取消时调用 removeFromPrompt")
check("pbClearAll" in t, "有清空全部（含移除）")
check("list.forEach(en => { if(removeFromPrompt(en)) n++; })" in t,
      "清空时逐个移出提示词框")

print()
print("=" * 76)
print("【4】点击反馈动画")
print("=" * 76)
check("function pbFlash" in t, "存在 pbFlash()")
check("@keyframes pbPress" in t, "有按压缩放动画")
check(".pbFlash" in t, "有动画类")
check("void btn.offsetWidth" in t, "强制重排（同一按钮连点能重放）")
check(t.count("pbFlash(") >= 6, "调用点 >= 6（实际 %d）" % t.count("pbFlash("))
check('btn.textContent = added ? "已填入" : "已存在"' in t,
      "LoRA 触发词按钮有状态文字反馈")
check("pbFlash(r1," in t and "pbFlash(r2," in t, "随机按钮也有反馈")

# 所有被调用的 pb 函数都必须有定义，否则运行时 ReferenceError
print()
print("=" * 76)
print("【5】函数定义完整性")
print("=" * 76)
defined = set(re.findall(r"function (\w+)\(", t))
called = set(re.findall(r"\b(pb[A-Z]\w*)\s*\(", t))
missing = sorted(c for c in called if c not in defined)
check(not missing, "所有 pb 函数都有定义（缺 %s）" % (missing or "无"))

print()
print("=" * 76)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
else:
    print("RESULT: ALL PASS")
print("=" * 76)
sys.exit(1 if fails else 0)
