# -*- coding: utf-8 -*-
"""从成品图读回生成参数 —— 纯函数层测试（不需要 ComfyUI 在线）。

为什么值得单测：这条链路的失败是**静默**的。读不到元数据只会让用户以为
"这张图没有参数"，而真正的 bug（比如把正负向读反、把 LoRA 权重读成 None、
A1111 的 Sampler 行解析错）不会报任何错。所以这里用真实的 ComfyUI 图 +
构造的 A1111 文本 + 故意做坏的图，三种都钉住。
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from server import (iter_output_images, parse_a1111_parameters,  # noqa: E402
                    parse_comfy_meta, read_image_meta)

fails = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------- 1. 真图
OUT = paths.OUT_ANIME
# ★ 必须用应用自己的枚举函数，不能用裸 glob。
#   裸 glob(os.path.join(OUT, "**", "*.png")) 会**连回收站一起扫**，于是测试
#   可能挑中一张已经被"删除"的图（收进 _recycle/ 的），再断言它的负向词和尺寸。
#   实测就是这样红的：它挑中了一张回收站里的局部重绘图。
#   iter_output_images 会把 `_` 开头的目录整体跳过 —— 那正是"给用户看的产出"
#   的定义，也让测试和界面看到的是同一批图。
real = [p for p, _f in iter_output_images(OUT)]
real.sort(key=os.path.getmtime)
print("[1] 本机产出的真实 PNG")
check(bool(real), "产出目录里有 PNG 可测（%d 张）" % len(real))
check(all("_recycle" not in p and "_depth" not in p for p in real),
      "列出来的图里不含回收站/深度图（和界面看到的一致）")


def newest_with_prompt(paths, limit=40):
    """找最近一张**文生图**，用来验证读回完整参数。

    为什么优先挑 `ui_` 开头的：那是 `/api/generate` 的产出，带完整的
    CheckpointLoader + KSampler 工作流。

    ★ 原来只要求"有提示词 + 有 steps"，于是**局部重绘的结果也会被选中** ——
      它确实有提示词（修图用的那句）和 steps，但：
        · 负向词是修图那句，不一定含 lowres/quality → 断言无辜变红
        · 尺寸来自被修的原图，工作流里没有 EmptyLatentImage → 读到 0×0
      实测就是这样红的。选图条件是**测试的输入**，它错了后面全错。
    """
    ui = [p for p in paths if os.path.basename(p).startswith("ui_")]
    for pool in (ui[-limit:], paths[-limit:]):
        for p in reversed(pool):
            try:
                with open(p, "rb") as fh:
                    m = read_image_meta(fh.read())
            except Exception:
                continue
            if m["positive"] and m["sampler"].get("steps"):
                return p, m
    return None, None


path1, m = newest_with_prompt(real)
check(path1 is not None, "在最近 40 张里找到一张带提示词的图")
if path1:
    blob = open(path1, "rb").read()
    print("       %s" % os.path.basename(path1))
    print("       kind=%s 正向 %d 字 / 负向 %d 字 / LoRA %d 个"
          % (m["kind"], len(m["positive"]), len(m["negative"]), len(m["loras"])))
    check(m["kind"] == "comfy", "判为 ComfyUI 格式")
    check(len(m["positive"]) > 10, "读到了正向提示词")
    check(m["positive"] != m["negative"], "正负向没有读反")
    check("lowres" in m["negative"] or "quality" in m["negative"],
          "负向词里有常见的质量排除词")
    check(m["sampler"]["steps"] and m["sampler"]["cfg"],
          "读到了 steps / cfg：%s / %s"
          % (m["sampler"]["steps"], m["sampler"]["cfg"]))
    check(m["size"][0] > 0 and m["size"][1] > 0,
          "读到了尺寸 %s×%s" % tuple(m["size"]))
    check(len(blob) > 1000, "原图读得进来")

# ---------------------------------------------------------------- 2. 手工图
print()
print("[2] 手工构造的 ComfyUI 图（正负向故意反着连，验证是照连线读的）")
GRAPH = {
    "1": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "base.safetensors"}},
    "2": {"class_type": "LoraLoader",
          "inputs": {"model": ["1", 0], "clip": ["1", 1],
                     "lora_name": "a.safetensors",
                     "strength_model": 0.85, "strength_clip": 0.6}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "NEGATIVE HERE"}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "POSITIVE HERE"}},
    # ★ 真实工作流里采样器两头连的是 ControlNet 节点，文本在它再上一层。
    #   第一版只往回走一层，于是真图读出来正负向全空 —— 这里钉住这个回归。
    "7": {"class_type": "ControlNetApplyAdvanced",
          "inputs": {"positive": ["4", 0], "negative": ["3", 0],
                     "strength": 0.8, "start_percent": 0.0, "end_percent": 0.8}},
    "8": {"class_type": "SetUnionControlNetType", "inputs": {"type": "depth"}},
    "5": {"class_type": "EmptyLatentImage",
          "inputs": {"width": 896, "height": 1152, "batch_size": 2}},
    "6": {"class_type": "KSampler",
          "inputs": {"model": ["2", 0], "positive": ["7", 0], "negative": ["7", 1],
                     "seed": 42, "steps": 28, "cfg": 5.5,
                     "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
}
import json  # noqa: E402
m = parse_comfy_meta(json.dumps(GRAPH))
check(m["positive"] == "POSITIVE HERE", "正向穿过 ControlNet 节点读到（不是节点编号顺序）")
check(m["negative"] == "NEGATIVE HERE", "负向穿过 ControlNet 节点读到")
check(m["checkpoint"] == "base.safetensors", "沿连线走到了底模")
check(len(m["loras"]) == 1 and m["loras"][0]["name"] == "a.safetensors",
      "LoRA 从 model 连线取到")
check(m["loras"][0]["model"] == 0.85 and m["loras"][0]["clip"] == 0.6,
      "LoRA 的 model/clip 双权重都保留")
check(m["sampler"]["seed"] == 42 and m["sampler"]["steps"] == 28,
      "seed / steps 读对")
check(m["size"] == [896, 1152] and m["batch"] == 2, "尺寸与张数读对")
check(len(m["controlnet"]) == 1 and m["controlnet"][0]["strength"] == 0.8,
      "ControlNet 强度读对：%s" % (m["controlnet"],))

print()
print("[2b] 没接上的 LoRA 不该被算进去")
g2 = dict(GRAPH)
g2["9"] = {"class_type": "LoraLoader",
           "inputs": {"model": ["1", 0], "lora_name": "orphan.safetensors",
                      "strength_model": 1.0, "strength_clip": 1.0}}
m2 = parse_comfy_meta(json.dumps(g2))
check([x["name"] for x in m2["loras"]] == ["a.safetensors"],
      "游离的 LoRA 节点被忽略（只认连线上的）")

# ---------------------------------------------------------------- 3. A1111
print()
print("[3] A1111 / Forge 的 parameters 文本块")
A1111 = ("1girl, solo, torii\n"
         "Negative prompt: lowres, bad anatomy, watermark\n"
         "Steps: 30, Sampler: DPM++ 2M Karras, CFG scale: 5.5, Seed: 123456, "
         'Size: 1344x768, Model hash: abcd1234, Model: someModel, '
         'Denoising strength: 0.55, Clip skip: 2')
a = parse_a1111_parameters(A1111)
check(a["positive"] == "1girl, solo, torii", "正向切到 Negative prompt 之前")
check(a["negative"] == "lowres, bad anatomy, watermark", "负向切到 Steps 之前")
check(a["sampler"]["steps"] == 30 and a["sampler"]["seed"] == 123456,
      "steps / seed 解析对")
check(a["sampler"]["cfg"] == 5.5, "cfg scale 解析对")
# ★ 回归：A1111 的采样器名和 ComfyUI **不是同一套**。"DPM++ 2M Karras" 直接写进
#   工作流会被 ComfyUI 拒掉（实测报错 sampler_name: Value not in list），
#   必须翻译成 dpmpp_2m + 调度器 karras。
check(a["sampler"]["sampler_name"] == "dpmpp_2m"
      and a["sampler"]["scheduler"] == "karras",
      "采样器名翻译成 ComfyUI 的叫法，Karras 拆成调度器")
check(a["size"] == [1344, 768], "Size 解析对")
check(a["checkpoint"] == "someModel", "Model 当作底模")

print()
print("[3b] A1111 里 ComfyUI 没有的采样器：留空，不猜相近的顶上")
a2 = parse_a1111_parameters("1girl\nSteps: 20, Sampler: DPM++ 2M SDE Heun Karras, "
                            "CFG scale: 7, Seed: 1, Size: 512x512")
check(a2["sampler"]["sampler_name"] == "dpmpp_2m_sde_heun",
      "dpm++ 2m sde heun 有对应项：%r" % a2["sampler"]["sampler_name"])
a3 = parse_a1111_parameters("1girl\nSteps: 20, Sampler: Restart, "
                            "CFG scale: 7, Seed: 1, Size: 512x512")
check(a3["sampler"]["sampler_name"] == "restart", "restart 原样保留")
a4 = parse_a1111_parameters("1girl\nSteps: 20, Sampler: SomeFutureSampler, "
                            "CFG scale: 7, Seed: 1, Size: 512x512")
check(a4["sampler"]["sampler_name"] == "",
      "认不出的采样器留空（不是随手挑一个）：%r" % a4["sampler"]["sampler_name"])
check(a4["sampler"]["steps"] == 20, "留空不影响其余字段照读")

print()
print("[3c] value_not_in_list 的报错要说清「收到什么、可选什么」")
from server import humanize_comfy_error  # noqa: E402
detail = json.dumps({"node_errors": {"12": {"class_type": "KSampler", "errors": [
    {"type": "value_not_in_list", "message": "Value not in list",
     "extra_info": {"input_name": "sampler_name", "received_value": "dpm++_2m",
                    "input_config": ["COMBO", {"options": ["euler", "dpmpp_2m",
                                                           "ddim"]}]}}]}}})
msg = humanize_comfy_error(detail)
check("dpm++_2m" in msg, "报错里带上了实际收到的值：%s" % msg)
check("dpmpp_2m" in msg, "报错里列出了可选值")
check("Value not in list" != msg.strip()[-len("Value not in list"):],
      "不是只回一句原样英文")

# ---------------------------------------------------------------- 4. 坏输入
print()
print("[4] 读不出参数时要给人话，不能崩")
try:
    read_image_meta(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    check(False, "坏图应该抛错")
except ValueError as e:
    check("没有生成参数" in str(e), "坏图给出可读提示：%s" % str(e)[:40])
except Exception as e:
    check(False, "坏图抛了意料外的异常：%s: %s" % (type(e).__name__, e))

print()
print("[5] 放大图：带着工作流但没有提示词，要说清楚该拿哪张图")
UPS = [p for p, f in iter_output_images(OUT) if f.startswith("up_")]
UPS.sort(key=os.path.getmtime)
if UPS:
    try:
        read_image_meta(open(UPS[-1], "rb").read())
        check(False, "放大图应该抛出「没有提示词」而不是给空结果")
    except ValueError as e:
        check("放大 / 重绘" in str(e) and "原图" in str(e),
              "提示指向原图：%s" % str(e)[:60])
    except Exception as e:
        check(False, "放大图抛了意料外的异常：%s: %s" % (type(e).__name__, e))
else:
    print("   [跳过] 产出目录里没有 up_*.png")

print()
print("[6] 尺寸兜底：重绘图的尺寸要能从图片本身读出来")
# 局部重绘的工作流里**没有 EmptyLatentImage**（尺寸来自被修的那张原图），
# 所以只从工作流找尺寸的话会读到 [0, 0] —— 而图本身明明是 640×640。
# 用户把重绘结果拖回界面就会看到"尺寸 0×0"。
INPS = [p for p, f in iter_output_images(OUT) if f.startswith("inp_")]
INPS.sort(key=os.path.getmtime)
if INPS:
    _p = INPS[-1]
    try:
        _m = read_image_meta(open(_p, "rb").read())
        try:
            from PIL import Image as _I
            with _I.open(_p) as _im:
                _real = [_im.width, _im.height]
        except ImportError:
            _real = None
        if _real:
            check(list(_m["size"]) == _real,
                  "重绘图读到的尺寸 %s == 图片实际像素 %s"
                  % (_m["size"], _real))
        else:
            check(_m["size"][0] > 0 and _m["size"][1] > 0,
                  "重绘图读到了非零尺寸 %s" % _m["size"])
    except ValueError as e:
        # 重绘图在没有提示词那一步被拦下也算正常（取决于它是哪种产物）
        print("   [跳过] 这张重绘图没有提示词：%s" % str(e)[:50])
    except Exception as e:
        check(False, "重绘图读尺寸抛了意料外的异常：%s: %s" % (type(e).__name__, e))
else:
    print("   [跳过] 产出目录里没有 inp_*.png")

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
