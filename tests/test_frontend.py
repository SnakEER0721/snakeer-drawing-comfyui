"""Static audit of ui.html: catch broken references that a structure check misses.

Earlier rounds only verified that elements exist. This checks the things that
actually break at runtime:
  * $("id") references with no matching element
  * onclick / onchange handlers pointing at undefined functions
  * fetch() calls to endpoints the server does not implement
  * variables used before assignment in the module scope
  * missing <script> closure / duplicated ids
"""
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

UI = paths.UI_HTML
SERVER = paths.SERVER_PY
# node 的位置别写死：装在别处（nvm、便携版、换电脑）就会让这一整块检查
# 静默失效。先按 PATH 找，再退回常见安装路径。
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

html = open(UI, encoding="utf-8").read()
server = open(SERVER, encoding="utf-8").read()

fails = []


def check(name, cond, detail=""):
    print("  %-50s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail[:70]) if not cond else ""))
    if not cond:
        fails.append((name, detail))


# ---------- collect ids declared in HTML ----------
declared = set(re.findall(r'\bid="([^"]+)"', html))
# ids created dynamically in JS strings
for m in re.finditer(r"""id="([^"]+)"|id='([^']+)'""", html):
    pass
dynamic = set(re.findall(r"""id=\\?["']([A-Za-z0-9_\-]+)\\?["']""", html))
declared |= dynamic

used = set(re.findall(r'\$\("([^"]+)"\)', html))
used |= set(re.findall(r"getElementById\(\"([^\"]+)\"\)", html))

print("=" * 78)
print("A. DOM 引用")
print("=" * 78)
print("  HTML 中声明的 id: %d 个" % len(re.findall(r'\bid="', html)))
missing = sorted(used - declared)
check("所有 $(\"id\") 引用的元素都存在", not missing,
      "缺失: %s" % ", ".join(missing[:8]))

# duplicated ids are a real bug (getElementById returns the first)
all_ids = re.findall(r'\bid="([^"]+)"', html)
dupes = sorted({i for i in all_ids if all_ids.count(i) > 1})
check("没有重复的 id", not dupes, "重复: %s" % ", ".join(dupes[:8]))

print()
print("=" * 78)
print("B. 函数与事件处理器")
print("=" * 78)
defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html))
defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", html))
defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*function", html))

called = set(re.findall(r"on\w+=\"([A-Za-z_$][\w$]*)\(", html))
undefined = sorted(c for c in called if c not in defined)
check("HTML 内联事件调用的函数都已定义", not undefined,
      "未定义: %s" % ", ".join(undefined[:8]))

# 这里原来写的是 `sorted(c for c in called if c not in html[...] and False)` ——
# 末尾的 `and False` 让整个推导式恒为空集，是个永远不报错的装饰。
# 保留它只会让人以为"这行在检查什么"。真正有意义的检查是下面这条：
# 内联处理器太少就说明上面的提取正则已经失效，那条"都已定义"会变成空断言。
check("内联处理器数量合理（否则上面的定义检查是空断言）",
      len(called) >= 5, "只找到 %d 个" % len(called))

print()
print("=" * 78)
print("C. 后端接口一致性")
print("=" * 78)
server_routes = set(re.findall(r'path\s*==\s*"(/api/[^"]+)"', server))
server_routes |= {m for m in re.findall(r'path\s+in\s+\(([^)]+)\)', server)
                  for m in re.findall(r'"([^"]+)"', m)}
client_calls = set(re.findall(r'fetch\(\s*"([^"]+)"', html))
client_calls |= set(re.findall(r"fetch\(\s*'([^']+)'", html))
client_calls = {c.split("?")[0] for c in client_calls}
client_calls = {c for c in client_calls if c.startswith("/")}
print("  服务端路由: %s" % ", ".join(sorted(server_routes)))
print("  前端调用  : %s" % ", ".join(sorted(client_calls)))
not_impl = sorted(c for c in client_calls if c not in server_routes)
check("前端调用的接口后端都实现了", not not_impl, "缺: %s" % ", ".join(not_impl))

print()
print("=" * 78)
print("D. JS 语法与低层检查（node）")
print("=" * 78)
jscheck = r"""
const fs=require('fs');
const file=process.argv[2];           // argv[1] is this script itself
if(!file){ console.log('NO_ARG'); process.exit(2); }
const h=fs.readFileSync(file,'utf8');
const m=h.match(/<script[^>]*>([\s\S]*?)<\/script>/);
if(!m){ console.log('NO_SCRIPT'); process.exit(2); }
const src=m[1];
try{ new Function(src); }catch(e){ console.log('SYNTAX:'+e.message); process.exit(3); }
let b=0,p=0,br=0;
for(const ch of src){
  if(ch==='{')b++; else if(ch==='}')b--;
  else if(ch==='(')p++; else if(ch===')')p--;
  else if(ch==='[')br++; else if(ch===']')br--;
}
console.log('OK braces='+b+' parens='+p+' brackets='+br+' lines='+src.split('\n').length);
"""
open(paths.tests_path("_tmp_jscheck.js"), "w", encoding="utf-8").write(jscheck)
if not NODE or not os.path.isfile(NODE):
    # 没有 node 就明确标记为"没检查"，不要伪装成通过。
    print("   未找到 node（PATH 和常见安装路径都没有），JS 语法检查未执行")
    check("node 可用（否则本节检查无效）", False,
          "没找到 node，D 节等于没跑")
else:
    r = subprocess.run([NODE, paths.tests_path("_tmp_jscheck.js"), UI],
                       capture_output=True, text=True, encoding="utf-8")
    out = (r.stdout or "").strip()
    check("JS 语法可解析", out.startswith("OK"), out[:100])
    if out.startswith("OK"):
        check("括号配平", "braces=0" in out and "parens=0" in out, out)

print()
print("=" * 78)
print("E. 关键功能点存在性")
print("=" * 78)
FEATURES = {
    "分块 base64（防栈溢出）": "function bytesToBase64",
    "源图清除": "function clearSource",
    "参考图清除": "function clearRef",
    "参考图多选": "function addRefFile",
    "缩略图渲染": "function renderRefThumbs",
    "手动放大": "function upscale",
    "最近产出": "function loadRecent",
    "触发词提示": "function buildLoraRow",
    "LoRA 分组渲染": "function renderLoraGroups",
    "LoRA 类型判定": "function loraKindOf",
    "LoRA 分组计算（纯函数）": "function loraGroupItems",
    "LoRA 候选选择（纯函数）": "function loraPoolFor",
    "实时翻译": "function doTranslate",
    "画板清空": "function clearPad",
    "画板下载": "function downloadPad",
    "画板空白检测": "function padIsBlank",
    "能力检测": "function loadCaps",
}
for label, needle in FEATURES.items():
    check(label, needle in html)

print()
print("=" * 78)
print("F. 潜在运行时问题")
print("=" * 78)
# 未定义变量风险：检查常见拼写
for var in ["refImages", "refThumbs", "sourceImage", "loraMeta", "caps", "mode"]:
    decl = len(re.findall(r"(?:let|var|const)\s+[^;]*\b%s\b" % var, html))
    check("变量 %s 已声明" % var, decl >= 1, "声明 %d 次" % decl)

# 检查是否还有旧的单参考图残留引用
check("无单参考图残留 (refImage)", not re.search(r"\brefImage\b", html),
      "仍引用 refImage")
check("无单参考图残留 (refObjectURL)", "refObjectURL" not in html)

# ★ 真实事故：uploadFile 里少了 `const buf = await file.arrayBuffer();` 这一行，
#   直接写 bytesToBase64(new Uint8Array(buf))。因为是异步函数体，语法检查过得去，
#   但一调用就抛 ReferenceError: buf is not defined ——
#   源图上传、参考图上传、草图上传三条路全废，而且只在真正点上传时才报。
#   这里盯死"先声明后使用"，不依赖任何不靠谱的通用扫描。
_m = re.search(r"async function uploadFile\(file\)\s*\{(.*?)\n\}", html, re.S)
_body = _m.group(1) if _m else ""
_p_decl = _body.find("const buf")
_p_use = _body.find("new Uint8Array(buf)")
check("uploadFile 里 buf 先声明后使用",
      _p_decl != -1 and _p_use != -1 and _p_decl < _p_use,
      "找到函数=%s 声明位置=%d 使用位置=%d" % (bool(_m), _p_decl, _p_use))

print()
print("=" * 78)
print("结果: %d 项失败" % len(fails))
if fails:
    for n, d in fails:
        print("   - %s  %s" % (n, d[:80]))
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 78)

# 退出码必须反映结果。
# 原来这里只打印「结果: N 项失败」就结束了，退出码永远是 0 ——
# run_tests.py 靠解析这行文字兜住了，但直接跑这个脚本、或者接进任何
# 按退出码判断的流程（CI、编辑器测试面板），失败会被读成通过。
sys.exit(1 if fails else 0)
