# -*- coding: utf-8 -*-
"""所有路径的唯一来源。

在这个文件出现之前，路径逻辑写在 server.py 里，而**其他 60 多个文件各自
写死 `D:\\dsh\\webapp`** —— 换台电脑就全线跑不起来。现在全部改成 import 这里。

三类路径，三种做法：

  1. 应用自身的位置（ui.html、词表、标签库、配置）
     用 __file__ 推算，**永远不写死，也不让用户配** —— 它跟着代码走。

  2. ComfyUI 的位置（input / output / models）
     每台机器不同，四级回退：
         config.json  >  环境变量  >  自动探测  >  应用目录下的兜底
     留空即自动探测，所以 config.example.json 里这些键全是空字符串。

  3. 用户数据（产出目录、回收站、深度图）
     跟着 ComfyUI 的 output 走，不用单独配。

用法：
    from paths import APP_DIR, OUT_ANIME, COMFY_INPUT
"""
from __future__ import annotations

import json
import os
import sys

# ---------------------------------------------------------------- 1. 应用自身
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

UI_HTML = os.path.join(APP_DIR, "ui.html")
VOCAB = os.path.join(APP_DIR, "高频词表.txt")
TAG_DB = os.path.join(APP_DIR, "data", "danbooru_tags.sqlite3")
BASE_TAGS = os.path.join(APP_DIR, "data", "base_tags.json")
LORA_ALIASES = os.path.join(APP_DIR, "lora_aliases.json")
PROVENANCE = os.path.join(APP_DIR, "_provenance.json")
PROMPT_BUILDER = os.path.join(APP_DIR, "prompt_builder.json")
CUSTOM_PROMPT_OPTIONS = os.path.join(APP_DIR, "custom_prompt_options.json")
TESTS_DIR = os.path.join(APP_DIR, "tests")
SERVER_PY = os.path.join(APP_DIR, "server.py")
VERSION_FILE = os.path.join(APP_DIR, "VERSION")


def _read_version() -> str:
    """读 VERSION 文件。读不到就给个明确的占位，不要假装是 0.0.0。

    为什么单独一个文件而不是常量：CHANGELOG、Release 标题、页头显示的版本
    必须是同一个数。散在代码里迟早对不上（这个项目在"默认值写两处"上已经
    吃过一次亏）。
    """
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            v = fh.read().strip()
        return v or "0.0.0-dev"
    except Exception:
        return "0.0.0-dev"


VERSION = _read_version()


def tests_path(name: str) -> str:
    """测试用的临时文件/夹具路径，例如 tests_path("_caps.json")。

    存在的意义：测试里原来把夹具路径写死成绝对路径，
    换个电脑或换个目录就全废。统一走这里，也就不会再有硬编码。
    """
    return os.path.join(TESTS_DIR, name)


# config.json 存在但读不出来时的原因（None = 没问题）。
#
# ★ 为什么单独存一份、还要一路报到界面上：
#   坏掉的 config 会被静默换成「自动探测」—— 用户填的 comfy_output 整份失效，
#   图跑到 ComfyUI 自己的 output 里去了，而**界面上看不出任何异常**。
#   实测（dev/probe_broken_config.py）：带 BOM 的、多一个逗号的、写成数组的，
#   三种都会走到这条路。这正是这个项目最忌讳的「静默失败」。
CONFIG_ERROR = None


def _load_config() -> dict:
    """读 config.json。不存在是正常情况（首次运行），不报错。

    ★ 编码用 **utf-8-sig**：带 BOM 的 UTF-8 也要认。
      Windows 上不少编辑器存 UTF-8 时会写 BOM，而 `json.load(encoding="utf-8")`
      碰到 BOM 直接报 "Unexpected UTF-8 BOM (decode using utf-8-sig)" ——
      实测：用户填的路径整份被丢掉。utf-8-sig 读**不带** BOM 的文件也完全正常，
      所以这么改没有任何代价。
    """
    global CONFIG_ERROR
    here = "（%s）" % CONFIG_PATH
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return {}                      # 首次运行 —— 正常，不出声
    except UnicodeDecodeError as e:
        CONFIG_ERROR = (
            "config.json 不是 UTF-8 编码%s —— %s。"
            "用记事本打开、另存为 UTF-8，或者把它删掉再跑一次「安装模型.bat」；"
            "现在用的是自动探测出来的目录，你填的路径没有生效。" % (here, e))
    except Exception as e:
        # 只有"文件在但坏了"才值得出声 —— 这种情况用户改了配置却看不出问题
        CONFIG_ERROR = (
            "config.json 读不出来%s —— %s。"
            "常见原因是多/少一个逗号、少一个引号。修好它，或者把它删掉再跑一次"
            "「安装模型.bat」；现在用的是自动探测出来的目录，"
            "你填的路径没有生效。" % (here, e))
    else:
        if isinstance(d, dict):
            return d
        # ★ 这一支原来**完全不出声**：文件是合法 JSON、但最外层是个数组/字符串，
        #   于是 `isinstance` 判断静默返回 {}，用户以为自己配好了。
        CONFIG_ERROR = (
            "config.json 的最外层应该是一个 { } 对象%s，实际是 %s。"
            "把它删掉再跑一次「安装模型.bat」，会得到一个带说明的正确模板。"
            % (here, type(d).__name__))
    print("[warn] %s" % CONFIG_ERROR, file=sys.stderr)
    return {}


CONFIG = _load_config()


def save_config(updates: dict) -> dict:
    """把少量键写回 config.json，返回写回后的完整配置。

    只给"应用自己也需要改"的设置用（目前只有底模 —— 用户在界面上选了之后
    不该每次刷新都重选）。别的键不会被碰。

    三个细节：
      · **保留原文件的换行风格**。config.json 是 CRLF（Windows 编辑器建的），
        用 json.dump 直接写会变成 LF —— 内容逐字节相同，但整个文件在 diff 里
        全变，看着像被重写了。
      · **原子写**：先写 .tmp 再 os.replace。中途断电或被杀不会留下半个配置
        —— 这个应用就是靠 config.json 找目录的，写坏它等于启动不了。
      · **原文件读不出来时先备份**（`config.json.bak`）。
        这是真实的数据丢失路径：用户的 config.json 里有一个逗号写错了，他改天
        在界面上换个底模 —— 那一刻 `CONFIG` 是空的（读失败过），写回就变成
        「只有 checkpoint 一个键」，他手写的那些路径**连同那个错一起没了**，
        连回去改的机会都没有。所以先把原文件原样存一份再覆盖。
    """
    global CONFIG
    raw = ""
    existed = os.path.isfile(CONFIG_PATH)
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig", newline="") as fh:
            raw = fh.read()
    except FileNotFoundError:
        existed = False
    except Exception:
        raw = ""                      # 读不出来（BOM/GBK/坏 JSON）：下面走备份
    if existed and CONFIG_ERROR and raw:
        try:
            with open(CONFIG_PATH + ".bak", "w", encoding="utf-8",
                      newline="") as fh:
                fh.write(raw)
            print("[warn] config.json 原来读不出来，已备份到 %s.bak"
                  % CONFIG_PATH, file=sys.stderr)
        except Exception as e:
            print("[warn] 备份 config.json 失败: %s" % e, file=sys.stderr)
    nl = "\r\n" if "\r\n" in raw else "\n"
    cfg = dict(CONFIG)
    cfg.update(updates)
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", nl))
    os.replace(tmp, CONFIG_PATH)
    CONFIG = cfg
    return cfg


def _detect_comfy_dirs() -> dict:
    """按 ComfyUI Desktop 的常见布局找 input / output / models。

    找不到就返回 None，由 config.json 或调用方补上。
    只认这几个位置，不做全盘搜索 —— 扫盘又慢又可能读到自己没权限的目录。
    """
    found = {"input": None, "output": None, "models": None}
    roots = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = os.path.join(local, "Comfy-Desktop")
        roots.append(os.path.join(base, "ComfyUI-Shared"))
        inst = os.path.join(base, "ComfyUI-Installs")
        if os.path.isdir(inst):
            for name in os.listdir(inst):
                roots.append(os.path.join(inst, name, "ComfyUI", "ComfyUI"))
    # 便携版 / 手动安装：由环境变量指定
    for extra in (os.environ.get("COMFYUI_ROOT"),):
        if extra:
            roots.append(extra)
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for key, sub in (("models", "models"), ("input", "input"), ("output", "output")):
            p = os.path.join(r, sub)
            if found[key] is None and os.path.isdir(p):
                found[key] = p
    return found


_DET = _detect_comfy_dirs()


def _pick(config_key: str, env_key: str, detected, fallback: str) -> str:
    return (CONFIG.get(config_key) or os.environ.get(env_key)
            or detected or fallback)


# ---------------------------------------------------------------- 2. ComfyUI
# 「本地服务」的地址。不是路径，但同属"每台机器不同"的东西，放一起便于排查。
COMFY_URL = CONFIG.get("comfy_url") or os.environ.get("COMFYUI_URL") \
    or "http://127.0.0.1:8188"

COMFY_INPUT = _pick("comfy_input", "COMFYUI_INPUT", _DET["input"],
                    os.path.join(APP_DIR, "comfy_input"))
COMFY_OUTPUT = _pick("comfy_output", "COMFYUI_OUTPUT", _DET["output"],
                     os.path.join(APP_DIR, "output"))
MODELS_DIR = _pick("models_dir", "COMFYUI_MODELS", _DET["models"],
                   os.path.join(APP_DIR, "models"))

# 默认底模。空 = 用下面这个兜底名，check_env.py 会告诉你装没装。
#
# ★ 兜底名必须和 README 让用户装的那个模型一致。
#   这一条踩过：更早的默认底模写的是 "Illustrious-XL-v2.0.safetensors"，
#   而 README 的模型清单和 install_models.py 装的是另一个名字 ——
#   于是**全新安装的用户明明装对了，check_env.py 却报"找不到底模"**，
#   而且报的是一个他从来没听说过的文件名。
#   正常流程里 config.json 会被 安装模型.bat 写好，但"没有 config.json"
#   恰恰是新装用户的默认状态，所以这个兜底值是真会被用到的。
#   当前默认 = Illustrious-XL 官方 v2.0（= install_models.MODELS 里的第一条，
#   两处必须一致；dev/audit_release.py 会核对发布包的 config.example.json）。
CHECKPOINT = CONFIG.get("checkpoint") or "Illustrious-XL-v2.0.safetensors"

# ---------------------------------------------------------------- 3. 用户数据
OUT_ANIME = os.path.join(COMFY_OUTPUT, "anime")
OUT_DEPTH = os.path.join(OUT_ANIME, "_depth")
RECYCLE_DIR = os.path.join(OUT_ANIME, "_recycle")

# ---------------------------------------------------------------- 4. 服务端口
# 端口以前写死在 main() 里，只能用 --port 覆盖。现在进配置：
# 留空 = 从 8765 开始往上找第一个空闲端口（别人机器上 8765 可能被占）。
DEFAULT_PORT = 8765
try:
    PORT = int(CONFIG.get("port") or 0) or DEFAULT_PORT
except (TypeError, ValueError):
    PORT = DEFAULT_PORT
