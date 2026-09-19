# -*- coding: utf-8 -*-
"""回归：LoRA 名字后面那个「来源」短标签（`aliasSourceLabel`）。

用户视角要解决的事：文件名是 SHA256 的那种（`8be1e5d2….safetensors`），
括号里的中文名是**自动**从 C 站作者那里取的。用户看着它像"我以前自己起的"，
于是分不清"我后来改的名字到底被记住没有" —— 下拉框里那一串中文就成了噪音。

这里验的是纯函数（从 ui.html 里抽出来在 node 里跑），不依赖浏览器：
  【1】五个已知来源各自映到什么中文
  【2】空来源必须是**空串**（本地命名不加后缀，别在名字后面挂个假来源）
  【3】认不出的来源**原样返回**，不许猜成"C站"
  【4】拿真实的 /api/capabilities 跑一遍：每个有名字的 LoRA 都能落到一个
      非空标签上（也就是"界面上不会出现没写来源的中文名"）
"""
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import paths  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
NODE = getattr(paths, "NODE", None) or r"C:\Program Files\nodejs\node.exe"
if not os.path.isfile(NODE):
    print("RESULT: SKIP（找不到 node.exe：%s）" % NODE)
    sys.exit(0)
fails = []


def check(cond, label, extra=""):
    print("   %s %s%s" % ("OK  " if cond else "FAIL", label,
                          "" if cond else "   " + str(extra)))
    if not cond:
        fails.append(label)


def extract(name):
    """从 ui.html 里抠出一个顶层声明（函数或 const），原样返回源码。"""
    html = open(os.path.join(ROOT, "ui.html"), encoding="utf-8").read()
    i = html.find("function " + name + "(")
    if i < 0:
        raise KeyError(name)
    k = html.find("{", i)
    depth, q = 0, None
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
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return html[i:k + 1]
        k += 1
    raise KeyError(name)


print("从 ui.html 抽出 aliasSourceLabel()")
try:
    src = extract("aliasSourceLabel")
except KeyError:
    print("RESULT: FAIL（ui.html 里找不到 aliasSourceLabel —— 这个函数被删了？）")
    sys.exit(1)

script = src + r"""
let fail = 0;
function check(n, c, extra){
  console.log('  ' + (c ? 'PASS' : 'FAIL') + '  ' + n + (c ? '' : '   ' + (extra||'')));
  if (!c) fail++;
}

console.log();
console.log('=== 已知来源 ===');
const known = {
  'civitai-hash': 'C站自动',
  'civitai':      'C站',
  'meta':         '文件里读的',
  'C站目录':      'C站目录',
  '手动':         '你改的',
};
Object.keys(known).forEach(k =>
  check(k + ' -> ' + known[k], aliasSourceLabel(k) === known[k],
        '实际 ' + JSON.stringify(aliasSourceLabel(k))));

console.log();
console.log('=== 边界 ===');
check('空串 -> 空串（本地命名不加后缀）', aliasSourceLabel('') === '');
check('undefined -> 空串', aliasSourceLabel(undefined) === '');
check('null -> 空串', aliasSourceLabel(null) === '');
check('纯空格 -> 空串', aliasSourceLabel('   ') === '');
check('认不出的原样返回（不猜成 C站）',
      aliasSourceLabel('某个新来源') === '某个新来源',
      '实际 ' + JSON.stringify(aliasSourceLabel('某个新来源')));
check('不会把 C站目录 说成 civitai-hash',
      aliasSourceLabel('C站目录') !== aliasSourceLabel('civitai-hash'));

console.log();
console.log('RESULT: ' + (fail === 0 ? 'ALL PASS' : fail + ' FAILED'));
process.exit(fail === 0 ? 0 : 1);
"""

js = os.path.join(HERE, "_alias_src_logic.js")
open(js, "w", encoding="utf-8").write(script)
r = subprocess.run([NODE, js], capture_output=True, text=True, encoding="utf-8")
print(r.stdout or "")
if r.stderr:
    print("stderr:", r.stderr[:900])
if r.returncode != 0:
    sys.exit(r.returncode)

print()
print("=== 真实数据：有名字的都落得到非空标签 ===")
try:
    caps = json.loads(urllib.request.urlopen(
        BASE + "/api/capabilities", timeout=30).read().decode("utf-8", "replace"))
except Exception as e:
    print("RESULT: SKIP（连不上 %s：%s）" % (BASE, e))
    sys.exit(0)
det = ((caps.get("data") or {}).get("lora_details") or [])
api_js = os.path.join(HERE, "_alias_src_real.js")
names = sorted(set((d.get("alias_source") or "") for d in det if d.get("alias")))
real = os.path.join(HERE, "_alias_src_names.txt")
# newline="\n"：进程间传 JSON，行尾多一个 \r 就是另一个键了
# （实测第一版就是这么坏的：'civitai\r' 查不到，报成"来源没有标签"）
open(real, "w", encoding="utf-8", newline="\n").write("\n".join(names))
# ★ 结果写文件、不经 node 的 stdout：Windows 上 node 的 stdout 编码跟着
#   控制台走，中文进来容易在管道里被换掉。写文件是明确的 UTF-8，不猜。
open(api_js, "w", encoding="utf-8", newline="\n").write(
    src + "\n"
    "const fs = require('fs');\n"
    "const rows = fs.readFileSync(process.argv[2], 'utf8')"
    ".split('\\n').filter(Boolean).map(s => [s, aliasSourceLabel(s)]);\n"
    "fs.writeFileSync(process.argv[3], JSON.stringify(rows), 'utf8');\n")
r2 = subprocess.run([NODE, api_js, real, api_js + ".out"], capture_output=True,
                    text=True, encoding="utf-8")
if r2.returncode != 0:
    print("   FAIL node 跑不起来：%s" % (r2.stderr or "")[:300])
    fails.append("node 跑不起来")
    mapped = {}
else:
    mapped = dict(json.loads(open(api_js + ".out", encoding="utf-8").read()))
for s in names:
    label = mapped.get(s, "")
    check(bool(label) or s == "",
          "来源 %r 有标签：%r" % (s or "(空)", label))
check("有名字的 LoRA 里没出现「来源写着空、名字却是自动取的」",
      all(mapped.get(s) for s in names if s),
      "空标签的：%s" % [s for s in names if s and not mapped.get(s)])

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
