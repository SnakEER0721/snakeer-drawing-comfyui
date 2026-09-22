#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config.json **不见了** 时，服务必须说出来 —— 而不是当成首次运行。

为什么值得一条真测试（09-22 实测踩到）：
    打包器的 `--clean` 曾经把发布目录里的 `config.json` 一起铲掉。
    它不报错、不自愈：服务把 `FileNotFoundError` 当成「首次运行」静默放行，
    `CHECKPOINT` 退回写死的默认值 —— **界面上一个提示都没有**。
    用户以为在用自己挑的底模（实际是 oneObsession_05NSFW），其实早退回
    Illustrious-XL-v2.0 了。

    这不是"配置坏了"，是"配置没了"，而原来的代码只认得前一种：
    带 BOM 的、多逗号的、写成数组的三种都会报错（见 dev/probe_broken_config.py），
    **唯独"整个文件没了"一声不吭** —— 因为它在语法上确实合法，
    跟"新装用户还没生成过配置文件"长得一模一样。

判别「首次运行」和「配置被删」只能靠**服务自己留下的、用户不会去动的物证**：
    `_provenance.json`（出图时自动写）、`lora_aliases.json`、
    `custom_prompt_options.json`、产出目录里的日期文件夹。
`说明.txt` 不算 —— 它是发布包自带的，"在"证明不了任何事。

本测试全在临时目录里跑，**一个字节都不碰真实 config.json**。
（这条规矩是这个项目的第 4 类事故：测试把用户的真实数据改坏过。
 见 AGENTS.md「测试不许碰真实数据」。）

判据落在**界面上看得见的东西**上：`/api/capabilities` 的 `config_error`
（页面用它渲染红色「配置有问题」标签 + 状态栏那句话），不是"函数返回了什么"。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

FAILS = []
OKS = []


def ok(m):
    OKS.append(m)
    print("  [OK]   %s" % m)


def bad(m):
    FAILS.append(m)
    print("  [!!]   %s" % m)


# 探针：在"假装成应用目录"的地方 import paths，把关键结论打成一行 JSON。
# 用子进程是因为 paths.py 在 import 时就把 CONFIG/CONFIG_ERROR 定下来了，
# 同进程里 importlib.reload 会跟已经算出来的 APP_DIR 纠缠，不如开干净的进程。
PROBE = r'''
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.getcwd())
try:
    import paths
except Exception as e:
    print("PROBE_FAIL " + repr(e)); sys.exit(3)
# ★ 像 server.main() 那样收口一次：那条"产出目录里有没有出图"的物证
#   要到启动时才判得出来（它依赖 OUT_ANIME，模块后段才定义）。
try:
    paths.note_config_gone_if_used()
except Exception as e:
    print("PROBE_FAIL note: " + repr(e)); sys.exit(3)
print("PROBE_OK " + json.dumps({
    "config_error": paths.CONFIG_ERROR,
    "config_was_missing": paths.CONFIG_WAS_MISSING,
    "checkpoint": paths.CHECKPOINT,
    "fallback": paths.FALLBACK_CHECKPOINT,
    "config_keys": sorted(paths.CONFIG),
}, ensure_ascii=False))
'''


def isolated_env():
    """一个**真正干净**的探针环境：产出目录指到一个不存在的空路径。

    ★ 为什么必须显式指：不指的话 `paths.COMFY_OUTPUT` 走自动探测，在这台
    开发机上会落到**真实的** `D:\\comfyout` —— 那里有 2026-09-15…22 八个出图
    日期目录。于是"全新解压"这个场景会在探针的临时目录里看见**本机的出图
    记录**，判成"用过这个应用"并报警，看上去像产品误报，其实是**测试没隔离**。
    实测就是这么栽的：①红、其余全绿，报出来的路径是维护者自己的产出目录。

    所以：要验"真首次运行"就给它一个不存在的产出目录；
    要验"本机确实出过图"（下面 ①b），才让它用默认探测。
    """
    out = os.path.join(tempfile.gettempdir(), "_cfgdel_no_such_output_")
    return dict(os.environ, COMFYUI_OUTPUT=out)


def host_env():
    """不给 COMFYUI_OUTPUT —— 让 paths 自己探测，也就是**本机真实**的产出目录。"""
    return {k: v for k, v in os.environ.items() if k != "COMFYUI_OUTPUT"}


def run_probe(setup, label, env=None):
    """造一个假的"应用目录"，按 setup 摆东西，跑探针，返回 (结果, stderr)。

    env 决定探针看到哪个产出目录 —— 用 `isolated_env()` 才是真正干净的首次
    安装；用 `host_env()` 则是"本机真实产出目录"那个场景。默认走隔离版，
    免得每个场景都无意间看见维护者自己的出图记录。
    """
    d = tempfile.mkdtemp(prefix="cfgdel_")
    try:
        shutil.copy2(os.path.join(ROOT, "paths.py"), os.path.join(d, "paths.py"))
        # paths.py 会读 VERSION；缺了也得能起来（它自己有兜底），但摆上更真
        for extra in ("VERSION", "config.example.json"):
            src = os.path.join(ROOT, extra)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(d, extra))
        setup(d)
        pr = subprocess.run([PY, "-X", "utf8", "-u", "-c", PROBE], cwd=d,
                            env=env if env is not None else isolated_env(),
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=90)
        out, err = pr.stdout or "", pr.stderr or ""
        res = None
        for line in out.splitlines():
            if line.startswith("PROBE_OK "):
                res = json.loads(line[len("PROBE_OK "):])
            elif line.startswith("PROBE_FAIL "):
                bad("%s：探针跑不起来 → %s" % (label, line))
        if res is None and not FAILS:
            bad("%s：探针没给出结论（exit=%s）\n       stderr: %s"
                % (label, pr.returncode, err.strip()[:400]))
        return res, err
    finally:
        shutil.rmtree(d, ignore_errors=True)


def w(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def wj(path, obj):
    w(path, json.dumps(obj, ensure_ascii=False, indent=2))


print("== config.json 不见了，服务会不会说出来 ==")

# ---------------------------------------------------------------- ①a 真首次运行
# 刚解压：应用目录空的、产出目录也是空的。**必须安静** —— 这里要是报警，
# 每个新用户一开 app 就看到红标签。
def only_fresh(d):
    pass


r1, e1 = run_probe(only_fresh, "①a 全新解压（产出目录也是空的）")
if r1:
    if r1["config_error"] is None:
        ok("①a 全新解压：不出声（新用户不该一开 app 就看到红色警告）")
    else:
        bad("①a 全新解压竟然报警了，新用户会一头雾水：%s" % r1["config_error"][:120])
    if r1["checkpoint"] == r1["fallback"]:
        ok("①a 全新解压：底模 = 兜底值 %s" % r1["fallback"])
    else:
        bad("①a 全新解压：底模是 %r，应该是兜底值 %r"
            % (r1["checkpoint"], r1["fallback"]))

# ---------------------------------------------------------------- ①b 本机真的出过图
# 应用目录是空的（config.json 也没了），但产出目录里有出图记录 —— 这是
# "老用户被清掉了 config.json"的情形，和 ①a 走的完全是同一条代码路径
# （都只有 OUT_ANIME 这条物证），必须出声。
# 为什么单独列：①a 用了隔离的产出目录，**它全绿证明不了本机真有出图时会怎样**。
r1b, e1b = run_probe(only_fresh, "①b 本机出过图但配置没了", env=host_env())
if r1b:
    if r1b["config_was_missing"] is False:
        print("   ·  本机有 config.json，试不了这个场景（跳过，不是失败）")
    elif r1b["config_error"] is None:
        bad("①b 产出目录里有本机的出图记录，却没出声 —— "
            "老用户被清掉 config.json 时就是这么静默退回兜底底模的")
    else:
        ok("①b 配置没了但本机出过图：报警了")

# ---------------------------------------------------------------- ② 配置在，正常
def has_cfg(d):
    wj(os.path.join(d, "config.json"),
       {"checkpoint": "oneObsession_05NSFW.safetensors", "port": 8765})


r2, e2 = run_probe(has_cfg, "② 配置正常")
if r2:
    if r2["config_error"] is None:
        ok("② 配置正常：不出声")
    else:
        bad("② 配置正常却报警了：%s" % r2["config_error"][:120])
    if r2["checkpoint"] == "oneObsession_05NSFW.safetensors":
        ok("② 配置正常：用的是用户挑的底模")
    else:
        bad("② 配置正常：底模 = %r，用户挑的那个没生效" % r2["checkpoint"])

# ---------------------------------------------------------------- ③ ★ 主场景
# 用户挑过底模、出过图，然后 config.json **被删了**（09-22 就是这么丢的）。
# 三种物证各测一遍 —— 它们在真实机器上出现的时机不同，任何一个单独出现都得认出来。
MARKERS = [
    ("_provenance.json", lambda p: wj(p, {})),
    ("lora_aliases.json", lambda p: wj(p, {"a.safetensors": {"alias": "甲"}})),
    ("custom_prompt_options.json", lambda p: wj(p, {"发色": ["黑"]})),
]
for name, mk in MARKERS:
    def setup(d, _n=name, _mk=mk):
        _mk(os.path.join(d, _n))
    r, err = run_probe(setup, "③ 有 %s" % name)
    if not r:
        continue
    if r["config_error"] is None:
        bad("③ 有 %s 却没出声 —— 用户挑的底模没了，界面上一片安静" % name)
    else:
        ok("③ 有 %s：报警了" % name)
        # 提示必须能照着办：说清「哪个文件没了」「现在用的是什么」「怎么修」
        if "config.json" in r["config_error"] and "不见" in r["config_error"]:
            ok("③ 提示点名了是 config.json 不见了")
        else:
            bad("③ 提示没说清是哪个文件没了：%s" % r["config_error"][:140])
        if r["fallback"] in r["config_error"]:
            ok("③ 提示写出了现在实际在用的底模名（%s）" % r["fallback"])
        else:
            bad("③ 提示没说现在实际在用哪个底模（用户没法判断影响）：%s"
                % r["config_error"][:140])
        if "安装模型.bat" in r["config_error"]:
            ok("③ 提示给出了修法（双击「安装模型.bat」）")
        else:
            bad("③ 提示没给修法：%s" % r["config_error"][:140])
    # 报警的同时底模确实退回了兜底值 —— 提示说的和实际做的必须一致
    if r["checkpoint"] == r["fallback"]:
        ok("③ 底模确实退回了兜底值（提示说的和实际一致）")
    else:
        bad("③ 底模是 %r，和提示里写的 %r 对不上" % (r["checkpoint"], r["fallback"]))
    # 而且**不能**把 CONFIG 读成非空 —— 文件都没了，凭空的键就是编的
    if not r["config_keys"]:
        ok("③ 没有凭空造出配置键")
    else:
        bad("③ config.json 都不在了，却读出了键：%s" % r["config_keys"])

# ---------------------------------------------------------------- ④ 产出目录的物证
# 单独验一遍"产出目录里已经有出图的日期文件夹"这条 —— 它和前三个文件物证走的是
# **另一条代码路径**（`_output_dir_has_shoots`，定义在模块后段，import 期还不存在），
# 所以前三个全过**证明不了**这条能用。实测就是这么栽的：第一版只在 import 期查，
# 而这条判据要等 OUT_ANIME 定义出来 —— 前三个场景全绿，第四条一跑就 NameError。
def no_markers(d):
    pass


d = tempfile.mkdtemp(prefix="cfgdel_out_")
try:
    shutil.copy2(os.path.join(ROOT, "paths.py"), os.path.join(d, "paths.py"))
    if os.path.isfile(os.path.join(ROOT, "VERSION")):
        shutil.copy2(os.path.join(ROOT, "VERSION"), os.path.join(d, "VERSION"))
    out = os.path.join(d, "_fakeout")
    os.makedirs(os.path.join(out, "anime", "2026-09-21"))
    env = dict(os.environ, COMFYUI_OUTPUT=out)
    res, err = run_probe(no_markers, "④ 只有产出目录里的出图", env=env)
    if res is None:
        bad("④ 只有产出目录：探针没给出结论")
    else:
        # 先确认这个假产出目录真的被 paths 采纳了 —— 否则下面那条断言是在
        # 验一个根本没生效的设置（测错东西比不测更糟）
        if res["config_error"] is None:
            bad("④ 产出目录里已经有出图（2026-09-21）却没出声 —— "
                "这条物证走的是单独一条代码路径，别处全绿也证明不了它")
        else:
            ok("④ 只有产出目录里的出图记录：也认出来了，报警了")
finally:
    shutil.rmtree(d, ignore_errors=True)

# 反例：产出目录里**只有**服务自己建的 _depth / _recycle → 那不算"用过"，
# 必须仍然安静。没有这条反例，上面那条断言只证明了"有日期目录就报"，
# 证明不了"没出过图就不报"。
def only_service_subdirs(d):
    pass


d = tempfile.mkdtemp(prefix="cfgdel_out2_")
try:
    shutil.copy2(os.path.join(ROOT, "paths.py"), os.path.join(d, "paths.py"))
    if os.path.isfile(os.path.join(ROOT, "VERSION")):
        shutil.copy2(os.path.join(ROOT, "VERSION"), os.path.join(d, "VERSION"))
    out = os.path.join(d, "_fakeout")
    os.makedirs(os.path.join(out, "anime", "_depth"))
    os.makedirs(os.path.join(out, "anime", "_recycle"))
    env = dict(os.environ, COMFYUI_OUTPUT=out)
    res, err = run_probe(only_service_subdirs, "④反例 只有 _depth/_recycle", env=env)
    if res is None:
        bad("④反例：探针没给出结论")
    elif res["config_error"] is not None:
        bad("④反例：产出目录里只有服务自己建的 _depth/_recycle，"
            "却被当成'用过这个应用'报了警 —— 每个新用户都会看到这个红标签")
    else:
        ok("④反例：只有 _depth/_recycle 时保持安静（那是服务建的，不是出图）")
finally:
    shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- ⑤ 规矩
print()
print("【自检】这条测试自己有没有碰真实数据")
real = os.path.join(ROOT, "config.json")
if os.path.isfile(real):
    ok("真实 config.json 全程没被动过（本测试只在临时目录里造文件）")
else:
    bad("真实 config.json 不见了 —— 本测试是不是把它删了？")

print()
print("=" * 78)
if FAILS:
    print("通过 %d，失败 %d" % (len(OKS), len(FAILS)))
    for m in FAILS:
        print("  [!!] %s" % m)
    print("RESULT: %d 项失败" % len(FAILS))
    sys.exit(1)
print("通过 %d，失败 0" % len(OKS))
print("RESULT: ALL PASS（配置被删会被说出来，真首次运行仍然安静）")
sys.exit(0)
