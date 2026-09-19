"""环境自检 —— 移植到新电脑后先跑这个。

它检查运行所需的每一项，并按「必须 / 建议」分级报出结果，缺什么、放哪里、
怎么修都会写清楚。不会修改任何文件。

用法：
    python check_env.py
"""
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAIL = []
WARN = []
OK = []
# 【4】里连上 ComfyUI 之后会置 True。下面【5】要靠它区分两种"读不到底模列表"：
#   · ComfyUI 没连上  -> 上面【4】已经报过了，这里不用重复吓人
#   · ComfyUI 在线但列表是空的 -> 一个底模都没装，这是**真的用不了**，必须报缺失
COMFY_ONLINE = False


def ok(msg):
    OK.append(msg)
    print("  [OK]   %s" % msg)


def warn(msg, fix=""):
    WARN.append((msg, fix))
    print("  [警告] %s" % msg)
    if fix:
        print("         → %s" % fix)


def fail(msg, fix=""):
    FAIL.append((msg, fix))
    print("  [缺失] %s" % msg)
    if fix:
        print("         → %s" % fix)


print("=" * 78)
print("Snakeer Drawing —— 环境自检")
print("=" * 78)

# ---------------------------------------------------------------- Python
print()
print("【1】Python 与依赖")
print("  本机 Python: %s (%s)" % (sys.version.split()[0], sys.executable))
if sys.version_info >= (3, 10):
    ok("Python 版本 >= 3.10")
else:
    fail("Python 版本过低（需要 3.10+）", "装 Python 3.12 后重试")

NEED = {
    "numpy": "pip install numpy",
    "cv2": "pip install opencv-python",
    "PIL": "pip install Pillow",
    "sqlite3": "（Python 自带，没问题说明解释器异常）",
    "urllib.request": "（标准库）",
}
for mod, fix in NEED.items():
    try:
        m = importlib.import_module(mod)
        v = getattr(m, "__version__", "")
        ok("依赖 %-16s %s" % (mod, v))
    except ImportError:
        if mod in ("sqlite3", "urllib.request"):
            fail("依赖 %s 不可用" % mod, fix)
        else:
            fail("缺少依赖 %s" % mod, fix)

# ---------------------------------------------------------------- config
print()
print("【2】路径配置")
cfg_path = os.path.join(HERE, "config.json")
cfg = {}
cfg_ok = False
if os.path.isfile(cfg_path):
    try:
        # ★ utf-8-sig，和 paths._load_config 保持一致：带 BOM 的 UTF-8 也要认。
        #   用 encoding="utf-8" 的话，编辑器存了 BOM 时这里会报"无法解析"，
        #   而应用其实**能**读它（paths.py 用的就是 utf-8-sig）—— 自检和应用
        #   说法不一致，用户只会更糊。
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        ok("找到 config.json")
        cfg_ok = isinstance(cfg, dict)
    except Exception as e:
        fail("config.json 无法解析: %s" % e,
             "常见原因是多/少一个逗号、少一个引号（反斜杠要写成 \\\\）。"
             "现在用的是自动探测的目录 —— 你在这里填的路径**没有生效**")
else:
    warn("没有 config.json", "会走自动探测；建议复制一份 config.example.json 改名")

try:
    # ★ paths 要单独 import：`server` 只是把 paths 的值搬过去用，**没有**把
    #   DIR_SOURCE 转出去。实测踩过 —— 这里写成 `S.DIR_SOURCE` 时
    #   getattr 一路拿到默认值 ""，于是来源说明静默地一个字都不打，
    #   而"路径是哪来的"正是这段输出**唯一想说的事**。
    #   不写 getattr(S, "DIR_SOURCE", {}) 那种兜底：那样它永远不会报错。
    import paths as P
    import server as S
except Exception as e:
    print()
    fail("无法导入 server.py: %s: %s" % (type(e).__name__, e),
         "先解决上面的依赖问题")
    print()
    print("=" * 78)
    print("自检中止：应用本体加载失败")
    print("=" * 78)
    sys.exit(2)

# ★ config.json 是合法 JSON、但最外层不是 { } 时，上面那段 `json.load` 不会报错，
#   而 paths._load_config 会静默丢掉整份配置。这一支原来**任何地方都不出声**，
#   所以在这里补一次（用 paths 的判断，不另写一套判据）。
if cfg_ok and getattr(S, "CONFIG_ERROR", None):
    fail("config.json 没被用上: %s" % S.CONFIG_ERROR,
         "把它删掉再跑一次「安装模型.bat」，会得到一个带说明的正确模板")

for label, path, need_write, level in (
        ("ComfyUI input 目录", S.COMFY_INPUT, True, "fail"),
        ("输出目录", S.COMFY_OUTPUT, True, "fail"),
        # ★ 这两个是**警告**不是缺失。实测（dev/audit_check_env.py）：
        #   LoRA 目录不存在时原来报「缺失」，于是自检说「必须先解决这些才能正常
        #   使用」并退 1 —— 而**没有 LoRA 完全能出图**，README 还明说本项目
        #   不附带任何 LoRA。用户会以为整个装坏了。
        ("models 根目录", S.MODELS_DIR, False, "warn"),
        ("LoRA 目录", S.LORA_DIR, False, "warn")):
    # ★ 把"这个路径是哪来的"一起打出来。探测不到时它会静默退回应用目录下的
    #   兜底，用户只看到"路径不对"，不知道**是哪一层给的**、该去改哪里。
    #   自动探测失败时还要告诉他探测过哪些地方、去哪儿看真答案。
    src = P.DIR_SOURCE.get(label, "")
    hint_src = "（%s）" % src if src else ""
    if os.path.isdir(path):
        ok("%s 存在: %s%s" % (label, path, hint_src))
        if need_write:
            probe = os.path.join(path, "_write_test.tmp")
            try:
                with open(probe, "w") as f:
                    f.write("x")
                os.remove(probe)
            except Exception as e:
                fail("%s 不可写: %s" % (label, e), "检查权限或换个目录")
    elif level == "fail":
        if src.startswith("兜底"):
            fail("%s 不存在: %s%s" % (label, path, hint_src),
                 "自动探测没找到 ComfyUI 的目录，于是退回了软件自己的目录，"
                 "而那个目录不存在。最省事的办法：启动一次 ComfyUI 桌面版，"
                 "它会把自己用的目录写进 %%APPDATA%%\\Comfy Desktop\\settings.json，"
                 "再跑一次本自检就有了；或者直接在 config.json 里填对路径")
        else:
            fail("%s 不存在: %s%s" % (label, path, hint_src),
                 "改 config.json 指向正确位置，或先启动一次 ComfyUI 让它创建")
    else:
        warn("%s 不存在: %s%s" % (label, path, hint_src),
             "出图不受影响，但相关功能会缺（LoRA 面板会是空的）。"
             "改 config.json 指向正确位置，或先启动一次 ComfyUI 让它创建")

# ---------------------------------------------------------------- vocab
print()
print("【3】词库")
try:
    import cn_translate as CT
    ok("词库文件: %s" % CT._DB_PATH)
    tr = CT.Translator()
    if getattr(tr, "db_missing", False):
        fail("词库无法打开，已退化为内置词表",
             "把 danbooru_tags.sqlite3 放到 %s 或用 COMEDY_VOCAB_DB 指定路径"
             % os.path.join(HERE, "data"))
    else:
        n = tr.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        ok("词库可用，%d 条，反向索引 %d 条" % (n, len(tr.reverse)))
        probes = ["少女", "手", "乳头", "阴道", "修手"]
        miss = [p for p in probes if not tr.lookup(p)]
        if miss:
            warn("这些常用词查不到: %s" % " ".join(miss))
        else:
            ok("常用词抽查通过")
except Exception as e:
    fail("词库模块异常: %s: %s" % (type(e).__name__, e))

# ---------------------------------------------------------------- comfy
print()
print("【4】ComfyUI 连接")
import urllib.error
import urllib.request


def comfy_get(path, timeout=15):
    with urllib.request.urlopen(S.COMFY + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


try:
    stats = comfy_get("/system_stats")
    COMFY_ONLINE = True
    ok("ComfyUI 在线: %s" % S.COMFY)
    # 版本要打出来：下面"缺节点"的改法里有一条是「升级 ComfyUI」，
    # 不告诉他现在是什么版本，他没法判断要不要升。
    ver = (stats.get("system") or {}).get("comfyui_version")
    if ver:
        ok("ComfyUI 版本 %s" % ver)
    for d in (stats.get("devices") or []):
        free = d.get("vram_free") or 0
        total = d.get("vram_total") or 0
        ok("显卡 %s  显存 %.1f/%.1f GB 可用"
           % (d.get("name", "?"), free / 1024**3, total / 1024**3))
    try:
        oi = comfy_get("/object_info", timeout=120)
        ok("能读到节点列表，共 %d 个节点" % len(oi))
    except Exception as e:
        warn("读不到 object_info: %s" % e)
        oi = {}
    # 三个字段：节点名、它是干什么的、**去哪儿装**。
    #   README 承诺 check_env「能直接指出原因和改法」，所以第三个字段不能空着 ——
    #   只说"缺 IPAdapterUnifiedLoader"用户并不知道那是什么、从哪儿来。
    REQUIRED_NODES = [
        ("CheckpointLoaderSimple", "底模加载", "升级 ComfyUI（这是自带节点）"),
        ("LoraLoader", "LoRA 加载", "升级 ComfyUI（这是自带节点）"),
        ("KSampler", "采样", "升级 ComfyUI（这是自带节点）"),
        ("VAEEncode", "图生图/蒙版编码", "升级 ComfyUI（这是自带节点）"),
        ("SetLatentNoiseMask", "蒙版局部重绘", "升级 ComfyUI（这是自带节点）"),
        ("ControlNetLoader", "ControlNet", "升级 ComfyUI（这是自带节点）"),
        ("ImageCompositeMasked", "蒙版贴回", "升级 ComfyUI（这是自带节点）"),
    ]
    OPTIONAL_NODES = [
        ("ControlNetInpaintingAliMamaApply", "专用 inpainting 模型（大幅改善蒙版修复）",
         "需要 ComfyUI 0.35 或更新版本，升级 ComfyUI"),
        ("MediaPipeFaceMask", "自动人脸蒙版",
         "需要 ComfyUI 0.35 或更新版本，升级 ComfyUI"),
        ("IPAdapterUnifiedLoader", "参考图（IPAdapter）",
         "装 ComfyUI_IPAdapter_plus：git clone "
         "https://github.com/cubiq/ComfyUI_IPAdapter_plus.git"
         "（在 ComfyUI 的 custom_nodes 目录里），装完重启 ComfyUI"),
        ("UpscaleModelLoader", "放大", "升级 ComfyUI（这是自带节点）"),
    ]
    miss = [(n, why, fix) for n, why, fix in REQUIRED_NODES if n not in oi]
    if miss:
        fail("缺少必需节点: %s" % ", ".join("%s（%s）" % (n, w) for n, w, _f in miss),
             "；".join(sorted({f for _n, _w, f in miss})))
    else:
        ok("必需节点齐全")
    omiss = [(n, why, fix) for n, why, fix in OPTIONAL_NODES if n not in oi]
    if omiss:
        warn("缺少可选节点: %s"
             % ", ".join("%s（%s）" % (n, w) for n, w, _f in omiss),
             "装了功能更全，不装也能用。分别是：" +
             "；".join("%s → %s" % (n, f) for n, _w, f in omiss))
    else:
        ok("可选节点齐全")
except urllib.error.URLError as e:
    # ★ 必须写出**它实际在连的地址**。原来这里写的是"确认端口是 8188" —— 把
    #   默认值当成事实了：用户把 ComfyUI 开在别的端口、或者 config.json 里
    #   comfy_url 填了别处时，这句提示只会把人带偏。
    #   而且 URLError 自己的文字里**没有 URL**（只有 WinError 10061 那种），
    #   所以不看这一行根本不知道它在连哪儿。
    fail("连不上 ComfyUI: %s（%s）" % (e, S.COMFY),
         "先启动 ComfyUI；如果它开在别处，改 config.json 里的 comfy_url")
except Exception as e:
    fail("ComfyUI 探测异常: %s: %s（%s）" % (type(e).__name__, e, S.COMFY))


def choices(node, field):
    """读取某个节点的可选值。

    ComfyUI 有两种格式：
        旧版  ["ckpt_name", {"required": {"ckpt_name": [["a","b"], {...}]}}]
        新版  ["ckpt_name", {"required": {"ckpt_name": ["COMBO", {"options": ["a","b"]}]}}]
    两种都要支持，否则新版会返回空列表，看起来像"没有模型"。
    """
    try:
        d = comfy_get("/object_info/" + node, timeout=60)
        spec = d[node]["input"]["required"][field]
        first = spec[0]
        if isinstance(first, list):
            return list(first)
        if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return list(spec[1].get("options") or [])
        return []
    except Exception:
        return []


# ---------------------------------------------------------------- models
print()
print("【5】模型文件")

# ★ ComfyUI 没连上时**整节跳过**。
#   这一节要做 5 次请求，而实测这台机器上每一次"连不上"要 2.05 秒才返回
#   （连一个开着的端口只要 0.014 秒）—— 也就是说 ComfyUI 没开时，用户要白等
#   十几秒，然后看到 5 条早就注定失败的"读不到…"。【4】已经明确报了
#   "连不上 ComfyUI + 它实际在连哪个地址"，跳过不影响任何判断。
if not COMFY_ONLINE:
    print("  [跳过] ComfyUI 没连上，模型这一节没法查 —— 先看上面【4】怎么修")
    ckpts = None

if COMFY_ONLINE:
    ckpts = choices("CheckpointLoaderSimple", "ckpt_name")
if ckpts is not None and not ckpts:
    # ★ 这是新用户**最常撞**的一种：ComfyUI 开着，但一个底模都没装。
    #   原来这里报的是「[警告] 读不到底模列表 → 确认 ComfyUI 在线」——
    #   两处都不对：①它是缺失，没有底模一张图都出不了，不是"建议处理"；
    #   ②ComfyUI 明明在线，让他去"确认在线"只会把人带偏。
    #   实测见 dev/audit_check_env.py 第【2】节。
    fail("一个底模都没有（ComfyUI 里的 checkpoints 列表是空的）",
         "把模型放进「刚需模型全部放这」文件夹，再双击「安装模型.bat」；"
         "或者看 README 的「模型清单」一节手动放到 "
         "models\\checkpoints\\ 里")
elif ckpts:
    if S.CHECKPOINT in ckpts:
        ok("底模就位: %s" % S.CHECKPOINT)
    else:
        fail("找不到底模 %s" % S.CHECKPOINT,
             "下载后放进 models/checkpoints，并把 config.json 的 checkpoint 改成实际文件名")
        print("         当前可用的底模: %s" % ", ".join(ckpts[:6]))

if COMFY_ONLINE:
    loras = choices("LoraLoader", "lora_name")
else:
    loras = []
if loras:
    ok("LoRA 共 %d 个" % len(loras))
    details = S.lora_details()
    incompatible = [d["file"] for d in details
                    if (d.get("key_layout") or {}).get("compatible") is False]
    if incompatible:
        warn("发现 %d 个不兼容 LoRA（格式不符，加载后不生效）: %s"
             % (len(incompatible), ", ".join(x[:30] for x in incompatible)),
             "删掉它们，否则容易误以为是模型问题")
    else:
        ok("已装 LoRA 格式全部兼容")
else:
    warn("没有检测到 LoRA", "至少装 1 个角色 LoRA 才有明显效果")

# 本包自带 loras\ 目录：如果它还在包里、ComfyUI 的 loras 目录里却没有，说明漏了复制步骤。
# 这里直接比对磁盘文件，不通过 ComfyUI 查询 —— ComfyUI 可能跑在别处（config 里
# comfy_url 与 models_dir 未必指向同一台机器），用它的列表会得出错误结论。
bundled_dir = os.path.join(HERE, "loras")
if os.path.isdir(bundled_dir):
    bundled = sorted(f for f in os.listdir(bundled_dir)
                     if f.lower().endswith(".safetensors"))
    if bundled:
        on_disk = set()
        if os.path.isdir(S.LORA_DIR):
            on_disk = set(os.listdir(S.LORA_DIR))
        pending = [f for f in bundled if f not in on_disk]
        if pending:
            fail("包内有 %d/%d 个 LoRA 还没复制到 ComfyUI"
                 % (len(pending), len(bundled)),
                 '执行：Copy-Item ".\\loras\\*" "%s\\" -Force' % S.LORA_DIR)
            print("         缺的（前 5 个）: %s" % ", ".join(pending[:5]))
            if not os.path.isdir(S.LORA_DIR):
                print("         而且目标目录不存在: %s" % S.LORA_DIR)
        else:
            ok("包内 %d 个 LoRA 已全部复制到 ComfyUI" % len(bundled))

cns = choices("ControlNetLoader", "control_net_name")
if not cns:
    warn("没有 ControlNet", "草图功能不可用；建议装 openpose 与 scribble 的 SDXL 版")
else:
    ok("ControlNet 共 %d 个: %s" % (len(cns), ", ".join(c[:34] for c in cns[:4])))
    if any("inpaint" in c.lower() for c in cns):
        ok("检测到 inpainting 专用 ControlNet（蒙版修复效果更好）")
    else:
        warn("没有 inpainting 专用 ControlNet",
             "修图功能仍可用，但纹理/瞳色保真度较差（实测差距明显）")

ups = choices("UpscaleModelLoader", "model_name")
if ups:
    ok("放大模型 %d 个: %s" % (len(ups), ", ".join(ups[:3])))
else:
    warn("没有放大模型", "「放大」按钮不可用；建议装 RealESRGAN_x4plus_anime_6B.pth"
                         "（动漫专用，17 MB）")

vae = choices("VAELoader", "vae_name") if "VAELoader" in (locals().get("oi") or {}) else []
if vae:
    ok("VAE %d 个" % len(vae))

# ---------------------------------------------------------------- result
print()
print("=" * 78)
print("自检结果：%d 项正常，%d 项警告，%d 项缺失"
      % (len(OK), len(WARN), len(FAIL)))
print("=" * 78)
if FAIL:
    print()
    print("必须先解决这些才能正常使用：")
    for i, (m, f) in enumerate(FAIL, 1):
        print("  %d) %s" % (i, m))
        if f:
            print("     → %s" % f)
if WARN:
    print()
    print("建议处理（不影响启动）：")
    for m, f in WARN:
        print("  - %s" % m)
        if f:
            print("    → %s" % f)
if not FAIL:
    print()
    print("可以直接启动：双击 启动UI.bat（或 python server.py）")
sys.exit(1 if FAIL else 0)
