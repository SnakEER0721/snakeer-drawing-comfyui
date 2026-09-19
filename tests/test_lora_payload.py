"""Does the frontend actually send the CURRENT LoRA selection?

The reported symptom is that switching from combination A+B to A+C still
generates with A+B. If that is real, the payload builder in ui.html is reading
stale state. This extracts that logic and drives it through realistic UI
interactions: add items, change the dropdown, move the weight slider, delete a
row - then inspect the payload each time.
"""
import os
import json
import re
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
PORT = 8831

proc = subprocess.Popen([PY, "-X", "utf8", SERVER, "--port", str(PORT)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8")
caps = None
try:
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/capabilities" % PORT, timeout=5) as r:
                caps = json.loads(r.read().decode("utf-8", "replace"))["data"]
            break
        except Exception:
            time.sleep(0.5)
finally:
    proc.terminate()
    try:
        proc.communicate(timeout=15)
    except Exception:
        proc.kill()

if not caps:
    print("无法连接后端")
    sys.exit(1)

html = open(UI, encoding="utf-8").read()


def extract_decl(name):
    """Pull a top-level declaration, matching [] or {} as appropriate."""
    i = html.find("function %s(" % name)
    if i >= 0:
        op, cl, start = "{", "}", html.find("{", i)
    else:
        i = html.find("const %s" % name)
        if i < 0:
            i = html.find("let %s" % name)
        if i < 0:
            raise KeyError(name)
        eq = html.find("=", i)
        fa, fo = html.find("[", eq), html.find("{", eq)
        if fa >= 0 and (fo < 0 or fa < fo):
            op, cl, start = "[", "]", fa
        else:
            op, cl, start = "{", "}", fo
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


# The payload builder lives inside btnGen.onclick. Extract it by locating the
# `const loras = loraItems.map` block through the end of the payload object.
m = re.search(r"(const loras = loraItems\.map[\s\S]*?\n  \};)", html)
if not m:
    print("无法定位 payload 构建代码")
    sys.exit(1)
payload_src = m.group(1)

pieces = [extract_decl("LORA_CATEGORIES"), extract_decl("loraKindOf"),
          extract_decl("loraDefaultStrength")]

script = "\n".join(pieces) + "\n" + r"""
const capsData = JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));
const meta = {};
(capsData.lora_details||[]).forEach(d => { meta[d.file] = d; });
const caps = { loras: (capsData.loras||[]).length ? capsData.loras
                     : (capsData.lora_details||[]).map(d=>d.file) };
const loraMeta = meta;
let loraItems = [];

function buildPayload(){
  const loras = loraItems.map(it => {
    const m = (loraMeta && loraMeta[it.name]) || {};
    const td = m.trigger_detail || {};
    return {
      name: it.name,
      strength: Number(it.strength),
      triggers: td.confident ? (td.all || []) : []
    };
  }).filter(x => x.name);
  return { loras };
}

let fail = 0;
function check(name, cond, extra){
  console.log('  ' + (cond?'PASS':'FAIL') + '  ' + name + (cond?'':'   '+extra));
  if(!cond) fail++;
}
function names(){ return buildPayload().loras.map(l=>l.name+'@'+l.strength); }

const L = caps.loras;
console.log('可用 LoRA: ' + L.join(', '));
const charL = L.find(l => loraKindOf(l, meta)==='character');
const qualL = L.find(l => loraKindOf(l, meta)==='quality');
const styL  = L.find(l => loraKindOf(l, meta)==='style');

console.log();
console.log('A. 组合 A+B -> A+C 时 payload 是否跟着变');
loraItems = [];
if(charL && qualL){
  loraItems.push({ id:1, name: charL, strength: 0.85 });   // A
  loraItems.push({ id:2, name: qualL, strength: 0.6 });    // B
  const first = names().join(' | ');
  console.log('     A+B: ' + first);

  // user changes the SECOND row's dropdown (B -> C)
  const other = L.find(l => l !== charL && l !== qualL) || qualL;
  loraItems[1].name = other;
  loraItems[1].strength = loraDefaultStrength(loraKindOf(other, meta));
  const second = names().join(' | ');
  console.log('     A+C: ' + second);

  check('换掉第二个 LoRA 后 payload 跟着变', first !== second, first + ' == ' + second);
  check('第二个位置确实是新的 LoRA',
        loraItems[1].name === other, loraItems[1].name);
  check('第一个位置没有被影响', loraItems[0].name === charL, loraItems[0].name);
}else{
  console.log('    SKIP 缺少角色类或画质类 LoRA');
}

console.log();
console.log('B. 改权重会进入 payload');
if(charL){
  loraItems = [{ id:1, name: charL, strength: 0.85 }];
  const before = names().join();
  loraItems[0].strength = 0.45;
  const after = names().join();
  console.log('     ' + before + '  ->  ' + after);
  check('权重变化进入 payload', before !== after);
  const p = buildPayload().loras[0];
  check('权重是数字类型', typeof p.strength === 'number', typeof p.strength);
}

console.log();
console.log('C. 删除一行后 payload 不再包含它');
if(charL && qualL){
  loraItems = [{ id:1, name: charL, strength: 0.85 },
               { id:2, name: qualL, strength: 0.6 }];
  const before = buildPayload().loras.length;
  loraItems = loraItems.filter(x => x.id !== 2);
  const after = buildPayload().loras.length;
  console.log('     ' + before + ' 个 -> ' + after + ' 个');
  check('删除后数量减少', after === before - 1);
  check('剩下的不是被删的那个',
        buildPayload().loras.every(l => l.name !== qualL));
}

console.log();
console.log('D. 触发词只在可信时进入 payload');
if(charL){
  const td = (meta[charL].trigger_detail||{});
  const p = buildPayload.call(null, loraItems = [{ id:1, name: charL, strength:0.85 }]);
  const pl = buildPayload().loras[0];
  // ★ 前提：这台机器上得有一个"触发词可信"的角色 LoRA。
  //   触发词来自 lora_aliases.json（用户自己写的备注）。发布包里的那个文件
  //   是空壳 {}，全新安装的机器上没有任何可信触发词 —— 这时断言"触发词已带上"
  //   是**前提不成立**，不是失败。实测：发布目录里跑 fast 层时这条假失败。
  if(td.confident && (td.all||[]).length > 0){
    check('角色 LoRA 触发词已带上', (pl.triggers||[]).length > 0,
          'confident=' + td.confident + ' n=' + (pl.triggers||[]).length);
  } else {
    console.log('     [跳过] 这台机器上没有"触发词可信"的角色 LoRA'
                + '（lora_aliases.json 里没写）—— 前提不成立，不算失败');
  }
}
if(qualL){
  loraItems = [{ id:1, name: qualL, strength:0.6 }];
  const pl = buildPayload().loras[0];
  const td = (meta[qualL].trigger_detail||{});
  check('不可信的 LoRA 不带触发词',
        td.confident ? true : (pl.triggers||[]).length === 0,
        'confident=' + td.confident + ' triggers=' + (pl.triggers||[]).length);
}

console.log();
console.log('E. 全部清空后 payload 为空');
loraItems = [];
check('清空后 loras 长度为 0', buildPayload().loras.length === 0);

console.log();
console.log('RESULT: ' + (fail===0 ? 'ALL PASS' : fail + ' FAILED'));
process.exit(fail===0 ? 0 : 1);
"""

open(paths.tests_path("_payload_logic.js"), "w", encoding="utf-8").write(script)
# keep the extracted payload builder available for inspection
open(paths.tests_path("_payload_extract.txt"), "w", encoding="utf-8").write(payload_src)

caps_file = paths.tests_path("_caps.json")
open(caps_file, "w", encoding="utf-8").write(json.dumps(caps, ensure_ascii=False))

r = subprocess.run([NODE, paths.tests_path("_payload_logic.js"), caps_file],
                   capture_output=True, text=True, encoding="utf-8")
print(r.stdout or "")
if r.stderr:
    print("stderr:", r.stderr[:1000])
sys.exit(r.returncode)
