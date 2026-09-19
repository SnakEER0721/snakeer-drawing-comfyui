"""Unit-test the LoRA grouping logic as pure functions.

The grouping code was refactored to take its data as explicit arguments, so it
can be exercised without a DOM. This specifically covers the reported bug:
adding a LoRA after clearing the list must still land in the correct category.
"""
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
PORT = 8822


def run_server_and_get_caps():
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


caps = run_server_and_get_caps()
if not caps:
    print("无法连接后端")
    sys.exit(1)

html = open(UI, encoding="utf-8").read()


def extract(name):
    """Grab a top-level declaration, matching the right bracket for its kind.

    An array declaration must be closed with ], a function with }. Matching the
    wrong pair truncates the fragment (an array of objects ended at its first
    element), which then fails to parse.
    """
    i = html.find("function %s(" % name)
    if i >= 0:
        opener, closer = "{", "}"
        start = html.find("{", i)
    else:
        i = html.find("const %s" % name)
        if i < 0:
            raise KeyError(name)
        eq = html.find("=", i)
        first = html.find("[", eq)
        firstobj = html.find("{", eq)
        if first >= 0 and (firstobj < 0 or first < firstobj):
            opener, closer = "[", "]"
            start = first
        else:
            opener, closer = "{", "}"
            start = firstobj
    if start < 0:
        raise KeyError(name)

    depth = 0
    k = start
    quote = None
    while k < len(html):
        c = html[k]
        if quote:
            if c == "\\":
                k += 2
                continue
            if c == quote:
                quote = None
        else:
            if c in "\"'`":
                quote = c
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return html[i:k + 1]
        k += 1
    raise KeyError(name)


pieces = [extract("LORA_CATEGORIES"), extract("loraKindOf"),
          extract("loraDefaultStrength"), extract("loraGroupItems"),
          extract("loraPoolFor")]

script = "\n".join(pieces) + r"""
const capsData = JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));
const meta = {};
(capsData.lora_details||[]).forEach(d => { meta[d.file] = d; });
const loras = capsData.loras || (capsData.lora_details||[]).map(d=>d.file);

let fail = 0;
function check(name, cond, extra){
  console.log('  ' + (cond ? 'PASS' : 'FAIL') + '  ' + name + (cond ? '' : '   ' + (extra||'')));
  if(!cond) fail++;
}

console.log('已装 LoRA 的分类:');
loras.forEach(l => console.log('   ' + loraKindOf(l, meta).padEnd(10) + ' ' + l));

// 1) every category's pool adds something of the right kind
console.log();
console.log('A. 各类型“＋”按钮会加入正确类型的 LoRA');
LORA_CATEGORIES.forEach(cat => {
  const pool = loraPoolFor(cat.kind, loras, meta);
  if(!pool.length){
    console.log('  SKIP  ' + cat.title + ' (本机没有这个类型)');
    return;
  }
  const chosen = pool[0];
  check(cat.title + ' 选中 ' + chosen + ' 且类型为 ' + cat.kind,
        loraKindOf(chosen, meta) === cat.kind);
});

// 2) THE REPORTED BUG: clear, then add from the character category
console.log();
console.log('B. 清空后再添加（用户报告的 bug）');
let items = [];
// simulate: clear
items = [];
const charPool = loraPoolFor('character', loras, meta);
if(charPool.length){
  items.push({ id:1, name: charPool[0], strength: 0.85 });
  const grouped = loraGroupItems(items, meta);
  check('添加的角色 LoRA 落在 character 分组',
        (grouped.character||[]).length === 1,
        '实际: ' + JSON.stringify(Object.keys(grouped).filter(k=>grouped[k].length)));
  check('它没有跑到 unknown 分组',
        (grouped.unknown||[]).length === 0,
        'unknown=' + JSON.stringify(grouped.unknown));
  check('强度取角色类的默认值 0.85',
        items[0].strength === loraDefaultStrength('character'));
}else{
  console.log('  SKIP  本机没有角色类 LoRA');
}

// 3) changing the file reassigns the category (previously it did not)
console.log();
console.log('C. 改选文件后分类会跟着变');
const qualityPool = loraPoolFor('quality', loras, meta);
if(charPool.length && qualityPool.length){
  const it = { id:2, name: charPool[0], strength: 0.85 };
  let g = loraGroupItems([it], meta);
  const beforeKind = Object.keys(g).find(k => g[k].length);
  it.name = qualityPool[0];
  g = loraGroupItems([it], meta);
  const afterKind = Object.keys(g).find(k => g[k].length);
  check('从 ' + beforeKind + ' 变为 ' + afterKind,
        beforeKind === 'character' && afterKind === 'quality',
        beforeKind + ' -> ' + afterKind);
}

// 4) unknown LoRA still shows up somewhere rather than vanishing
console.log();
console.log('D. 未识别类型的 LoRA 不会丢失');
const unk = '__no_such_lora__.safetensors';
const g = loraGroupItems([{ id:3, name: unk, strength: 0.8 }], meta);
check('落入 unknown 分组', (g.unknown||[]).length === 1);

// 5) no LoRA appears in two groups
console.log();
console.log('E. 每个 LoRA 只出现在一个分组');
const mixed = loras.slice(0, 5).map((l,i) => ({ id:100+i, name:l, strength:0.8 }));
const gm = loraGroupItems(mixed, meta);
const total = Object.values(gm).reduce((a,b)=>a+b.length, 0);
check('分组总数等于条目数 (' + total + ' == ' + mixed.length + ')',
      total === mixed.length);

console.log();
console.log('RESULT: ' + (fail === 0 ? 'ALL PASS' : fail + ' FAILED'));
process.exit(fail === 0 ? 0 : 1);
"""

open(paths.tests_path("_lora_logic.js"), "w", encoding="utf-8").write(script)
caps_file = paths.tests_path("_lora_caps.json")
open(caps_file, "w", encoding="utf-8").write(json.dumps(caps, ensure_ascii=False))

r = subprocess.run([NODE, paths.tests_path("_lora_logic.js"), caps_file],
                   capture_output=True, text=True, encoding="utf-8")
print(r.stdout or "")
if r.stderr:
    print("stderr:", r.stderr[:900])
sys.exit(r.returncode)
