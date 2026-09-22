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

# config.json **根本不在**（不是"坏了"）。同样为 None 的 CONFIG_ERROR 分不清这两种，
# 而它们的处理方式不同：文件在但坏了 → 直接报；文件不在 → 还要判是不是首次运行。
CONFIG_WAS_MISSING = False


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
#
#   ★ 放在这么靠前是**必须的**，不是随手放的：`_load_config()` 在模块中段就
#     被调用，它那条"config.json 不见了"的提示里要写出这个文件名 ——
#     定义太靠后就会在 import 期抛 `NameError`（服务起不来）。
FALLBACK_CHECKPOINT = "Illustrious-XL-v2.0.safetensors"


def _config_was_deleted() -> bool:
    """config.json 不在了 —— 但这台机器**已经用过这个应用**了吗？

    ★ 为什么必须问这一句（09-22 实测踩到）：
      打包器的 `--clean` 曾经把发布目录里的 `config.json` 一起铲掉，
      于是服务的 `FileNotFoundError` 分支把它当成「首次运行」静默放行 ——
      `CHECKPOINT` 退回写死的 `Illustrious-XL-v2.0.safetensors`，
      **界面上一个提示都没有**。用户以为在用自己挑的底模，其实没有。
      这不是"配置坏了"，是"配置没了"，而原来的代码只能认出前一种。

    判据只能落在**服务自己留下的、用户不会去动的东西**上：
      ① `_provenance.json` —— 出图时自动写的溯源记录
      ② `lora_aliases.json` / `custom_prompt_options.json` —— 用户在界面里配的
     `说明.txt` 不行：它是发布包自带的，"在"证明不了任何事。

    这个函数是 `note_config_gone_if_used()` 的**第一半**（import 期就能查的那半）：
    它只看得见模块前段的路径常量。第四条物证"产出目录里已经有出图"依赖
    `OUT_ANIME`，排在文件后半段，import 期还不存在 —— 那半在
    `_output_dir_has_shoots()` 里，同样由收口函数调用。

    踩点：第一版直接在函数体里写 `os.listdir(OUT_ANIME)`，看着没事
    （函数体是调用时才解析名字）—— 但**这个函数就是在 import 期被调用的**，
    于是"配置没了"这条分支第一次真的被执行时抛 `NameError`，
    **症状是服务根本起不来**，而不是某个功能不对。
    """
    for p in (PROVENANCE, LORA_ALIASES, CUSTOM_PROMPT_OPTIONS):
        if os.path.isfile(p):
            return True
    return False


def _load_config() -> dict:
    """读 config.json。不存在时要分清「首次运行」和「配置没了」。

    ★ 编码用 **utf-8-sig**：带 BOM 的 UTF-8 也要认。
      Windows 上不少编辑器存 UTF-8 时会写 BOM，而 `json.load(encoding="utf-8")`
      碰到 BOM 直接报 "Unexpected UTF-8 BOM (decode using utf-8-sig)" ——
      实测：用户填的路径整份被丢掉。utf-8-sig 读**不带** BOM 的文件也完全正常，
      所以这么改没有任何代价。
    """
    global CONFIG_ERROR, CONFIG_WAS_MISSING
    here = "（%s）" % CONFIG_PATH
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        # 文件不在。**这里先不出声**：光凭 import 期能看到的东西分不清
        # "首次运行"和"配置被删了"—— 最后一条物证（产出目录里有没有出图）
        # 要等 OUT_ANIME 定义出来才知道。交给 `note_config_gone_if_used()`，
        # 由启动流程在最后调一次。
        CONFIG_WAS_MISSING = True
        return {}
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


def save_json(path: str, data, indent: int = 1) -> None:
    """原子地把一个 dict 写回某个 JSON 文件。

    和 `save_config` 同一套讲究，抽出来是因为现在有**第二个**这样的文件了
    （`lora_aliases.json` —— 用户在界面上给自己 LoRA 改名字/改分类）。这套讲究
    不是洁癖，每一条都有理由：

      · **保留原文件的换行风格**。用户可能用记事本编辑过（CRLF），也可能一直是
        LF。直接 `json.dump` 会把整个文件的换行换掉 —— 内容没变，diff 里却全变。
      · **原子写**：先写 `.tmp` 再 `os.replace`。中途断电/被杀不会留下半个文件。
      · **读不出来时先备份**（`.bak`）。这是真实的数据丢失路径：用户手写的 JSON
        有一个逗号错了，然后在界面上改了一个 LoRA 的名字 —— 那一刻解析是失败的，
        覆盖下去就把**他手写的全部备注**连同那个错一起抹了。先存一份再说。

    写失败会往上抛，由调用方决定怎么报告 —— 静默失败是这个项目最忌讳的事。
    """
    raw = ""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            raw = fh.read()
    except FileNotFoundError:
        raw = ""
    except Exception:
        raw = ""
    broke = bool(raw) and raw.strip() not in ("", "{}")
    if broke:
        try:
            json.loads(raw.lstrip("\ufeff"))
        except Exception:
            try:
                with open(path + ".bak", "w", encoding="utf-8",
                          newline="") as fh:
                    fh.write(raw)
                print("[warn] %s 原来读不出来，已备份到 %s.bak"
                      % (os.path.basename(path), path), file=sys.stderr)
            except Exception as e:
                print("[warn] 备份 %s 失败: %s" % (path, e), file=sys.stderr)
    nl = "\r\n" if "\r\n" in raw else "\n"
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace("\n", nl) + nl)
    os.replace(tmp, path)


def _desktop_conf_dirs() -> list:
    """ComfyUI Desktop 把它的数据放在哪几个目录（ROAMING/APPDATA）。

    Windows: %APPDATA%\\Comfy Desktop     （实测本机就是这个）
    macOS/Linux: Electron 的 app.getPath("userData")

    三种都列出来、哪个在就用哪个 —— 找不到就是空列表。
    顺带认一下 data-location.json：桌面版有个 dev 模式会把数据放别处
    （{"mode":"local-appdata"} 是常规；别的值我们认不出来，就不猜）。
    """
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "Comfy Desktop"))
    home = os.path.expanduser("~")
    out.append(os.path.join(home, "Library", "Application Support",
                            "Comfy Desktop"))            # macOS
    out.append(os.path.join(home, ".config", "Comfy Desktop"))  # Linux
    return [p for p in out if p and os.path.isdir(p)]


def _read_desktop_settings() -> list:
    """读桌面版 settings.json，返回 [(类型, 路径), ...]。

    类型是 "models" / "input" / "output" / None —— None 表示"这是一个
    可能的安装根目录，去它下面找 models/input/output 子目录"。

    ★ 为什么不能按文件夹名字去认（这一版踩过）：
      我第一版写成"路径最后一段叫 output 才算输出目录"，于是用户把输出目录
      取名叫 `D:\\comfyout` 时**认不出来**，白白浪费了配置里写着的答案。
      真正可靠的做法是**看这个值挂在哪把钥匙上**：`outputDir` 就是输出目录，
      跟它叫什么都不相干。测试 `tests/test_paths_detect.py` 钉的就是这条。

    ★ 为什么这个文件最可信：**是用户在桌面版界面里选的**，不是我们猜的。
      用户把模型挪到 D 盘、把输出改成 D:\\comfyout，答案就写在这里。
      我们的自动探测再怎么加猜法，都不如直接问他一句。

    读不出来/格式不认识**一律返回空**，绝不出声 —— 这只是一条"锦上添花"的
    探测路径，文件不存在（没装桌面版）是完全正常的。半路报错会把用户吓到，
    而且真正的兜底在下面。
    """
    found = []
    for conf in _desktop_conf_dirs():
        path = os.path.join(conf, "settings.json")
        try:
            with open(path, encoding="utf-8-sig") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue

        # modelsDirs：桌面版支持多个模型目录，第一个是默认那个。
        # 它按约定就叫 ...\models，但也可能被改过名 —— 认不出来就当根目录试。
        md = d.get("modelsDirs")
        if isinstance(md, str):
            md = [md]
        if isinstance(md, list):
            for p in md:
                if isinstance(p, str) and p.strip():
                    p = p.strip()
                    key = ("models" if os.path.basename(
                        p.rstrip("\\/")).lower() == "models" else None)
                    found.append((key, p))
        for key, name in (("input", "inputDir"), ("output", "outputDir")):
            v = d.get(name)
            if isinstance(v, str) and v.strip():
                found.append((key, v.strip()))

        # installDir：桌面版装在别的盘时，它的 input/output/models 跟着走
        v = d.get("installDir")
        if isinstance(v, str) and v.strip():
            found.append((None, v.strip()))
    return found


def _shared_model_base_paths() -> list:
    """从桌面版生成的 `shared_model_paths.yaml` 里抠出所有 `base_path`。

    ★ 为什么还要看这个 yaml：它就是**ComfyUI 启动时真正吃进去的那份**。
      实测 ComfyUI 的启动命令行里有
      `--extra-model-paths-config ...\\instance-model-paths\\inst-*.yaml`，
      那些 yaml 是这份的按实例副本；桌面版的 settings.json 和它一般一致，
      但万一不一致，**以 ComfyUI 真正读的那份为准**。
      而且桌面版支持"多个模型目录"，其它的 base_path 也会出现在这里。

    不装 PyYAML：这个文件的格式是桌面版自己生成的，很规整，一行一条
    `base_path: '...'`，用正则抠足够。装了 PyYAML 也不许 import ——
    用户机器上不一定有，**不能让我们多一个依赖**。
    """
    out = []
    for conf in _desktop_conf_dirs():
        path = os.path.join(conf, "shared_model_paths.yaml")
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except Exception:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith("base_path:"):
                continue
            v = s.split(":", 1)[1].strip().strip("'\"")
            # yaml 里 Windows 路径可能是 C:\x\y（单引号内不转义）或 C:\\x\\y
            v = v.replace("\\\\", "\\")
            if v:
                out.append(v)
    return out


def _desktop_install_roots() -> list:
    """从桌面版 installations.json 里找出每个实例装在哪个盘。

    装到 D 盘时（桌面版允许），附带的东西 —— 它自己的 `models` 兜底目录、
    `input` / `output` —— 会跟着那个 installPath 走，而不是 %LOCALAPPDATA%。
    调用方会在这些根目录下面找 models/input/output 子目录。
    """
    out = []
    for conf in _desktop_conf_dirs():
        try:
            with open(os.path.join(conf, "installations.json"),
                      encoding="utf-8-sig") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not isinstance(d, list):
            continue
        for inst in d:
            if not isinstance(inst, dict):
                continue
            p = inst.get("installPath")
            if isinstance(p, str) and p.strip():
                out.append(p.strip())
    return out


def _detect_comfy_dirs() -> dict:
    """按 ComfyUI Desktop 的常见布局找 input / output / models。

    找不到就返回 None，由 config.json 或调用方补上。
    只认这几个位置，不做全盘搜索 —— 扫盘又慢又可能读到自己没权限的目录。

    ★ 为什么还要去读 ComfyUI Desktop 自己的配置文件（下面 Settings.json 那段）：
      前面那几条路都是**猜目录在哪**，只在"用户把模型放在默认位置"时猜得中。
      桌面版允许把模型目录、输入目录、输出目录整个挪到别的盘（模型动辄 20 GB，
      换到 D 盘是常态），挪完以后它就写在 `%APPDATA%\\Comfy Desktop\\settings.json`
      的 `modelsDirs` / `inputDir` / `outputDir` 里 —— 那是**用户自己指定的答案**，
      比任何猜测都准，而且不用用户再教我们一遍。
      实测（一台把 models 放在 C 盘、output 放在 D:\\comfyout 的机器，见
      `dev/probe_paths_detect.py`）：只靠猜，output 会猜错、模型目录猜对；
      读了这个文件之后两个都对上了。
    """
    found = {"input": None, "output": None, "models": None}

    # ---- ① 按"谁说了算"排好序，一个一个问，先答的算数 --------------------
    #   顺序就是优先级，**从最明确到最靠猜**：
    #     1. COMFYUI_ROOT          —— 用户/README 专门为便携版设的开关，一句明确的指令
    #     2. settings.json         —— 用户在桌面版界面里选的（modelsDirs/inputDir/outputDir）
    #     3. shared_model_paths.yaml —— ComfyUI 启动时真正吃进去的那份
    #     4. installations.json    —— 桌面版装在别的盘时，附带目录跟着走
    #     5. %LOCALAPPDATA% 默认位置 —— 纯猜，最不靠谱，放最后
    #   ★ 实测踩过（tests/test_paths_detect.py 钉着）：我第一版把第 1 条排在第 3
    #     条后面，于是"用户明确指定的便携版目录"被 YAML 里读出来的值抢走了 ——
    #     **只把 roots 排序不够，三路答案必须合成同一条有序链**。
    # 1. 明确的开关：COMFYUI_ROOT —— **单独一批，第一个问，谁都不许抢**
    explicit = []
    for extra in (os.environ.get("COMFYUI_ROOT"),):
        if extra:
            explicit.append((None, extra))       # None = 去它下面找 models/input/output
    # 2. 桌面版 settings.json：key 已经说清"这是什么目录"，直接用
    # 3. yaml 里的 base_path 是模型根目录（ComfyUI 真正读的那份）
    # 4. installations.json：桌面版装在别的盘时附带目录跟着走
    # 5. %LOCALAPPDATA% 默认位置：纯猜，最不靠谱，放最后
    sources = list(_read_desktop_settings())
    for p in _shared_model_base_paths():
        sources.append(("models", p))
    for p in _desktop_install_roots():
        sources.append((None, p))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = os.path.join(local, "Comfy-Desktop")
        sources.append((None, os.path.join(base, "ComfyUI-Shared")))
        inst = os.path.join(base, "ComfyUI-Installs")
        if os.path.isdir(inst):
            for name in os.listdir(inst):
                sources.append((None, os.path.join(
                    inst, name, "ComfyUI", "ComfyUI")))

    # 两轮：先认"已说明这是什么目录"的，再拿剩下的当根目录试子目录。
    # 分两轮是因为同一路来源里可能混着两种东西，一次遍历分不清谁优先。
    # ★ COMFYUI_ROOT 自己走完整两轮，排在所有来源前面 —— 见上面那段注释。
    for batch in (explicit, sources):
        for key, p in batch:
            if key and os.path.isdir(p) and found[key] is None:
                found[key] = p
        for key, p in batch:
            if key is not None or not os.path.isdir(p):
                continue
            for k, sub in (("models", "models"), ("input", "input"),
                           ("output", "output")):
                cand = os.path.join(p, sub)
                if found[k] is None and os.path.isdir(cand):
                    found[k] = cand
    return found


_DET = _detect_comfy_dirs()


# 每个路径**是哪来的** —— 「配置文件 > 环境变量 > 自动探测 > 兜底」四层里命中的是哪层。
# 为什么要有这个东西：探测不到时会静默退回应用目录下的兜底（那个目录通常不存在），
# 用户只看到"路径不对"却不知道**是哪一层给的**，只能瞎试。
# check_env.py 的【2】路径配置 会把这张表打出来。
DIR_SOURCE = {}


def _pick(config_key: str, env_key: str, detected, fallback: str,
          what: str = "") -> str:
    if CONFIG.get(config_key):
        DIR_SOURCE[what] = "配置文件 config.json 里的 %s" % config_key
        return CONFIG[config_key]
    if os.environ.get(env_key):
        DIR_SOURCE[what] = "环境变量 %s" % env_key
        return os.environ[env_key]
    if detected:
        DIR_SOURCE[what] = "自动探测到的"
        return detected
    # 不写成"兜底（…）"：调用方已经会用括号把它括起来了，再带括号会变成
    # `（兜底（自动探测没找到…））` —— 用户在自检输出里看到的就是这坨。
    DIR_SOURCE[what] = "兜底：自动探测没找到，退回了软件自己的目录"
    return fallback


# ---------------------------------------------------------------- 2. ComfyUI
# 「本地服务」的地址。不是路径，但同属"每台机器不同"的东西，放一起便于排查。
COMFY_URL = CONFIG.get("comfy_url") or os.environ.get("COMFYUI_URL") \
    or "http://127.0.0.1:8188"
DIR_SOURCE["ComfyUI 地址"] = (
    "配置文件 config.json 里的 comfy_url" if CONFIG.get("comfy_url")
    else "环境变量 COMFYUI_URL" if os.environ.get("COMFYUI_URL")
    else "默认值（没配过；ComfyUI 装在别的端口就要改）")

COMFY_INPUT = _pick("comfy_input", "COMFYUI_INPUT", _DET["input"],
                    os.path.join(APP_DIR, "comfy_input"), "ComfyUI input 目录")
COMFY_OUTPUT = _pick("comfy_output", "COMFYUI_OUTPUT", _DET["output"],
                     os.path.join(APP_DIR, "output"), "输出目录")
MODELS_DIR = _pick("models_dir", "COMFYUI_MODELS", _DET["models"],
                   os.path.join(APP_DIR, "models"), "models 根目录")

CHECKPOINT = CONFIG.get("checkpoint") or FALLBACK_CHECKPOINT

# ---------------------------------------------------------------- 3. 用户数据
OUT_ANIME = os.path.join(COMFY_OUTPUT, "anime")
OUT_DEPTH = os.path.join(OUT_ANIME, "_depth")
RECYCLE_DIR = os.path.join(OUT_ANIME, "_recycle")


def _output_dir_has_shoots() -> bool:
    """产出目录里已经有出图吗？（日期文件夹 = 出过图）

    `_depth` / `_recycle` / `_depth_test` 不算 —— 它们是服务自己建的下属目录，
    首次启动就会出现，拿它们当"用过"的证据等于人人都算用过。

    ⚠️ 定义在这里（`OUT_ANIME` 下面）是**必须的**：它在 `note_config_gone_if_used()`
    里被调用，而那个函数在启动时才跑（那时整个模块已经加载完）。
    """
    try:
        for e in os.listdir(OUT_ANIME):
            if e not in ("_depth", "_recycle", "_depth_test") \
                    and os.path.isdir(os.path.join(OUT_ANIME, e)):
                return True
    except OSError:
        pass
    return False


def note_config_gone_if_used() -> str:
    """★ 启动流程最后调一次：config.json 不在，但**这台机器用过这个应用**吗？

    为什么必须单独一步、不能在 import 期判：

      · import 期能看到的物证只有 `_provenance.json` / `lora_aliases.json` /
        `custom_prompt_options.json` 三个文件 —— 都在模块前段，可以判；
      · "产出目录里已经有出图的日期文件夹"同样是最硬的证据，但它依赖
        `OUT_ANIME`，而 `OUT_ANIME` 排在文件后半段 —— import 期根本还不存在。

    所以这里把两条合起来，**在启动时**（整模块已加载完）做最终判定。
    返回那句提示（同时写进 `CONFIG_ERROR`），`None` = 什么都没发生。

    为什么是这个 `**` 加粗的底模名：用户必须一眼看出"我现在用的不是你挑的那个"。
    """
    global CONFIG_ERROR
    if not CONFIG_WAS_MISSING or CONFIG_ERROR:
        return None
    if not _config_was_deleted() and not _output_dir_has_shoots():
        return None                     # 真首次运行 —— 保持安静
    CONFIG_ERROR = (
        "config.json 不见了（%s），但这台机器已经用过这个应用（有出图记录或"
        "你配过的东西），所以这不是首次运行。"
        "现在底模退回写死的默认值 **%s**，你原来挑的那个没有生效，"
        "产出目录等设置也一并退回自动探测。"
        "修复：双击「安装模型.bat」会按 config.example.json 重建一份"
        "带说明的 config.json（再重挑一次底模）。" % (CONFIG_PATH, FALLBACK_CHECKPOINT))
    print("[warn] %s" % CONFIG_ERROR, file=sys.stderr)
    return CONFIG_ERROR

# ---------------------------------------------------------------- 4. 服务端口
# 端口以前写死在 main() 里，只能用 --port 覆盖。现在进配置：
# 留空 = 从 8765 开始往上找第一个空闲端口（别人机器上 8765 可能被占）。
DEFAULT_PORT = 8765
try:
    PORT = int(CONFIG.get("port") or 0) or DEFAULT_PORT
except (TypeError, ValueError):
    PORT = DEFAULT_PORT
