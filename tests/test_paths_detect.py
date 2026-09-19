# -*- coding: utf-8 -*-
"""验 `_detect_comfy_dirs()`：桌面版把模型/输入/输出搬到别的盘时还找不找得到。

之前它只会在 `%LOCALAPPDATA%\\Comfy-Desktop\\...` 底下**猜**。
桌面版把模型目录挪到 D 盘（模型 20 GB，这是常态）之后，那个地方就没有 models，
于是它一路退回"软件自己的 models 文件夹"—— 那个文件夹**根本不存在**，
用户看到的就是"探测不到、让你手填配置"。

修法是去读桌面版**自己写下的答案**：`%APPDATA%\\Comfy Desktop\\settings.json`。

这里全程用临时目录 + 临时 APPDATA，不碰真实配置。
"""
import json
import os
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK = []
BAD = []


def chk(cond, label, extra=""):
    (OK if cond else BAD).append(label)
    print("  [%s] %s %s" % ("OK" if cond else "!!", label, extra))


def load_paths(env):
    """在指定环境变量下，全新 import 一次 paths，拿到它的探测结果。"""
    saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "APPDATA", "COMFYUI_ROOT")}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        for m in [m for m in list(sys.modules) if m == "paths"]:
            del sys.modules[m]
        import paths
        return paths._DET, paths
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.modules.pop("paths", None)


print("=" * 78)
print("自动探测：桌面版把目录搬到别的盘时还找不找得到")
print("=" * 78)

root = tempfile.mkdtemp(prefix="snakeer_detect_")
try:
    # --- 场景：桌面版装在别的盘，模型在 D 盘、输出在另一个地方 -----------
    local = os.path.join(root, "AppData", "Local")     # 假 LOCALAPPDATA
    roaming = os.path.join(root, "AppData", "Roaming")  # 假 APPDATA
    os.makedirs(local)
    real_models = os.path.join(root, "D_Comfy-Desktop", "ComfyUI-Shared", "models")
    real_input = os.path.join(root, "D_Comfy-Desktop", "ComfyUI-Shared", "input")
    real_output = os.path.join(root, "D_comfyout")
    for d in (real_models, real_input, real_output):
        os.makedirs(d)

    conf = os.path.join(roaming, "Comfy Desktop")
    os.makedirs(conf)
    with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"modelsDirs": [real_models], "inputDir": real_input,
                   "outputDir": real_output,
                   "installDir": os.path.join(root, "D_Comfy-Desktop",
                                              "ComfyUI-Installs")}, fh)
    with open(os.path.join(conf, "shared_model_paths.yaml"), "w",
              encoding="utf-8") as fh:
        fh.write("# generated\ncomfy.desktop_0:\n  base_path: '%s'\n  is_default: true\n"
                 % real_models.replace("\\", "\\\\"))

    det, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                         "COMFYUI_ROOT": None})
    print()
    print("  桌面版配置里写的  models=%s" % real_models)
    print("                    input =%s" % real_input)
    print("                    output=%s" % real_output)
    print("  探测到的          models=%s" % det["models"])
    print("                    input =%s" % det["input"])
    print("                    output=%s" % det["output"])
    chk(det["models"] == real_models, "模型目录：认桌面版写下的答案（不是靠猜）")
    chk(det["input"] == real_input, "输入目录：认桌面版写下的答案")
    chk(det["output"] == real_output, "输出目录：认桌面版写下的答案")

    # --- 反例 1：没有 settings.json 时必须还能按老办法猜 -----------------
    os.remove(os.path.join(conf, "settings.json"))
    os.remove(os.path.join(conf, "shared_model_paths.yaml"))
    guess_shared = os.path.join(local, "Comfy-Desktop", "ComfyUI-Shared")
    for sub in ("models", "input", "output"):
        os.makedirs(os.path.join(guess_shared, sub))
    det2, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": None})
    chk(det2["models"] == os.path.join(guess_shared, "models"),
        "没有 settings.json 时，仍按默认位置猜得到（不回归）")
    chk(det2["output"] == os.path.join(guess_shared, "output"),
        "没有 settings.json 时，输出目录也猜得到")

    # --- 反例 2：配置写坏了，不许出声、不许崩 -----------------------------
    with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as fh:
        fh.write('{"modelsDirs": [这就是坏 JSON,,,}')
    det3, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": None})
    chk(det3["models"] == os.path.join(guess_shared, "models"),
        "settings.json 是坏 JSON 时静默跳过、退回猜（不崩、不报错）")

    # --- 反例 3：配置里指的目录不存在，不许拿它当真 -----------------------
    with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"modelsDirs": [os.path.join(root, "根本不存在的目录")],
                   "outputDir": os.path.join(root, "也不存在")}, fh)
    det4, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": None})
    chk(det4["models"] == os.path.join(guess_shared, "models"),
        "配置里指的目录不存在时，退回猜（不把一个不存在的路径当成答案）")
    chk(det4["output"] == os.path.join(guess_shared, "output"),
        "同上：输出目录也退回猜")

    # --- 反例 4：settings.json 最外层是数组也不行 ------------------------
    with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as fh:
        fh.write('["不是对象"]')
    det5, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": None})
    chk(det5["models"] == os.path.join(guess_shared, "models"),
        "settings.json 最外层不是对象时静默跳过")

    # --- 反例 5：COMFYUI_ROOT 仍然最高优先（便携版的活路）----------------
    #   ★ 这条测试第一版是错的：我断言 COMFYUI_ROOT 应该赢，但当时代码把
    #     `installPath + \ComfyUI` 这个**猜测出来的**根目录排在了它前面，
    #     而那个猜测出来的目录里恰好有个 models 兜底文件夹 —— 于是"明确指示"
    #     输给了"猜测"。测试写对之后才暴露出来：**优先级顺序本身就是个 bug**。
    portable = os.path.join(root, "便携版ComfyUI")
    for sub in ("models", "input", "output"):
        os.makedirs(os.path.join(portable, sub))
    # 造一个"桌面版装在别的盘"的假实例，它自己也有 models 兜底目录
    inst_path = os.path.join(root, "D_Comfy-Desktop", "ComfyUI-Installs", "ComfyUI")
    os.makedirs(os.path.join(inst_path, "models"))
    with open(os.path.join(conf, "installations.json"), "w", encoding="utf-8") as fh:
        json.dump([{"id": "inst-x", "name": "ComfyUI", "installPath": inst_path}], fh)
    with open(os.path.join(conf, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"modelsDirs": [real_models]}, fh)
    det6, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": portable})
    chk(det6["models"] == os.path.join(portable, "models"),
        "COMFYUI_ROOT（便携版）压过桌面版的 installPath 和 settings")

    # --- 反例 6：桌面版装在别的盘，它自己的 models 兜底目录要被找到 --------
    det7, _ = load_paths({"LOCALAPPDATA": local, "APPDATA": roaming,
                          "COMFYUI_ROOT": None})
    chk(det7["models"] == real_models,
        "桌面版装在别的盘时，仍优先用 settings 里写的模型目录")
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
print("=" * 78)
print("通过 %d，失败 %d" % (len(OK), len(BAD)))
for b in BAD:
    print("  失败：%s" % b)
print("RESULT: %s" % ("ALL PASS" if not BAD else "FAIL"))
print("=" * 78)
sys.exit(1 if BAD else 0)
