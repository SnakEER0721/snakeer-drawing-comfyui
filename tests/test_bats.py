# -*- coding: utf-8 -*-
"""两个 .bat 的测试。

为什么要测 .bat（一般项目不测这个）：
    「找 Python」那段是本项目最容易静默失败的一处。Windows 自带一个微软
    商店的 python.exe 占位程序，`where python` 会找到它，但它什么都不干 ——
    bat 会判定"找到了"，然后启动失败、窗口一闪而过。这种失败**没有任何报错**，
    用户只会说"双击没反应"。

    所以这里把 bat 里那段找 Python 的代码**真的拿出来在 cmd 里跑一遍**，
    确认它要么选出一个能用的解释器，要么给出可照做的提示。

跑法：
    python tests/test_bats.py
"""
import io
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def read_gbk(name):
    with io.open(os.path.join(APP, name), "rb") as fh:
        raw = fh.read()
    return raw, raw.decode("gbk")


print("=" * 74)
print("两个 .bat 的测试")
print("=" * 74)

BATS = ["启动UI.bat", "安装模型.bat"]

# ---------------------------------------------------------------- 编码
print()
print("【1】编码必须是 GBK + CRLF")
for name in BATS:
    p = os.path.join(APP, name)
    if not os.path.isfile(p):
        check(False, "%s 存在" % name)
        continue
    raw, text = read_gbk(name)
    check(True, "%s 存在（%d 字节）" % (name, len(raw)))
    check(b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"),
          "  %s 全部是 CRLF（没有裸 LF —— cmd 会被裸 LF 弄出奇怪的错）" % name)
    check(b"\xef\xbb\xbf" not in raw, "  %s 没有 UTF-8 BOM" % name)
    check(not raw.startswith(b"@echo off \xef"), "  %s 开头是干净的 @echo off" % name)

# ---------------------------------------------------------------- 不许写死
print()
print("【2】不许写死路径 / 端口 / Python 位置")
for name in BATS:
    _raw, text = read_gbk(name)
    # ★ 只看代码行，跳过 rem 注释。
    #   第一版没跳，于是把注释里为了说明而写的
    #       rem   C:\Users\<你>\AppData\...\WindowsApps\python.exe
    #   和
    #       rem   没有就从 8765 开始往上找
    #   当成了"硬编码"报出来 —— 检查工具自己没验证，正是纪律清单里那一条。
    code = "\n".join(l for l in text.splitlines()
                     if not l.strip().lower().startswith("rem"))
    bad = []
    if re.search(r"[A-Za-z]:\\", code):
        bad.append("硬编码盘符路径")
    if re.search(r"\b87\d\d\b", code):
        bad.append("硬编码端口号")
    for pat, why in ((r"python\.exe", "写死 python.exe 路径"),
                     (r"Python3\d\d", "写死 Python 安装目录")):
        if re.search(pat, code):
            bad.append(why)
    check(not bad, "%s 的代码里没有%s"
          % (name, "、".join(bad) if bad else "硬编码"))
    # 反向自检：确认这个检查真的能抓到硬编码（否则它可能只是永远通过）
    #
    # ★ 样例路径用 sys.executable 拼，不在源码里写任何盘符开头的字面量 ——
    #   tests/ 自己也被 test_path_hygiene 扫，源码里出现 "X:\\..." 会让那边报红。
    #   sys.executable 本来就是绝对路径，拿它当样例既真实又不留字面量。
    fake = 'set "SRV=%s"\npython.exe -c "x"\n' % sys.executable
    caught = bool(re.search(r"[A-Za-z]:\\", fake)
                  and re.search(r"python\.exe", fake))
    check(caught, "  自检：这个检查确实能抓到写死的路径")

# ---------------------------------------------------------------- 找 Python
print()
print("【3】「找 Python」那段在真 cmd 里跑得通（最关键的一条）")
src_path = os.path.join(APP, "dev", "make_bats.py")
if not os.path.isfile(src_path):
    # dev/ 不进发布包。发布目录里跑时这个文件不存在 —— 上面【1】【2】
    # 该查的都查过了（编码、换行、不写死），这里跳过就好，不是失败。
    print()
    print("【3】[跳过] 没有 dev/make_bats.py（发布包里本来就没有 dev/）")
    print("     上面【1】【2】已经检查过两个 .bat 的编码、换行和硬编码。")
    FIND = None
else:
    src = io.open(src_path, encoding="utf-8").read()
    m = re.search(r"FIND_PY = r'''(.*?)'''", src, re.S)
    if not m:
        check(False, "从 dev/make_bats.py 里提取到 FIND_PY 片段")
        FIND = None
    else:
        FIND = m.group(1)
        check(True, "提取到 FIND_PY 片段（%d 字符）" % len(FIND))

# 这三条直接看 .bat 自己的内容，不需要 dev/ —— 发布目录里也该照常检查
for name in BATS:
    _raw, text = read_gbk(name)
    check("if not defined PY" in text,
          "  %s 里有找 Python 的逻辑" % name)
    check("WindowsApps" in text,
          "  %s 的注释里写清了为什么不能用 where python" % name)
    check("sys.version_info>=(3,10)" in text,
          "  %s 顺带验了 Python 版本 >= 3.10" % name)

if FIND:
    tmp = tempfile.mkdtemp(prefix="snakeer_bat_")
    probe = os.path.join(tmp, "probe.bat")
    # 只跑"找 Python"那段，然后把结果打出来。
    #
    # ★ 必须把换行统一成 CRLF 再写。
    #   FIND 是从 make_bats.py（LF 文件）里正则抠出来的，里面是裸 LF；
    #   直接把裸 LF 的文本写成 .bat，cmd 会解析错乱 —— 报出来的是
    #   'defined' is not recognized / 'else was unexpected' 这类**和代码内容
    #   毫无关系**的错，害我查了三轮才定位到是探针文件本身的问题。
    #   （真正的产品 .bat 是 make_bats.py 写的，那边做了 CRLF 归一化，没问题。）
    text = ("@echo off\nsetlocal\nchcp 936 >nul\n" + FIND
            + "\necho RESULT=[%PY%]\nendlocal\n")
    body = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("gbk", "replace")
    io.open(probe, "wb").write(body)
    check(body.count(b"\n") == body.count(b"\r\n"),
          "  探针 .bat 全部是 CRLF（裸 LF 会让 cmd 解析错乱）")
    try:
        # ★ 必须在 "chcp 936" 之下跑，否则会得到假的失败。
        #
        #   实测（dev/dbg_enc.py、dev/dbg_env.py，以及一次独立控制台的对照）：
        #     · 本机注册表 ACP=OEMCP=936，**双击 .bat 时控制台就是 936**，
        #       GBK 的中文完全正常；
        #     · 但 pwsh / 这个 agent 会话把 [Console]::OutputEncoding 设成了
        #       utf-8，于是从 Python 的 subprocess 里调 cmd，cmd 会拿 UTF-8
        #       去读 GBK 的 .bat —— 中文全变成 U+FFFD，用 GBK 解就是"锟斤拷"。
        #     · 对照实验：裸跑=乱码；先 chcp 936=正确；先 chcp 65001=乱码；
        #       **新开一个独立控制台跑=正确**。
        #   结论：GBK 是对的，是本测试环境的控制台被设成了 UTF-8。
        #   所以这里显式 chcp 936，还原真实用户的控制台。
        #
        #   另外，用 "cmd /c call <bat>" 而不是 "cmd /c <bat>"：后者是
        #   subprocess 特有的一种调用形式，实测更容易踩上面的编码坑。
        CHCP = ""   # 已在探针 bat 内部 chcp 936，这里不用再包一层（引号会打架）
        r = subprocess.run(["cmd", "/c", "call", probe],
                           capture_output=True, timeout=90)
        out = r.stdout.decode("gbk", "replace")
        print("      cmd 输出: %s" % out.strip().replace("\n", " | "))
        mm = re.search(r"RESULT=\[(.*?)\]", out)
        got = mm.group(1).strip() if mm else ""
        check(got in ("python", "py"),
              "选出的解释器是 %r（必须是 python 或 py）" % got)
        if got:
            # 选出来的那个必须真的能跑 —— 这是整个测试的核心
            v = subprocess.run([got, "-c",
                                "import sys;print(sys.version_info[:2])"],
                               capture_output=True, timeout=60)
            vout = v.stdout.decode("utf-8", "replace").strip()
            check(v.returncode == 0 and "(" in vout,
                  "  %r 真的能执行（版本 %s）" % (got, vout))
            check(vout in ("(3, 10)", "(3, 11)", "(3, 12)", "(3, 13)", "(3, 14)"),
                  "  版本 >= 3.10（实际 %s）" % vout)

        # 反例：故意把 PATH 换成"只有一个假的 python"，看它会不会误判
        fake = os.path.join(tmp, "fakebin")
        os.makedirs(fake, exist_ok=True)
        # 一个"存在但什么都不干"的 python.bat —— 模拟微软商店那个占位程序
        io.open(os.path.join(fake, "python.bat"), "wb").write(
            b"@echo off\r\nexit /b 9009\r\n")
        io.open(os.path.join(fake, "py.bat"), "wb").write(
            b"@echo off\r\nexit /b 9009\r\n")
        env = dict(os.environ)
        env["PATH"] = fake + os.pathsep + r"C:\Windows\System32"
        r2 = subprocess.run(["cmd", "/c", "call", probe],
                            capture_output=True, timeout=90, env=env)
        out2 = r2.stdout.decode("gbk", "replace")
        mm2 = re.search(r"RESULT=\[(.*?)\]", out2)
        got2 = (mm2.group(1).strip() if mm2 else "")
        check(got2 == "",
              "  占位程序骗不过它（选了 %r，应该是空的）" % got2)
        # 这条同时验证了 bat 里的中文 cmd 能正确读出来（GBK 编码是对的）
        check("没有找到可用的 Python" in out2,
              "  而且给出了可照做的提示（中文没有乱码）")
        if "没有找到可用的 Python" not in out2:
            print("      stdout(gbk) = %r" % out2[:200])
        check(r2.returncode != 0,
              "  退出码非 0（实际 %s）" % r2.returncode)
    except subprocess.TimeoutExpired:
        check(False, "cmd 探测超时（bat 里可能卡住了）")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 拖放
print()
print("【4】安装模型.bat 要能把拖进来的路径转给脚本")
_raw, ins = read_gbk("安装模型.bat")
check("%*" in ins, "命令行里带了 %*（拖放的路径才能传进去）")
check("install_models.py" in ins, "调用的是 install_models.py")
check("刚需模型全部放这" in ins, "提示里写明了该把文件放哪个文件夹")
check("-X utf8" in ins, "带 -X utf8（否则中文输出在 GBK 控制台下会炸）")

_raw, lch = read_gbk("启动UI.bat")
check("-X utf8" in lch, "启动UI.bat 也带 -X utf8")
check("server.py" in lch, "启动UI.bat 调用的是 server.py")
check("%~dp0" in lch, "启动UI.bat 用 %~dp0 定位，不写死目录")

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m2 in FAILS:
        print("  FAIL %s" % m2)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
