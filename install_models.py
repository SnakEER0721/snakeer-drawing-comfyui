# -*- coding: utf-8 -*-
"""把下载好的模型放到该放的地方，并写好配置。

为什么需要这个脚本：
    ComfyUI 的模型分散在 models/ 下的七八个子目录里（checkpoints、controlnet、
    upscale_models、ipadapter、clip_vision…），而且**每个子目录放错都不报错** ——
    ComfyUI 只是"列表里没有它"，界面上看起来像"没装模型"。让用户自己判断
    哪个文件进哪个目录，是首次安装最容易卡住的地方。

它做什么：
    1. 认文件名（精确表，不靠扩展名猜），把每个文件搬到 models/ 下正确的子目录；
    2. 搬完把 config.json 的 checkpoint 改成实际装进去的那个底模名；
    3. 认不出来的文件**列出来**，不静默忽略 —— 静默失败是这个项目最忌讳的事。

它会去哪里找：
    应用目录下的「刚需模型全部放这」文件夹 —— 就这一个地方。
    也可以把文件/文件夹**直接拖到「安装模型.bat」上**，那样只扫拖进来的那些。

用法：
    python install_models.py                    # 扫「刚需模型全部放这」
    python install_models.py --dry              # 只看会怎么搬，不动任何文件
    python install_models.py --from D:\\某个目录   # 只扫这个目录（调试用）
    python install_models.py --copy             # 复制而不是移动（默认移动，省空间）
    python install_models.py --yes              # 不问确认，直接搬
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

# ---------------------------------------------------------------------------
# 模型清单：文件名 -> models/ 下的子目录
#
# ★ 为什么写死文件名而不是按扩展名分类：
#   .safetensors 可能是底模、ControlNet、LoRA、IPAdapter —— 靠扩展名分不出来。
#   靠"目录名里有没有 controlnet 字样"也不可靠（网盘包常被改名）。
#   精确表是唯一不会搬错的做法。表里没有的文件会被列出来让人自己判断。
#
# 大小只用于两件事：界面预估、以及"目标已存在且大小一致就跳过"，
# 另外还用来做"这个文件是不是没下完"的内容检查（见 file_problem）。
# 它不参与判断文件类型。
#
# ★ 这里的数字**必须和用户实际要装的那份文件对齐**。原来 CLIP 那条写的是 3762
#   （照 laion 仓库里那个 3.674 GiB 的文件抄的），而实际需要的是
#   h94/IP-Adapter 里 2411 MiB 的那个 —— 两个文件 sha256 不同、结构也不同。
#   写错会让"没下完"的判断按错误预期走，也会让 README 的占用空间说法是错的。
#   `dev/audit_readme_claims.py` 现在会**量磁盘**来核对这张表。
# ---------------------------------------------------------------------------
MODELS = [
    # (文件名, 子目录, 约多少 MB, 必需?, 说明)
    ("Illustrious-XL-v2.0.safetensors", "checkpoints", 6617, True,
     "底模（Illustrious-XL 官方 v2.0）—— 没有它什么都生成不了"),
    ("controlnet-scribble-sdxl.safetensors", "controlnet", 2386, False,
     "草图：画板上手画的线（默认的草图类型）"),
    ("controlnet-openpose-sdxl.safetensors", "controlnet", 2386, False,
     "姿势：要渲染好的骨架图，手画的火柴人它认不出来"),
    ("controlnet-union-sdxl-xinsir.safetensors", "controlnet", 2396, False,
     "构图迁移：参考图 -> 深度图 -> 这里"),
    ("noobaiInpainting_v10.fp16.safetensors", "controlnet", 2386, False,
     "局部重绘专用，纹理/瞳色保真度明显更好"),
    ("RealESRGAN_x4plus_anime_6B.pth", "upscale_models", 17, False,
     "放大（动漫专用，线条最干净）"),
    ("ip-adapter-plus_sdxl_vit-h.safetensors", "ipadapter", 808, False,
     "参考图：把一张图的风格/人物带进来"),
    ("CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors", "clip_vision", 2411, False,
     "上面那个的配套，两个必须同时装（来自 h94/IP-Adapter 的 image_encoder）"),
]

# 文件名 -> 条目，大小写不敏感
BY_NAME = {m[0].lower(): m for m in MODELS}

# 唯一的默认扫描目录，就在应用根目录下。
#
# ★ 为什么只扫这一个文件夹（第一版扫了五处：应用根目录、下载文件夹、
#   staging_dir、四个候选名…）：
#   扫描位置越多，用户越不知道"我该把文件放哪"，而"下载文件夹"里躺着他
#   几百个无关文件。一个名字、一个位置，用户不用猜，脚本也不用维护一堆别名。
#   拖放和 --from 仍然可用（那是明确指定，不是猜）。
DROP_DIR = "刚需模型全部放这"

# 大文件搬运要显示进度。超过这个体积才显示，小文件一闪而过反而刷屏。
PROGRESS_MIN = 64 * 1024 * 1024


def human(n: float) -> str:
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024 ** 2)
    return "%.0f KB" % (n / 1024)


def parse_args(argv):
    o = {"dry": False, "copy": False, "yes": False, "from": [], "paths": []}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--dry":
            o["dry"] = True
        elif a == "--copy":
            o["copy"] = True
        elif a == "--yes" or a == "-y":
            o["yes"] = True
        elif a == "--from" and i + 1 < len(argv):
            i += 1
            o["from"].append(argv[i])
        elif a.startswith("--from="):
            o["from"].append(a.split("=", 1)[1])
        elif a.startswith("-"):
            print("  未知参数: %s（忽略）" % a)
        else:
            # 拖到 .bat 上的文件/目录会变成裸参数
            o["paths"].append(a)
        i += 1
    return o


def source_dirs(opt) -> list:
    """返回 [(目录, 说明)]。

    默认只有一个：应用目录下的「刚需模型全部放这」。
    显式指定了来源（拖放 / --from）时就**只扫那些**，不看默认目录。
    """
    out = []
    for p in opt["paths"]:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            # 拖上来的可能是单个文件 —— 用它的所在目录当来源，靠清单过滤
            out.append((os.path.dirname(p), "拖进来的文件"))
        elif os.path.isdir(p):
            out.append((p, "拖进来的文件夹"))
        else:
            print("  !! 拖进来的路径不存在: %s" % p)
    for d in opt["from"]:
        if os.path.isdir(d):
            out.append((os.path.abspath(d), "--from 指定"))
        else:
            print("  !! --from 指定的目录不存在: %s" % d)
    if out:
        return _dedup(out)

    out.append((os.path.join(HERE, DROP_DIR), DROP_DIR))
    return _dedup(out)


def _dedup(pairs) -> list:
    """同一目录只扫一次（拖进来的文件夹可能和 --from 指定的是同一个）。"""
    seen = set()
    uniq = []
    for d, why in pairs:
        k = os.path.normcase(os.path.abspath(d))
        if k in seen:
            continue
        seen.add(k)
        uniq.append((d, why))
    return uniq


def scan(dirs) -> list:
    """在给定目录里找清单上的文件。返回 [(文件名, 源路径, 说明, 条目, 来源目录说明)]。"""
    hits = []
    for d, why in dirs:
        try:
            names = os.listdir(d)
        except OSError as e:
            print("  !! 读不了目录 %s: %s" % (d, e))
            continue
        for fn in names:
            full = os.path.join(d, fn)
            if not os.path.isfile(full):
                continue
            e = BY_NAME.get(fn.lower())
            if e:
                hits.append((fn, full, e, why))
    return hits


def target_for(entry) -> str:
    fn, sub = entry[0], entry[1]
    return os.path.join(paths.MODELS_DIR, sub, fn)


def need_move(hits) -> list:
    """过滤掉"已经在目标位置且大小一致"的，剩下的才要搬。

    还要按**目标路径**去重：同一个文件名可能同时出现在「刚需模型全部放这」
    和拖进来的一批文件里，不去重就会搬两遍，第二遍把第一遍刚搬好的覆盖掉
    —— 白等一次 2.4 GB。
    """
    todo = []
    seen_dst = {}
    for fn, src, entry, why in hits:
        dst = target_for(entry)
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue                      # 文件本来就在正确位置
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            continue                      # 已经装过了，且大小一致
        key = os.path.normcase(os.path.abspath(dst))
        if key in seen_dst:
            # 已经有一个来源要搬到这 —— 保留先找到的那个，另一个只在日志里提一句
            print("     注意：%s 有多份，只用「%s」里的那份（另一个在「%s」）"
                  % (fn, seen_dst[key], why))
            continue
        seen_dst[key] = why
        todo.append((fn, src, entry, why, dst))
    return todo


def same_volume(a: str, b: str) -> bool:
    return os.path.splitdrive(os.path.abspath(a))[0].lower() == \
        os.path.splitdrive(os.path.abspath(b))[0].lower()


# ---------------------------------------------------------------------------
# 搬之前先看内容 —— 这一步以前完全没有
#
# 实测（dev/probe_model_files.py）：把一个 **171 字节**的文件命名成清单里的
# 底模（约 6617 MB）放进投放目录，
# 脚本报「完成 0 KB」、把它搬进 models\checkpoints\，**还把 config.json 的
# checkpoint 指到了它身上**，退出码 0。一张网盘的 HTML 错误页存成
# .safetensors 也一样照搬。
#
# 20 GB 的模型走网盘下载，这三种坏法都很现实：
#   ① 没下完（下载中断）      ② 下到的是一张 HTML 错误页（链接过期/要提取码）
#   ③ 空文件
# 坏文件进了模型目录之后，ComfyUI 那边要么列出一个加载不了的名字、要么干脆不列 ——
# 而用户坚信"我明明下好了"。所以宁可不搬，也要先说清楚。
# ---------------------------------------------------------------------------
# 清单里的体积是"约多少 MB"，会有舍入，所以容忍一半：真实的截断远小于一半，
# 而同一版本的重新上传不会差这么多。太严会挡住正常的文件，得不偿失。
SIZE_TOLERANCE = 0.5


def _safetensors_shape(path: str):
    """读 safetensors 的自述结构，返回 (声明总长度, 出错原因)。

    格式：8 字节小端头长度 + 头(JSON) + 数据。
    ★ safetensors 是**自述**的：头里每个张量都写了 data_offsets，所以
      「8 + 头长 + 最大偏移」就是文件应有的总长度 —— 截断能**精确**查出来，
      不用猜。这一点比"看开头是不是 JSON"强得多：截断的文件开头完全正常。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(8)
            if len(raw) < 8:
                return None, "太小了，连 8 字节的文件头都不完整"
            # 出错时把开头几个字节也带出来 —— 只报一个天文数字（把 "<!DOCTYP"
            # 当成 8 字节整数）用户看不出是什么问题；带上原文一眼就明白是网页
            with open(path, "rb") as fh2:
                head8 = fh2.read(64)
            hlen = int.from_bytes(raw, "little")
            if not (0 < hlen <= 100 * 1024 * 1024):
                return None, ("开头不像 safetensors（前 64 字节是 %r）—— "
                              "如果是从网盘下的，多半下到的是一张网页"
                              % head8[:24])
            head = fh.read(hlen)
            if len(head) < hlen:
                return None, "文件头都不完整（没下完）"
        import json as _js
        d = _js.loads(head.decode("utf-8"))
        if not isinstance(d, dict):
            return None, "文件头不是 safetensors 的结构"
        end = 0
        for k, v in d.items():
            if k == "__metadata__" or not isinstance(v, dict):
                continue
            off = v.get("data_offsets") or [0, 0]
            if isinstance(off, list) and len(off) == 2:
                end = max(end, int(off[1]))
        return 8 + hlen + end, None
    except UnicodeDecodeError:
        return None, ("文件头不是文本格式（前 64 字节是 %r）—— 如果是从网盘下的，"
                      "多半下到的是一张网页" % head8[:24])
    except Exception as e:
        return None, "读不出来: %s: %s" % (type(e).__name__, e)


def file_problem(fn: str, path: str, entry) -> str:
    """这个文件能不能装？能装返回空串，不能装返回一句给用户看的原因。"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return "读不到这个文件: %s" % e
    if size == 0:
        return "文件是空的（0 字节）"

    if fn.lower().endswith(".safetensors"):
        declared, err = _safetensors_shape(path)
        if err:
            return err
        if declared != size:
            return ("内容说它应该有 %s，实际只有 %s —— 这个文件没下完"
                    % (human(declared), human(size)))

    want_mb = entry[2] if len(entry) > 2 else 0
    if want_mb:
        want = want_mb * 1024 * 1024
        if size < want * SIZE_TOLERANCE:
            return ("清单里这个文件约 %s，实际只有 %s —— 看起来没下完"
                    "（或者下错了文件）" % (human(want), human(size)))
    return ""


def split_bad(todo):
    """把"不能装"的挑出来。返回 (能装的, [(文件名, 原因, 路径)])。"""
    good, bad = [], []
    for item in todo:
        fn, src, entry = item[0], item[1], item[2]
        why = file_problem(fn, src, entry)
        if why:
            bad.append((fn, why, src))
        else:
            good.append(item)
    return good, bad


def move_with_progress(src, dst, copy_mode: bool) -> None:
    """搬文件。同盘用 os.replace（瞬间完成，不产生进度）；跨盘手动分块复制并显示进度。

    为什么不用 shutil.move：跨盘时它内部也是一次性 copy，11 GB 会黑屏好几分钟，
    用户以为卡死了会去关窗口 —— 半途中断留下半个文件，比慢更糟。
    """
    size = os.path.getsize(src)
    if not copy_mode and same_volume(src, dst):
        os.replace(src, dst)              # 同盘：原子改名，多快都不需要进度
        return

    show = size >= PROGRESS_MIN
    tmp = dst + ".part"                   # 先写成 .part，完成才改名
    done = 0
    buf = 4 * 1024 * 1024
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        while True:
            b = fi.read(buf)
            if not b:
                break
            fo.write(b)
            done += len(b)
            if show:
                pct = done * 100 // max(size, 1)
                sys.stdout.write("\r        %s  %3d%%  %s / %s"
                                 % ("复制中" if copy_mode else "搬运中", pct,
                                    human(done), human(size)))
                sys.stdout.flush()
    if show:
        sys.stdout.write("\n")
    os.replace(tmp, dst)
    if not copy_mode:
        os.remove(src)


def main() -> int:
    opt = parse_args(sys.argv)
    mode = "复制" if opt["copy"] else "移动"

    print("=" * 78)
    print("Snakeer Drawing —— 模型安装" + ("   [试运行，不会动任何文件]" if opt["dry"] else ""))
    print("=" * 78)

    # ---- 目标根目录 ----
    print()
    print("【1】模型目录")
    md = paths.MODELS_DIR
    # ★ 来源**照 paths 记的念**，不要在这里自己拼一句"来自：自动探测 / 环境变量"。
    #   原来那行是猜的：config.json 里没填就一律印"自动探测 / 环境变量"，
    #   而真实情况可能是"自动探测一个都没探到，退回了兜底" —— 印出来的那句话
    #   正好把最需要看见的那件事盖掉。
    src = getattr(paths, "DIR_SOURCE", {}).get("models 根目录", "")
    print("  %s" % md)
    print("  （来自：%s）" % (src or "未记录"))

    # ★ 判据是**来源**，不是"目录在不在"。
    #   两件事都能退 2，但原因完全不同：目录不存在（下面那条），和目录存在但
    #   它是兜底 —— 自动探测没探到时 paths 会退回软件自己的 `models` 目录，
    #   脚本随后**自己把它建出来**，于是它"存在"了，接着就把 14 GB 模型搬进一个
    #   ComfyUI 根本不读的地方，还打印"装好了"。用户下次出图照旧"找不到底模"，
    #   而且完全不知道该怀疑哪一步。
    #   ★ 顺序踩过坑：第一版把这段放在 `isdir` 判断**后面**，而"目录不存在"那条
    #     路已经先 return 了 —— 于是这段永远执行不到（死代码，
    #     dev/simulate_fresh_install.py 的【6】节当场抓出来）。
    #     必须放在 `isdir` 之前。
    if src.startswith("兜底"):
        print()
        print("!! 自动探测没找到你的 ComfyUI 模型目录，退回了一个兜底位置。")
        print("   ComfyUI 不读这里 —— 把模型搬进去也没用，出图照旧找不到。")
        print("   两种可能：")
        print("     a) ComfyUI 还没启动过 —— 先启动一次 ComfyUI Desktop，")
        print("        它会把自己真正在用的目录写下来，重跑本脚本就能认出来")
        print("     b) ComfyUI 装在别的地方（便携版、装在别的盘）—— 编辑 config.json，")
        print("        把 models_dir 填成你的 ComfyUI 里那个 models 目录")
        print("   改完重跑本脚本。")
        return 2

    if not os.path.isdir(md):
        print()
        print("!! 这个目录不存在，脚本不敢往下走。")
        print("   两种可能：")
        print("     a) ComfyUI 还没装/没跑过 —— 先启动一次 ComfyUI Desktop，它会创建这个目录")
        print("     b) ComfyUI 装在别的地方 —— 编辑 config.json 的 models_dir 指过去")
        print("   改完重跑本脚本。")
        return 2

    # ---- 找文件 ----
    print()
    print("【2】扫描这些位置")
    dirs = source_dirs(opt)
    for d, why in dirs:
        print("  %-46s （%s）" % (d, why))

    hits = scan(dirs)
    todo = need_move(hits)
    already = len(hits) - len(todo)

    if not hits:
        # ★ 先看看是不是「已经装好了」。
        #   第一版直接报「没有找到任何模型」并退 1 —— 但用户装完再跑一次时，
        #   来源目录里当然已经没有那些文件了（都被搬走了），于是他看到
        #   「没有找到」，第一反应是"我刚装完怎么说没找到？是不是装坏了"。
        #   真正该说的是「全部已装好」。
        have = [m for m in MODELS if os.path.isfile(target_for(m))]
        if have:
            print()
            print("=" * 78)
            print("这些模型已经装好了，不用再装：")
            print("=" * 78)
            for m in have:
                print("  %-46s models\\%s\\" % (m[0], m[1]))
            missing = [m for m in MODELS if m not in have]
            if missing:
                print()
                print("还缺这些（放好文件后再跑一次本脚本）：")
                for m in missing:
                    print("  %-46s (%s)  %s"
                          % (m[0], "必需" if m[3] else "可选", m[4]))
            _fix_config([(m[0], "", m, "") for m in have], opt)
            print()
            print("跑 python check_env.py 可以确认 ComfyUI 那边也认出来了。")
            return 0

        print()
        print("=" * 78)
        print("没有找到任何清单里的模型文件。")
        print("=" * 78)
        print()
        print("请把从网盘下载的模型文件放进这个文件夹：")
        print()
        print("    %s" % os.path.join(HERE, DROP_DIR))
        print()
        print("然后重新双击「安装模型.bat」。也可以把文件直接拖到那个 .bat 上。")
        print()
        print("清单里的文件名（必须一模一样，改过名就认不出来了）：")
        for m in MODELS:
            print("  %-46s -> models\\%s\\" % (m[0], m[1]))
        return 1

    # ---- 清单 ----
    #
    # ★ 已就位的也要逐个列出来。第一版只打了"其中 N 个已在正确位置"，
    #   结果是：用户看不出脚本认出了哪些文件，也看不出它认错了没有 ——
    #   和"什么都没做"看起来一模一样。这个项目最忌讳静默。
    print()
    print("【3】找到 %d 个，其中 %d 个已在正确位置" % (len(hits), already))
    done_names = {os.path.normcase(os.path.abspath(t[1])) for t in todo}
    for fn, src, entry, why in hits:
        if os.path.normcase(os.path.abspath(src)) in done_names:
            continue
        print("    已装  %-46s （在「%s」里也有一份）" % (fn, why))

    # ★ 搬之前**先看内容**。这一步以前完全没有，于是 171 字节的假文件会被
    #   当成 6.5 GB 的底模搬进去（实测见 dev/probe_model_files.py）。
    todo, bad_files = split_bad(todo)
    if bad_files:
        print()
        print("=" * 78)
        print("【!】这些文件不对劲，不会装（%d 个）" % len(bad_files))
        print("=" * 78)
        for fn, why, src in bad_files:
            print("    %s" % fn)
            print("      %s" % why)
            print("      位置: %s" % src)
        print()
        print("  重新下载这几个文件再跑一次即可。**不要**改文件名 —— 脚本靠")
        print("  文件名认出它是哪个模型。如果你确定文件没问题，见 README 的")
        print("  「模型清单」一节手动放到对应目录。")

    total = sum(os.path.getsize(t[1]) for t in todo)
    if todo:
        print()
        print("  接下来会%s这些（共 %s）：" % (mode, human(total)))
        for fn, src, entry, why, dst in todo:
            print()
            print("    %s  %s" % (fn, human(os.path.getsize(src))))
            print("      %s（在「%s」里找到）" % (entry[4], why))
            print("      从 %s" % src)
            print("      到 %s" % dst)
    else:
        print()
        print("  全部就位，不用搬。")

    # ---- 认不出来的文件（只说，不动）----
    known_ext = (".safetensors", ".ckpt", ".pth", ".pt", ".bin")
    unknown = []
    for d, why in dirs:
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and fn.lower().endswith(known_ext) \
                        and fn.lower() not in BY_NAME:
                    unknown.append((fn, p, why))
        except OSError:
            pass
    if unknown:
        print()
        print("【4】这些模型文件清单里没有，脚本不会碰它们：")
        for fn, p, why in unknown:
            print("    %-46s （在「%s」里）" % (fn, why))
        print("    如果是 LoRA，放到 models\\loras\\；其它类型请自己判断，")
        print("    或者告诉我该加进清单。")

    if not todo:
        # 即使不用搬，也要保证配置对。
        # ★ 不能把"不对劲"的文件算进去：`have` 是**磁盘上有这个名字**就算数，
        #   而坏文件也可能已经在目标位置（上一次搬进去的）。用它去改
        #   config.json 的 checkpoint，就是把配置指向一个加载不了的文件。
        bad_names = {fn.lower() for fn, _w, _s in bad_files}
        _fix_config([h for h in hits if h[0].lower() not in bad_names], opt)
        return 1 if bad_files else 0

    # ---- 确认 ----
    if not opt["dry"] and not opt["yes"]:
        print()
        try:
            ans = input("确认%s？(y/N) " % mode).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 130
        if ans not in ("y", "yes"):
            print("已取消，什么都没动。")
            return 0

    # ---- 搬 ----
    print()
    print("【5】开始%s" % mode)
    okn = 0
    failed = []
    for fn, src, entry, why, dst in todo:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        print("  -> %s" % fn)
        if opt["dry"]:
            print("     [试运行] 会搬到 %s" % dst)
            okn += 1
            continue
        try:
            move_with_progress(src, dst, opt["copy"])
            got = os.path.getsize(dst)
            want = os.path.getsize(src) if opt["copy"] else got
            if got <= 0:
                raise IOError("目标文件是空的")
            print("     完成 %s" % human(got))
            okn += 1
        except Exception as e:
            failed.append((fn, "%s: %s" % (type(e).__name__, e)))
            print("     !! 失败: %s: %s" % (type(e).__name__, e))
            # 别留半个文件在 ComfyUI 目录里 —— 它会让 ComfyUI 列表里出现一个
            # 加载不了的名字，比"没有"更难查
            if os.path.isfile(dst) and os.path.getsize(dst) == 0:
                try:
                    os.remove(dst)
                except OSError:
                    pass

    _fix_config([h for h in hits
                 if h[0].lower() not in {fn.lower() for fn, _w, _s in bad_files}],
                opt)

    # ---- 结果 ----
    print()
    print("=" * 78)
    print("搬完 %d 个，失败 %d 个，没装 %d 个（文件不对劲）"
          % (okn, len(failed), len(bad_files)))
    print("=" * 78)
    for fn, err in failed:
        print("  !! %s —— %s" % (fn, err))
    for fn, why, _src in bad_files:
        print("  !! %s —— %s" % (fn, why))
        print("     重新下载这个文件再跑一次。")
    if failed:
        print()
        print("失败通常是：目标磁盘满了，或者文件被 ComfyUI/网盘客户端占用。")
        print("关掉那些程序再跑一次即可，已经搬好的会自动跳过。")

    print()
    print("下一步：跑一遍 python check_env.py，它会逐项确认模型都认出来了。")
    return 1 if (failed or bad_files) else 0


def _seed_config_from_example() -> int:
    """config.json 不存在时，用 config.example.json 的内容把它建出来。

    为什么必须这么做（实测踩到）：
        新用户第一次跑本脚本时 config.json 还不存在，`save_config({"checkpoint": ...})`
        会新建一个**只有那一个键**的文件：
            {"checkpoint": "<底模文件名>"}
        可是 README 第 7 步之后让他"改 config.json" —— 打开一看，一个键、
        零说明，还得回去翻 README 逐项对照。而 config.example.json 就躺在旁边，
        里面每个键都有一行中文说明。

    只在**文件不存在**时播种。已经有 config.json 的用户，他的设置一个都不碰。

    返回播种了几个键（0 = 没做）。
    """
    if os.path.isfile(paths.CONFIG_PATH):
        return 0
    ex = os.path.join(HERE, "config.example.json")
    if not os.path.isfile(ex):
        return 0
    try:
        seed = json.load(io.open(ex, encoding="utf-8"))
    except Exception as e:
        print("  !! 读 config.example.json 失败（跳过）：%s" % e)
        return 0
    if not isinstance(seed, dict) or not seed:
        return 0
    try:
        paths.save_config(seed)
    except Exception as e:
        print("  !! 生成 config.json 失败：%s" % e)
        return 0
    print()
    print("  已按 config.example.json 生成 config.json（%d 项，每项带中文说明）"
          % len(seed))
    return len(seed)


def _fix_config(hits, opt) -> None:
    """把 config.json 的 checkpoint 指向实际装好的底模。

    只在"清单里的底模真的就位了"时才改 —— 不能因为用户装了别的底模就把配置
    改掉，也不能在没装底模时写一个不存在的名字（那样 check_env 会报"找不到底模"，
    而用户明明什么都没做错）。
    """
    ck = [h for h in hits if h[2][1] == "checkpoints"]
    if not ck:
        return
    name = ck[0][0]
    dst = target_for(ck[0][2])
    installed = os.path.isfile(dst) or opt["dry"]
    if not installed:
        return
    if opt["dry"]:
        print()
        print("  [试运行] 会把 config.json 的 checkpoint 改成 %s" % name)
        print("  [试运行] 会按 config.example.json 补齐 config.json 里缺的说明项")
        return
    seeded = _seed_config_from_example()
    if (paths.CONFIG.get("checkpoint") or "") == name:
        if not seeded:
            print()
            print("  config.json 的 checkpoint 已经是 %s，不用改。" % name)
        return
    print()
    try:
        paths.save_config({"checkpoint": name})
        print("  已把 config.json 的 checkpoint 改成 %s" % name)
    except Exception as e:
        print("  !! 写 config.json 失败: %s: %s" % (type(e).__name__, e))
        print("     手动把 checkpoint 改成 %s 即可。" % name)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
