"""Test the two-level LoRA menu: top-level grouping + Civitai sub-categories."""
import os
import json
import subprocess
import sys
import time
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

PY = sys.executable
NODE = r"C:\Program Files\nodejs\node.exe"
SERVER = paths.SERVER_PY
UI = paths.UI_HTML
PORT = 8843


def run_server_caps():
    proc = subprocess.Popen([PY, "-X", "utf8", SERVER, "--port", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/capabilities" % PORT, timeout=5) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))["data"]
            except Exception:
                time.sleep(0.5)
        return None
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except Exception:
            proc.kill()


caps = run_server_caps()
if not caps:
    print("无法连接后端")
    sys.exit(1)

print("=== 后端分类结果（一级 / 二级）===")
for d in caps.get("lora_details", []):
    print("  %-46s %-12s %-12s (%s)"
          % (d["file"][:46], d["label"], d.get("category"),
             d.get("category_source")))

html = open(UI, encoding="utf-8").read()


def extract(name):
    i = html.find("function %s(" % name)
    if i < 0:
        i = html.find("const %s" % name)
        if i < 0:
            i = html.find("let %s" % name)
    if i < 0:
        raise KeyError(name)
    eq = html.find("=", i)
    # fb 是从声明位置之后找的第一个 "{"。原来的 else 分支用的是 fo（等号之后的
    # 第一个 "{"），两者在常见写法下相等；但 `const x = [ ... ]` 里若数组元素自带
    # 对象字面量，fo 会指向数组内部而不是声明体。用 fb 更贴合「从这个声明的开头
    # 找第一个块」的意图。改完跑全量测试确认没有回归。
    fa, fo, fb = html.find("[", eq), html.find("{", eq), html.find("{", i)
    if html[i:i + 8] == "function":
        op, cl, start = "{", "}", html.find("{", i)
    elif fa >= 0 and (fo < 0 or fa < fo):
        op, cl, start = "[", "]", fa
    else:
        op, cl, start = "{", "}", fb
    depth, k, q = 0, start, None
    while k < len(html):
        c = html[k]
        if q:
            if c == "\\":
                k += 2
                continue
            if c == q:
                q = None
        else:
            if c in "\"'`":
                q = c
            elif c == op:
                depth += 1
            elif c == cl:
                depth -= 1
                if depth == 0:
                    return html[i:k + 1]
        k += 1
    raise KeyError(name)


pieces = [extract("LORA_CATEGORIES"), extract("ALL_SUBCATS"),
          extract("LORA_SUBCATS"), extract("SUBCAT_LABEL"),
          extract("loraKindOf"), extract("loraSubcatOf"),
          extract("loraDefaultStrength"), extract("loraGroupItems"),
          extract("loraPoolFor"), extract("loraPoolForSub")]

script = "\n".join(pieces) + r"""
const fs = require('fs');
const capsData = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const meta = {};
(capsData.lora_details || []).forEach(d => { meta[d.file] = d; });
const loras = (capsData.loras && capsData.loras.length)
              ? capsData.loras
              : (capsData.lora_details || []).map(d => d.file);

let fail = 0;
function check(n, c, extra){
  console.log('  ' + (c ? 'PASS' : 'FAIL') + '  ' + n + (c ? '' : '   ' + (extra||'')));
  if (!c) fail++;
}

console.log();
console.log('=== 一级 -> 二级 结构 ===');
LORA_CATEGORIES.forEach(cat => {
  const subs = LORA_SUBCATS[cat.kind] || [];
  const mine = loras.filter(l => loraKindOf(l, meta) === cat.kind);
  const active = subs.filter(s => loraPoolForSub(cat.kind, s, loras, meta).length);
  console.log('  %s  (共 %d 个 LoRA)', cat.title, mine.length);
  subs.forEach(s => {
    const n = loraPoolForSub(cat.kind, s, loras, meta).length;
    if (!n) return;
    const label = (SUBCAT_LABEL[s] || s);
    const names = loraPoolForSub(cat.kind, s, loras, meta)
                    .map(x => x.slice(0, 22)).join(', ');
    console.log('      └ ' + label.padEnd(22) + ' ' + n + ' 个: ' + names);
  });
});

console.log();
console.log('=== 检查项 ===');
// 1) every installed LoRA lands in exactly one (kind, sub)
let missing = [];
loras.forEach(l => {
  const k = loraKindOf(l, meta), s = loraSubcatOf(l, meta);
  if (!(LORA_SUBCATS[k] || []).includes(s)) missing.push(l + ' -> ' + k + '/' + s);
});
check('每个 LoRA 的二级分类都在其一级菜单下', missing.length === 0, missing.join('; '));

// 2) the pools cover everything, nothing is orphaned
let covered = new Set();
LORA_CATEGORIES.forEach(cat =>
  (LORA_SUBCATS[cat.kind] || []).forEach(s =>
    loraPoolForSub(cat.kind, s, loras, meta).forEach(l => covered.add(l))));
check('所有 ' + loras.length + ' 个 LoRA 都出现在某个二级菜单里',
      covered.size === loras.length,
      '覆盖 ' + covered.size + ': 缺 ' + loras.filter(l=>!covered.has(l)).join(','));

// 3) no LoRA appears in two sub-categories
let dupes = [];
LORA_CATEGORIES.forEach(cat => {
  const seen = {};
  (LORA_SUBCATS[cat.kind] || []).forEach(s =>
    loraPoolForSub(cat.kind, s, loras, meta).forEach(l => {
      if (seen[l]) dupes.push(cat.kind + ':' + l);
      seen[l] = s;
    }));
});
check('没有 LoRA 重复出现在两个二级项', dupes.length === 0, dupes.join('; '));

// 4) each top-level group's items stay inside its own group
let cross = [];
LORA_CATEGORIES.forEach(cat =>
  Object.values(loraGroupItems(
    loras.map((l,i)=>({id:i,name:l,strength:0.8})), meta))[0] || []);
const grouped = loraGroupItems(loras.map((l,i)=>({id:i,name:l,strength:0.8})), meta);
const total = Object.values(grouped).reduce((a,b)=>a+b.length,0);
check('一级分组总数 = LoRA 总数 (' + total + ')', total === loras.length);

console.log();
console.log('RESULT: ' + (fail === 0 ? 'ALL PASS' : fail + ' FAILED'));
process.exit(fail === 0 ? 0 : 1);
"""

open(paths.tests_path("_subcat_logic.js"), "w", encoding="utf-8").write(script)
caps_file = paths.tests_path("_caps2.json")
open(caps_file, "w", encoding="utf-8").write(json.dumps(caps, ensure_ascii=False))
r = subprocess.run([NODE, paths.tests_path("_subcat_logic.js"), caps_file],
                   capture_output=True, text=True, encoding="utf-8")
print(r.stdout or "")
if r.stderr:
    print("stderr:", r.stderr[:900])
sys.exit(r.returncode)
