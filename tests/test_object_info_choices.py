# -*- coding: utf-8 -*-
"""object_info_choices 必须同时认 ComfyUI 的**新旧两种** COMBO 格式。

为什么专门测：旧格式 `[[...], {...}]` 和新格式 `["COMBO", {"options": [...]}]`
只认一种时，另一种会**静默返回垃圾** —— 新格式按旧格式取 spec[0] 得到的是
字符串 "COMBO"，list() 一拆就是 ['C','O','M','B','O']。一个看着像列表、其实是
五个字母的返回值，不报错，只让界面上出现"采样器 C 不是这台 ComfyUI 支持的"。

实测这台机器（ComfyUI 0.35.1）两种混用：KSampler/LoraLoader/CheckpointLoaderSimple
还是旧格式，UpscaleModelLoader 已经是新格式。

纯函数测试，不需要 ComfyUI 在线（comfy_get 被替换掉）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import server  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


FAKE = {
    "UpscaleModelLoader": {
        "input": {"required": {"model_name":
                 ["COMBO", {"multiselect": False,
                            "options": ["b.pth", "a.pth", "c.pth"]}]}}},
    "LoraLoader": {
        "input": {"required": {"lora_name":
                 [["x.safetensors", "y.safetensors"], {"tooltip": "..."}]}}},
    "EmptyNode": {"input": {"required": {"foo": ["COMBO", {}]}}},
}
server.comfy_get = lambda path, timeout=60: FAKE

print("=" * 70)
print("【1】新格式 [" + '"COMBO", {"options": [...]}]')
got = server.object_info_choices("UpscaleModelLoader", "model_name")
check(got == ["b.pth", "a.pth", "c.pth"], "拿到真实选项：%s" % got)
check("COMBO" not in "".join(got) or got == [], "没有把 \"COMBO\" 拆成字母")

print()
print("【2】旧格式 [[...], {...}] 还得照旧能用")
got = server.object_info_choices("LoraLoader", "lora_name")
check(got == ["x.safetensors", "y.safetensors"], "拿到真实选项：%s" % got)

print()
print("【3】新格式但没有 options —— 返回空列表，不是 ['C','O','M','B','O']")
got = server.object_info_choices("EmptyNode", "foo")
check(got == [], "返回空列表：%r" % got)

print()
print("【4】节点不存在时走 default")
check(server.object_info_choices("Nope", "x") == [], "返回 []")
check(server.object_info_choices("Nope", "x", ["d"]) == ["d"], "返回传入的默认值")

print()
print("【5】返回值里不允许出现单字符（那就是格式认错了的铁证）")
for node, field in (("UpscaleModelLoader", "model_name"),
                    ("LoraLoader", "lora_name")):
    got = server.object_info_choices(node, field)
    check(not (got and all(len(x) == 1 for x in got)),
          "%s.%s 不是一串单字符：%s" % (node, field, got))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
