# -*- coding: utf-8 -*-
"""config.json 不见了 —— 那句话**真的走到界面上**了吗？（端到端）

★ 为什么光有 test_config_missing_warns.py 不够：
   那条测的是 paths.py 的**逻辑**——它在临时目录里摆好文件，然后自己 import
   paths 去问 CONFIG_ERROR 是什么。逻辑全对，**接线断掉它照样全绿**：

     · server.py 没在启动时调 `note_config_gone_if_used()`（比如有人重构 main），
     · 或者接口读的是 `from paths import CONFIG_ERROR` 抄下来的那个旧值
       （import 期算不出这条判据，所以抄下来永远是 None）。

   这两种断法都不会让那条单测变红 —— 因为它压根没经过 server.py。
   而后果正是原来那个 bug 本身：**用户界面上一个提示都没有**。

所以这条从**临时目录里起一个真服务**，真的去敲 `/api/capabilities`，
断言那句话确实出现在响应里，并且 ui.html 认得这个字段。

规矩：真实 config.json 一个字节都不碰（全程在临时目录里跑）。
"""
import sys

sys.dont_write_bytecode = True

import io          # noqa: E402
import json        # noqa: E402
import os          # noqa: E402
import shutil      # noqa: E402
import subprocess  # noqa: E402
import tempfile    # noqa: E402
import time        # noqa: E402
import urllib.request  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8871

FAILS = []
PASSES = []


def ok(m):
    PASSES.append(m)
    print("   [OK]   %s" % m)


def bad(m):
    FAILS.append(m)
    print("   [!!]   %s" % m)


def note(m):
    print("   ·  %s" % m)


print("=" * 78)
print("== config.json 不见了，界面上真的会说出来吗（端到端）")
print("=" * 78)
print("   应用目录: %s" % ROOT)

# 真实文件的指纹 —— 跑完再比一次。这条测试不该碰它。
REAL_CFG = os.path.join(ROOT, "config.json")


def fp(p):
    if not os.path.isfile(p):
        return None
    with io.open(p, "rb") as fh:
        raw = fh.read()
    return (len(raw), abs(hash(raw)))


BEFORE = fp(REAL_CFG)
print("   真实 config.json: %s（指纹 %s）" % (REAL_CFG, BEFORE))

TMP = tempfile.mkdtemp(prefix="cfggone_e2e_")
APP = os.path.join(TMP, "app")
FAKEOUT = os.path.join(TMP, "fakeout")
proc = None

try:
    # --------- 摆一个"配置没了但用过这个应用"的安装 ---------
    # 只复制服务跑起来需要的那几个文件。**故意不复制 config.json** ——
    # 这就是被测的场景。真正跑起来要的模块不止这几个，少了的会报 ImportError，
    # 那种红是测试自己摆错了，不是产品问题。
    os.makedirs(APP)
    for fn in ("server.py", "paths.py", "ui.html", "cn_translate.py",
               "poisson_blend.py", "VERSION", "config.example.json"):
        src = os.path.join(ROOT, fn)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(APP, fn))
        else:
            note("%s 不在源头，跳过（不影响这条判据）" % fn)

    if os.path.isfile(os.path.join(APP, "config.json")):
        bad("测试自己摆错了：拷贝出来的应用目录里居然有 config.json")
    else:
        ok("临时应用目录里**没有** config.json（正是被测场景）")

    # 产出目录里造一个"出过图"的日期文件夹 —— 这是那份最硬的物证
    os.makedirs(os.path.join(FAKEOUT, "anime", "2026-09-21"))
    ok("假产出目录里造了出图记录 2026-09-21")

    env = dict(os.environ)
    env["COMFYUI_OUTPUT"] = FAKEOUT
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", "server.py",
         "--port", str(PORT), "--no-browser"],
        cwd=APP, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    base = "http://127.0.0.1:%d" % PORT

    def get(path):
        with urllib.request.urlopen(base + path, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    caps = None
    for _ in range(80):
        if proc.poll() is not None:
            break
        try:
            caps = get("/api/capabilities")
            break
        except Exception:
            time.sleep(0.5)

    if caps is None:
        out = b""
        err = b""
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            pass
        bad("临时服务没起来（exit=%s）\n       stdout: %s\n       stderr: %s"
            % (proc.poll(), out.decode("utf-8", "replace")[-500:],
               err.decode("utf-8", "replace")[-800:]))
    else:
        ok("临时服务起来了（端口 %d）" % PORT)
        d = caps.get("data") or {}
        ce = d.get("config_error")
        ck = d.get("checkpoint")
        if ce:
            ok("接口把原因送出来了：%s…" % ce[:60])
        else:
            bad("接口的 config_error 是空的 —— 服务端算出来了、但没送到接口上。"
                "最可能的原因：接口读的是 `from paths import CONFIG_ERROR` "
                "抄下来的那个旧值（import 期这条判据还算不出来）")
        if ce and "config.json" in ce:
            ok("提示点名了是 config.json 不见了")
        if ce and "安装模型.bat" in ce:
            ok("提示给出了修法（双击「安装模型.bat」）")
        if ce and ck and ck in ce:
            ok("提示里写的底模名 %r 和实际在用的那个对得上" % ck)
        else:
            bad("提示里没写清现在实际在用哪个底模（提示说的是 %r，"
                "而接口报的 checkpoint 是 %r）—— 用户得一眼看出"
                "用的不是他挑的那个" % (ce, ck))

        # 界面真的认得这个字段吗？界面不认的话后端送出来也没人看。
        ui = io.open(os.path.join(APP, "ui.html"), encoding="utf-8").read()
        if "config_error" in ui:
            ok("ui.html 里确实读 config_error 这个字段")
        else:
            bad("ui.html 里没有 config_error —— 后端送了，界面不会显示")

    # --------- 反例：真首次运行**必须安静** ---------
    # 换个空的产出目录重启一次。没有这条，上面全绿只证明"会说话"，
    # 证明不了"该安静的时候安静"—— 而后者才是新用户会不会被误吓的问题。
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()

    EMPTYOUT = os.path.join(TMP, "emptyout")
    os.makedirs(os.path.join(EMPTYOUT, "anime", "_depth"))
    env2 = dict(env)
    env2["COMFYUI_OUTPUT"] = EMPTYOUT
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", "server.py",
         "--port", str(PORT), "--no-browser"],
        cwd=APP, env=env2,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    caps2 = None
    for _ in range(80):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base + "/api/capabilities", timeout=60) as r:
                caps2 = json.loads(r.read().decode("utf-8", "replace"))
            break
        except Exception:
            time.sleep(0.5)

    if caps2 is None:
        bad("反例：临时服务没起来")
    else:
        d2 = caps2.get("data") or {}
        if d2.get("config_error") is None:
            ok("反例：真首次运行（产出目录里只有 _depth）→ 不出声")
        else:
            bad("反例：真首次运行却报警了，新用户一开 app 就看到红标签：%s"
                % str(d2.get("config_error"))[:80])

finally:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
    shutil.rmtree(TMP, ignore_errors=True)

# ---------------------------------------------------------------- 自检
print()
print("【自检】这条测试自己有没有碰真实数据")
AFTER = fp(REAL_CFG)
if AFTER == BEFORE:
    ok("真实 config.json 全程没被动过（指纹 %s → %s）" % (BEFORE, AFTER))
else:
    bad("真实 config.json 被改了！指纹 %s → %s" % (BEFORE, AFTER))

leftover = [d for d in os.listdir(tempfile.gettempdir())
            if d.startswith("cfggone_e2e_")]
if not leftover:
    ok("临时目录清干净了")
else:
    bad("临时目录还在：%s" % leftover)

print()
print("=" * 78)
if FAILS:
    print("通过 %d，失败 %d" % (len(PASSES), len(FAILS)))
    for f in FAILS:
        print("   [!!] %s" % f)
    print("RESULT: %d 项失败" % len(FAILS))
    sys.exit(1)
print("RESULT: ALL PASS（config.json 不见了会一路走到界面上，首次运行仍然安静）")
sys.exit(0)
