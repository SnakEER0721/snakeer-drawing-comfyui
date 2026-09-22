# -*- coding: utf-8 -*-
"""守住「别名文字会反向决定分类」这条坑。

背景（真实踩过，见 AGENTS.md 同名那一节）：
    server.py 的 classify_lora() 里有一层「拿别名文字猜分类」——
    别名含「画质/增强/修复/锐化/细节」判成 quality，含「角色/人物/人脸/脸」
    判成 character，而且**盖掉训练元数据**算出来的结论。

    给 r17329_illuu 起名「r17329 角色(作者LyliaEngine)」→ 被判成角色；
    实际那是 Shiiro0 的画风 LoRA。只去掉「角色」二字，分类立刻变回 style。

判据：别名文字里含这些触发词、却没显式给 kind 的条目 = 隐患。
      写了 kind 就早返回（server.py L1144），不受那层影响。

这个测试**只读**，不改任何东西：隐患样本写在临时文件里喂给工具，
真实 lora_aliases.json 一个字节都不动。

⚠️ 这里必须验"工具会红"。只验"正式表现在是绿的"是**空洞断言**——
   正式表是绿的，可能是因为工具压根什么都没查。所以：
     ① 造一份**必然触发**的样本 -> 工具必须报红
     ② 拿正式表跑              -> 必须绿
     ③ 工具必须先从 server.py 源码里解析出 _Q/_C 再比对（否则查的是别的东西）
"""
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "dev", "check_alias_kind.py")
ALIAS = os.path.join(ROOT, "lora_aliases.json")

sys.dont_write_bytecode = True

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def run_tool(alias_path):
    """跑工具，返回 (exit_code, stdout+stderr)。"""
    p = subprocess.run([sys.executable, "-X", "utf8", TOOL, "--file", alias_path],
                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


print("== 别名分类守卫（dev/check_alias_kind.py）==")

# ---- 前置①：有没有可查的别名表 ----
# 全新安装（发布包就是）根本没有 lora_aliases.json —— 用户还没写过别名。
# 没有可查的东西时**说清理由并跳过**，不能判失败。
if not os.path.isfile(ALIAS):
    print("   SKIP 没有 lora_aliases.json（用户还没写过别名），本次跳过")
    print("\nRESULT: SKIP")
    sys.exit(0)

# ---- 前置②：工具在不在 ----
# dev/ 不进发布包，所以发布包里这个工具必然不存在。
# 上面那道闸已经把"发布包"这种情况拦在 SKIP 里了；能走到这里说明
# 有正式别名表，那是维护者本机 —— 此时工具缺了就是**真的坏了**，判失败。
check(os.path.isfile(TOOL), "工具存在：dev/check_alias_kind.py")
if not os.path.isfile(TOOL):
    print("\nRESULT: 1 项失败")
    sys.exit(1)

real = json.loads(io.open(ALIAS, encoding="utf-8").read())

# ---- ① 造一份必然触发的样本，工具必须报红 ----
# 关键：不能随便拿一个条目改。要选**别名里本来没有触发词**的那种，
# 把它改成含触发词的，才真正制造出「文字会反向决定分类」这个隐患。
tmpdir = tempfile.mkdtemp(prefix="alias_kind_")
sample = os.path.join(tmpdir, "_sample.json")
bad = dict(real)
picked = None
for k, v in bad.items():
    al = (v.get("alias") or "")
    if not any(w in al for w in ("角色", "人物", "人脸", "脸",
                                 "画质", "增强", "修复", "锐化", "细节")):
        bad[k] = dict(v)
        bad[k]["alias"] = "角色"          # 制造隐患
        bad[k].pop("kind", None)          # 并且不给 kind
        picked = k
        break

if picked is None:
    print("   SKIP 别名表里每条都含触发词且已给 kind，造不出样本")
    print("\nRESULT: SKIP")
    sys.exit(0)

with io.open(sample, "w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(bad, ensure_ascii=False))

code, out = run_tool(sample)
check(code != 0, "① 必然触发的样本 -> 工具报红（exit=%d）" % code)
check(picked in out, "① 报红时点名了出问题的条目：%s" % picked[:44])
check("角色" in out, "① 报红时说明了命中的触发词")

# ---- ② 正式表必须绿 ----
code, out = run_tool(ALIAS)
check(code == 0, "② 正式别名表 -> 工具通过（exit=%d）" % code)
check("ALL PASS" in out, "② 通过时打印了 ALL PASS")

# 绿的时候也要确认它真的查了东西，不是空转
check("别名共 %d 条" % len(real) in out,
      "② 确实读了全部 %d 条（不是空转）" % len(real))

# ---- ③ 工具必须先从源码解析 _Q/_C 再比对 ----
check("server.py" in out and "一致" in out,
      "③ 触发词表是从 server.py 源码解析出来并比对过的")

# ---- ④ 确认真实文件没被动过 ----
after = json.loads(io.open(ALIAS, encoding="utf-8").read())
check(after == real, "④ 真实 lora_aliases.json 一个字节都没动")

# ---- 收尾：清掉临时样本 ----
try:
    os.remove(sample)
    os.rmdir(tmpdir)
except OSError:
    pass

print()
if fails:
    print("RESULT: %d 项失败" % len(fails))
    for f in fails:
        print("   - %s" % f)
    sys.exit(1)
print("RESULT: ALL PASS")
sys.exit(0)
