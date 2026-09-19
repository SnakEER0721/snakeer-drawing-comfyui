# -*- coding: utf-8 -*-
"""install_models.py 的测试。

★ 这个脚本会**移动文件**，所以测试必须完全在沙箱里跑：
    · 假的 MODELS_DIR（临时目录）
    · 假的来源目录（临时目录）
    · 假的 config.json（临时目录）
  并且在开头和结尾都断言**真实的 config.json 和真实的 models 目录没被动过**
  —— 一个会搬文件的脚本，测试里碰了真数据就等于毁用户的东西。
  （这个项目以前真的发生过：测试里 os.remove 了用户的自定义项，永久丢失。）

跑法：
    python tests/test_install_models.py
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import paths  # noqa: E402
import install_models as IM  # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def fake(name, size):
    return (name, b"\0" * size)


def fake_st(name, payload=1024):
    """一个**结构合法**的 safetensors：8 字节头长 + JSON 头 + 数据。

    为什么测试的夹具也要长得像真的：检查内容之后（见 【8b】），随手拼的
    一块零字节会被正确地判成"没下完" —— 夹具不合格，测的就不是搬运流程了。
    """
    import json as _js
    import struct as _st
    hdr = _js.dumps({"w": {"dtype": "F32", "shape": [1, payload // 4],
                           "data_offsets": [0, payload]}}).encode()
    return (name, _st.pack("<Q", len(hdr)) + hdr + b"\0" * payload)


def truncated_st(name, declared=4 * 1024 * 1024, actual=64):
    """**真截断**：头里声明 declared 字节数据，实际只有 actual 字节。"""
    import json as _js
    import struct as _st
    hdr = _js.dumps({"w": {"dtype": "F32", "shape": [1024, 1024],
                           "data_offsets": [0, declared]}}).encode()
    return (name, _st.pack("<Q", len(hdr)) + hdr + b"\0" * actual)


def listing(root):
    """目录树快照：相对路径 -> 大小。用于断言"没被动过"。"""
    out = {}
    if not os.path.isdir(root):
        return out
    for dp, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            try:
                out[os.path.relpath(p, root).replace("\\", "/")] = os.path.getsize(p)
            except OSError:
                out[os.path.relpath(p, root).replace("\\", "/")] = -1
    return out


# ---------------------------------------------------------------- 真实环境快照
REAL_CFG = paths.CONFIG_PATH
REAL_MODELS = paths.MODELS_DIR
real_cfg_before = None
if os.path.isfile(REAL_CFG):
    with open(REAL_CFG, "rb") as fh:
        real_cfg_before = hashlib.sha256(fh.read()).hexdigest()
real_models_before = listing(REAL_MODELS)

print("=" * 74)
print("install_models.py 测试")
print("=" * 74)
print("真实 config.json : %s" % REAL_CFG)
print("真实 models 目录 : %s (%d 个文件)"
      % (REAL_MODELS, len(real_models_before)))

# ---------------------------------------------------------------- 清单一致性
print()
print("【1】清单与 server.py 的常量一致")
try:
    import server as S
    PAIRS = [
        ("RealESRGAN_x4plus_anime_6B.pth", S.UPSCALE_MODEL_DEFAULT),
        ("controlnet-scribble-sdxl.safetensors", S.CONTROLNET_SCRIBBLE),
        ("controlnet-openpose-sdxl.safetensors", S.CONTROLNET_OPENPOSE),
        ("controlnet-union-sdxl-xinsir.safetensors", S.CONTROLNET_UNION),
        ("ip-adapter-plus_sdxl_vit-h.safetensors", S.IPADAPTER_FILE),
        ("CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors", S.CLIP_VISION_FILE),
    ]
    names = {m[0] for m in IM.MODELS}
    for want, const in PAIRS:
        check(want in names and want == const,
              "%s 与 server.py 常量一致" % want)
except Exception as e:
    check(False, "导入 server.py 失败: %s: %s" % (type(e).__name__, e))

# ---------------------------------------------------------------- 沙箱
tmp = tempfile.mkdtemp(prefix="snakeer_imtest_")
try:
    MODELS = os.path.join(tmp, "models")
    SRC = os.path.join(tmp, "网盘下载")
    SRC2 = os.path.join(tmp, "另一份")
    os.makedirs(MODELS)
    os.makedirs(SRC)
    os.makedirs(SRC2)

    FILES = [
        fake_st("Illustrious-XL-v2.0.safetensors", 1024 * 1024),
        fake_st("controlnet-scribble-sdxl.safetensors", 1024 * 1024),
        fake("RealESRGAN_x4plus_anime_6B.pth", 1024 * 1024),
    ]
    for n, b in FILES:
        with open(os.path.join(SRC, n), "wb") as fh:
            fh.write(b)
    # 认不出来的文件：必须留在原地、必须被列出来
    UNKNOWN = fake_st("某个人的角色lora.safetensors", 512)
    with open(os.path.join(SRC, UNKNOWN[0]), "wb") as fh:
        fh.write(UNKNOWN[1])
    with open(os.path.join(SRC, "readme.txt"), "wb") as fh:
        fh.write(b"not a model")

    # ★ 给沙箱一份**缩小版清单**。理由：清单里底模写的是 6617 MB，而夹具
    #   只有 1 MB —— 检查内容之后它会（正确地）被判成"没下完"，
    #   于是这一节测不到搬运流程了。文件名/目录/必需标记照旧，只把期望体积改小。
    #   检查本身在【8b】单独验。
    REAL_MANIFEST = list(IM.MODELS)
    IM.MODELS = [(m[0], m[1], 1, m[3], m[4]) for m in REAL_MANIFEST]
    IM.BY_NAME = {m[0].lower(): m for m in IM.MODELS}

    CFG = os.path.join(tmp, "config.json")
    SANDBOX_CFG = {"_说明": ["沙箱"], "checkpoint": "旧的底模.safetensors"}
    with open(CFG, "w", encoding="utf-8") as fh:
        json.dump(SANDBOX_CFG, fh)

    # ---- 换掉路径（这是沙箱的关键一步）----
    #
    # ★ 不要用 paths.save_config() 来初始化沙箱配置：它会把 paths.CONFIG
    #   （import 时已经从**真实** config.json 读到的内容）合并进去写下来，
    #   于是临时 config 的 checkpoint 被真实底模名覆盖 —— 测试第一版就是这么
    #   自己把自己绊倒的（断言本身没错，是 setup 错了）。
    #   直接赋内存里的 CONFIG 就行。
    paths.MODELS_DIR = MODELS
    paths.CONFIG_PATH = CFG
    paths.CONFIG = dict(SANDBOX_CFG)

    def run(argv):
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["install_models.py"] + argv
        try:
            with redirect_stdout(buf):
                code = IM.main()
        finally:
            sys.argv = old
        return code, buf.getvalue()

    # ------------------------------------------------------------ 试运行
    print()
    print("【2】--dry 不能动任何文件")
    before = listing(SRC) | {"__models__" + k: v for k, v in listing(MODELS).items()}
    code, out = run(["--from", SRC, "--dry"])
    after = listing(SRC) | {"__models__" + k: v for k, v in listing(MODELS).items()}
    check(code == 0, "--dry 退出码 0（实际 %s）" % code)
    check(before == after, "--dry 前后文件树完全一致")
    check("试运行" in out, "输出里明确写了是试运行")
    cfg_now = json.load(io.open(CFG, encoding="utf-8"))
    check(cfg_now.get("checkpoint") == "旧的底模.safetensors",
          "--dry 没有改 config.json 的 checkpoint")

    # ------------------------------------------------------------ 真搬
    print()
    print("【3】真的搬（移动模式）")
    code, out = run(["--from", SRC, "--yes"])
    check(code == 0, "退出码 0（实际 %s）" % code)
    for n, b in FILES:
        sub = IM.BY_NAME[n.lower()][1]
        dst = os.path.join(MODELS, sub, n)
        check(os.path.isfile(dst), "%s 进了 models\\%s\\" % (n, sub))
        if os.path.isfile(dst):
            check(os.path.getsize(dst) == len(b), "  %s 大小不变 (%d)" % (n, len(b)))
        check(not os.path.exists(os.path.join(SRC, n)), "  %s 已从来源移走" % n)

    check(os.path.isfile(os.path.join(SRC, UNKNOWN[0])),
          "认不出的 lora 没被碰过")
    check(os.path.isfile(os.path.join(SRC, "readme.txt")),
          "非模型文件没被碰过")
    check(UNKNOWN[0] in out, "输出里列出了认不出的 lora")
    check("清单里没有" in out, "输出里解释了为什么不动它")

    # ------------------------------------------------------------ 配置
    print()
    print("【4】config.json 的 checkpoint 被改成实际装进去的底模")
    cfg_now = json.load(io.open(CFG, encoding="utf-8"))
    check(cfg_now.get("checkpoint") == "Illustrious-XL-v2.0.safetensors",
          "checkpoint = %s" % cfg_now.get("checkpoint"))
    check(cfg_now.get("_说明") == ["沙箱"], "其它键没被动过")

    # ------------------------------------------------------------ 幂等
    print()
    print("【5】重跑一次不能出错、不能重复搬")
    snap = listing(MODELS)
    code, out = run(["--from", SRC, "--yes"])
    check(code == 0, "重跑退出码 0（实际 %s）" % code)
    check(listing(MODELS) == snap, "models 目录没有变化")
    check("已经装好" in out, "重跑时说清了「已经装好」，而不是「没找到」")

    # 来源里再放一份同样的文件（模拟网盘下了两次 / 上次没删原件）
    with open(os.path.join(SRC, FILES[1][0]), "wb") as fh:
        fh.write(FILES[1][1])
    code, out = run(["--from", SRC, "--yes"])
    check(code == 0, "来源里有重复文件时退出码 0（实际 %s）" % code)
    check(listing(MODELS) == snap, "重复文件没有把已装好的覆盖/弄乱")
    check(os.path.isfile(os.path.join(SRC, FILES[1][0])),
          "重复的那份留在来源里没被乱搬")

    # ------------------------------------------------------------ 复制模式
    print()
    print("【6】--copy 保留原件")
    # 内容必须是**合格的** —— 999 字节的假底模现在会被正确地拒收（见【8b】），
    # 那样这一节测的就不是复制模式了。目标目录是全新的，所以内容一样也会搬。
    with open(os.path.join(SRC2, FILES[0][0]), "wb") as fh:
        fh.write(fake_st(FILES[0][0], 1024 * 1024)[1])
    far = os.path.join(tmp, "models2")
    os.makedirs(far)
    paths.MODELS_DIR = far
    code, out = run(["--from", SRC2, "--yes", "--copy"])
    check(code == 0, "退出码 0（实际 %s）" % code)
    check(os.path.isfile(os.path.join(SRC2, FILES[0][0])), "--copy 保留来源文件")
    check(os.path.isfile(os.path.join(far, "checkpoints", FILES[0][0])),
          "--copy 目标文件存在")

    # ------------------------------------------------------------ 什么都找不到
    print()
    print("【7】来源是空的、目标也是空的 —— 才该报「没找到」")
    #
    # ★ 必须换成一个空的 models 目录。用上面那个已经装好的目录时，脚本会
    #   走「已经装好了」那条分支并退 0 —— 那是**正确**行为（见【5】），
    #   但就测不到「真的什么都没找到」这条路径了。
    EMPTY = os.path.join(tmp, "空的")
    FRESH = os.path.join(tmp, "models_empty")
    os.makedirs(EMPTY)
    os.makedirs(FRESH)
    paths.MODELS_DIR = FRESH
    code, out = run(["--from", EMPTY, "--yes"])
    check(code == 1, "退出码 1（实际 %s）" % code)
    check("没有找到" in out, "明说了没找到")
    check(IM.DROP_DIR in out, "告诉了用户该放进「%s」" % IM.DROP_DIR)
    check("Illustrious-XL-v2.0.safetensors" in out, "列出了清单文件名")

    # ------------------------------------------------------------ 已装好
    print()
    print("【7b】装完之后目标是满的、来源是空的 —— 该说「已经装好了」并退 0")
    paths.MODELS_DIR = MODELS
    code, out = run(["--from", EMPTY, "--yes"])
    check(code == 0, "退出码 0（实际 %s）" % code)
    check("已经装好" in out, "明说了已经装好")
    check("Illustrious-XL-v2.0.safetensors" in out, "列出了已装的是哪些")
    check("没有找到" not in out, "不再说「没有找到」（那会让用户以为装坏了）")

    # ------------------------------------------------------------ 目标不存在
    print()
    print("【8】models 目录不存在时要停下并说明怎么修")
    paths.MODELS_DIR = os.path.join(tmp, "根本没有这个目录")
    code, out = run(["--from", SRC, "--yes"])
    check(code == 2, "退出码 2（实际 %s）" % code)
    check("不存在" in out and "models_dir" in out, "给出了 models_dir 的改法")

    # ------------------------------------------------------------ 内容检查
    print()
    print("【8b】搬之前的内容检查：坏文件不许装，好文件必须放行")
    # 背景（实测，见 dev/probe_model_files.py）：把一个 **171 字节**的文件命名成
    # 清单里的底模（约 6617 MB）放进投放目录，脚本原来报「完成 0 KB」、
    # 把它搬进 models\checkpoints\，**还把 config.json 指到它身上**，退出码 0。
    # 一张网盘的 HTML 错误页存成 .safetensors 也一样照搬。
    # BY_NAME 的键是**小写**的（见 install_models.BY_NAME），查表要 .lower()
    CKPT_ENTRY = IM.BY_NAME["illustrious-xl-v2.0.safetensors"]
    BAD = os.path.join(tmp, "坏文件")
    os.makedirs(BAD, exist_ok=True)

    def put(name, data):
        p = os.path.join(BAD, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    cases = [
        ("没下完的底模（头里声明 4 MB，实际 64 字节）",
         put("Illustrious-XL-v2.0.safetensors",
             truncated_st("x")[1]), True, "没下完"),
        ("网盘的 HTML 错误页被存成 .safetensors",
         put("controlnet-scribble-sdxl.safetensors",
             "<!DOCTYPE html><html>链接不存在</html>".encode("utf-8")),
         True, "网页"),
        ("0 字节的空文件",
         put("RealESRGAN_x4plus_anime_6B.pth", b""), True, "空"),
    ]
    for label, path, want_bad, needle in cases:
        fn = os.path.basename(path)
        entry = IM.BY_NAME.get(fn.lower(), CKPT_ENTRY)
        why = IM.file_problem(fn, path, entry)
        check(bool(why) == want_bad and (needle in why if want_bad else True),
              "%s -> %s" % (label, why or "（放行）"))

    # 对照组：**合格**的文件必须放行（把好文件也挡住的检查比没有检查更糟）
    good = put("RealESRGAN_x4plus_anime_6B.pth", b"\0" * (1024 * 1024))
    why = IM.file_problem("RealESRGAN_x4plus_anime_6B.pth", good,
                          IM.BY_NAME["realesrgan_x4plus_anime_6b.pth"])
    check(not why, "尺寸达标的文件放行（%s）" % (why or "通过"))

    # 对照组：本机**真实**装好的模型（如果装了）必须全部放行 —— 这一条防的是
    # "检查太严、把正常安装也挡住"。三种大小都过一遍。
    real_ok, real_bad = 0, []
    for m in REAL_MANIFEST:
        p = os.path.join(REAL_MODELS, m[1], m[0])
        if not os.path.isfile(p):
            continue
        real_ok += 1
        w = IM.file_problem(m[0], p, m)
        if w:
            real_bad.append("%s: %s" % (m[0], w))
    if real_ok:
        check(not real_bad, "本机 %d 个真实模型文件全部放行%s"
              % (real_ok, "" if not real_bad else "（误报：%s）" % real_bad))
    else:
        print("  [跳过] 本机一个清单里的模型都没装，这一条没验到")

    # 端到端：坏文件放进投放目录 -> 不许搬、退出码非 0、并且说清为什么
    print()
    print("【8c】端到端：坏文件不许进 models 目录")
    # ★ 必须把 models 目录指回来 —— 【8】把它指到了一个**不存在**的目录，
    #   不指回来的话脚本会在"目录不存在"那里就退出（退出码 2），
    #   根本走不到内容检查这一步，于是这一节测了个寂寞。
    paths.MODELS_DIR = MODELS
    for name, data in (("Illustrious-XL-v2.0.safetensors",
                        truncated_st("x")[1]),):
        put(name, data)
    before = listing(MODELS)
    code, out = run(["--from", BAD, "--yes"])
    check(code != 0, "退出码非 0（实际 %s）" % code)
    check("不会装" in out, "明说了「不会装」")
    check("没下完" in out or "网页" in out, "说清了为什么（下没下完/是不是网页）")
    check(listing(MODELS) == before, "一个坏文件都没进 models 目录")
    check("重新下载" in out, "给了下一步：重新下载")

finally:
    IM.MODELS = REAL_MANIFEST
    IM.BY_NAME = {m[0].lower(): m for m in REAL_MANIFEST}
    paths.MODELS_DIR = REAL_MODELS
    paths.CONFIG_PATH = REAL_CFG
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- 没碰真数据
print()
print("【9】真实数据没被动过（最重要的一条）")
if real_cfg_before is not None:
    with open(REAL_CFG, "rb") as fh:
        now = hashlib.sha256(fh.read()).hexdigest()
    check(now == real_cfg_before, "真实 config.json 逐字节未变")
else:
    print("  [SKIP] 没有真实 config.json")
check(listing(REAL_MODELS) == real_models_before, "真实 models 目录未变")

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m in FAILS:
        print("  FAIL %s" % m)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
