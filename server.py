#!/usr/bin/env python3
"""Local web UI for ComfyUI - backend.

Single-file, Python standard library only. Serves ui.html and exposes JSON
endpoints that translate Chinese prompts via the offline dictionary, build a
ComfyUI API graph, run it, and hand back the resulting image paths.

Features and their dependencies are detected at runtime and reported to the
frontend, so a feature whose models are missing shows as unavailable rather
than failing silently.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from cn_translate import Translator  # noqa: E402
import poisson_blend  # noqa: E402

# ---------------------------------------------------------------- 路径
# ★ 路径逻辑全部搬到了 paths.py，这里只做 import。
#   为什么：这套四级回退（config.json > 环境变量 > 自动探测 > 应用目录兜底）
#   原来只在这个文件里有，而其他 60 多个脚本和测试各自写死了本机的绝对路径。
#   现在 paths.py 是唯一来源，谁都能 import。
from paths import (  # noqa: E402
    APP_DIR, CHECKPOINT, CONFIG, CONFIG_ERROR, CONFIG_PATH, PORT, VERSION,  # noqa: F401
    COMFY_INPUT, COMFY_OUTPUT, COMFY_URL, MODELS_DIR, OUT_ANIME, OUT_DEPTH,
    RECYCLE_DIR, TAG_DB, VOCAB,  # noqa: F401
)
# ★ 同时 import **模块本身**（不只是里面的常量）。
#   为什么必须两个都要：`from paths import X` 是把 X 的**当时那个值**抄进来，
#   之后 paths 里再改它，这里看到的还是旧的。而有一条判据要在**启动之后**
#   才能算出来（产出目录里有没有出图记录 —— 那个目录是启动时才建好的），
#   算出来要写回 paths.CONFIG_ERROR，接口得读到**新的**那个值。
#   踩点：这条判据第一次写完，接口返回的仍是上次 import 时抄下来的 None，
#   界面上什么都没显示 —— 换个写法还得靠这一行。
import paths as APPATHS  # noqa: E402

COMFY = COMFY_URL
# 正向质量词。Illustrious 系通用（官方 v0.1 / v1.x / v2.0 和它的各种 finetune
# 都吃这一套）—— 最早是从 RIN Flanime 那个 finetune 的模型页抄下来的，
# 换默认底模之后它照样适用，所以值没动，只把出处写准。
QUALITY = "masterpiece, best quality"

# ---------------------------------------------------------------------------
# 生成参数的兜底默认值 —— 单一事实来源。
#
# 为什么要有这个表：同一个参数的默认值以前写在两个地方（ui.html 里控件的
# value=，和这里的 p.get(x, 默认)）。两边不一致时，**界面显示一个数、服务端
# 用另一个数，而且不报任何错** —— 用户以为 cfg 是 5.5，实际跑的是 5。
# 这个坑已经踩过一次（滑杆显示 5.5、后端兜底 5）。
#
# 现在所有兜底值都从这张表取，tests/test_defaults_parity.py 逐条比对它和
# ui.html 里控件的 value=，对不上就红。改默认值只改这张表 + 前端控件。
#
# 注意 denoise 有两个值：文生图/图生图 是 1.0（文生图必须 1.0，否则出灰图），
# 局部重绘是 0.75。同一个键、两个语义，所以分成两个名字。
# ---------------------------------------------------------------------------
DEFAULTS = {
    "steps": 30,
    "cfg": 5.5,
    "clip_skip": 2,
    "count": 1,
    "width": 1024,
    "height": 1024,
    "sampler": "dpmpp_2m",
    "scheduler": "karras",
    "gen_denoise": 1.0,
    "inpaint_denoise": 0.75,
    "controlnet_strength": 0.8,
    "mask_grow": 12,
    "mask_feather": 12,
    "sketch_strength": 0.8,
    "ref_strength": 0.8,
    "depth_strength": 0.7,
}
# 主体词曾经是 SUBJECT_DEFAULT = "1girl, solo"，用户提示词里没写人物时自动补上。
# 2026-09-20 用户要求**彻底删掉**，理由是它和人打架：
#   * 判断只看整段文本里有没有 "1girl"/"1boy"/"girls"/"boys"/"solo"，
#     于是 "2girls"（两个女孩）不匹配 → 照样补 "1girl, solo"，
#     和「我要画两个人」直接矛盾
#   * "solo" 的语义是「画面里只有一个人」，用户想要多人时必须手动去删
# 现在的行为：程序一个字都不加，写什么就是什么。代价（用户已知悉并接受）：
# 提示词里没写人物时，出来的是纯风景。
# 别再把它加回来 —— 要恢复成「自动补」需要先问用户。
# 默认负向词（同样是 Illustrious 系通用的一套，出处见上面 QUALITY 的注释）
NEGATIVE_DEFAULT = ("lowres, bad quality, worst quality, worst detail, "
                    "username, signature")
CONTROLNET_SCRIBBLE = "controlnet-scribble-sdxl.safetensors"
CONTROLNET_OPENPOSE = "controlnet-openpose-sdxl.safetensors"
# union 模型：一个文件顶多种控制类型，必须用 SetUnionControlNetType 指定走哪一路。
# 用它做「构图迁移」——参考图先经 DA3 转成深度图，再喂给它。
CONTROLNET_UNION = "controlnet-union-sdxl-xinsir.safetensors"
# Depth Anything 3（ComfyUI 0.35+ 内置，节点 LoadDA3Model/DA3Inference/DA3Render）
DEPTH_MODEL = "depth_anything_3_small.safetensors"
# 深度图产出目录 OUT_DEPTH 由 paths.py 给（跟着产出目录走）
# 放大输出的单边像素上限。8 GB 显存上再大就容易爆。
# ★ 它是**整体**上限（长边顶到它就按比例缩），不是宽高各自的封顶 ——
#   各自封顶会把 16:9 压成 4:3，见 /api/upscale 里的说明。
MAX_UPSCALE_SIDE = 4096
# 默认放大模型。
#
# 为什么是 RealESRGAN_x4plus_anime_6B 而不是 Remacri（原来的默认值）：
# 同一张 1344×768 的插画、同一个 2× 目标，四种做法实测对比过（脚本见
# compare_upscale_models.py（在 dev/ 目录），脸部与鸟居两块细节各裁 1:1 人眼看过）：
#   Remacri            13 秒  5.16MB  线条发闷、蝴蝶结边缘有灰白晕
#   anime_6B            5 秒  4.26MB  线条最利落、颜色干净 —— 最接近原图
#   4x-UltraSharp      13 秒  6.08MB  很锐，但黑梁上沿有明显亮边（过冲）
#   animevideov3-x2     2 秒  5.48MB  很干净，但要靠外部 exe，ComfyUI 里用不了
# 结论：anime 专用的 6B 版在这个场景全面胜出，还快 2.6 倍、体积只有 1/4。
# Remacri 是照片向模型，在平涂线稿上并不占优。
UPSCALE_MODEL_DEFAULT = "RealESRGAN_x4plus_anime_6B.pth"
# 说明文字的字典（键是文件名，匹配不到就不显示）。
# 注意：没装的模型在这里留着条目不会出错（永远匹配不上），所以这里允许比
# 实际装的多 —— 它同时也是"以后要是加了别的模型，该期待它什么表现"的备注。
UPSCALE_HINT = {
    "RealESRGAN_x4plus_anime_6B.pth":
        "动漫专用（推荐）—— 线条干净、不出串色，速度最快，17 MB",
    "4x-UltraSharp.pth":
        "通用锐化 —— 最锐，但平涂边缘容易出亮边（过冲），64 MB",
    "4x-AnimeSharp.pth":
        "动漫专用 —— 线条强化，偏锐",
    "AnimeSharp.pth":
        "动漫专用（旧版）",
    "4x_NMKD-Siax_200k.pth":
        "通用照片向，噪点少的图表现好",
}
# 「删除」不是真删，是移到这里（RECYCLE_DIR 由 paths.py 给）。为什么：
# 这个应用早期有一次真的 os.remove 过用户的文件，没法恢复
# （见 tests/test_prompt_builder.py 顶部那段注释）。出图是几十分钟的算力，
# 误点一下不该等于永久丢失；想彻底清空自己删这个目录。
# what each ControlNet is actually good at, surfaced in the UI
CONTROLNET_INFO = {
    CONTROLNET_SCRIBBLE: ("涂鸦 / 粗线条",
                          "画板上手画的线，会照着原样锁住形状 —— 手画草图用这个"),
    # ★ 这条提示以前写的是「画头+躯干+四肢，严格锁定姿势构图」，**实测是错的**。
    # 画板是个自由涂抹的画布，而 openpose ControlNet 是按**渲染好的骨架图**
    # 训练的（彩色四肢 + 关节点，见 dev/user_journey.py 阶段 13），它认不出
    # 人手画的线。同图同种子的 A/B（阶段 12）：手画火柴人 + scribble 把姿势
    # 1:1 锁住了；换成 openpose，出的是"双手垂着"的普通少女，姿势完全没锁。
    # 所以这里如实写清楚，并且默认值改成涂鸦（见 capabilities() 的 sketch_detail）。
    # 注意：这句是**界面上的纯文本**（textContent），不要写 markdown 的 ** 强调。
    CONTROLNET_OPENPOSE: ("骨架图 / 姿势（openpose）",
                          "要「渲染好的骨架图」（彩色四肢 + 关节点）才锁得住姿势；"
                          "手画的火柴人它认不出来 —— 那种请选「涂鸦」"),
    # 这两个也会出现在「草图类型」下拉里（ComfyUI 把 models/controlnet 底下
    # 所有文件都报出来）。以前它们显示成两个光秃秃的「其他」，用户很可能随手
    # 选一个然后发现效果莫名其妙 —— 说清各自是干什么用的。
    # 名字含 inpaint 的都算局部重绘用的（和上面 inpaint_cns 同一个判据）。
    CONTROLNET_UNION: ("构图迁移用（union）",
                       "给「构图迁移」那条链用的；手画的草图选它意义不大"),
}
IPADAPTER_FILE = "ip-adapter-plus_sdxl_vit-h.safetensors"
CLIP_VISION_FILE = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"

_TR = None
_TR_LOCK = threading.Lock()


def _controlnet_info(fname: str) -> tuple:
    """ControlNet 文件名 -> (界面上显示的名字, 一句提示)。

    表里没有的按**用途**猜一把，而不是一律「其他」：ComfyUI 会把
    models/controlnet 底下所有文件都报出来，包括用户自己下的、名字里带
    inpaint 的那些 —— 显示成光秃秃的「其他」，用户就会随手选一个然后
    发现效果莫名其妙。判据和 capabilities() 里的 inpaint_cns 一致。
    """
    if fname in CONTROLNET_INFO:
        return CONTROLNET_INFO[fname]
    if re.search(r"inpaint", fname, re.I):
        return ("局部重绘专用（Inpainting）", "给「修图」用的，不是草图模型")
    return ("其他", "")


def _default_first(items: list, default) -> list:
    """把 default 那一项挪到最前面（只影响显示顺序）。"""
    if default not in items:
        return list(items)
    return [default] + [x for x in items if x != default]

# Which original image an inpaint result came from, so the UI can show a
# before/after comparison without the user searching the output folder.
PROV_FILE = os.path.join(HERE, "_provenance.json")
_PROV_LOCK = threading.Lock()


def _load_provenance() -> dict:
    try:
        with open(PROV_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        warn("读取产出溯源失败", e)
        return {}


def _resolve_prov_path(path: str) -> str | None:
    """把溯源里记录的路径解析成实际存在的文件。

    记录里存的是绝对路径，一旦产出目录被整理（例如按日期分子目录），
    旧记录就会失效。调用方原本用 os.path.isfile 守着，失效时静默丢掉
    对比功能 —— 没有报错，比较难发现。这里按文件名在产出树里兜底查找。
    """
    if not path:
        return None
    if os.path.isfile(path):
        return path
    want = os.path.basename(path)
    if not want:
        return None
    for p, f in iter_output_images(OUT_ANIME):
        if f == want:
            return p
    return None


# ---------------------------------------------------------------- 路径白名单
# 这个应用会按用户给的路径读文件（显示产出、放大、局部重绘），而它自己是个
# 只听 127.0.0.1 的本地服务 —— 但"本地"不等于"可信"：浏览器里任何一个网页
# 都能向它发请求。之前这些端点只判断 os.path.isfile，等于把整个硬盘当成可读。
#
# 实测（修复前）：GET /api/image?path=C:\Windows\win.ini 返回 HTTP 200 和文件
# 原文；/api/upscale 传 win.ini 也一路读到 PIL 才报错，说明文件已经打开了。
#
# 所以这里定死：只有产出目录和 ComfyUI 的 input 目录下面的文件可以读。
def resolve_seed(value) -> int:
    """把前端传来的种子归一成一个合法整数，非法就换成随机。

    为什么需要它：`p.setdefault("seed", 随机)` 只在该键**不存在**时才生效。
    而前端种子框里填了非数字（比如手滑打进去的 abc）时，parseInt 得到 NaN，
    JSON.stringify 把 NaN 变成 null —— 后端收到的键存在、值是 None，
    setdefault 不生效，下游 int(None) 直接 500（已实测）。
    生成失败，提示还是 "TypeError: int() argument must be..." 这种看不懂的话。

    抽成独立函数是为了能被测试直接断言 —— 内联在请求处理里没法单测。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int.from_bytes(os.urandom(4), "big")
    if n < 0 or n > 4294967295:          # ComfyUI 的 seed 是 32 位无符号
        return int.from_bytes(os.urandom(4), "big")
    return n


def _safe_read_roots() -> list:
    """可以被读取的目录集合（不存在的不算）。"""
    roots = [OUT_ANIME, COMFY_OUTPUT, COMFY_INPUT]
    out = []
    for r in roots:
        if not r:
            continue
        try:
            rp = os.path.realpath(r)
        except Exception:
            continue
        if rp not in out:
            out.append(rp)
    return out


def is_readable_path(path: str) -> bool:
    """这个路径是否允许被读取。

    用 realpath 之后比较，所以 `..`、`.` 和符号链接都会被展开，
    绕过不了。比较用 commonpath 而不是 startswith —— startswith 会把
    D:\\comfyout\\anime_evil 误判成在 D:\\comfyout\\anime 里面。
    """
    if not path or not isinstance(path, str):
        return False
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    for root in _safe_read_roots():
        try:
            if os.path.commonpath([real, root]) == root:
                return True
        except ValueError:
            # 不同盘符，commonpath 会抛异常 —— 那就是不在这个根下面
            continue
    return False


# 按扩展名给正确的类型；认不出来的一律 octet-stream。
# 之前不管什么文件都声明 image/png，浏览器会照着图片去解析任意内容。
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def content_type_for(path: str) -> str:
    return _CONTENT_TYPES.get(os.path.splitext(path)[1].lower(),
                              "application/octet-stream")


def _record_provenance(result_path: str, source_path: str) -> None:
    """Remember that result_path was repaired from source_path."""
    try:
        with _PROV_LOCK:
            d = _load_provenance()
            d[result_path] = source_path
            # keep the file from growing forever
            if len(d) > 400:
                for k in sorted(d, key=lambda x: os.path.getmtime(x)
                                if os.path.isfile(x) else 0)[:-300]:
                    d.pop(k, None)
            tmp = PROV_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
            os.replace(tmp, PROV_FILE)
    except Exception as e:
        warn("记录产出溯源失败", e)


def warn(where: str, err: BaseException) -> None:
    """Report a caught-and-recovered error.

    Silent handlers are how several real bugs stayed invisible in this project
    (a stuck download loop and a discarded HTTP error body were both `except:
    pass`). Anything swallowed deliberately still gets a line on stderr so the
    console shows what happened.
    """
    try:
        # 位置是**现取**的，不是写死的 —— 以前这里靠调用方在标签里手写
        # "(L####)"，文件一长就全部漂移（实测 7 处差 470~2424 行，
        # 照着手写行号翻过去会翻到完全不相干的代码，反而误导排查）。
        f = sys._getframe(1)
        loc = "%s:%d %s" % (os.path.basename(f.f_code.co_filename),
                            f.f_lineno, f.f_code.co_name)
        print("[warn] %s [%s]: %s: %s" % (where, loc, type(err).__name__, err),
              file=sys.stderr, flush=True)
    except Exception:
        pass


def translator() -> Translator:
    global _TR
    with _TR_LOCK:
        if _TR is None:
            _TR = Translator()
    return _TR


# ---------------------------------------------------------------- comfy api
_COMFY_WARNED: set = set()


def _warn_once(key: str, where: str, err: BaseException) -> None:
    """同一类 ComfyUI 连接错误只报一次。

    实测：ComfyUI 关着的时候刷一次页面会打 **10 行** `[warn]`（capabilities 要
    挨个问 object_info），把真正的错误淹掉。同一个原因报一次就够 ——
    第一次仍然会报，所以没有"静默失败"。连上之后就忘掉，下次再断还会报
    （见 comfy_open 的成功分支）。
    """
    if key in _COMFY_WARNED:
        return
    _COMFY_WARNED.add(key)
    warn(where, err)


def _log_exc(e: Exception) -> None:
    """真 bug 打完整 traceback；我们**故意抛**的、以及客户端断开的，只打一行或不出声。

    理由：ComfyUI 没开是**预期内**的情况。用户双击 `启动UI.bat` 之后那个黑窗口
    里不该出现一大段 Python traceback —— 那会让人以为程序坏了，去翻代码。
    消息本身已经是"照着改"的一句话，一行就够。

    客户端断开（`CLIENT_GONE`）也一样：那是用户刷新/关标签页，不是错误。
    """
    if isinstance(e, DELIBERATE):
        try:
            print("[err] %s" % e, file=sys.stderr, flush=True)
        except Exception:
            pass
        return
    if isinstance(e, CLIENT_GONE):
        return                          # 用户走了，没什么好报的
    import traceback
    traceback.print_exc()


class ComfyUnreachable(RuntimeError):
    """连不上 ComfyUI（没开 / 地址不对 / 卡住不动）。

    为什么要有这个类：不拦的话 `urlopen` 抛的是
        URLError: <urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>
    实测（`dev/probe_comfy_down.py`，把 ComfyUI 关掉点生成）：界面上就是这一句。
    用户看到"目标计算机积极拒绝"，既不知道说的是 ComfyUI，也不知道该怎么办 ——
    而这个项目的规矩是**报错要能照着改**。
    """


def _comfy_error(e: Exception) -> ComfyUnreachable:
    """把连接类异常翻成一句用户能照着做的事。

    ★ 这句话会**原样出现在界面状态栏里**（`失败：<这句话>`），而状态栏是生成
      按钮旁边一条右对齐的窄条 —— 太长会把那一行挤变形。所以两句都压到 60 字
      上下，只留"是什么 + 怎么办"；解释性的内容留给服务端日志那一行。
    """
    why = getattr(e, "reason", e)
    if isinstance(why, (socket.timeout, TimeoutError)):
        return ComfyUnreachable(
            "ComfyUI 没有响应（%s 超时）—— 它可能正忙或卡住了，"
            "看一眼它自己的窗口，必要时重启它再点生成。" % COMFY)
    return ComfyUnreachable(
        "连不上 ComfyUI（%s）—— 先启动它再点生成；"
        "如果你把它开在别的地址，改 config.json 里的 comfy_url。" % COMFY)


def comfy_open(target, timeout: int = 60):
    """打开一个 ComfyUI 的 URL。连不上/超时一律转成 ComfyUnreachable。

    ★ **所有**对 ComfyUI 的请求都要走这里。漏一个入口，那个入口的用户看到的
      就是 Python 的 URLError 原文 —— 而"漏了哪个入口"没人能一眼看出来，
      所以 `tests/test_comfy_down.py` 有一条静态检查钉着这件事。

    HTTPError（ComfyUI 自己的 4xx/5xx）**不在这里处理**：那是业务错误，
      响应体里有 ComfyUI 的解释（见 comfy_post / humanize_comfy_error）。
    """
    url = getattr(target, "full_url", None) or str(target)
    try:
        r = urllib.request.urlopen(target, timeout=timeout)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        # ★ 去重的 key 用**来源**（scheme://host:port），不用整个 URL。
        #   按整个 URL 去重等于没去重：capabilities 挨个问 object_info，
        #   每个路径都不同，于是刷一次页面还是 9 行 warn（实测）。
        sp = urllib.parse.urlsplit(url)
        _warn_once("%s://%s|%s" % (sp.scheme, sp.netloc,
                                   type(getattr(e, "reason", e)).__name__),
                   "请求 ComfyUI 失败(%s)" % url, e)
        raise _comfy_error(e) from None
    _COMFY_WARNED.clear()          # 连上了 —— 下次再断要重新报
    return r


def comfy_get(path: str, timeout: int = 60):
    with comfy_open(f"{COMFY}{path}", timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def humanize_comfy_error(detail: str) -> str:
    """Turn ComfyUI's validation JSON into a sentence the user can act on.

    It returns structures like:
      {"node_errors": {"4": {"errors": [{"type": "value_bigger_than_max",
        "message": "Value 999999 bigger than max of 16384",
        "extra_info": {"input_name": "height", ...}}]}}}
    Dumping that raw is unhelpful, so map the common cases to plain advice.
    """
    try:
        d = json.loads(detail)
    except Exception:
        return detail[:400]
    parts = []
    for node_id, blk in (d.get("node_errors") or {}).items():
        for err in (blk.get("errors") or []):
            t = err.get("type", "")
            info = err.get("extra_info") or {}
            field = info.get("input_name") or ""
            msg = err.get("message") or ""
            cls = blk.get("class_type") or ""
            if t == "value_bigger_than_max":
                cfg = info.get("input_config") or []
                mx = None
                if len(cfg) > 1 and isinstance(cfg[1], dict):
                    mx = cfg[1].get("max")
                parts.append("%s 超过上限%s：收到 %s，最大 %s"
                             % (field or cls, ("（最大 %s）" % mx) if mx else "",
                                info.get("received_value"), mx))
            elif t == "value_smaller_than_min":
                parts.append("%s 低于下限" % (field or cls))
            elif t == "value_not_in_list":
                # ★ 这一条原来掉进下面的 else，只输出 "sampler_name: Value not in
                #   list" —— 不说是哪个值、也不说合法值有哪些，等于没报错。
                #   收到什么 + 可选什么，才是能照着改的信息。
                cfg = info.get("input_config") or []
                opts = []
                if len(cfg) > 1 and isinstance(cfg[1], dict):
                    opts = [str(x) for x in (cfg[1].get("options") or [])]
                got = info.get("received_value")
                parts.append("%s 填的是 %r，ComfyUI 认不出来%s"
                             % (field or cls or ("节点 %s" % node_id), got,
                                ("。可选：%s%s" % (", ".join(opts[:10]),
                                                   " …" if len(opts) > 10 else ""))
                                if opts else ""))
            elif t in ("custom_validation_failed", "invalid_input"):
                m = err.get("details") or msg
                if "Invalid image file" in str(m):
                    name = str(m).split(":")[-1].strip()
                    parts.append("图片文件不存在或格式不支持：%s" % name)
                else:
                    parts.append(str(m)[:160])
            else:
                parts.append("%s: %s" % (field or cls or node_id, msg[:140]))
    if not parts:
        return (d.get("error", {}) or {}).get("message") or detail[:300]
    return "；".join(parts[:3])


def comfy_post(path: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        f"{COMFY}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with comfy_open(req, timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # ComfyUI explains rejected prompts in the response body; without this
        # the caller only sees a bare "HTTP Error 400".
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"ComfyUI 拒绝了任务：{humanize_comfy_error(detail)}") from None
    return json.loads(body) if body.strip() else {}


# ------------------------------------------------- 自动释放显存
# 为什么需要它：我们**从来不让 ComfyUI 卸载模型**。每出一张图，模型就进一次
# 显存；换底模时新的进来了、旧的还挂着。ComfyUI 自己有 unload_all_models() /
# cleanup_models() / soft_empty_cache()，但**得有人喊它才动**。
#
# 实测症状（RTX 4060 Laptop 8 GB）：显存吃到 4.18 GB，ComfyUI 那个进程的
# 系统内存挂到 7.5 GB —— 显存不够就往系统内存挤，两头都满就卡死。
# 用户反馈「app 开太久容易卡死」就是这么来的。
#
# 做法：每出 N 张图，主动喊一次 ComfyUI 的 POST /free。
#   * 只在**队列空了**的时候喊。ComfyUI 的 /free 不打断正在跑的任务，但
#     用户用 ComfyUI 界面自己排了队时，卸载会白白增加一次重载。
#   * 只在**出完图**之后喊，不在提交前喊 —— 提交前喊等于刚卸载就重载，
#     每张图都多一次 6.5 GB 的读盘。
_AUTO_FREE_LOCK = threading.Lock()
_AUTO_FREE_COUNT = 0
# 为什么是 5：实测这张 8 GB 卡出一张 512x512 的图就吃掉 5.51 GB 显存，
# 出第二张时就已经在往系统内存挤了。所以留的余量必须小于 5 张。
# 调大（更少清理）会在 8 GB 卡上重新出现卡顿；调小（更勤清理）不会更安全，
# 只会让每张图都多等一次模型重载（6.5 GB 读盘）。2026-09-20 由 10 调成 5。
_AUTO_FREE_EVERY = 5           # 每 N 张释放一次；0 = 关掉


def free_comfy_memory(force: bool = False) -> bool:
    """让 ComfyUI 卸载模型、清掉显存缓存。返回是否真的执行了。

    `force=False` 时只在队列为空时动手。任何失败都只记日志、不抛 ——
    清理是「顺手做的好事」，**绝不能因为它失败而让出图报错**。
    """
    try:
        if not force:
            q = comfy_get("/queue")
            running = len(q.get("queue_running") or [])
            pending = len(q.get("queue_pending") or [])
            if running or pending:
                return False
        comfy_post("/free", {"unload_models": True, "free_memory": True})
        return True
    except Exception as e:
        warn("释放显存失败（不影响出图）", e)
        return False


def maybe_auto_free() -> bool:
    """累计出图张数，够 N 张就释放一次显存。"""
    global _AUTO_FREE_COUNT
    if _AUTO_FREE_EVERY <= 0:
        return False
    with _AUTO_FREE_LOCK:
        _AUTO_FREE_COUNT += 1
        if _AUTO_FREE_COUNT < _AUTO_FREE_EVERY:
            return False
        _AUTO_FREE_COUNT = 0
    ok = free_comfy_memory()
    # 必须留一行日志。踩过的坑：第一版成功时**完全静默**，于是「它到底有没有
    # 在跑」只能靠猜 —— 实测时用户出了 6 张图，我只能从"显存没满、两次出图
    # 之间隔了 30 秒"倒推出它触发过。清理失败会 warn，成功却无声，等于
    # 这条链路没有任何可观察性：哪天它不触发了，没有任何迹象。
    # 只每 N 张打一行，不刷屏。
    print("[清理] 已连续出图 %d 张，%s（显存/系统内存已让 ComfyUI 释放）"
          % (_AUTO_FREE_EVERY, "成功" if ok else "跳过（队列非空或 ComfyUI 不可达）"),
          file=sys.stderr, flush=True)
    return ok


def object_info_choices(node: str, field: str, default: list | None = None) -> list:
    """Options ComfyUI reports for a COMBO input, e.g. installed model files.

    ★ ComfyUI 现在**两种格式混用**，只认一种会静默拿到垃圾：
        旧： "model_name": [["a.pth", "b.pth"], {...}]        -> spec[0] 是 list
        新： "model_name": ["COMBO", {"options": ["a.pth"]}]  -> spec[0] 是字符串
      实测这台机器（ComfyUI 0.35.1）：KSampler / LoraLoader / CheckpointLoaderSimple /
      VAELoader / ControlNetLoader / CLIPVisionLoader 还是旧格式，而
      **UpscaleModelLoader 已经是新格式**。
      只按旧格式取 spec[0]，新格式会返回 list("COMBO") == ['C','O','M','B','O'] ——
      一个看着像列表、其实是五个字母的返回值，不会报错，只会让界面上出现
      "采样器 C 不是这台 ComfyUI 支持的"这种鬼话。
    """
    try:
        info = comfy_get(f"/object_info/{node}")
        spec = info[node]["input"]["required"][field]
        if isinstance(spec[0], str):
            # 新格式：["COMBO", {"multiselect": false, "options": [...]}]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            return list(opts.get("options") or [])
        return list(spec[0])
    except Exception:
        return default or []


# ---------------------------------------------------------------- capabilities
def _read_safetensors_header(path: str, max_header: int = 8_000_000) -> dict | None:
    """Read ONLY the JSON header of a .safetensors file.

    The header sits at the start, so this never reads the multi-hundred-MB
    weight payload.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(8)
            if len(raw) < 8:
                return None
            n = int.from_bytes(raw, "little")
            if n <= 0 or n > max_header:
                return None
            return json.loads(fh.read(n).decode("utf-8"))
    except Exception:
        return None


# Tags that appear in nearly every anime caption and therefore carry no
# identifying information. Including them as "trigger words" would be noise.
_GENERIC_TAGS = {
    "1girl", "1boy", "2girls", "solo", "smile", "open mouth", "blush",
    "looking at viewer", "close-up", "portrait", "upper body", "full body",
    "bangs", "long sleeves", "short sleeves", "blurry", "simple background",
    "white background", "solo focus", "from side", "from behind", "sitting",
    "standing", "outdoors", "indoors", "day", "night", "holding",
    "closed eyes", "hair between eyes", "light particles", "depth of field",
}


def extract_triggers(path: str) -> dict:
    """Derive trigger words from a LoRA's own training metadata.

    What the data supports (verified against the real Viola file): kohya writes
    ss_tag_frequency, a per-bucket map of caption tag -> count. Merging the
    buckets yields the full training vocabulary - 109 tags for Viola - and the
    identifying ones are recoverable from it:
        concept   the Danbooru "name (series)" tag, highest count
        appearance dark green hair / mole under mouth / one side up / ...
        clothing  akiha school uniform / ...
    Coverage is good but NOT perfect: the official page also lists
    "single hair bun" (the caption used "single side bun") and "uneven eyes"
    (absent). Anything missing can be added by hand in the UI.

    Hand-made enhancer LoRAs carry no metadata at all and simply have no
    trigger words - they work through their weight alone.
    """
    hdr = _read_safetensors_header(path)
    if not hdr:
        return {"all": [], "concept": "", "appearance": [], "clothing": [],
                "candidates": [], "auto": False, "confident": False,
                "note": "读不到元数据，该 LoRA 无触发词"}
    meta = hdr.get("__metadata__") or {}

    # 1) an explicit list supplied by the author wins
    for key in ("trainedWords", "trained_words", "ss_trained_words",
                "trigger_words", "activation_text"):
        v = meta.get(key)
        if not v:
            continue
        try:
            d = json.loads(v) if isinstance(v, str) else v
        except Exception as e:
            warn("解析触发词字段失败(L%s)" % key, e)
            continue
        seq = d if isinstance(d, list) else ([d] if isinstance(d, str) else [])
        seq = [str(x).strip() for x in seq if str(x).strip()]
        if seq:
            return {"all": seq, "concept": seq[0], "appearance": [], "clothing": [],
                    "candidates": seq, "auto": True, "confident": True,
                    "note": "来自 LoRA 元数据中的作者声明"}

    tf = meta.get("ss_tag_frequency")
    if not tf:
        return {"all": [], "concept": "", "appearance": [], "clothing": [],
                "candidates": [], "auto": False, "confident": False,
                "note": "无触发词：该 LoRA 未包含训练元数据，挂上即生效"}
    try:
        d = json.loads(tf) if isinstance(tf, str) else tf
    except Exception as e:
        warn("解析 ss_tag_frequency 失败", e)
        return {"all": [], "concept": "", "appearance": [], "clothing": [],
                "candidates": [], "auto": False, "confident": False,
                "note": "训练元数据无法解析"}

    counts: dict = {}
    for bucket in (d or {}).values():
        if isinstance(bucket, dict):
            for tag, c in bucket.items():
                t = str(tag).replace("\\", "").strip()
                if t:
                    counts[t] = counts.get(t, 0) + (c if isinstance(c, int) else 0)
    if not counts:
        return {"all": [], "concept": "", "appearance": [], "clothing": [],
                "candidates": [], "auto": False, "confident": False,
                "note": "训练元数据为空"}

    # concept: a Danbooru "name (series)" tag, else the highest-count distinct tag
    named = [t for t in counts if "(" in t and ")" in t]
    concept = max(named, key=lambda t: counts[t]) if named else ""
    if not concept:
        rest = [t for t in counts if t.lower() not in _GENERIC_TAGS]
        concept = max(rest, key=lambda t: counts[t]) if rest else ""

    def distinctive(t: str) -> bool:
        low = t.lower()
        if low in _GENERIC_TAGS:
            return False
        if t == concept:
            return False
        if len(t) < 3:
            return True          # things like ":3" are real triggers
        return True

    ranked = [t for t in sorted(counts, key=lambda x: -counts[x]) if distinctive(t)]

    # clothing tags are recognisable by a small keyword set
    cloth_kw = ("uniform", "skirt", "dress", "shirt", "jacket", "coat", "socks",
                "shoes", "ribbon", "bow", "hat", "gloves", "boots", "sweater",
                "vest", "tie", "scarf", "cloak", "cape", "outfit", "wear",
                "sleeves", "stockings", "pantyhose", "choker", "earrings")
    clothing = [t for t in ranked if any(k in t.lower() for k in cloth_kw)][:4]
    appearance = [t for t in ranked if t not in clothing][:8]

    # Confidence gate. A genuine character LoRA is named after its concept, so
    # the concept tag matches the filename. Quality/style LoRAs that DO carry
    # metadata (e.g. illustrious_masterpieces_v3 has 2560 tags) have their
    # sample-image captions stored instead, and mining those yields nonsense
    # like "star (symbol)". Auto-applying that would corrupt every prompt, so
    # only a filename-matching concept is trusted for automatic insertion;
    # everything else is offered as a suggestion the user can opt into.
    stem = re.sub(r"[^a-z0-9]", "",
                  os.path.splitext(os.path.basename(path))[0].lower())
    concept_key = re.sub(r"[^a-z0-9]", "", concept.lower()) if concept else ""
    confident = bool(concept_key) and concept_key in stem

    # Do not flood the prompt: concept + a few identifiers is what the model
    # needs; the full training vocabulary would crowd out the user's own tags.
    seq = [x for x in ([concept] + appearance + clothing) if x]

    if not confident:
        note = ("该 LoRA 的元数据不像角色触发词（可能是画质/画风类的训练示例标签），"
                "已默认不自动添加；如确认需要可在下方手动勾选")
        return {"all": [], "concept": concept, "appearance": appearance,
                "clothing": clothing, "candidates": seq, "auto": False,
                "confident": False, "note": note}

    return {
        "all": seq,
        "concept": concept,
        "appearance": appearance,
        "clothing": clothing,
        "candidates": seq,
        "auto": True,
        "confident": True,
        "note": "自动提取自 LoRA 训练元数据（%d 个候选标签中筛选）" % len(counts),
    }


# ComfyUI accepts BOTH LoRA key spellings. `comfy/lora.py`'s
# `model_lora_keys_unet` builds its key_map from two sources: the model's own
# `diffusion_model.input_blocks.*` keys (giving the kohya spelling
# `lora_unet_input_blocks_1_0_in_layers_2`) and `comfy.utils.unet_to_diffusers()`
# (giving the diffusers spelling `lora_unet_down_blocks_0_resnets_0_conv1`).
# Both names point at the same weight.
#
# Measured on the real rinFlanimeIllustrious_v50 checkpoint: the key_map holds
# 6970 entries, split by KEY spelling into 3346 kohya-form and 3624
# diffusers-form (3346 + 3624 = 6970; counted on `km`'s KEYS -- its VALUES are
# always the base model's own `diffusion_model.*` keys and contain no
# `down_blocks` at all, so counting values gives a nonsense "0 diffusers").
# A diffusers-named LoRA therefore loads fine -- verified by feeding the file's
# real tensors to `comfy.lora.load_lora` and getting 346 patches, and by
# converting the same file to kohya spelling and getting the identical 346
# patches with zero differing values.
#
# This function used to call diffusers naming "incompatible", which made the
# app refuse to wire a perfectly good LoRA into the graph. Do not reinstate
# that: naming scheme is NOT a compatibility signal. Only `readable` and
# `not a UNet LoRA at all` are.
_SDXL_KOHYA = re.compile(r"lora_unet_(input_blocks|middle_block|output_blocks)_")
_SDXL_DIFFUSERS = re.compile(r"lora_unet_(down_blocks|mid_block|up_blocks)_")
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def lora_key_layout(path: str) -> dict:
    """Which naming scheme does this LoRA use? (Both work, so this is cosmetic.)

    `compatible` means "ComfyUI can load this", not "uses one particular
    spelling". It is False only when we can tell the file is not an SDXL UNet
    LoRA at all.
    """
    hdr = _read_safetensors_header(path)
    if not hdr:
        return {"compatible": None, "kohya": 0, "diffusers": 0, "text_encoder": 0,
                "note": "读不到文件头"}
    keys = [k for k in hdr if k != "__metadata__"]
    kohya = sum(1 for k in keys if _SDXL_KOHYA.search(k))
    diff = sum(1 for k in keys if _SDXL_DIFFUSERS.search(k))
    te = sum(1 for k in keys if k.startswith("lora_te"))
    if kohya and diff:
        return {"compatible": True, "kohya": kohya, "diffusers": diff,
                "text_encoder": te,
                "note": "命名格式兼容（两种命名混用，ComfyUI 都认）"}
    if kohya:
        return {"compatible": True, "kohya": kohya, "diffusers": 0,
                "text_encoder": te, "note": "命名格式兼容"}
    if diff:
        return {"compatible": True, "kohya": 0, "diffusers": diff,
                "text_encoder": te,
                "note": "命名格式兼容（diffusers 命名 down_blocks；"
                        "ComfyUI 同样认得，不影响使用）"}
    return {"compatible": None, "kohya": 0, "diffusers": 0, "text_encoder": te,
            "note": "不是 SDXL UNet LoRA（可能是 SD1.5 / Pony / Flux 版本）"}


# Civitai's own category set (the site's "Filter by Category" panel). Used as a
# SECONDARY signal only: matching a local file to a Civitai model by filename
# proved unreliable (illustrious_masterpieces_v3 matched a Dandadan character
# model), so a Civitai category is accepted only on a high-confidence name
# match. Filename and training metadata remain the primary signals.
CIVITAI_CATEGORIES = (
    "Action", "Animal", "Assets", "Background", "Base Model", "Buildings",
    "Celebrity", "Character", "Clothing", "Concept", "Objects", "Poses",
    "Style", "Tool", "Vehicle",
)

# Filename keyword -> Civitai-style category. Checked before metadata because
# the author's own naming is usually the most honest description of purpose.
_NAME_CATEGORY = (
    (r"hand|foot|feet|finger|arm|leg\b", "Poses"),
    (r"anatomy|proportion|body|pose", "Poses"),
    (r"face|eye|skin|hair", "Character"),
    (r"nipple|penis|pussy|genital|nsfw|lewd|hentai", "Concept"),
    (r"cloth|outfit|dress|uniform|suit|wear", "Clothing"),
    (r"style|artstyle|painting|sketch|lineart", "Style"),
    (r"detail|quality|enhance|sharp|upscale|texture", "Tool"),
    (r"background|scene|scenery|landscape", "Background"),
    (r"light|shadow|glow|effect", "Tool"),
    (r"character|girl|boy", "Character"),
)


def _cat_by_name(stem: str, origin: str) -> dict | None:
    """按名字里的关键词判类。命中返回 {category, source, kind}，没命中返回 None。

    单独抽出来是因为现在有**两个**名字要一起看（见 `lora_category`）：文件名，
    以及从 C站目录查回来的作者给的真名。两个都要过同一套规则，命中谁先看优先级。
    """
    if not stem:
        return None
    # A LoRA named "画质/quality/detail/enhance" is a quality tool, and it must
    # NOT then be filed under Style/Concept - that is the mismatch the UI showed
    # ("画质增强·通用" sitting inside 画风 > Concept). Checked after the filename
    # pass so an explicit "hand/pose" name still wins.
    _TOOL_WORDS = ("quality", "masterpiece", "aesthetic", "detail", "enhance",
                   "sharpen", "texture", "upscale", "improve", "fix")
    has_tool_word = any(w in stem for w in _TOOL_WORDS)

    for pat, cat in _NAME_CATEGORY:
        if re.search(pat, stem):
            if cat == "Tool":
                return {"category": "Tool", "source": origin, "kind": "quality"}
            if has_tool_word:
                return {"category": "Tool", "source": origin + "+画质词",
                        "kind": "quality"}
            return {"category": cat, "source": origin}

    if has_tool_word:
        return {"category": "Tool", "source": origin + "画质词", "kind": "quality"}
    return None


# Civitai's own category labels as they appear inside the **tags the author
# picked on the upload form** (`style` / `character` / `concept` / `poses` …).
#
# ★ 这里有个踩过的坑：C站的「分类」不是一个字段。v1 的模型对象键里根本没有
#   `category`，`type` 永远是 "LORA"，而 by-hash 那个接口返回的 `tags` 是**空
#   数组** —— 只看它就会得出"A站上查不到分类"。真正的标签要去
#   `/api/v1/models/<id>` 抓（`dev/make_lora_catalog.py` 现在会抓第二趟）。
#
# 实测 22 个本机 LoRA：`style` 标签覆盖了 13 条里除 2 条外的所有；而
# `detail` / `enhancer` 一起出现时，"style" 说的其实是**底模**的画风而不是
# 这个 LoRA 的用途（`IL20-NP43i_v2` 是个 ADetailer 用的乳首 LoRA，也挂着
# style+anime）—— 所以"画质词兼职"这条规则在这儿照样要挡一道。
_TAG_CATEGORY = {
    "character": ("Character", "character"),
    "anime character": ("Character", "character"),
    "game character": ("Character", "character"),
    "female characters": ("Character", "character"),
    "clothing": ("Clothing", None),
    "clothings": ("Clothing", None),
    "poses": ("Poses", None),
    "pose": ("Poses", None),
    "action": ("Action", None),
    "background": ("Background", None),
    "backgrounds": ("Background", None),
    "background plate": ("Background", None),
    "objects": ("Objects", None),
    "vehicle": ("Vehicle", None),
    "animal": ("Animal", None),
    "tool": ("Tool", "quality"),
    "enhancer": ("Tool", "quality"),
}
# 只有**画风**这一族需要多个同义词（作者挂的是 `style` / `styles` / `artstyle`
# / `art style` / `style pack`，写法很散），其余用精确匹配就够 —— 精确匹配不会
# 把 `background pony`（一个底模梗）误判成 Background。
_TAG_STYLE = ("style", "styles", "artstyle", "art style", "art styles",
              "flat style", "screencap style", "style pack")
_TOOL_TAG_WORDS = ("detail", "enhancer", "undetailed", "quality", "fix")


def _cat_by_tags(tags, origin: str) -> dict | None:
    """按作者挂的 C站标签判类。`tags` 是字符串序列。"""
    if not tags:
        return None
    low = [str(t).strip().lower() for t in tags if str(t).strip()]
    toolish = any(w in t for t in low for w in _TOOL_TAG_WORDS)
    for t in low:
        hit = _TAG_CATEGORY.get(t)
        if hit:
            cat, kind = hit
            d = {"category": cat, "source": origin}
            if kind:
                d["kind"] = kind
            return d
    for t in low:
        if t in _TAG_STYLE:
            if toolish:
                return {"category": "Tool", "source": origin + "·画质标签",
                        "kind": "quality"}
            return {"category": "Style", "source": origin}
    return None


def lora_category(name: str, meta: dict, filename: str,
                  facts: dict | None = None) -> dict:
    """Best-effort category for a LoRA, borrowing Civitai's vocabulary.

    Returns {"category", "source"} and sometimes "kind". The order of trust:
      1. **C站目录里作者给的真名 / 作者挂的标签**（`facts`）—— 事实层，最高
      2. filename keywords  (the author's own label)
      3. training metadata  (base model + tensor layout)
      4. default Concept

    `facts` 是 `lora_catalog.json` 里按 SHA256 查回来的公开信息（作者的模型名 /
    版本名 / 触发词 / 标签）。以前只有文件名可看，于是本地叫
    `8be1e5d2cc7cd938037337603fb51565.safetensors` 的文件连猜都猜不了；而
    `MSS_v2_IL.safetensors` 这种名字**完全看不出**它是 "Illustrious Style Pack"。
    现在按「真名 → 标签 → 文件名」的顺序各过一遍，谁先命中听谁的。

    ★ 为什么"真名"排在"标签"前面：真名里出现 `Style` / `Screencap` 是作者
    **主动写给这个 LoRA** 的，比 upload 表单上随手勾的标签更具体；反过来先看
    标签的话，`[Illustrious-XL] Nipple LORA for ADetailer` 会因为挂了 `style`
    标签被判成画风，而它实际上是个画质工具类的东西。
    """
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    cat = _cat_by_name(stem, "文件名")

    if facts:
        cn = str(facts.get("civitai_name") or "")
        hit = _cat_by_name(cn.lower(), "C站模型名") if cn else None
        if hit:
            cat = hit
        else:
            tag_cat = _cat_by_tags(facts.get("civitai_tags") or [], "C站标签")
            if tag_cat:
                cat = tag_cat
            elif cn and cat is not None and cat.get("source") == "文件名":
                # 真名和标签都没命中，只剩文件名猜的 —— 保留它，但把来源标清楚，
                # 让界面上能看出"这条是猜的"，别和事实混在一起。
                cat = dict(cat)
                cat["source"] = "文件名（猜的）"

    if cat is not None:
        return cat

    # Filing every metadata-bearing file under Character was wrong:
    # illustrious_masterpieces_v3 has training metadata but is a quality/style
    # enhancer, so metadata alone is not enough.
    has_tf = bool(meta.get("ss_tag_frequency"))
    if has_tf:
        try:
            tf = meta["ss_tag_frequency"]
            d = json.loads(tf) if isinstance(tf, str) else tf
            merged = {}
            for bucket in (d or {}).values():
                if isinstance(bucket, dict):
                    for tag, c in bucket.items():
                        t = str(tag).replace("\\", "").strip().lower()
                        merged[t] = merged.get(t, 0) + (c if isinstance(c, int) else 0)
            # a "name (series)" tag means a specific subject was trained
            if any("(" in t and ")" in t for t in merged):
                return {"category": "Character", "source": "训练元数据(角色标签)",
                        "kind": "character"}
            return {"category": "Style", "source": "训练元数据", "kind": "style"}
        except Exception as e:
            # 元数据坏了就退回默认分类，但不能静默 —— 否则用户只看到
            # 「分类是 Concept」，不知道是解析炸了还是本来就没元数据。
            warn("读取 LoRA 训练元数据失败", e)

    return {"category": "Concept", "source": "默认"}


def classify_lora(path: str, alias: str = "", alias_over: dict | None = None,
                  facts: dict | None = None) -> dict:
    """Guess what a LoRA is for, from its own training metadata.

    Signals actually present in the files examined here:
      * LoRAs trained by kohya write a full training block (ss_* keys) that
        names the concept, the base model and the tag frequency.
      * Hand-made "enhancer" LoRAs often carry NO metadata at all.
      * A concept/character LoRA patches only the UNet; general-purpose ones
        frequently patch the text encoder too.
      * `facts` = the shipped LoRA catalogue entry for this exact file (looked up
        by name, generated from a SHA256 match against Civitai). It carries the
        author's own model name and declared trigger words, which beats every
        guess above.
    """
    name = os.path.basename(path)
    info = {
        "file": name,
        "size_mb": round(os.path.getsize(path) / 1024 / 1024, 1),
        "kind": "unknown",
        "label": "未识别",
        "concept": "",
        "base_model": "",
        "trained_images": None,
        "note": "",
    }
    hdr = _read_safetensors_header(path)
    if not hdr:
        info["note"] = "读不到文件头"
        return info

    meta = hdr.get("__metadata__") or {}
    keys = [k for k in hdr if k != "__metadata__"]
    te = sum(1 for k in keys if k.startswith("lora_te") or "text_model" in k)
    unet = sum(1 for k in keys if k.startswith("lora_unet"))
    info["tensors_te"] = te
    info["tensors_unet"] = unet

    base = str(meta.get("ss_sd_model_name") or meta.get("ss_base_model_version") or "")
    info["base_model"] = base

    # pull a human-readable concept out of tag_frequency if present
    concept = ""
    tf = meta.get("ss_tag_frequency")
    if tf:
        try:
            d = json.loads(tf) if isinstance(tf, str) else tf
            counts = {}
            for bucket in (d or {}).values():
                if isinstance(bucket, dict):
                    for tag, c in bucket.items():
                        clean = tag.replace("\\", "").strip()
                        counts[clean] = counts.get(clean, 0) + (c if isinstance(c, int) else 0)
            stem = os.path.splitext(name)[0].lower()
            cands = list(counts)
            # Danbooru character tags look like "viola (bang dream!)"; the
            # parentheses are the strongest signal of a specific subject.
            paren = [t for t in cands if "(" in t and ")" in t]
            named = [t for t in cands if t.lower().replace(" ", "") in stem.replace("_", "")]
            concept = (paren[0] if paren else
                       named[0] if named else
                       (min(counts, key=counts.get) if counts else ""))
        except Exception as e:
            warn("清理中转文件失败", e)
    info["concept"] = concept
    tri = extract_triggers(path)
    info["triggers"] = tri.get("all", [])
    info["trigger_detail"] = tri
    info["key_layout"] = lora_key_layout(path)
    if meta.get("ss_num_train_images"):
        try:
            info["trained_images"] = int(meta["ss_num_train_images"])
        except Exception as e:
            warn("解析训练图片数失败", e)

    has_training_meta = bool(
        meta.get("ss_output_name") or meta.get("ss_tag_frequency")
        or meta.get("ss_num_train_images") or meta.get("ss_dataset_dirs"))

    # ★ `kind_source` 从第一处分派就要写上。它是界面上那个"来源：xxx"角标唯一
    #   的输入 —— 以前只在最后 cat 分支里赋值，于是**走元数据分派的那些条目
    #   角标是空的**，而那恰恰是最需要标出来的（元数据也会猜错）。
    if has_training_meta and 0 < unet and te == 0:
        info["kind"] = "character"
        info["label"] = "角色 / 概念"
        info["note"] = "训练元数据齐全，只改图像模型 —— 典型的角色或特定概念 LoRA"
        info["kind_source"] = "训练元数据"
    elif has_training_meta:
        info["kind"] = "style"
        info["label"] = "画风 / 概念"
        info["note"] = "有训练元数据，同时调整文本编码器"
        info["kind_source"] = "训练元数据"
    elif not meta and info["size_mb"] < 150:
        info["kind"] = "quality"
        info["label"] = "画质增强"
        info["note"] = "无训练元数据且体积小 —— 多为细节/画质增强类"
        info["kind_source"] = "体积推断"
    else:
        info["kind"] = "other"
        info["label"] = "其他"
        info["note"] = "无法从元数据判定"
        info["kind_source"] = "兜底"

    # Compatibility OUTRANKS the guess above: a LoRA whose keys ComfyUI cannot
    # match loads without error but applies nothing, so it must never be
    # presented as a usable "quality" or "style" entry. This check has to come
    # after the classification or the branch above would overwrite it.
    if info["key_layout"].get("compatible") is False:
        info["kind"] = "incompatible"
        info["label"] = "不兼容"
        info["kind_source"] = "格式检测"
        info["note"] = info["key_layout"]["note"]
        info["category"] = "不兼容"
        info["category_source"] = "格式检测"
        return info

    cat = lora_category(name, meta, path, facts)
    # An explicit local name beats inference. If the alias says 画质增强 then the
    # LoRA belongs in that group, whatever the heuristics concluded - the user
    # naming it is stronger evidence than a filename guess.
    alias = alias or (info.get("alias") or "")
    # 别名表里若已明确指定分类（来自 C站原文核对），直接采用，不再靠关键词猜
    if isinstance(alias_over, dict) and alias_over.get("kind"):
        info["kind"] = alias_over["kind"]
        info["kind_source"] = "你标的"
        info["category"] = alias_over.get("category") or info.get("category")
        info["category_source"] = "C站原文核对"
        info["label"] = {"character": "角色", "style": "画风",
                         "quality": "画质增强"}.get(alias_over["kind"],
                                                    alias_over["kind"])
        if info["key_layout"].get("compatible") is not False:
            return info
    _Q = ("画质", "增强", "修复", "锐化", "细节")
    _C = ("角色", "人物", "人脸", "脸")
    if alias:
        if any(w in alias for w in _Q):
            cat = {"category": "Tool", "source": "本地命名", "kind": "quality"}
        elif any(w in alias for w in _C):
            cat = {"category": "Character", "source": "本地命名", "kind": "character"}
    info["category"] = cat["category"]
    info["category_source"] = cat["source"]
    # The sub-category can override the top-level group. Without this the two
    # were computed independently and disagreed, which is how a LoRA named
    # "画质增强·通用" ended up listed under 画风 > Concept.
    if cat.get("kind"):
        info["kind"] = cat["kind"]
        info["label"] = {"character": "角色", "style": "画风",
                         "quality": "画质增强"}.get(cat["kind"], cat["kind"])
        info["kind_source"] = cat["source"]
    return info


LORA_DIR = os.path.join(MODELS_DIR, "loras")
# Local display names. The file itself is never renamed - only what the UI shows.
ALIAS_FILE = os.path.join(HERE, "lora_aliases.json")


def lora_aliases() -> dict:
    """Read the local alias table (file name -> {alias, note, url, source})."""
    try:
        with open(ALIAS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        warn("读取本地命名表失败", e)
        return {}


# ★ 随包发布的 LoRA「事实目录」。
#
# 为什么需要它：发布版原来**没有任何分类数据** —— `lora_aliases.json` 是作者的
# 私人备注（手工核对过的分类），永远不进发布包，发的是空模板。于是陌生用户装完
# 以后分类只剩「文件名关键词 + 训练元数据」两层硬猜，而文件名是 hash 的
# （`8be1e5d2cc7cd938037337603fb51565.safetensors`）连猜都猜不了，猜错还不报错。
#
# 这个文件里**只放事实**：按 SHA256 从 C站反查回来的作者真名 / 版本 / 底模 /
# 作者声明的触发词 / 链接。分类判断不进去（那是编辑判断，留在私人备注里）。
CATALOG_FILE = os.path.join(HERE, "lora_catalog.json")
# (数据, 用来判失效的 (mtime_ns, size), 同一个数据) —— 第三个位置存"读出来
# 的东西"，读失败时它是 `{}`，第二个位置是 None，于是下次调用会重试一次。
_CATALOG_CACHE = ({}, None, {})


def _catalog_load() -> dict:
    """读目录文件。按 mtime 缓存 —— 这个函数在每次 /api/capabilities 里都会走。

    两个 except 这样分的理由（原来写反了，`FileNotFoundError` 那一支**永远
    执行不到** —— 它是 `OSError` 的子类，先被上面那条吃掉）：
      · `OSError`：文件不在（发布版用户自己没生成过）或没权限 —— **正常的**，
        静默返回空目录，分类自动退回到文件名/元数据。
      · 其它异常：文件在、但内容是坏 JSON —— 这是**真出事了**，要说一声，
        但不能让整个 /api/capabilities 挂掉（那会让界面直接报"连不上"）。
    """
    global _CATALOG_CACHE
    try:
        st = os.stat(CATALOG_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CATALOG_CACHE = ({}, None, {})
        return {}
    if _CATALOG_CACHE[1] == key:
        return _CATALOG_CACHE[0]
    data = {}
    try:
        with open(CATALOG_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("loras"), dict):
            data = d["loras"]
        else:
            warn("LoRA 目录内容不是预期的 {loras:{...}} 形状（分类退回文件名/元数据）",
                 ValueError("顶层键 %r，loras 是 %s"
                            % (sorted(d)[:6] if isinstance(d, dict) else type(d).__name__,
                               type(d.get("loras")).__name__ if isinstance(d, dict)
                               else "n/a")))
    except Exception as e:
        # 静默失败是这个项目最忌讳的事：目录坏了要说一声，但不该拖垮整个接口。
        warn("读取 LoRA 目录失败（分类会退回到文件名/元数据）", e)
        data = {}
    _CATALOG_CACHE = (data, key, data)
    return data


def catalog_facts(name: str) -> dict | None:
    """按 ComfyUI 报出来的名字（可能带子目录）在目录里找一条。

    匹配顺序：完整名字 → 纯文件名 → 大小写不敏感的文件名。
    最后那一层是为了容忍用户在 Windows 上把扩展名改成 `.SAFETENSORS` 之类。
    """
    if not name:
        return None
    cat = _catalog_load()
    if not cat:
        return None
    base = os.path.basename(name.replace("\\", "/"))
    row = cat.get(name) or cat.get(base)
    if row is None:
        low = base.lower()
        for k, v in cat.items():
            if k.lower() == low:
                row = v
                break
    if not isinstance(row, dict):
        return None
    cv = row.get("civitai") or {}
    if not isinstance(cv, dict):
        return None
    # ★ 必须有 Civitai 那一节的**真名**才算查到。只判 sha256 是不够的：
    #   `dev/make_lora_catalog.py` 会给每个文件都写上 sha256（那是本地算的），
    #   而 C站上真查不到模型的条目它的 `civitai` 是 null。只判 sha256 的话这种
    #   条目会返回一个**名字全是空字符串的空壳**，于是：
    #     · 界面上的「来源」角标会显示"C站数据"—— 其实一条 C站数据都没有，
    #       这是在拿本地哈希冒充 C站事实；
    #     · 分类链会被一个空壳截胡，永远走不到文件名那一层。
    #   宁可返回 None：查不到就是查不到，让下一层去猜。
    if not (cv.get("model_name") or "").strip():
        return None
    out = {
        "sha256": row.get("sha256") or "",
        "bytes": row.get("bytes"),
        "civitai_name": cv.get("model_name") or "",
        "civitai_version": cv.get("version_name") or "",
        "civitai_base": cv.get("base_model") or "",
        "civitai_url": cv.get("url") or "",
        "civitai_words": [str(x) for x in (cv.get("trained_words") or []) if str(x).strip()],
        "civitai_tags": [str(x) for x in (cv.get("tags") or []) if str(x).strip()],
        "words_source": cv.get("words_source") or "",
        "weight_hint": cv.get("weight_hint") or None,
        "version_id": cv.get("version_id"),
        "file_meta": row.get("file_meta") or {},
    }
    return out


def _lora_path(name: str) -> str | None:
    """Resolve a LoRA name (as ComfyUI lists it) to a file on disk.

    ComfyUI reports files inside a scanned subfolder with a separator in the
    name ("_dsh_webapp_lora\\foo.safetensors"), and can hand back a doubled
    backslash, so the path is normalised before use. A bare basename is also
    accepted as a fallback so aliases keyed on the plain filename keep working
    for files that live in a subfolder.
    """
    if not name:
        return None
    rel = name.replace("\\", os.sep).replace("/", os.sep)
    rel = os.path.normpath(rel)
    p = os.path.join(LORA_DIR, rel)
    if os.path.isfile(p):
        return p
    flat = os.path.join(LORA_DIR, os.path.basename(rel))
    return flat if os.path.isfile(flat) else None


def lora_details() -> list:
    out = []
    aliases = lora_aliases()
    try:
        names = object_info_choices("LoraLoader", "lora_name")
    except Exception:
        return out
    # 用配置解析出来的目录，不要写死 —— 换电脑后写死的路径会指向不存在的
    # 位置，导致 lora_details() 返回空列表，看起来像"一个 LoRA 都没有"。
    root = LORA_DIR
    if not os.path.isdir(root):
        return out
    for n in names:
        p = os.path.join(root, n.replace("\\", os.sep).replace("/", os.sep))
        if os.path.isfile(p):
            # ComfyUI reports a subfolder file as "sub\name.safetensors"; the
            # alias table is keyed on the plain filename, so try both.
            a = (aliases.get(n) or aliases.get(os.path.basename(n))
                 or aliases.get(os.path.basename(n).replace("\\", "/")) or {})
            # 随包发布的 C站目录（只存事实）。查不到就是 None，退回到原有三层。
            cf = catalog_facts(n)
            # the alias is passed in so classification can honour an explicit
            # local name instead of only guessing from the filename
            info = classify_lora(p, alias=(a.get('alias') or '').strip(),
                                 alias_over=a, facts=cf)
            info['alias'] = (a.get('alias') or '').strip()
            info['alias_note'] = (a.get('note') or '').strip()
            info['alias_url'] = (a.get('url') or '').strip()
            info['alias_source'] = (a.get('source') or '').strip()
            if cf:
                # 作者真名只在**用户没自己命名**时才拿来当显示名 —— 本地命名
                # 是用户自己的话，优先级永远高于 C站。
                info['catalog_name'] = cf['civitai_name']
                info['catalog_version'] = cf['civitai_version']
                info['catalog_url'] = cf['civitai_url']
                info['catalog_base'] = cf['civitai_base']
                info['catalog_tags'] = cf['civitai_tags']
                info['catalog_sha256'] = (cf['sha256'] or '')[:12]
                info['catalog_weight_hint'] = cf['weight_hint']
                info['display'] = (info['alias'] or cf['civitai_name']
                                   or info['file'])
                # 备注：用户自己写的永远优先；没写才用 C站的版本名兜一句。
                if not info['alias_note'] and cf['civitai_version']:
                    info['catalog_note'] = "C站版本：%s" % cf['civitai_version']
                if not info['alias_url']:
                    info['alias_url'] = cf['civitai_url']
                if not info['alias_source']:
                    info['alias_source'] = "C站目录"
                # 作者声明的触发词优先于训练元数据推断 —— 后者常常只是训练样本
                # 图的标签，会误报。目录里的触发词还额外标了**来源**（字段还是
                # 正文），这样"字段为空"不会被当成"作者没写"。
                if cf['civitai_words']:
                    td = dict(info.get('trigger_detail') or {})
                    td['all'] = list(cf['civitai_words'])
                    td['candidates'] = td['all']
                    td['confident'] = True
                    td['auto'] = True
                    td['note'] = ("作者声明的触发词（来源：%s，按文件 SHA256 定位）"
                                  % (cf['words_source'] or "C站原文"))
                    info['trigger_detail'] = td
                    info['triggers'] = td['all']
                    info['triggers_source'] = "C站作者声明"
                info['civitai_model'] = cf['civitai_name']
                info['civitai_version'] = cf['civitai_version']
            else:
                info['display'] = info['alias'] or info['file']
            # 作者声明的触发词（通过文件哈希从 C站原文精确取得）优先于
            # 训练元数据推断 —— 后者常常只是训练样本图的标签，会误报。
            #
            # ⚠️ 这两条路的**来源不同，说明必须跟着变**：目录那一路是真按
            # SHA256 从 C站查回来的；别名表那一路是**用户手填**的。早先这里
            # 无论走哪条都写「来源：C站原文，按文件哈希定位」—— 用户手填的
            # 触发词被标成"哈希查证过"，等于给一个没验证过的值盖了个验证章。
            # 实测踩到：`r17329_illuu` 的 `sketch` 是手填的，界面却显示
            # 「按文件哈希定位」。
            src_trig = a.get('triggers_source')
            if isinstance(src_trig, list) and src_trig:
                td = dict(info.get('trigger_detail') or {})
                td['all'] = [str(x) for x in src_trig]
                td['candidates'] = td['all']
                # 手填的一份不标 confident —— 那是"哈希查证过"的意思，
                # 而这里只是用户说了算。
                td['confident'] = bool(cf)
                td['auto'] = bool(cf)
                td['note'] = ("作者声明的触发词（来源：C站原文，按文件哈希定位）"
                              if cf else
                              "你在本地命名表里写的触发词（未经哈希核对）")
                info['trigger_detail'] = td
                info['triggers'] = td['all']
            # ★ 别名表里没写的时候**不要用空串覆盖** —— 上面刚填好的目录信息会被
            #   抹掉。原来这里无条件赋值，那个 `or ''` 就是元凶：目录里的真名和
            #   版本刚写进 info，紧接着被 '' 覆盖，界面上什么都看不到。
            if a.get('civitai_model'):
                info['civitai_model'] = a['civitai_model']
            if a.get('civitai_version'):
                info['civitai_version'] = a['civitai_version']
            out.append(info)
        else:
            out.append({"file": n, "kind": "unknown", "label": "未识别",
                        "note": "文件未找到", "size_mb": None})
    return out


def capabilities() -> dict:
    # ★ 先花一次探测确认 ComfyUI 到底在不在，不在就**立刻返回**。
    #
    # 为什么必须这样：实测这台机器上**每一次"连不上"要 2.05 秒**才返回
    # （连一个开着的端口只要 0.014 秒 —— 某个安全软件/过滤驱动把 loopback 的
    #   RST 拖后了）。而下面这些探测加起来有 10 次请求 —— 于是 ComfyUI 没开时，
    # 页面要 **20 秒**才把能力标签算出来、才告诉用户"连不上"。
    # 用户看到的是"页面卡了半天然后报错"，而这 10 次请求全都不可能成功。
    # 现在：先探一次（成功就继续，失败就 2 秒返回同样形状的结果）。
    try:
        comfy_get("/system_stats", timeout=3)
        reachable = True
    except Exception:
        reachable = False

    if reachable:
        loras = object_info_choices("LoraLoader", "lora_name")
        ckpts = object_info_choices("CheckpointLoaderSimple", "ckpt_name")
        cnets = object_info_choices("ControlNetLoader", "control_net_name")
        clipv = object_info_choices("CLIPVisionLoader", "clip_name")

        # the IPAdapter node pack may or may not have loaded
        try:
            info = comfy_get("/object_info/IPAdapterUnifiedLoader")
            has_ipadapter_nodes = "IPAdapterUnifiedLoader" in info
        except Exception:
            has_ipadapter_nodes = False

        # KSampler exposes 45 samplers and 9 schedulers; the UI previously had a
        # hardcoded shortlist, so most of what the backend supports was hidden.
        samplers = object_info_choices("KSampler", "sampler_name")
        schedulers = object_info_choices("KSampler", "scheduler")
        vaes = object_info_choices("VAELoader", "vae_name")
        upscalers = object_info_choices("UpscaleModelLoader", "model_name")
    else:
        # 形状必须和"在线但什么都没有"时一致 —— 界面按同一套键读
        loras = ckpts = cnets = clipv = []
        samplers = schedulers = vaes = upscalers = []
        has_ipadapter_nodes = False

    # dedicated inpainting ControlNets, identifiable by filename
    inpaint_cns = [c for c in cnets
                   if re.search(r"inpaint", c, re.I)]

    return {
        "comfy_online": bool(ckpts),
        # "ComfyUI 到底连不连得上"—— 和 comfy_online 的区别：这个是**探到过它**
        # （/system_stats 有回应），comfy_online 是"读到了底模列表"。
        # 两者不同意味着"连得上但一个模型都没装"，那是最常见的新手状态。
        "comfy_reachable": reachable,
        "version": VERSION,
        # 本服务是从哪个目录跑起来的。
        # 为什么放进接口：端到端测试要确认"我连的服务就是我这个安装"。
        # 以前没有这个字段，于是测试只知道去敲 127.0.0.1:8765 —— 敲到的可能是
        # 另一个目录跑起来的实例（路径配置不同），然后报一堆和被测代码无关的
        # 失败。见 tests/test_server_guard.py。
        "app_dir": APP_DIR,
        # config.json 有问题的时候，这里是一句给用户看的原因（否则为 None）。
        # 为什么报到界面上：坏掉的 config 会静默回退成"自动探测"，用户填的
        # 产出目录整份失效、图跑到别处，而他**看不出任何异常**。服务端那行
        # [warn] 在黑窗口里一闪就过去了，所以必须有一条走到界面上。
        # 见 paths.CONFIG_ERROR 和 dev/probe_broken_config.py。
        #
        # ★ 读 `APPATHS.CONFIG_ERROR`（模块属性）而**不是**上面 import 进来的
        #   那个 `CONFIG_ERROR`：后者是 import 那一刻抄下来的值，而"config.json
        #   不见了"这条判据要到**启动时**才算得出来（要看产出目录里有没有出图），
        #   写回的是 paths 里的那个变量。用抄来的值的话，界面上什么都不显示 ——
        #   而且这正好是最该显示的那一种。
        "config_error": APPATHS.CONFIG_ERROR,
        "checkpoint": CHECKPOINT if CHECKPOINT in ckpts else (ckpts[0] if ckpts else None),
        "checkpoints": ckpts,
        "vaes": vaes,
        "loras": loras,
        "samplers": samplers,
        "schedulers": schedulers,
        "inpaint_controlnets": inpaint_cns,
        # ★ 也要跳过 lora_details()：它内部**又**会去问一次 ComfyUI
        #   （object_info_choices("LoraLoader")），于是"离线"这条路上白多花
        #   一次 2.05 秒。实测：不跳它 capabilities() 要 4.1 秒，跳掉只要 2.0 秒。
        "lora_details": lora_details() if reachable else [],
        "txt2img": bool(ckpts),
        "img2img": bool(ckpts),
        "sketch": bool(cnets) and bool(ckpts),
        "sketch_detail": {
            "controlnet_installed": bool(cnets),
            "controlnets": [
                {"file": c, "label": _controlnet_info(c)[0],
                 "hint": _controlnet_info(c)[1]}
                # 下拉里的顺序 = 默认那个排最前，免得用户点开看到的第一项
                # 是个"手画用不上"的模型（默认值见下面 default）
                for c in _default_first(cnets, CONTROLNET_SCRIBBLE)
            ],
            # 默认给「涂鸦」：这个下拉的主要输入是**画板上手画的草图**，而
            # openpose 要渲染好的骨架图才认（实测见 CONTROLNET_INFO 上面那段）。
            # 以前默认 openpose，于是用户按提示画个头+躯干+四肢，出来的图
            # 完全不按他画的样子摆姿势，还以为自己画错了。
            "default": (CONTROLNET_SCRIBBLE if CONTROLNET_SCRIBBLE in cnets
                        else (CONTROLNET_OPENPOSE if CONTROLNET_OPENPOSE in cnets
                              else (cnets[0] if cnets else None))),
        },
        "reference": has_ipadapter_nodes and bool(clipv),
        "reference_detail": {
            "ipadapter_nodes_loaded": has_ipadapter_nodes,
            "clip_vision_installed": bool(clipv),
            "clip_vision_expected": CLIP_VISION_FILE,
        },
        # 放大模型：界面上没有下拉框了（只剩一个模型，摆个单选项的下拉框没意义），
        # 只报"实际会用哪一个"，让界面显示成一行说明。改名/换模型只动上面那个常量。
        "upscale_model": (UPSCALE_MODEL_DEFAULT if UPSCALE_MODEL_DEFAULT in upscalers
                          else (upscalers[0] if upscalers else None)),
        "upscale_hint": UPSCALE_HINT,
    }


# ---------------------------------------------------------------- graph build
def resolve_lora_list(p: dict) -> list:
    """把请求里的 LoRA 选择解析成列表。**键存在就用它，不做「空即回退」。**

    这里修的是一个真实的静默回退 bug：
        loras = p.get("loras") or ([{"name": lora, ...}] if lora else [])
    空列表在 Python 里是 falsy，所以前端发 `loras: []`（用户取消了所有 LoRA）
    会掉进 or 的右边，把遗留的旧版单数 `lora` 字段重新挂回图上 ——
    **`loras: []` 表达不出「一个 LoRA 都不要」。**

    当前界面只发 `loras`、从不发 `lora`，所以触发不了；但任何发 `lora` 的
    旧客户端、脚本或测试都会中招。改成按「键是否存在」判断：
    显式传了 loras（哪怕是空数组）就是用户的最终意图，旧字段一律忽略。
    """
    if "loras" in p:
        return [x for x in (p.get("loras") or []) if isinstance(x, dict)]
    lora = p.get("lora") or ""
    if not lora:
        return []
    return [{"name": lora, "strength": float(p.get("lora_strength", 0.8))}]


def build_graph(p: dict) -> dict:
    """Assemble the ComfyUI API graph from the request parameters."""
    check_param_ranges(p)          # 越界/NaN 在写进图之前就拦住（-> 400）
    positive = p["positive"]
    negative = p.get("negative") or NEGATIVE_DEFAULT
    seed = int(p.get("seed") or 0)
    steps = int(p.get("steps", DEFAULTS["steps"]))
    cfg = float(p.get("cfg", DEFAULTS["cfg"]))
    sampler = p.get("sampler", DEFAULTS["sampler"])
    scheduler = p.get("scheduler", DEFAULTS["scheduler"])
    denoise = float(p.get("denoise", DEFAULTS["gen_denoise"]))
    # A denoise below 1.0 in txt2img leaves part of the initial gaussian noise in
    # the result, which renders as a translucent grey haze. The value is an
    # img2img control only, so it is forced here regardless of what the client
    # sends - the slider is shared and was leaking its default into txt2img.
    if p.get("mode", "txt2img") != "img2img":
        denoise = 1.0
    width = int(p.get("width", DEFAULTS["width"]))
    height = int(p.get("height", DEFAULTS["height"]))
    batch = int(p.get("count", DEFAULTS["count"]))
    # 旧版的 lora / lora_strength 字段已由 resolve_lora_list() 统一处理（含兼容），
    # 这里不再单独取值 —— 之前留着两个没人用的局部变量，ruff 报 F841。
    mode = p.get("mode", "txt2img")
    g: dict = {}
    nid = [0]

    def nid_next() -> str:
        nid[0] += 1
        return str(nid[0])

    # 1. checkpoint
    ckpt = nid_next()
    g[ckpt] = {"class_type": "CheckpointLoaderSimple",
               "inputs": {"ckpt_name": p.get("checkpoint") or CHECKPOINT}}
    model_src, clip_src = [ckpt, 0], [ckpt, 1]
    vae_src = [ckpt, 2]

    # 2. optional LoRA (can chain: list of {name, strength, triggers})
    # Trigger words come from the LoRA's own training metadata; they are added
    # to the positive prompt automatically and injected right after the quality
    # tags so the concept is established early.
    # 见 resolve_lora_list：显式传 loras 就以它为准，空数组 = 一个都不要。
    loras = resolve_lora_list(p)
    trigger_words = []
    skipped_loras = []
    for item in loras:
        if not item.get("name") or float(item.get("strength", 0)) <= 0:
            continue
        # Never wire an incompatible LoRA into the graph. It contributes no
        # weights (its keys do not match), yet still goes through attention
        # patching, which is what produced the flat grey output.
        lay = lora_key_layout(_lora_path(item["name"]) or "")
        if lay.get("compatible") is False:
            skipped_loras.append(item["name"])
            warn("跳过不兼容的 LoRA（命名格式不符，加载后不生效）", 
                 RuntimeError(item["name"]))
            continue
        ln = nid_next()
        g[ln] = {"class_type": "LoraLoader",
                 "inputs": {"model": model_src, "clip": clip_src,
                            "lora_name": item["name"],
                            "strength_model": float(item["strength"]),
                            "strength_clip": float(item["strength"])}}
        model_src, clip_src = [ln, 0], [ln, 1]
        for t in (item.get("triggers") or []):
            t = str(t).strip()
            if t and t.lower() not in positive.lower() and t not in trigger_words:
                trigger_words.append(t)

    if skipped_loras:
        p["skipped_loras"] = skipped_loras

    if trigger_words:
        # `positive` already starts with QUALITY (the caller builds it that way),
        # so insert triggers after the quality block and never re-add QUALITY -
        # otherwise the tags are duplicated and waste the 77-token budget.
        rest = positive
        if rest.lower().startswith(QUALITY.lower()):
            rest = rest[len(QUALITY):].lstrip(", ").strip()
        positive = ", ".join(x for x in (
            QUALITY, ", ".join(trigger_words), rest) if x)
        p["positive"] = positive
        p["trigger_words"] = trigger_words

    # 3. conditioning
    cond_model = model_src
    # Clip skip —— Illustrious 系的标准值（2）。是 RIN Flanime 模型页写明的，
    # 官方 Illustrious 系列同样按 2 来（这套模型就是在 clip skip 2 上训的）。
    # ComfyUI 用 CLIPSetLastLayer 实现，stop_at_clip_layer = -clip_skip。
    # 位置很重要：必须插在 LoRA 之后、文本编码之前，否则 LoRA 对文本编码器
    # 的影响会被这一步跳过。clip_src 此刻已指向 LoRA 链的末端。
    clip_skip_src = clip_src
    clip_skip = int(p.get("clip_skip", DEFAULTS["clip_skip"]) or 0)
    if clip_skip > 0:
        cs = nid_next()
        g[cs] = {"class_type": "CLIPSetLastLayer",
                 "inputs": {"clip": clip_skip_src, "stop_at_clip_layer": -clip_skip}}
        clip_skip_src = [cs, 0]
    pos = nid_next()
    g[pos] = {"class_type": "CLIPTextEncode",
              "inputs": {"clip": clip_skip_src, "text": positive}}
    neg = nid_next()
    g[neg] = {"class_type": "CLIPTextEncode",
              "inputs": {"clip": clip_skip_src, "text": negative}}
    pos_src, neg_src = [pos, 0], [neg, 0]

    # 4. latent: empty for txt2img, encoded source image for img2img
    if mode == "img2img" and p.get("source_image"):
        img = nid_next()
        g[img] = {"class_type": "LoadImage", "inputs": {"image": p["source_image"]}}
        enc = nid_next()
        g[enc] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": [img, 0], "vae": vae_src}}
        latent_src = [enc, 0]
    else:
        lat = nid_next()
        g[lat] = {"class_type": "EmptyLatentImage",
                  "inputs": {"width": width, "height": height, "batch_size": batch}}
        latent_src = [lat, 0]

    # 5. sketch guidance（画板上手画的草图 → ControlNet）
    if p.get("sketch_image") and p.get("use_sketch"):
        cimg = nid_next()
        g[cimg] = {"class_type": "LoadImage", "inputs": {"image": p["sketch_image"]}}
        cnl = nid_next()
        g[cnl] = {"class_type": "ControlNetLoader",
                  # 兜底值要和界面的默认值一致（涂鸦），不然"没传 controlnet"
                  # 的请求会走到另一个模型上。见 capabilities() 的 sketch_detail。
                  "inputs": {"control_net_name": p.get("controlnet") or CONTROLNET_SCRIBBLE}}
        cna = nid_next()
        g[cna] = {"class_type": "ControlNetApplyAdvanced",
                  "inputs": {"positive": pos_src, "negative": neg_src,
                             "control_net": [cnl, 0], "image": [cimg, 0],
                             "strength": float(p.get("sketch_strength", DEFAULTS["sketch_strength"])),
                             "start_percent": float(p.get("sketch_start", 0.0)),
                             "end_percent": float(p.get("sketch_end", 1.0))}}
        pos_src, neg_src = [cna, 0], [cna, 1]

    # 5b. 构图迁移（参考图 → 深度图 → ControlNet union）
    # 与上面 sketch 那条是同一条链：ControlNetApplyAdvanced 支持串接，
    # 后一个的 positive/negative 接前一个的输出，两路约束叠加。
    if p.get("depth_image") and p.get("use_depth"):
        dimg = nid_next()
        g[dimg] = {"class_type": "LoadImage", "inputs": {"image": p["depth_image"]}}
        dcnl = nid_next()
        g[dcnl] = {"class_type": "ControlNetLoader",
                   "inputs": {"control_net_name":
                              p.get("depth_controlnet") or CONTROLNET_UNION}}
        # union 一个顶多种，不指定类型它会按 auto 猜 —— 必须显式设成 depth
        dcn = nid_next()
        g[dcn] = {"class_type": "SetUnionControlNetType",
                  "inputs": {"control_net": [dcnl, 0], "type": "depth"}}
        dcna = nid_next()
        g[dcna] = {"class_type": "ControlNetApplyAdvanced",
                   "inputs": {"positive": pos_src, "negative": neg_src,
                              "control_net": [dcn, 0], "image": [dimg, 0],
                              "strength": float(p.get("depth_strength", DEFAULTS["depth_strength"])),
                              "start_percent": float(p.get("depth_start", 0.0)),
                              "end_percent": float(p.get("depth_end", 0.8))}}
        pos_src, neg_src = [dcna, 0], [dcna, 1]

    # 6. reference image(s) via IP-Adapter
    # IMPORTANT: IPAdapterAdvanced only honours combine_embeds when a SINGLE
    # node receives several images (its source checks img_cond_embeds.shape[0]
    # > 1). Chaining one node per image silently ignores combine_embeds, which
    # made every combine mode produce identical output. So the references are
    # resized to a common size and merged into one batch first.
    refs = p.get("ref_images")
    if not refs:
        refs = [p["ref_image"]] if p.get("ref_image") else []
    if refs and p.get("use_reference"):
        batch_src = None
        for r in refs:
            rn = nid_next()
            g[rn] = {"class_type": "LoadImage", "inputs": {"image": r}}
            sc = nid_next()
            # 尺寸必须统一，ImageBatch 才能叠成一批 —— 但**不能用居中裁切**。
            # 原来这里写的是 crop="center"，把竖版参考图裁成正方形，
            # 705×907 的图会丢掉约 22% 的高度（上下各 11%），构图信息直接砍掉两成。
            # 代码注释当时说"CLIP vision 反正缩到 224，所以没损失" —— 这句在
            # 构图上不成立：缩放到 224 是等比压缩，裁切是**真的丢内容**。
            # 改成 crop="disabled"（等比拉伸到 512×512）：不丢任何画面。
            g[sc] = {"class_type": "ImageScale",
                     "inputs": {"image": [rn, 0], "upscale_method": "lanczos",
                                "width": 512, "height": 512, "crop": "disabled"}}
            if batch_src is None:
                batch_src = [sc, 0]
            else:
                bt = nid_next()
                g[bt] = {"class_type": "ImageBatch",
                         "inputs": {"image1": batch_src, "image2": [sc, 0]}}
                batch_src = [bt, 0]

        uni = nid_next()
        g[uni] = {"class_type": "IPAdapterUnifiedLoader",
                  "inputs": {"model": cond_model,
                             "preset": p.get("ref_preset", "PLUS (high strength)")}}
        ipa = nid_next()
        g[ipa] = {"class_type": "IPAdapterAdvanced",
                  "inputs": {"model": [uni, 0], "ipadapter": [uni, 1],
                             "image": batch_src,
                             "weight": float(p.get("ref_strength", DEFAULTS["ref_strength"])),
                             "weight_type": p.get("ref_weight_type", "linear"),
                             "combine_embeds": p.get("ref_combine", "concat"),
                             "start_at": float(p.get("ref_start", 0.0)),
                             "end_at": float(p.get("ref_end", 1.0)),
                             "embeds_scaling": p.get("ref_embeds_scaling", "V only")}}
        cond_model = [ipa, 0]

    # 7. sampler + decode + save
    ks = nid_next()
    g[ks] = {"class_type": "KSampler",
             "inputs": {"model": cond_model, "positive": pos_src, "negative": neg_src,
                        "latent_image": latent_src, "seed": seed, "steps": steps,
                        "cfg": cfg, "sampler_name": sampler, "scheduler": scheduler,
                        "denoise": denoise}}
    dec = nid_next()
    g[dec] = {"class_type": "VAEDecode", "inputs": {"samples": [ks, 0], "vae": vae_src}}
    sv = nid_next()
    g[sv] = {"class_type": "SaveImage",
             # Unique per run. A fixed prefix lets ComfyUI's file counter
             # collide with the previous run's number; combined with the
             # de-duplication below (which removes ComfyUI's copy) that made
             # every second generation resolve to an already-deleted file.
             "inputs": {"images": [dec, 0], "filename_prefix": p.get("prefix") or "temp/uigen"}}
    return g


# ---------------------------------------------------------------- upload/run
def _safe_image_ext(filename: str) -> str:
    """从上传文件名里取一个**安全的**扩展名；取不到就退回 `.png`。

    ★ 为什么不能直接用 `os.path.splitext(filename)[1]`：这个值会进 multipart 的
      `Content-Disposition` 头。实测 filename = `'a.png\\r\\nContent-Type: x'`
      得到 safe = `'ui_XXXXXXXXXX.png\\r\\ncontent-type: x'` —— 客户端给的换行
      直接进了请求头，可以在 multipart 里多插一个头/段。
      只放行 `.` + 1~5 位小写字母数字，其余一律 `.png`。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".png"


def upload_image(raw: bytes, filename: str) -> str:
    """Push an image into ComfyUI's input dir; return the name it stored under."""
    safe = "ui_" + uuid.uuid4().hex[:10] + _safe_image_ext(filename)
    boundary = "----dsh" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image"; filename="' + safe.encode() + b'"\r\n',
        b"Content-Type: application/octet-stream\r\n\r\n",
        raw,
        f"\r\n--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{COMFY}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with comfy_open(req, 180) as r:
        r.read()
    return safe


def build_upscale_graph(image_name: str, model: str, width: int, height: int,
                        prefix: str) -> dict:
    """Load an existing image and upscale it.

    Generating at 2K directly takes ~100s here; generating at 1024 and
    upscaling takes ~30s, so this is the faster path for a 2K result.
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScale",
              "inputs": {"image": ["3", 0], "upscale_method": "lanczos",
                         "width": int(width), "height": int(height),
                         "crop": "disabled"}},
        "5": {"class_type": "SaveImage",
              "inputs": {"images": ["4", 0], "filename_prefix": prefix}},
    }


def build_inpaint_graph(image_name: str, mask_name: str, p: dict,
                        prefix: str) -> dict:
    """Redraw only the masked area of an existing image.

    Why this shape:
      * LoadImage returns both IMAGE and MASK. The MASK output is the image's
        ALPHA channel, so the frontend uploads a PNG carrying the original RGB
        plus a painted alpha channel - one file for both roles.
      * GrowMask expands the painted area a little. A stroke that stops exactly
        at the bad region leaves the seam inside the result, so a small grow is
        what makes the repair blend.
      * VAEEncodeForInpaint expects WHITE = area to regenerate, which is the
        convention the frontend paints with.
      * ImageCompositeMasked pastes the redrawn version back over the original
        using the mask, so pixels outside the mask keep their exact original
        values instead of being re-decoded.
    """
    check_param_ranges(p)          # 和 build_graph 同一道闸：两个入口不许分叉
    steps = int(p.get("steps", DEFAULTS["steps"]))
    cfg = float(p.get("cfg", DEFAULTS["cfg"]))
    denoise = float(p.get("denoise", DEFAULTS["inpaint_denoise"]))
    grow = int(p.get("mask_grow", DEFAULTS["mask_grow"]))
    feather = int(p.get("mask_feather", DEFAULTS["mask_feather"]))

    g = {
        # the image supplies both the pixels and (via alpha) the mask
        "1": {"class_type": "LoadImage",
              "inputs": {"image": image_name, "upload": "image"}},
        "2": {"class_type": "LoadImageMask",
              "inputs": {"image": mask_name, "channel": "alpha"}},
        "3": {"class_type": "CheckpointLoaderSimple",
              # ★ 必须用 `or` 而不是 p.get("checkpoint", CHECKPOINT)：
              #   前端没选底模时送的是 null，键**存在**、值是 None，
              #   带默认值的 .get() 不会生效，None 会一路塞进图里让 ComfyUI 报错。
              #   这个坑项目里踩过（见 resolve_seed 的 docstring）。
              "inputs": {"ckpt_name": p.get("checkpoint") or CHECKPOINT}},
    }
    mask_src = ["2", 0]
    nid = 10

    if grow > 0:
        g[str(nid)] = {"class_type": "GrowMask",
                       "inputs": {"mask": mask_src, "expand": grow,
                                  "tapered_corners": True}}
        mask_src = [str(nid), 0]
        nid += 1
    if feather > 0:
        g[str(nid)] = {"class_type": "FeatherMask",
                       "inputs": {"mask": mask_src, "left": feather,
                                  "top": feather, "right": feather,
                                  "bottom": feather}}
        mask_src = [str(nid), 0]
        nid += 1

    # Prompt for the redraw.
    #   * the caller's mask-specific text wins; if absent, fall back to the main
    #     prompt (the UI promises "leave empty to reuse the main prompt" and
    #     nothing implemented that until now)
    #   * LoRA trigger words are appended - they are part of what makes the LoRA
    #     work, and the main path already adds them
    #   * quality tags are prepended once, never duplicated
    raw_prompt = (p.get("prompt") or "").strip()
    fallback = (p.get("main_prompt") or "").strip()
    pos = raw_prompt or fallback
    neg = p.get("negative") or ""

    # The mask box accepts Chinese, same as the main prompt - so translate it
    # here. Passing CJK straight to CLIPTextEncode produces garbage tokens
    # rather than an error, which is how a Chinese repair prompt silently
    # wrecked the redraw.
    unknown_words = []
    if _HAS_CJK.search(pos):
        try:
            res = translator().translate(pos)
            # translate() returns (english, resolved_pairs, unknown_terms)
            en = res[0] if isinstance(res, (tuple, list)) and res else ""
            if len(res) > 2 and isinstance(res[2], list):
                unknown_words = [str(x) for x in res[2]]
            if en:
                pos = en
        except Exception as e:
            warn("局部重绘提示词翻译失败", e)

    triggers = []
    for item in (p.get("loras") or []):
        for t in (item.get("triggers") or []):
            t = str(t).strip()
            if t and t.lower() not in pos.lower() and t not in triggers:
                triggers.append(t)

    q = p.get("quality")
    parts = []
    if isinstance(q, list):
        qt = ", ".join(str(x).strip() for x in q if str(x).strip())
        if qt:
            parts.append(qt)
    if triggers:
        parts.append(", ".join(triggers))
    if pos:
        # drop a duplicated quality prefix so the tags are not paid for twice
        low = pos.lower()
        for tag in (str(x).strip().lower() for x in (q or []) if str(x).strip()):
            if low.startswith(tag):
                pos = pos[len(tag):].lstrip(", ").strip()
                low = pos.lower()
        if pos:
            parts.append(pos)
    pos = ", ".join(parts).strip(", ")
    p["_inpaint_used_fallback"] = bool(not raw_prompt and fallback and pos)
    p["_inpaint_unknown"] = unknown_words

    model_src = ["3", 0]
    clip_src = ["3", 1]
    # 与主生成用同一套解析规则，避免两条路径对「loras 为空」的理解不一致
    for item in resolve_lora_list(p):
        if not item.get("name") or float(item.get("strength", 0)) <= 0:
            continue
        if lora_key_layout(_lora_path(item["name"]) or "").get("compatible") is False:
            warn("局部重绘跳过不兼容的 LoRA", RuntimeError(item["name"]))
            continue
        g[str(nid)] = {"class_type": "LoraLoader",
                       "inputs": {"model": model_src, "clip": clip_src,
                                  "lora_name": item["name"],
                                  "strength_model": float(item["strength"]),
                                  "strength_clip": float(item["strength"])}}
        model_src, clip_src = [str(nid), 0], [str(nid), 1]
        nid += 1

    # Clip skip 也要作用于重绘（与主生成保持一致）
    clip_skip = int(p.get("clip_skip", DEFAULTS["clip_skip"]) or 0)
    if clip_skip > 0:
        g[str(nid)] = {"class_type": "CLIPSetLastLayer",
                       "inputs": {"clip": clip_src,
                                  "stop_at_clip_layer": -clip_skip}}
        clip_src = [str(nid), 0]
        nid += 1

    # encoded once, through the LoRA-patched CLIP so trigger words apply
    g[str(nid)] = {"class_type": "CLIPTextEncode",
                   "inputs": {"clip": clip_src, "text": pos}}
    pos_id = str(nid); nid += 1
    g[str(nid)] = {"class_type": "CLIPTextEncode",
                   "inputs": {"clip": clip_src, "text": neg}}
    neg_id = str(nid); nid += 1

    # Optional dedicated inpainting ControlNet (NoobAI Inpainting).
    # ControlNetInpaintingAliMamaApply takes the ORIGINAL image plus the mask and
    # builds the masked conditioning internally, which is exactly what an
    # inpaint ControlNet expects. The plain base checkpoint has no idea a mask
    # exists, so it re-imagines the whole area; the ControlNet supplies the
    # surrounding structure, which is what stops the hair texture from shifting
    # and the eye colour from drifting.
    cn_name = str(p.get("inpaint_controlnet") or "").strip()
    cn_strength = float(p.get("controlnet_strength", DEFAULTS["controlnet_strength"]))
    if cn_name and cn_strength > 0:
        g[str(nid)] = {"class_type": "ControlNetLoader",
                       "inputs": {"control_net_name": cn_name}}
        cn_id = str(nid); nid += 1
        g[str(nid)] = {"class_type": "ControlNetInpaintingAliMamaApply",
                       "inputs": {"positive": [pos_id, 0], "negative": [neg_id, 0],
                                  "control_net": [cn_id, 0], "vae": ["3", 2],
                                  "image": ["1", 0], "mask": mask_src,
                                  "strength": cn_strength,
                                  "start_percent": 0.0, "end_percent": 1.0}}
        pos_id = str(nid); neg_id = str(nid)
        nid += 1

    # Standard inpainting: encode the ORIGINAL image at full strength and then
    # restrict sampling to the mask in latent space. The previous version fed
    # VAEEncodeForInpaint, which blends the mask into the latent during encode;
    # combined with pasting the result back over the original, the two treatments
    # disagreed at the border and the model - seeing a torn latent - filled the
    # masked area with a flat colour block instead of redrawing it.
    g[str(nid)] = {"class_type": "VAEEncode",
                   "inputs": {"pixels": ["1", 0], "vae": ["3", 2]}}
    enc = str(nid); nid += 1

    g[str(nid)] = {"class_type": "SetLatentNoiseMask",
                   "inputs": {"samples": [enc, 0], "mask": mask_src}}
    latent = [str(nid), 0]; nid += 1

    g[str(nid)] = {"class_type": "KSampler",
                   "inputs": {"model": model_src, "positive": [pos_id, 0],
                              "negative": [neg_id, 0], "latent_image": latent,
                              # ★ 原来是 int(p.get("seed", 0))：前端修图请求压根不送
                              #   seed，于是每次局部重绘都用 seed=0 —— 画出来的东西
                              #   每次都一样。改成走 resolve_seed（不送就随机）。
                              "seed": resolve_seed(p.get("seed")), "steps": steps,
                              "cfg": cfg,
                              "sampler_name": p.get("sampler", DEFAULTS["sampler"]),
                              "scheduler": p.get("scheduler", DEFAULTS["scheduler"]),
                              "denoise": denoise}}
    samp = str(nid); nid += 1

    g[str(nid)] = {"class_type": "VAEDecode",
                   "inputs": {"samples": [samp, 0], "vae": ["3", 2]}}
    dec = str(nid); nid += 1

    # paste the redrawn region back over the untouched original
    g[str(nid)] = {"class_type": "ImageCompositeMasked",
                   "inputs": {"destination": ["1", 0], "source": [dec, 0],
                              "x": 0, "y": 0, "resize_source": False,
                              "mask": mask_src}}
    comp = str(nid); nid += 1

    g[str(nid)] = {"class_type": "SaveImage",
                   "inputs": {"images": [comp, 0], "filename_prefix": prefix}}
    return g


def day_dir(out_root: str, when: float | None = None) -> str:
    """按日期分子目录：<out_root>/YYYY-MM-DD/。

    文件名里已经有时间戳，但一个目录堆几千张图不好找；按天分目录后
    每天的量级可控。用本地时间，与文件名里的时间戳口径一致。
    """
    d = time.strftime("%Y-%m-%d", time.localtime(when if when else time.time()))
    path = os.path.join(out_root, d)
    os.makedirs(path, exist_ok=True)
    return path


def iter_output_images(out_root: str):
    """递归列出**给用户看的**产出图片，返回 (完整路径, 文件名)。

    ★ 下划线开头的子目录整体跳过：那是应用自己的中间产物和回收站，不是作品。
      `_depth/`（深度图，也是 png）以前会混进「最近产出」那一栏，`_recycle/`
      是删除键的回收站 —— 不排掉的话，删掉的图会立刻从回收站里重新冒出来。
    """
    if not os.path.isdir(out_root):
        return
    for cur, dirs, fnames in os.walk(out_root):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for f in fnames:
            if f.lower().endswith(".png"):
                yield os.path.join(cur, f), f


def build_depth_graph(image_name: str, model: str = None, resolution: int = 504) -> dict:
    """参考图 -> 深度图（Depth Anything 3，ComfyUI 0.35+ 内置，不用装插件）。

    为什么单独跑一条图、而不是塞进出图流程里：
      1) 用户要先**看见**深度图长什么样。DA3 对平面感强的插画经常提取不准，
         不看就出图等于盲赌。
      2) 省时间。同一条参考图只解析一次，之后每次出图直接用那张深度图。

    ★ COMFY_DYNAMICCOMBO_V3 的传参格式是**点号路径**，不是嵌套字典。
      这是实测出来的：先按嵌套传，ComfyUI 报
      `required_input_missing: output.apply_sky_clip / output.normalization`，
      去 comfy_api/latest/_io.py 的 _expand_schema_for_dynamic 看才确认
      子输入名是 "output.xxx"。
    """
    g: dict = {}
    nid = [0]

    def nid_next() -> str:
        nid[0] += 1
        return str(nid[0])

    li = nid_next()
    g[li] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    lm = nid_next()
    g[lm] = {"class_type": "LoadDA3Model",
             "inputs": {"model_name": model or DEPTH_MODEL,
                        "weight_dtype": "default"}}
    inf = nid_next()
    g[inf] = {"class_type": "DA3Inference",
              "inputs": {"da3_model": [lm, 0], "image": [li, 0],
                         "resolution": int(resolution),
                         "resize_method": "upper_bound_resize",
                         "mode": "mono"}}
    ren = nid_next()
    g[ren] = {"class_type": "DA3Render",
              "inputs": {"da3_geometry": [inf, 0], "output": "depth",
                         "output.normalization": "v2_style",
                         "output.apply_sky_clip": False}}
    sv = nid_next()
    g[sv] = {"class_type": "SaveImage",
             # ★ 前缀必须带 `temp/`，和出图 / 重绘 / 放大三条图一样。
             #
             # 写成裸 "depth" 时，ComfyUI 会把深度图存进**产出目录的根**
             # （ComfyUI 报 subfolder=''，实测落在用户的产出目录正下方），
             # 而 run_graph 的清理只删 `COMFY_OUTPUT/temp/` 底下的副本 ——
             # 于是够不着，每解析一张深度图就在产出目录根上留一个文件，
             # 本机累积到 8 个（实测）。清理那段见 run_graph 里
             # "Remove ComfyUI's own copy"。
             #
             # 程序自己的那份在 OUT_DEPTH (= anime/_depth)，由 run_graph 下载。
             # 这里只是 ComfyUI 的中转副本，落进 temp/ 才会被清掉。
             "inputs": {"images": [ren, 0], "filename_prefix": "temp/depth"}}
    return g


def run_graph(graph: dict, out_dir: str, stem: str, timeout_s: int = 900) -> list[str]:
    res = comfy_post("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})
    pid = res.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝了这个任务: {json.dumps(res, ensure_ascii=False)[:500]}")

    deadline = time.time() + timeout_s
    poll_failures = 0
    while time.time() < deadline:
        try:
            hist = comfy_get(f"/history/{pid}")
        except Exception as poll_err:
            poll_failures += 1
            # Log the FIRST failure only: if ComfyUI dies mid-run this is the
            # only clue the user gets, but repeating it every second would bury
            # the console.
            if poll_failures == 1:
                warn("轮询 ComfyUI 失败（将重试直到超时）", poll_err)
            time.sleep(1.0)
            continue
        e = hist.get(pid)
        if e:
            st = e.get("status", {})
            if st.get("status_str") == "error":
                msgs = st.get("messages", [])
                detail = ""
                for m in msgs:
                    if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict):
                        detail = m[1].get("exception_message") or detail
                raise RuntimeError(f"执行失败: {detail or json.dumps(msgs, ensure_ascii=False)[:400]}")
            if st.get("completed"):
                saved = []
                for node_out in (e.get("outputs") or {}).values():
                    for k, img in enumerate(node_out.get("images", [])):
                        fname = img.get("filename") or ""
                        sub = img.get("subfolder", "") or ""
                        ftype = img.get("type", "output") or "output"
                        url = (f"{COMFY}/view?filename={urllib.parse.quote(fname)}"
                               f"&subfolder={urllib.parse.quote(sub)}"
                               f"&type={urllib.parse.quote(ftype)}")
                        dst = os.path.join(out_dir, f"{stem}_{k}.png")
                        try:
                            with comfy_open(url, 300) as s, \
                                    open(dst, "wb") as fh:
                                shutil.copyfileobj(s, fh)
                        except Exception as dl_err:
                            # Surface which exact file failed; a bare 404 here is
                            # useless without the filename and subfolder.
                            raise RuntimeError(
                                f"下载产出失败: {dl_err} | type={ftype} "
                                f"subfolder={sub!r} filename={fname!r}") from None
                        saved.append(dst)
                        # Remove ComfyUI's own copy, but ONLY when it sits under
                        # the app's temp/ staging folder - never touch anything
                        # the user saved from the ComfyUI GUI itself.
                        try:
                            stage_root = os.path.realpath(os.path.join(COMFY_OUTPUT, "temp"))
                            fp = os.path.realpath(
                                os.path.join(COMFY_OUTPUT, sub, fname) if sub
                                else os.path.join(COMFY_OUTPUT, fname))
                            if os.path.isfile(fp) and fp.startswith(stage_root + os.sep):
                                os.remove(fp)
                        except Exception as e:
                            warn("清理中转文件失败", e)
                if not saved:
                    raise RuntimeError("任务完成但没有产出图片")
                # 出完图再释放显存：每 N 张一次，队列空着才动手。
                # 放在 return 之前、**不放进 finally** —— 失败的任务留给用户
                # 重试时模型还在显存里，重试更快。
                maybe_auto_free()
                return saved
        time.sleep(0.6)
    raise TimeoutError(f"超时 {timeout_s}s")


# ------------------------------------------------------ 从成品图读回参数
# 为什么这件事不需要任何模型：ComfyUI 的 SaveImage 会把**整份 API 工作流 JSON**
# 原样写进 PNG 的 tEXt 块 "prompt"。所以「拿一张成品图反推提示词」在出图工具
# 自己的产物上是**原样读回**，不是猜。WD14 那类反推器只在拿到一张没有元数据的
# 外站图时才需要。
#
# 也认 A1111 / Forge / SwarmUI 的 "parameters" 文本块，方便读别人给的图。
def _meta_texts(raw: bytes) -> dict:
    """把图片里的文本元数据挖出来 -> {关键字: 文本}。

    PNG 走 Pillow 的 info（tEXt/iTXt 都在里面）；JPEG/WebP 的 A1111 参数存在
    EXIF UserComment（tag 0x9286）。两条路都试，拿不到就返回空。
    """
    import io as _io
    from PIL import Image as _Image
    out = {}
    try:
        with _Image.open(_io.BytesIO(raw)) as im:
            for k, v in (im.info or {}).items():
                if isinstance(v, (str, bytes)):
                    out[str(k)] = v.decode("utf-8", "replace") if isinstance(v, bytes) else v
            try:
                ex = im.getexif()
                uc = ex.get(0x9286) if ex else None
                if uc:
                    if isinstance(uc, bytes):
                        uc = uc.decode("utf-8", "replace")
                    # EXIF UserComment 前 8 字节是字符集标识
                    out.setdefault("parameters", uc[8:] if uc[:4] in
                                   ("ASCII", "UNICO", "JIS  ") else uc)
            except Exception as e:
                warn("读 EXIF 失败", e)
    except Exception as e:
        warn("读图片元数据失败", e)
    return out


def _strip_meta_prefix(s: str) -> str:
    return s.strip().lstrip("\x00").strip()


# A1111 / Forge / NovelAI 的采样器名和 ComfyUI 的**不是同一套**：
#   "DPM++ 2M Karras" 在 ComfyUI 里要写成 dpmpp_2m + 调度器 karras。
# 不映射的话，读回来的名字写进工作流会被 ComfyUI 直接拒掉
# （实测报错就是 sampler_name: Value not in list）。
# 表里没有的一律留空，由前端提示"这台 ComfyUI 没有"，绝不猜一个相近的顶上 ——
# 采样器不同就是不同的图。
A1111_SAMPLERS = {
    "euler": "euler",
    "euler a": "euler_ancestral",
    "lms": "lms",
    "heun": "heun",
    "dpm2": "dpm_2",
    "dpm2 a": "dpm_2_ancestral",
    "dpm++ 2s a": "dpmpp_2s_ancestral",
    "dpm++ 2m": "dpmpp_2m",
    "dpm++ sde": "dpmpp_sde",
    "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ 2m sde heun": "dpmpp_2m_sde_heun",
    "dpm++ 3m sde": "dpmpp_3m_sde",
    "dpm fast": "dpm_fast",
    "dpm adaptive": "dpm_adaptive",
    "ddim": "ddim",
    "plms": "plms",
    "unipc": "uni_pc",
    "lcm": "lcm",
    "restart": "restart",
}
# A1111 的调度器叫法和 ComfyUI 一样，只有 "automatic"/"uniform" 这种要翻译。
A1111_SCHEDULERS = {
    "automatic": "",
    "uniform": "normal",
    "karras": "karras",
    "exponential": "exponential",
    "polyexponential": "exponential",
    "sgm uniform": "sgm_uniform",
    "simple": "simple",
    "ddim uniform": "ddim_uniform",
    "beta": "beta",
}


def parse_a1111_parameters(text: str) -> dict:
    """A1111 / Forge / SwarmUI 的 parameters 文本 -> 结构化字段。

    格式固定：第一段是正向词，可选一行 "Negative prompt: ..."，
    末行是 "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1, Size: 512x768"。
    """
    text = text.replace("\r\n", "\n")
    m = re.search(r"^Negative prompt:", text, re.M)
    tail = re.search(r"^Steps:\s*\d+", text, re.M)
    if tail:
        head, meta_line = text[:tail.start()], text[tail.start():]
    else:
        head, meta_line = text, ""
    if m and m.start() < len(head):
        positive = head[:m.start()]
        negative = head[m.end():]
    else:
        positive, negative = head, ""
    d = {}
    for k, v in re.findall(r"([A-Za-z][A-Za-z0-9 _]*?):\s*(\"[^\"]*\"|[^,]*)(?:,\s*|$)",
                           meta_line):
        d[k.strip().lower()] = v.strip().strip('"')
    sampler = d.get("sampler", "")
    sched = ""
    for name in ("karras", "normal", "exponential", "sgm_uniform", "simple",
                 "ddim_uniform", "beta"):
        if sampler.lower().endswith(" " + name):
            sched, sampler = name, sampler[: -len(name) - 1]
            break
    # A1111 v1.7+ 会把调度器单独写成 "Schedule type: ..."，有就以它为准
    if d.get("schedule type"):
        sched = d["schedule type"].strip().lower()
    comfy_sampler = A1111_SAMPLERS.get(sampler.strip().lower(), "")
    comfy_sched = A1111_SCHEDULERS.get(sched.strip().lower(), sched.strip().lower())
    size = d.get("size", "")
    w = h = 0
    if "x" in size:
        try:
            w, h = (int(x) for x in size.lower().split("x", 1))
        except ValueError:
            w = h = 0
    return {
        "positive": _strip_meta_prefix(positive),
        "negative": _strip_meta_prefix(negative),
        "checkpoint": d.get("model", ""),
        "loras": [],
        "sampler": {
            "seed": int(d["seed"]) if d.get("seed", "").isdigit() else None,
            "steps": int(d["steps"]) if d.get("steps", "").isdigit() else None,
            "cfg": float(d["cfg scale"]) if d.get("cfg scale", "").replace(".", "", 1).isdigit() else None,
            "sampler_name": comfy_sampler,
            # 原样读到的名字，可能是 ComfyUI 认不出的（就是它被拒掉的原因）。
            # 界面上「没套用」的提示要指名道姓，就靠这个字段。
            "sampler_name_raw": sampler.strip(),
            "scheduler": comfy_sched,
            "scheduler_raw": sched.strip(),
            "denoise": float(d["denoising strength"]) if d.get("denoising strength", "").replace(".", "", 1).isdigit() else None,
        },
        "size": [w, h],
        "batch": 1,
        "controlnet": [],
        "meta_extra": d,
    }


def parse_comfy_meta(text: str) -> dict:
    """ComfyUI API 工作流 JSON -> 结构化参数（按图里的实际连线取，不靠猜顺序）。"""
    g = json.loads(text)
    if not isinstance(g, dict):
        raise ValueError("工作流不是节点字典")
    nodes = {str(k): v for k, v in g.items() if isinstance(v, dict)}

    def cls(nid):
        return nodes.get(str(nid), {}).get("class_type", "")

    def ins(nid):
        return nodes.get(str(nid), {}).get("inputs", {}) or {}

    def ref(v):
        return str(v[0]) if isinstance(v, list) and v and not isinstance(v[0], dict) else None

    sampler_id = next((n for n in nodes if cls(n).startswith("KSampler")), None)
    si = ins(sampler_id) if sampler_id else {}

    def text_of(slot):
        """顺着连线找正向/负向文本。

        ★ 不能假设采样器的 positive 直接连着 CLIPTextEncode：接了 ControlNet 的
        工作流里，采样器两头连的都是 ControlNetApplyAdvanced，文本在它再上一层。
        第一版就是直接取一层，结果正负向全是空的（真图实测发现）。所以这里
        逐层往回走，同名插槽优先。
        """
        seen, queue = set(), [ref(si.get(slot))]
        while queue:
            nid = queue.pop(0)
            if not nid or nid in seen:
                continue
            seen.add(nid)
            if cls(nid) == "CLIPTextEncode":
                return str(ins(nid).get("text", "")).strip()
            i = ins(nid)
            nxt = [ref(i.get(slot))] + [ref(v) for v in i.values()
                                        if isinstance(v, list)]
            queue.extend(x for x in nxt if x)
        return ""

    positive, negative = text_of("positive"), text_of("negative")
    if not positive and not negative:
        # 没有采样器节点（放大/预处理这类图）就把文本节点都收起来
        texts = [str(ins(n).get("text", "")).strip()
                 for n in nodes if cls(n) == "CLIPTextEncode"]
        positive = " / ".join(t for t in texts if t)

    # 沿 model 连线往回走：LoRA 链 + 底模。比"把图里所有 LoRA 都列出来"准 ——
    # 工作流里常留着没接上的节点。
    loras, ckpt, seen = [], "", set()
    cur = ref(si.get("model"))
    while cur and cur not in seen:
        seen.add(cur)
        c = cls(cur)
        if c == "LoraLoader":
            loras.append({"name": ins(cur).get("lora_name", ""),
                          "model": ins(cur).get("strength_model"),
                          "clip": ins(cur).get("strength_clip")})
        elif c in ("CheckpointLoaderSimple", "UNETLoader", "CheckpointLoader"):
            ckpt = ins(cur).get("ckpt_name") or ins(cur).get("unet_name") or ""
            break
        cur = ref(ins(cur).get("model"))

    size, batch = [0, 0], 1
    for n in nodes:
        if cls(n) == "EmptyLatentImage":
            size = [int(ins(n).get("width") or 0), int(ins(n).get("height") or 0)]
            batch = int(ins(n).get("batch_size") or 1)
            break

    cns = []
    for n in nodes:
        if cls(n) == "ControlNetApplyAdvanced":
            i = ins(n)
            cns.append({"type": "", "strength": i.get("strength"),
                        "start": i.get("start_percent"), "end": i.get("end_percent")})
    union_types = [ins(n).get("type") for n in nodes
                   if cls(n) == "SetUnionControlNetType"]
    for i, t in enumerate(union_types):
        if i < len(cns):
            cns[i]["type"] = t

    # 回退着走出来的顺序是"最后应用的在前"，反过来才是图里的应用顺序。
    # 照应用顺序还原，套用回界面时叠加次序才和原图一致。
    loras.reverse()

    return {
        "positive": positive,
        "negative": negative,
        "checkpoint": ckpt,
        "loras": loras,
        "sampler": {
            "seed": si.get("seed") if si.get("seed") is not None else si.get("noise_seed"),
            "steps": si.get("steps"),
            "cfg": si.get("cfg"),
            "sampler_name": si.get("sampler_name", ""),
            "sampler_name_raw": si.get("sampler_name", ""),
            "scheduler": si.get("scheduler", ""),
            "scheduler_raw": si.get("scheduler", ""),
            "denoise": si.get("denoise"),
        },
        "size": size,
        "batch": batch,
        "controlnet": [c for c in cns if c["strength"] is not None],
        "source_image": next((str(ins(n).get("image", "")) for n in nodes
                              if cls(n) == "LoadImage"), ""),
        "node_count": len(nodes),
    }


def upscale_target(w0: int, h0: int, factor: float) -> tuple:
    """放大到多大：等比缩放 + 整体限幅。返回 (宽, 高)。

    ★ 为什么必须等比、必须整体限幅：下游接的是 ImageScale + crop="disabled"，
      那是**拉伸**语义（把图拉满到给定宽高，不是按比例裁）。所以只要传进去的
      宽高比例和原图不一致，画面就会被压扁或拉长，而且不会报任何错。
      踩过的坑：原来是 tw/th 各自 min(..., 4096)，1344×768（16:9）选 4× 时
      宽度 5376 被砍到 4096、高度 3072 照给 —— 比例 1.75 变成 1.33，
      整张图横向压扁 24%（用户实测报过"放大把尺寸拉伸了"）。

    抽成独立函数是为了能被测试直接断言 —— 内联在请求处理里没法单测。
    """
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        # 前端下拉框理论上只送数字，但 JSON 里 null 是能进来的
        # （resolve_seed 那边踩过同一个坑），所以不信任入参。
        factor = 2.0
    if not (factor > 0):
        factor = 2.0
    scale = min(factor,
                MAX_UPSCALE_SIDE / max(1, w0),
                MAX_UPSCALE_SIDE / max(1, h0))
    return max(8, round(w0 * scale)), max(8, round(h0 * scale))


def read_image_meta(raw: bytes) -> dict:
    """图片字节 -> 生成参数。认 ComfyUI，也认 A1111/Forge/SwarmUI。"""
    texts = _meta_texts(raw)
    notes = []
    meta = None
    if texts.get("prompt"):
        try:
            meta = parse_comfy_meta(texts["prompt"])
            meta["kind"] = "comfy"
        except Exception as e:
            warn("解析图里的工作流失败", e)
            notes.append("工作流 JSON 解析失败：%s" % e)
    if meta is None and texts.get("parameters"):
        meta = parse_a1111_parameters(texts["parameters"])
        meta["kind"] = "a1111"
        notes.append("这是 A1111 / Forge / SwarmUI 格式的参数块，LoRA 与"
                     " ControlNet 无法从文本里还原")
    if meta is not None and meta.get("kind") == "a1111":
        raw_sampler = (meta.get("meta_extra") or {}).get("sampler", "")
        if raw_sampler and not meta["sampler"].get("sampler_name"):
            notes.append("采样器 %r 在 ComfyUI 里没有对应项，不会套用"
                         "（采样器换一个就是另一张图，不猜相近的顶上）" % raw_sampler)
    if meta is None:
        raise ValueError(
            "这张图里没有生成参数。本机出的 PNG 一定有；被聊天软件/图床转过存、"
            "或者别的工具出的 JPG 通常会被洗掉")
    # 放大/局部重绘这类图只带改造那一步的工作流，提示词在它引用的原图上。
    # 直接给个空结果会让人以为"读不出来"，所以这里说清楚该拿哪张图。
    if meta["kind"] == "comfy" and not meta["positive"] \
            and not meta["sampler"].get("steps"):
        src = meta.get("source_image") or ""
        raise ValueError(
            "这张图带的是「放大 / 重绘」那一步的工作流，里面没有提示词和采样参数。"
            + ("提示词在它引用的原图 %s 上 —— 拿那张图来读。" % src if src
               else "请改用原始那张图。"))
    # ★ 尺寸兜底：从图片自己的像素读。
    #
    #   为什么需要：尺寸原来只从工作流里的 EmptyLatentImage 取。而**局部重绘
    #   和放大的图里根本没有那个节点** —— 重绘的尺寸来自被修的那张原图，
    #   放大的尺寸是算出来的。实测：一张 640×640 的重绘结果读出来是 [0, 0]，
    #   用户把它拖回界面就看到"尺寸 0×0"。
    #
    #   PNG 的 IHDR 里本来就写着真实宽高，直接用它。这比在工作流里找更可靠：
    #   能读到这里，说明图片本身就是这张。
    if not meta["size"][0] or not meta["size"][1]:
        try:
            import io as _io
            from PIL import Image as _Image
            with _Image.open(_io.BytesIO(raw)) as im:
                meta["size"] = [im.width, im.height]
            notes.append("尺寸是从图片本身读的（这张图的工作流里没有尺寸节点 —— "
                         "重绘/放大就是这样）")
        except Exception as e:
            warn("读图片像素尺寸失败", e)

    meta["ok"] = True
    meta["meta_keys"] = sorted(k for k in texts if k in
                               ("prompt", "workflow", "parameters", "Comment"))
    if "workflow" in texts:
        notes.append("图里同时带着 ComfyUI 界面版工作流（workflow），"
                     "可以拖进 ComfyUI 直接打开")
    meta["notes"] = notes
    return meta


# ---------------------------------------------------------------- http server
def drop_null_params(body) -> dict:
    """把请求体里"等于没传"的值**删掉**（null，以及数值参数的空字符串）。

    ★ 为什么必须在入口做这一件事：

      `dict.get(k, 默认值)` **挡不住 null** —— 键存在、值是 None 时它直接返回
      None，默认值根本不生效。于是

          steps = int(p.get("steps", DEFAULTS["steps"]))   # int(None) -> TypeError

      一个 null 就让用户看到 Python traceback，而不是"steps 收到了 null、
      应该是数字"。这和项目的硬规矩（报错要能照着改）直接冲突。

      这个项目被同一类咬过两次，都是事后单点修的：
        · `resolve_seed`：前端送 null，`int(None)` 崩
        · `build_inpaint_graph` 的 checkpoint：返回 None，变成"没选底模"

      **真实触发路径不是"有人手搓 API"**：前端数字框被清空时 `parseFloat("")`
      是 NaN，而 `JSON.stringify({steps: NaN})` 会变成 `null` —— 也就是
      "把步数那一格删干净再点生成"就能踩到。

      实测（dev/probe_null_params.py，直接调建图函数，不走 HTTP）：
      16 处中招 —— 12 处抛 TypeError，4 处把 None 悄悄写进图里，
      到 ComfyUI 那边才报一句看不懂的错（sampler / scheduler）。

    做法是"删掉"而不是"逐个补默认值"：删掉之后，下游所有
    `.get(k, 默认值)` 都按"没传"处理，默认值自然生效 —— **不用改那 16 个
    调用点**（那动的是最核心的建图函数，风险大得多）。

    空字符串只对**数值参数**删（它们都在 DEFAULTS 里）。文本字段的 "" 有语义
    （比如"负向词留空"），不能碰。

    只处理顶层。嵌套结构（如 loras 列表里的元素）不碰 —— 那里的 None 各有
    语义，而且已经各自处理过了。
    """
    if not isinstance(body, dict):
        return {}
    NUMERIC_EXTRA = {"denoise", "sketch_start", "sketch_end",
                     "depth_start", "depth_end", "ref_start", "ref_end",
                     "factor"}
    out = {}
    for k, v in body.items():
        if v is None:
            continue                      # null = 没传
        if v == "" and (k in DEFAULTS or k in NUMERIC_EXTRA):
            continue                      # 数值参数的空串 = 没填
        out[k] = v
    return out


class BadParam(ValueError):
    """请求里的参数不合法。handler 捕获后回 HTTP 400 + 一句能照着改的话。

    存在的意义：直接 `int("abc")` 抛的是
        invalid literal for int() with base 10: 'abc'
    —— Python 味，用户照不了改；而且会被通用的 `except Exception` 兜成
    HTTP 500，把"你参数写错了"报成"服务器挂了"。
    """


# ★ 我们自己**故意抛**的异常：消息已经是写给用户看的一句话。
#   通用的 `except Exception` 会用 f"{type(e).__name__}: {e}" 兜底 ——
#   那对真 bug 是对的（类型名是线索），但对这几个会把好好的中文变成
#   "ComfyUnreachable: 连不上 ComfyUI（…）"，多一个用户看不懂的前缀。
DELIBERATE = (BadParam, ComfyUnreachable)


def _err_text(e: Exception) -> str:
    """异常 -> 给用户看的一句话。

    故意抛的（`DELIBERATE`）直接用它的消息；其他保留类型名 —— 那是真 bug，
    把 `KeyError`/`TypeError` 这些线索删掉等于把排查依据也删了。
    """
    if isinstance(e, DELIBERATE):
        return str(e)
    return f"{type(e).__name__}: {e}"


def _err_code(e: Exception) -> int:
    """HTTP 状态码。ComfyUI 没开是"上游不可用"（503），不是"服务端出错"（500）。"""
    if isinstance(e, ComfyUnreachable):
        return 503
    return 400 if isinstance(e, BadParam) else 500


# ---------------------------------------------------------------- 参数范围
#
# 上游报告（2026-09-20，P1-2 / P1-3 / P1-4）实测确认的三件事：
#
#   ① 服务端**零范围校验**。直接调 build_graph（纯函数，零副作用）：
#      10 个参数里 9 个原样透传 —— width=999999 就真写 999999、
#      width=-100 就真写 -100、count=99999 就真写 batch_size 99999、
#      steps=0 就真写 0、checkpoint="../../evil.safetensors" 也原样写。
#
#   ② 顺带更正报告一处说法：报告写「ComfyUI 侧宽高/批量不挡」，
#      但打真实 HTTP 时 width/height/batch_size/clip_skip **都被它拒了**
#      （"width 超过上限（最大 16384）"）。所以准确的结论不是"会崩"，
#      而是**这道防线不在我们手里**：报错文案是 ComfyUI 的，走的是 HTTP 500
#      （"服务器挂了"）而不是 400（"你参数写错了"）—— 和项目自己在
#      `/api/upscale` 里明确区分这两种情况的做法冲突。
#
#   ③ cfg=NaN **静默出图成功（HTTP 200）**，没有任何提示 —— 这条最危险：
#      报错至少能被发现，一张"看起来正常但参数无意义"的产物会被当正常结果用下去。
#      实测过：`{"cfg":NaN}` 真的返回 200 并写出一张 PNG。
#
# 所以这里加的是**纵深防御**：界面本身有完整范围（steps 是 min=10 max=50 的
# range、count 只有 1/2/4、宽高是下拉框），正常点界面触发不了这几条 ——
# 它们只在"前端漏了 min"、"脚本调 API"、"复制粘贴的请求体"这类情况下生效。
#
# 范围取的是「比界面能送出的更宽」：不拦正常用法，只拦明显要毁事的值。
# 宽高下限 64 / 上限 2048：界面只有 768~1360，而 ComfyUI 自己上限 16384，
# 2048 挡住 width=999999 那种，又给脚本留了余量。
PARAM_RANGES = [
    ("width", 64, 2048, "像素"),
    ("height", 64, 2048, "像素"),
    ("steps", 1, 200, "步"),
    ("cfg", 0.1, 30.0, ""),
    ("count", 1, 8, "张"),
    ("clip_skip", 0, 12, "层"),
    ("mask_grow", 0, 256, "像素"),
    ("mask_feather", 0, 256, "像素"),
]

# 这些只要"是个有限数字"就行 —— 上下界由各自的业务逻辑管（例如权重允许负值）。
#
# 注意**没有** denoise：它不在 DEFAULTS 里，而且 `build_graph` 自己会把它夹到
# 1.0（txt2img 下 denoise<1 会留一层灰雾，见那里的注释）。这里再拦一道范围
# 只会在"以后放宽那个夹取"时突然拦住正常请求，所以只要求它有限。
PARAM_FINITE_ONLY = ("seed", "denoise", "sketch_strength", "depth_strength",
                     "ref_strength", "factor", "lora_strength")


def check_param_ranges(p: dict) -> None:
    """越界/NaN/Infinity 一律抛 BadParam（-> HTTP 400 + 一句能照着改的话）。

    为什么放这里而不是逐个写进 build_graph：那是核心建图函数，
    而且 `build_graph` / `build_inpaint_graph` 两个入口都要用 ——
    写两遍必然分叉。这里是单一入口，两个建图函数开头各调一次。
    """
    if not isinstance(p, dict):
        return                      # 不是 dict 的情况由 _read_body 管
    for key, lo, hi, unit in PARAM_RANGES:
        if key not in p:
            continue                # 没传 = 用 DEFAULTS，不用校验
        v = p[key]
        if isinstance(v, str) or isinstance(v, bool):
            continue                # 字符串交给下游 int()/float() 报 BadParam；
                                    # bool 是 int 的子类，不能当数字过 range
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue                # 下游会给出"应该是整数"那句更贴切的话
        # ★ NaN 的比较**全是 False** —— 不单独判就会**漏过去**
        #   （`not (NaN_lo <= NaN <= NaN_hi)` 里两个比较都是 False，
        #    但 `NaN <= hi` 也是 False，所以下面那行的 not 会变成 True 拦住它。
        #    这里显式写出来是为了让别人一眼看到"NaN 已经被想到了"，不靠推导。）
        if math.isnan(fv) or math.isinf(fv):
            raise BadParam("%s 收到了 %s，应该是 %s 到 %s 之间的数字"
                           % (key, "NaN" if math.isnan(fv) else "无穷大", lo, hi))
        if not (lo <= fv <= hi):
            raise BadParam("%s 收到了 %s，超出范围 —— 合法范围是 %s 到 %s%s"
                           % (key, ("%g" % fv), lo, hi, (" " + unit) if unit else ""))

    for key in PARAM_FINITE_ONLY:
        if key not in p:
            continue
        v = p[key]
        if isinstance(v, str) or isinstance(v, bool) or v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(fv) or math.isinf(fv):
            raise BadParam("%s 收到了 %s，应该是一个有限的数字"
                           % (key, "NaN" if math.isnan(fv) else "无穷大"))


def num_param(body: dict, key: str, default, cast=float):
    """从请求体取一个数字参数。坏值抛 BadParam（-> 400），不抛 TypeError。

    和 `drop_null_params` 是同一类问题的两半：
      · drop_null_params 管「键在、值是 null」
        （真实路径：前端数字框清空 -> parseFloat("") 是 NaN -> JSON 里变 null）
      · 这个管「值在、但不是数字」（手搓 API、脚本写错、复制粘贴带了引号）

    空串也当"没传"（返回 default）—— `int("")` 会抛 ValueError，
    而"输入框空着"的语义就是"用默认值"。

    ★ NaN / Infinity 也在这里拦：`float("inf")` 和 `float("nan")` **不抛异常**
    （那是合法的 Python float），所以下面那个 except 抓不到它们 ——
    上游报告 P1-3 实测 `{"width":Infinity}` 一路走到 `int(inf)` 才抛
    `OverflowError: cannot convert float infinity to integer`，用户看到的是
    Python 内部错误名。这里提前判掉，换成"收到的是什么、合法取值有哪些"。
    """
    v = body.get(key)
    if v is None or v == "":
        return default
    try:
        out = cast(v)
    except (TypeError, ValueError):
        raise BadParam("%s 收到了 %r，应该是%s"
                       % (key, v, "整数" if cast is int else "一个数字"))
    if isinstance(out, float) and not math.isfinite(out):
        raise BadParam("%s 收到了 %s，应该是一个有限的数字"
                       % (key, "NaN" if math.isnan(out) else "无穷大"))
    return out


def move_to_recycle(img_path: str) -> str:
    """把一张产出图移进回收站，返回新路径。

    为什么是"移"不是"删"：出图是几十分钟的算力，误点一下不该等于永久丢失。
    文件名前面加时间戳，重名就再编号 —— 回收站里可能已经有同名的。

    抽成独立函数是因为有两个调用点：
      · `/api/delete`：用户点删除
      · 局部重绘做完 poisson 融合之后：普通合成结果是**中间产物**，
        前端只拿到融合版，留着它会让用户点一次修图看到两张图
        （见 _blend 那段的注释）
    """
    os.makedirs(RECYCLE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(img_path)
    dst = os.path.join(RECYCLE_DIR, "%s_%s" % (stamp, base))
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(RECYCLE_DIR, "%s_%d_%s" % (stamp, n, base))
    shutil.move(img_path, dst)
    return dst


def is_in_recycle(img_path: str) -> bool:
    """这张图是不是已经在回收站里了。"""
    try:
        return os.path.commonpath([os.path.realpath(img_path),
                                   os.path.realpath(RECYCLE_DIR)]) \
            == os.path.realpath(RECYCLE_DIR)
    except ValueError:
        return False          # 不同盘符，那就不在


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):        # keep the console readable
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")

        def _no_const(token: str):
            """拒掉裸 JSON 里的 NaN / Infinity / -Infinity。

            ★ 这三样是 Python 的 `json.loads` **默认就收**的（不是标准 JSON，
            但 json 模块出于历史原因放行），而 `json.dumps` 默认又能产出它们。
            于是 `{"steps":NaN}` 这种请求体会被解析成 float('nan') 一路带进建图。

            上游报告（P1-3/P1-4）实测确认两条后果：
              · `{"steps":NaN}`    -> 500 ValueError: cannot convert float NaN to integer
              · `{"width":Infinity}` -> 500 OverflowError: cannot convert float infinity
              · `{"cfg":NaN}`      -> **200，静默出了一张图**（没有任何提示）
            最后一条最危险：报错至少能被发现，一张参数无意义的产物会被当正常结果用下去。

            这里在**入口**就拒，而不是在各处补 `math.isfinite` —— 和
            `drop_null_params` 同一个理由：入口一件事，下游 16 个调用点都不用改。
            """
            raise BadParam("请求体里有 %s —— JSON 里不允许 NaN/Infinity，"
                           "数字请写成普通数字（NaN 会被当没填、无穷大会被拒）"
                           % token)

        try:
            body = json.loads(raw, parse_constant=_no_const)
        except BadParam:
            raise
        except (ValueError, TypeError) as e:
            # `json.loads` 抛的是 "Expecting value: line 1 column 2 (char 1)" 这种
            # 英文位置信息 —— 把它包成项目自己的话（400），别让它变成 500。
            raise BadParam("请求体不是合法 JSON：%s" % e)
        return drop_null_params(body)

    def _origin_ok(self) -> bool:
        """这个请求是不是"自己人"发的。

        背景：这是个只听 127.0.0.1 的本地服务，**但"本地"不等于"可信"** ——
        浏览器里任何一个网页都能向它发请求，能让显卡跑一批图、能删产出、
        能改配置。文件里那段路径白名单只防了"读盘"，防不了这个。

        挡法不是加密码（用户还得输一遍），而是看浏览器**必然会带**、攻击者
        **伪造不了**的两个头：

          Host   : 必须是 127.0.0.1 / localhost / [::1]，端口也要对。
                   这一条防 DNS rebinding —— 攻击者把 evil.com 解析到 127.0.0.1，
                   页面的 Origin 和 Host 都会是 evil.com。只判 Origin 的话
                   "同源"这关会被绕过，Host 能戳穿它。
          Origin : 有就必须是我们的来源。浏览器跨源 POST 一定带这个头。

        为什么不做 token：本地脚本（urllib / curl）**不发 Origin**，所以这条
        规则对它们完全透明 —— 仓库里 24 个直接打 8765 的测试一个都不用改。
        而真正要防的那个场景（恶意网页），浏览器一定会带 Origin。
        """
        port = self.server.server_address[1]
        allowed = {"127.0.0.1:%d" % port, "localhost:%d" % port,
                   "[::1]:%d" % port, "::1:%d" % port}
        if (self.headers.get("Host") or "").strip().lower() not in allowed:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True               # 本地脚本/测试：不带 Origin，放行
        return origin in {"http://" + h for h in allowed}

    def _deny_cross_site(self, path: str) -> bool:
        """不通过就回 403 并返回 True（调用方直接 return）。"""
        if self._origin_ok():
            return False
        msg = ("只接受本机页面发来的请求（Host 必须是 127.0.0.1，Origin 也必须是 "
               "本服务）。如果你是用浏览器打开的，请从 127.0.0.1 或 localhost 访问。")
        if path.startswith("/api/"):
            self._json({"ok": False, "error": msg}, 403)
        else:
            # ★ 不能直接用 self.send_error(403, msg)：它把消息写进模板时用的是
            #   latin-1，中文会抛 UnicodeEncodeError，连接直接断掉、浏览器只看到
            #   "无法连接"（实测过）。这里自己按 utf-8 发。
            body = ("<!doctype html><meta charset='utf-8'>"
                    "<h3>403 拒绝访问</h3><p>%s</p>" % msg).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return True

    # ---- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if self._deny_cross_site(path):
            return
        if path in ("/", "/index.html"):
            fp = os.path.join(HERE, "ui.html")
            if not os.path.isfile(fp):
                self.send_error(404, "ui.html missing")
                return
            data = open(fp, "rb").read()
            # 注入构建戳：页面上直接显示两份源文件的修改时间。
            # 目的只有一个 —— 把"我看到的到底是不是最新界面"变成可自查的事，
            # 而不是靠猜浏览器缓存。顺便 no-store，别让浏览器拿旧页面。
            try:
                stamp = "v%s ・ 界面 %s ・ 后端 %s" % (
                    VERSION,
                    time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(fp))),
                    time.strftime("%m-%d %H:%M",
                                  time.localtime(os.path.getmtime(os.path.abspath(__file__)))))
                data = data.replace(b"<!--BUILD-->", stamp.encode("utf-8"))
            except Exception as e:
                warn("注入构建戳失败", e)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/capabilities":
            try:
                self._json({"ok": True, "data": capabilities()})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return
        # 提示词拼装：只做两件通用的事，不含业务规则 ——
        #   GET  /api/prompt_builder       读取分维度选项 + 用户自定义项
        #   POST /api/prompt_builder       保存一条自定义项（显示名 + 英文标签）
        # 选项数据放在应用目录的 prompt_builder.json，自定义项放
        # custom_prompt_options.json，改这两个文件即可，无需动代码。
        if path == "/api/prompt_builder":
            try:
                groups = []
                pb = os.path.join(HERE, "prompt_builder.json")
                if os.path.isfile(pb):
                    with open(pb, encoding="utf-8") as fh:
                        groups = json.load(fh)
                custom = {}
                # 允许用环境变量改到别处：测试脚本必须能避开真实数据文件，
                # 否则一次测试就会抹掉用户加的自定义项。
                cf = os.environ.get("COMEDY_CUSTOM_OPTIONS") or \
                    os.path.join(HERE, "custom_prompt_options.json")
                if os.path.isfile(cf):
                    try:
                        with open(cf, encoding="utf-8") as fh:
                            custom = json.load(fh)
                    except Exception as e:
                        warn("自定义选项读取失败", e)
                # 把用户自定义项并进对应分组
                for g in groups:
                    for item in (custom.get(g.get("group")) or []):
                        if isinstance(item, dict) and item.get("en"):
                            g.setdefault("items", []).append(
                                {"cn": str(item.get("cn") or item["en"]),
                                 "en": str(item["en"]), "custom": True})
                # 只在自定义里、分组表里没有的组，也一并给前端
                known = {g.get("group") for g in groups}
                for gname, items in custom.items():
                    if gname in known or not isinstance(items, list):
                        continue
                    groups.append({"group": gname, "items": [
                        {"cn": str(x.get("cn") or x.get("en")),
                         "en": str(x.get("en")), "custom": True}
                        for x in items if isinstance(x, dict) and x.get("en")]})
                self._json({"ok": True, "data": groups})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return
        if path == "/api/last":
            try:
                files = sorted(
                    (p for p, _f in iter_output_images(OUT_ANIME)),
                    key=os.path.getmtime, reverse=True)[:12]
                self._json({"ok": True, "files": files})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/recent":
            try:
                limit = 60
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("limit"):
                    # ★ 不能直接 int(q["limit"][0])：
                    #   `?limit=abc` 会抛 ValueError，被下面的 except 兜住，
                    #   于是用户拿到 HTTP 500 + "invalid literal for int() with
                    #   base 10: 'abc'" —— 既是 Python 味的报错（照不了改），
                    #   状态码也错了（这是坏请求，该 400 而不是"服务器挂了"）。
                    #   和 null 参数是同一类问题：坏输入要给出可照做的提示。
                    raw = q["limit"][0]
                    try:
                        limit = max(1, min(int(raw), 200))
                    except (TypeError, ValueError):
                        self._json({"ok": False,
                                    "error": "limit 收到了 %r，应该是 1 到 200 之间的整数"
                                             % raw}, 400)
                        return
                out = []
                for fp, f in iter_output_images(OUT_ANIME):
                    out.append({"path": fp, "name": f,
                                "mtime": os.path.getmtime(fp),
                                "size": os.path.getsize(fp)})
                out.sort(key=lambda d: d["mtime"], reverse=True)
                # Attach the source image for inpaint results so the UI can offer
                # a before/after comparison without the user hunting the file.
                prov = _load_provenance()
                for d in out[:limit]:
                    src = _resolve_prov_path(prov.get(d["path"]))
                    if src:
                        d["prev"] = src
                        d["origin"] = "inpaint"
                self._json({"ok": True, "files": out[:limit], "dir": OUT_ANIME})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if path == "/api/image":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fp = (q.get("path") or [""])[0]
            # 先过白名单再看文件在不在 —— 顺序很重要：先报越界，不要用
            # "文件不存在" 和 "不允许读" 混在一起，否则探测起来两者分不清。
            if not fp:
                self._json({"ok": False, "error": "缺少 path 参数"}, 400)
                return
            if not is_readable_path(fp):
                self._json({"ok": False,
                            "error": "只能读取产出目录和 ComfyUI input 目录下的文件"},
                           403)
                return
            if not os.path.isfile(fp):
                self._json({"ok": False, "error": "找不到这个文件"}, 404)
                return
            try:
                with open(fp, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                self._json({"ok": False, "error": "读取失败: %s" % e}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type_for(fp))
            self.send_header("Content-Length", str(len(data)))
            # 不要让浏览器靠内容去猜类型：一个 .txt 里如果写着 HTML，
            # 没有这个头它会被当成网页解析。
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    # ---- POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self._deny_cross_site(path):
            return
        try:
            body = self._read_body()
        except Exception as e:
            # 用 _err_text 而不是 f"{e}"：BadParam 的消息本身已经是写给用户的
            # 一句话（"width 收到了 NaN…"），再套一层和项目其他地方的文案就不一致了。
            self._json({"ok": False, "error": _err_text(e)},
                       _err_code(e) if isinstance(e, DELIBERATE) else 400)
            return

        # 保存一条自定义选项：{"group": 分组, "cn": 显示名, "en": 英文标签}
        if path == "/api/prompt_builder":
            try:
                group = str(body.get("group") or "").strip()
                cn = str(body.get("cn") or "").strip()
                en = str(body.get("en") or "").strip()
                if not group or not en:
                    self._json({"ok": False, "error": "分组和英文标签必填"}, 400)
                    return
                # 允许用环境变量改到别处：测试脚本必须能避开真实数据文件，
                # 否则一次测试就会抹掉用户加的自定义项。
                cf = os.environ.get("COMEDY_CUSTOM_OPTIONS") or \
                    os.path.join(HERE, "custom_prompt_options.json")
                data = {}
                if os.path.isfile(cf):
                    try:
                        with open(cf, encoding="utf-8") as fh:
                            data = json.load(fh)
                    except Exception as e:
                        warn("自定义选项读取失败，将重建", e)
                if not isinstance(data, dict):
                    data = {}
                lst = data.setdefault(group, [])
                if any(str(x.get("en", "")).lower() == en.lower() for x in lst
                       if isinstance(x, dict)):
                    self._json({"ok": True, "note": "已存在，未重复添加"})
                    return
                lst.append({"cn": cn or en, "en": en})
                tmp = cf + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, cf)          # 原子写入，避免写坏
                self._json({"ok": True, "count": len(lst)})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/translate":
            try:
                tr = translator()
                text = (body.get("text") or "").strip()
                if not text:
                    self._json({"ok": True, "english": "", "unknown": [],
                                "not_tags": []})
                    return
                # translate_detailed 比 translate 多一个 not_tags：手打的英文里
                # 哪些任何一层都查不到。界面用它显示提示，不改变 english。
                # 误报率实测 0/400 条真实组合提示词。
                d = tr.translate_detailed(text)
                self._json({"ok": True, "english": d["english"],
                            "unknown": d["unknown"], "not_tags": d["not_tags"]})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/set_config":
            """把界面上的选择写回 config.json。**白名单式**：只认下面这几个键。

            为什么要这样：用户换了底模，下次打开不该再选一遍。写回配置文件
            是唯一能持久的地方（这个应用没有数据库）。
            白名单是必须的 —— 否则任何人都能用它改 comfy_output 之类，
            把产出目录指到别处去。
            """
            global CHECKPOINT
            WRITABLE = {"checkpoint"}
            try:
                key = str(body.get("key") or "")
                val = body.get("value")
                if key not in WRITABLE:
                    self._json({"ok": False, "error": "这个键不允许改：%s" % key}, 400)
                    return
                if key == "checkpoint":
                    # 必须是 ComfyUI 真的认得的模型名，不然写进去就是启动即报错
                    ckpts = object_info_choices("CheckpointLoaderSimple", "ckpt_name")
                    if val not in ckpts:
                        self._json({"ok": False,
                                    "error": "ComfyUI 里没有这个底模：%s" % val}, 400)
                        return
                    import paths as _paths
                    _paths.save_config({"checkpoint": val})
                    CHECKPOINT = val      # 内存里的也要跟着变，否则本进程还在用旧的
                self._json({"ok": True, "key": key, "value": val})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/lora_alias":
            """给某个 LoRA 存一条**用户自己**的命名/分类，写进 lora_aliases.json。

            为什么需要这个写入口：分类链最高一层是"C站目录"（随包发布的公开事实），
            但它只覆盖得住在 C站上找得到的 LoRA —— 用户自己融合的、从别处下的、
            改过名的，目录里查不到，那条就只能靠文件名猜。**用户自己知道那是什么**，
            所以必须有条路让他把判断存下来。

            三个安全点：
              · 文件名必须在 ComfyUI 真的列得出来的 LoRA 里 —— 否则这个接口就成了
                "往用户 json 里塞任意键"的入口。
              · 只认下面这几个键（白名单），verdict 值也要在集合里。写进去的
                `kind` 是会被前端直接拿去分组的，随便一个字符串会让那个 LoRA
                从界面上消失（分组里找不到它）—— 所以要挡。
              · 走 `paths.save_json`（原子写 + 读不出来先备份）。用户这份文件里
                有他手写的全部备注，不能因为界面点一下就毁掉。
            """
            try:
                fname = str(body.get("file") or "").strip()
                if not fname:
                    self._json({"ok": False, "error": "缺少 file 参数"}, 400)
                    return
                try:
                    names = object_info_choices("LoraLoader", "lora_name")
                except Exception as e:
                    self._json({"ok": False,
                                "error": "现在连不上 ComfyUI，改不了（%s）"
                                         % _err_text(e)}, 503)
                    return
                base = os.path.basename(fname.replace("\\", "/"))
                hit = fname if fname in names else (
                    base if base in names else None)
                if hit is None:
                    self._json({"ok": False,
                                "error": "ComfyUI 的 LoRA 列表里没有这个文件：%s"
                                         % fname}, 400)
                    return
                KINDS = {"", "character", "style", "quality", "other"}
                alias = str(body.get("alias") or "").strip()[:40]
                kind = str(body.get("kind") or "").strip()
                if kind not in KINDS:
                    self._json({"ok": False,
                                "error": "分类只能是 %s 之一，收到的是 %r"
                                         % ("/".join(sorted(KINDS - {""})), kind)},
                               400)
                    return
                if "\n" in alias or "\r" in alias:
                    self._json({"ok": False, "error": "名字里不能有换行"}, 400)
                    return
                # ★ 读一遍现文件再改一个键 —— 不能拿内存里的副本整体覆盖：
                #   进程跑着的这段时间用户可能自己编辑过那个文件。
                cur = lora_aliases()
                entry = dict(cur.get(hit) or {})
                if alias:
                    entry["alias"] = alias
                else:
                    entry.pop("alias", None)
                if kind:
                    entry["kind"] = kind
                    entry["source"] = entry.get("source") or "手动"
                else:
                    entry.pop("kind", None)
                if entry:
                    cur[hit] = entry
                else:
                    cur.pop(hit, None)
                import paths as _paths
                _paths.save_json(ALIAS_FILE, cur)
                self._json({"ok": True, "file": hit, "alias": alias,
                            "kind": kind, "saved_to": ALIAS_FILE})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/delete":
            """把一张产出图移进回收站（_recycle/），不是真删。

            为什么不做真删：出图是几十分钟的算力，误点一下不该等于永久丢失；
            这个应用以前真的 os.remove 过用户文件且无法恢复（那次是自定义选项，
            见 tests/test_prompt_builder.py 顶部）。想彻底清掉，自己删 _recycle。
            """
            try:
                img_path = body.get("path") or ""
                if not img_path:
                    self._json({"ok": False, "error": "缺少 path 参数"}, 400)
                    return
                if not is_readable_path(img_path):
                    self._json({"ok": False,
                                "error": "只能删除产出目录和 ComfyUI input 目录下的文件"},
                               403)
                    return
                if not os.path.isfile(img_path):
                    self._json({"ok": False, "error": "找不到这张图"}, 404)
                    return
                # 已经在回收站里的就不要再套一层
                if is_in_recycle(img_path):
                    self._json({"ok": False, "error": "这张图已经在回收站里了"}, 400)
                    return
                dst = move_to_recycle(img_path)
                self._json({"ok": True, "moved_to": dst,
                            "name": os.path.basename(img_path)})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/upload":
            try:
                raw = base64.b64decode(body["data"])
                name = upload_image(raw, body.get("filename") or "upload.png")
                self._json({"ok": True, "name": name})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/read_meta":
            """成品图 -> 生成参数。原样读回 PNG 里的工作流，不做任何猜测。"""
            try:
                b64 = body.get("data") or ""
                if "," in b64 and "base64" in b64.split(",", 1)[0]:
                    b64 = b64.split(",", 1)[1]
                if not b64:
                    self._json({"ok": False, "error": "没有收到图片数据"}, 400)
                    return
                try:
                    raw = base64.b64decode(b64)
                except Exception:
                    self._json({"ok": False, "error": "图片数据解码失败"}, 400)
                    return
                if len(raw) < 100:
                    self._json({"ok": False, "error": "图片是空的"}, 400)
                    return
                self._json(read_image_meta(raw))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/depth":
            """参考图 -> 深度图。返回深度图的绝对路径，前端用 /api/image 预览。

            跟出图分开跑：用户先看深度图对不对，再决定要不要用它。
            """
            try:
                img = str(body.get("image") or "").strip()
                if not img:
                    raise ValueError("缺少 image 参数")
                os.makedirs(OUT_DEPTH, exist_ok=True)
                graph = build_depth_graph(img, body.get("model"),
                                          int(body.get("resolution") or 504))
                saved = run_graph(graph, OUT_DEPTH, "depth", timeout_s=600)
                depth_path = saved[-1]
                # 深度图得先回到 ComfyUI 的 input 目录，出图时才喂得进 ControlNet。
                # 放在这里做，前端就只用调一个接口。
                with open(depth_path, "rb") as fh:
                    staged = upload_image(fh.read(), "depth_" + os.path.basename(depth_path))
                self._json({"ok": True, "path": depth_path, "name": staged})
            except Exception as e:
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/reveal":
            try:
                os.startfile(OUT_ANIME)  # noqa: S606 - local convenience only
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/inpaint":
            """Redraw only the painted region of an existing image.

            The frontend sends the mask as a data URL: a PNG the same size as
            the source whose ALPHA channel is 255 where the user painted. The
            source image and that mask are staged into ComfyUI's input dir and
            the inpaint graph reads the mask from the alpha channel.
            """
            staged = None
            mask_staged = None
            try:
                img_path = body.get("path") or ""
                if not img_path:
                    self._json({"ok": False, "error": "缺少 path 参数"}, 400)
                    return
                if not is_readable_path(img_path):
                    self._json({"ok": False,
                                "error": "只能读取产出目录和 ComfyUI input 目录下的文件"},
                               403)
                    return
                if not os.path.isfile(img_path):
                    self._json({"ok": False, "error": "找不到这张图"}, 400)
                    return
                data_url = body.get("mask") or ""
                if "," not in data_url:
                    self._json({"ok": False, "error": "没有收到蒙版数据"}, 400)
                    return
                head, b64 = data_url.split(",", 1)
                if "base64" not in head:
                    self._json({"ok": False, "error": "蒙版格式不认识"}, 400)
                    return
                import base64 as _b64
                try:
                    raw = _b64.b64decode(b64)
                except Exception:
                    self._json({"ok": False, "error": "蒙版解码失败"}, 400)
                    return
                if len(raw) < 100:
                    self._json({"ok": False, "error": "蒙版是空的，请先在图上涂抹"}, 400)
                    return

                # ★ 两边都没提示词时**在这里就拒绝**，不要拿"只有质量词"的提示词
                #   去重画。实测（dev/user_journey.py 阶段 4，以用户视角走一遍）：
                #   蒙版框留空、主界面也空着，点「重画涂抹区域」真的跑了 187 秒，
                #   然后拿 `masterpiece, best quality` 重画那块区域 —— 结果和用户
                #   想改的东西毫无关系，他只会觉得"重绘坏了"。
                #   主界面的「生成」早就有这道闸（"请先写中文提示词"），重绘漏了。
                #   ★ 放在**入口**而不是 build_inpaint_graph 里：建图函数是纯函数，
                #     测参数的测试会直接调它、不传提示词（实测：放里面会让
                #     test_null_params / test_checkpoint_switch 一起红）。
                if not ((body.get("prompt") or "").strip()
                        or (body.get("main_prompt") or "").strip()):
                    self._json({"ok": False, "error":
                                "没有提示词可以重画：蒙版那个框留空 = 沿用左边主界面的"
                                "提示词，而现在两边都是空的。请先写一句要画成什么样"
                                "（例如「脸 更精细」「红裙子」）。"}, 400)
                    return

                from PIL import Image as _Image
                with _Image.open(img_path) as im:
                    w0, h0 = im.size

                # stage the source image
                ext = os.path.splitext(img_path)[1].lower() or ".png"
                staged = "inp_" + uuid.uuid4().hex[:8] + ext
                shutil.copyfile(img_path, os.path.join(COMFY_INPUT, staged))

                # stage the mask. ComfyUI's LoadImageMask("alpha") reads the
                # alpha channel, so the mask must be a valid image with alpha.
                mask_staged = "msk_" + uuid.uuid4().hex[:8] + ".png"
                mpath = os.path.join(COMFY_INPUT, mask_staged)
                with open(mpath, "wb") as f:
                    f.write(raw)
                try:
                    with _Image.open(mpath) as m:
                        if m.size != (w0, h0):
                            m = m.convert("RGBA").resize((w0, h0))
                            m.save(mpath)
                        if m.mode not in ("RGBA", "LA"):
                            # no alpha -> nothing painted; refuse rather than
                            # silently redrawing the whole image
                            self._json({"ok": False,
                                        "error": "蒙版没有透明通道，请重新涂抹"}, 400)
                            return
                        # ★ 这里**不要**再翻一次极性。
                        #
                        # 前端 exportMaskDataURL() 送出的约定是「涂过的地方 alpha=0」
                        # （先铺不透明黑底，再用 destination-out 把笔画擦成透明）。
                        # 而 ComfyUI 的 load_image() 读 alpha 时**自己就取反**
                        # （alpha=0 → mask=1 = 要重绘），两边正好对上。
                        #
                        # 实测 A/B（dev/truth_inpaint_scope.py，自造四象限色块图，
                        # 只涂右下角 3.85%，两个极性各跑一次）：
                        #   alpha=0 处=涂过 → 涂抹区内变 100.00%，区外仅边界 1 像素
                        #   alpha=255 处=涂过 → 涂抹区内变   0.00%，区外变 99.93%
                        # 所以"涂过=alpha 0"就是正确极性，服务端保持原样即可。
                except Exception as e:
                    self._json({"ok": False, "error": "蒙版读取失败: %s" % e}, 400)
                    return

                stem = time.strftime("inp_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
                graph = build_inpaint_graph(staged, mask_staged, body, f"temp/{stem}")
                files = run_graph(graph, day_dir(OUT_ANIME), stem)

                # Poisson (gradient-domain) blending.
                # The graph's ImageCompositeMasked has ALREADY produced
                # original-outside + redrawn-inside, so that file cannot be its
                # own destination - blending it with itself is a no-op (measured
                # 0.000 difference). The destination must be the ORIGINAL image
                # and the source the composited one: Poisson then keeps the
                # redrawn GRADIENTS (hair texture, detail) while solving for
                # values that match the surrounding original, which is what stops
                # the colour drift that made eyes change colour.
                blend_mode = str(body.get("blend") or "alpha").lower()
                if blend_mode != "alpha" and files:
                    try:
                        from PIL import Image as _Img
                        hard = os.path.join(COMFY_INPUT, "_hm_%s.png" % uuid.uuid4().hex[:6])
                        with _Img.open(mpath) as mm:
                            a = mm.convert("RGBA").getchannel("A")
                            # frontend paints alpha=0 for "redraw" -> invert so
                            # white marks the area to blend
                            a.point(lambda v: 255 - v).save(hard)
                        blended_files = []
                        for i, f in enumerate(files):
                            dstp = os.path.join(day_dir(OUT_ANIME), "%s_blend%d.png" % (stem, i))
                            poisson_blend.poisson_blend(
                                img_path,        # destination = ORIGINAL
                                f,               # source = redrawn composite
                                hard, dstp, mode="mixed")
                            # ★ 融合版是 cv2 写的，不带 PNG 文本块 —— 不补这一步，
                            #   用户对着一张刚生成的图点「读回参数」会得到
                            #   "这张图里没有生成参数…被聊天软件转过存？"。
                            #   实测见 dev/probe_read_meta.py。
                            poisson_blend.copy_png_text(f, dstp)
                            blended_files.append(dstp)
                        try:
                            os.remove(hard)
                        except Exception:
                            pass
                        # ★ 普通合成结果是**中间产物**：用户要的是融合版，前端也只
                        #   拿到融合版（下面 files = blended_files）。但 ComfyUI
                        #   已经把它写进产出目录了 —— 不管它就会一直留在那儿，
                        #   还会出现在「最近产出」里：用户点一次修图，画廊里
                        #   却多出两张图（一张他从来没要求过）。
                        #   移进回收站而不是删掉：万一融合效果不好，还能找回来。
                        for f in files:
                            try:
                                move_to_recycle(f)
                            except Exception as e:
                                warn("回收中间产物（普通合成结果）失败", e)
                        files = blended_files
                    except Exception as e:
                        warn("Poisson 融合失败，保留普通合成结果", e)

                # remember where each result came from, for before/after compare
                for f in files:
                    _record_provenance(f, img_path)

                # Report the prompt that was actually used, so a Chinese repair
                # prompt or a fallback to the main prompt is visible rather than
                # silently guessed at.
                used = ""
                for node in graph.values():
                    if node.get("class_type") == "CLIPTextEncode":
                        t = str(node["inputs"].get("text") or "")
                        if t and t != (body.get("negative") or ""):
                            used = t
                            break
                self._json({"ok": True, "files": files, "size": [w0, h0],
                            "used_prompt": used,
                            "used_fallback": bool(body.get("_inpaint_used_fallback")),
                            "unknown": body.get("_inpaint_unknown") or []})
            except Exception as e:
                _log_exc(e)
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            finally:
                for f in (staged, mask_staged):
                    if not f:
                        continue
                    try:
                        os.remove(os.path.join(COMFY_INPUT, f))
                    except Exception as e:
                        warn("清理蒙版中转文件失败", e)
            return

        if path == "/api/upscale":
            try:
                img_path = body.get("path") or ""
                if not img_path:
                    self._json({"ok": False, "error": "缺少 path 参数"}, 400)
                    return
                if not is_readable_path(img_path):
                    self._json({"ok": False,
                                "error": "只能读取产出目录和 ComfyUI input 目录下的文件"},
                               403)
                    return
                if not os.path.isfile(img_path):
                    self._json({"ok": False, "error": "找不到这张图"}, 400)
                    return
                # copy into ComfyUI's input dir so LoadImage can read it
                base = os.path.basename(img_path)
                staged = "up_" + uuid.uuid4().hex[:8] + os.path.splitext(base)[1]
                shutil.copyfile(img_path, os.path.join(COMFY_INPUT, staged))

                from PIL import Image as _Image
                with _Image.open(img_path) as im:
                    w0, h0 = im.size
                factor = num_param(body, "factor", 2.0, float)
                # 4096 的上限必须**整体**施加，不能宽高各自 min() ——
                # 细节和踩过的坑都写在 upscale_target() 的 docstring 里。
                tw, th = upscale_target(w0, h0, factor)
                tw = num_param(body, "width", tw, int)
                th = num_param(body, "height", th, int)
                # 默认值 = UPSCALE_MODEL_DEFAULT（理由见那个常量的注释：
                # 同一张图四方实测过，anime 专用的 6B 版全面胜出）。
                model = body.get("model") or UPSCALE_MODEL_DEFAULT
                stem = time.strftime("up_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
                graph = build_upscale_graph(staged, model, tw, th, f"temp/{stem}")
                files = run_graph(graph, day_dir(OUT_ANIME), stem)
                try:
                    os.remove(os.path.join(COMFY_INPUT, staged))
                except Exception as e:
                    warn("清理上传文件失败", e)
                self._json({"ok": True, "files": files,
                            "from": [w0, h0], "to": [tw, th]})
            except BadParam as e:
                # 参数写错是"坏请求"，不是"服务器挂了" —— 状态码要分开，
                # 否则用户按 500 去查服务日志，永远找不到原因。
                self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                _log_exc(e)
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        if path == "/api/generate":
            try:
                p = dict(body)
                tr = translator()
                chinese = (p.get("prompt_cn") or "").strip()
                if chinese:
                    english, resolved, unknown = tr.translate(chinese)
                else:
                    english, resolved, unknown = (p.get("prompt_en") or "").strip(), [], []
                # Distinguish two cases, because they need different rules:
                #   * Chinese input -> at least one Chinese word must have
                #     resolved, otherwise the user asked for something the
                #     dictionary cannot express (garbage in, empty prompt out).
                #     Counting dictionary hits alone is not enough: "zzz" IS a
                #     real Danbooru tag (it means sleeping), so pure-garbage
                #     Chinese input could otherwise sneak through on an ASCII
                #     fragment.
                #   * Pure English input -> the user is writing raw tags on
                #     purpose, so pass it through untouched.
                has_cjk = bool(re.search(r"[\u4e00-\u9fff]", chinese)) if chinese else False
                if has_cjk:
                    # Count only resolutions whose SOURCE term was Chinese.
                    # `resolved` also carries ASCII pass-throughs (a stray
                    # "zzzz" yields "zzz"), so a non-empty list does not prove
                    # any Chinese word was understood.
                    cn_hits = [en for src, en in resolved
                               if en and re.search(r"[\u4e00-\u9fff]", src)]
                    if not english or not cn_hits:
                        self._json({"ok": False,
                                    "error": "提示词里没有可识别的中文词，请换个说法"
                                             "（参考 %s）" % VOCAB,
                                    "unknown": unknown[:20]}, 400)
                        return
                elif not english:
                    self._json({"ok": False, "error": "提示词为空"}, 400)
                    return
                # 主体词不再自动补（原 SUBJECT_DEFAULT = "1girl, solo"）。
                # 用户 2026-09-20 要求彻底删掉：它会给 "2girls" 这种多人提示词
                # 补上自相矛盾的 "1girl, solo"。现在只送用户自己写的。
                # The client sends the quality tags it has ticked; fall back to
                # the default set when it sends none (older clients omit it).
                q = p.get("quality")
                if isinstance(q, list):
                    quality = ", ".join(str(x).strip() for x in q if str(x).strip())
                elif q is None:
                    quality = QUALITY
                else:
                    quality = str(q).strip()
                p["positive"] = ", ".join(
                    x for x in (quality, english) if x)
                # LoRA 触发词不再自动注入：同一个 LoRA 常有不同服装/姿势/器官
                # 变体，各自的触发词不同，统一注入会与用户提示词打架。
                # 触发词改由界面提供「填入」按钮，用户自己决定用哪一组。
                p.setdefault("seed", int.from_bytes(os.urandom(4), "big"))
                # 见 resolve_seed 的说明：setdefault 挡不住「键在但值是 None」。
                p["seed"] = resolve_seed(p["seed"])
                os.makedirs(OUT_ANIME, exist_ok=True)
                # 文件名带完整种子，便于从种子反查是哪张图；批次序号由
                # ComfyUI 的 counter 追加，同一秒多次提交也不会撞名。
                stem = time.strftime("ui_%Y%m%d_%H%M%S") + f"_{int(p['seed'])}"
                # unique per run so ComfyUI's file counter can never collide
                p["prefix"] = f"temp/{stem}"
                graph = build_graph(p)
                files = run_graph(graph, day_dir(OUT_ANIME), stem)
                self._json({"ok": True, "files": files,
                            "english": p.get("positive"),
                            "trigger_words": p.get("trigger_words") or [],
                            "unknown": unknown, "seed": p["seed"]})
            except Exception as e:
                _log_exc(e)
                self._json({"ok": False, "error": _err_text(e)}, _err_code(e))
            return

        self.send_error(404)


def _port_free(port: int) -> bool:
    """端口是不是真的空着。

    ★ 不能用"试着 bind 一下 HTTPServer"来判断：HTTPServer 默认
      allow_reuse_address=1，而 **Windows 的 SO_REUSEADDR 允许多个 socket 绑同一
      端口**（Unix 语义是只允许 TIME_WAIT 复用）。于是第二个实例会"启动成功"、
      日志一切正常，浏览器连过去却卡在第一句话上 —— 实测就是这么卡住的。

      用不带 SO_REUSEADDR 的裸 socket 试绑，Windows 上才真的会抛 WSAEADDRINUSE。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# 客户端自己断开时抛的异常。**这是正常的**（关标签页、刷新、取消一个还没画完的
# 请求都会这样），不是我们这边的错。
#
# 实测（dev/audit_client_abort.py 量的）：一个跑了一天的服务日志里
# **1075 段 traceback 全来自这里**，1.36 MB 全是这个 —— 用户双击 .bat 打开的
# 黑窗口会被它刷屏，真正该看的错误反而埋在中间。
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError,
               ConnectionError)


class QuietServer(ThreadingHTTPServer):
    """客户端断开连接时不要打 traceback。

    `socketserver` 默认会在 `handle_error()` 里把任何异常整个打成 traceback，
    而"用户在页面还在加载时按了刷新"就会走到这里。那些 traceback：
      · 对用户毫无意义（他什么都没做错，我们也没出错）
      · 把控制台刷满，真错误被埋掉
      · 实测一天 1075 段
    所以这类异常直接吞掉，其他异常照旧走父类（真 bug 的 traceback 必须留着）。
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            return                      # 预期内：客户端走了
        super().handle_error(request, client_address)


def bind_server(preferred: int, tries: int = 20):
    """在 127.0.0.1 上绑定端口，被占用就往上找下一个空闲的。

    为什么要自动找：8765 只是约定，别人的机器上完全可能被别的东西占着。
    写死端口的话，用户双击启动只看到一句 "Address already in use"，
    根本不知道该怎么办。宁可换个端口也要起得来 —— 地址会打印在窗口里。

    只在 127.0.0.1 上绑，不监听外网：这个服务没有任何鉴权（只有路径白名单），
    暴露到局域网等于把产出目录和出图能力交给同网段的任何人。
    """
    for i in range(tries):
        p = preferred + i
        if not _port_free(p):
            if i == 0:
                print("  端口 %d 被占用（可能已经有一个在跑），往上找空闲端口…" % p)
            continue
        return QuietServer(("127.0.0.1", p), Handler), p
    raise SystemExit("  从 %d 往上找了 %d 个端口都被占用，请先关掉占用的程序。"
                     % (preferred, tries))


def main() -> int:
    port = PORT                      # paths.py 给的：config.json 的 port，默认 8765
    open_browser = True
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
        # 手动双击启动时希望自动打开页面；但脚本/测试反复重启服务时，
        # 每重启一次就弹一个新标签页，很干扰。这类场景显式加 --no-browser。
        if a in ("--no-browser", "--headless"):
            open_browser = False

    os.makedirs(OUT_ANIME, exist_ok=True)

    # ★ 收口判据：config.json 不在，但这台机器用过这个应用吗？
    #   必须排在 `os.makedirs(OUT_ANIME)` **之后** —— 这条判据要看的就是
    #   产出目录里有没有出图的日期文件夹。写在前面的话，首次启动刚建出来的
    #   空目录会让它判成"没用过"，看着也对；但顺序反了就会变成"永远判没用过"，
    #   而这正是它要抓的那种静默失败。
    #   顺带：它一设就把提示打到 stderr，界面上再从 /api/capabilities 读到同一句。
    APPATHS.note_config_gone_if_used()

    srv, port = bind_server(port)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print("  Snakeer Drawing")
    print("=" * 62)
    print(f"  地址   : {url}")
    print(f"  输出   : {OUT_ANIME}")
    print(f"  ComfyUI: {COMFY}")
    print()
    print("  浏览器应该会自动打开。关闭这个窗口即停止服务。"
          if open_browser else
          "  已关闭自动打开浏览器。关闭这个窗口即停止服务。")
    print("=" * 62)
    if open_browser:
        try:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception as e:
            warn("自动打开浏览器失败", e)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
