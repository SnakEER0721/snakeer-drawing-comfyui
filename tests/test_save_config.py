# -*- coding: utf-8 -*-
"""验证 paths.save_config：只改指定键、保留换行风格、原子写、坏了能发现。

★ 这个测试**会在临时目录里造一份 config.json 来测**，绝不碰用户真实的
  D:\\dsh\\webapp\\config.json —— 那个文件被写坏就等于应用启动不了。
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


real = paths.CONFIG_PATH
real_before = io.open(real, encoding="utf-8", newline="").read() \
    if os.path.isfile(real) else None

tmpdir = tempfile.mkdtemp(prefix="dsh_cfg_")
try:
    fake = os.path.join(tmpdir, "config.json")
    # 故意写成 CRLF + 无结尾换行，和真实文件一样
    original = ('{\r\n  "_说明": [\r\n    "别删我"\r\n  ],\r\n'
                '  "checkpoint": "old.safetensors",\r\n'
                '  "comfy_url": "http://127.0.0.1:8188"\r\n}')
    io.open(fake, "w", encoding="utf-8", newline="").write(original)

    # 把 paths 指向临时文件
    paths.CONFIG_PATH = fake
    paths.CONFIG = json.loads(original)

    print("=" * 74)
    print("【1】只改指定键，别的原样")
    out = paths.save_config({"checkpoint": "new.safetensors"})
    check(out["checkpoint"] == "new.safetensors", "checkpoint 已改：%s" % out["checkpoint"])
    check(out["comfy_url"] == "http://127.0.0.1:8188", "comfy_url 没被动")
    check(out["_说明"] == ["别删我"], "用户的 _说明 保住了")

    print()
    print("【2】保留 CRLF 换行风格（不然整个文件在 diff 里全变）")
    after = io.open(fake, encoding="utf-8", newline="").read()
    check("\r\n" in after, "写回后仍是 CRLF")
    check(after.count("\n") == after.count("\r\n"),
          "没有混进单个 LF（CRLF %d 行 / LF %d）"
          % (after.count("\r\n"), after.count("\n")))
    check(json.loads(after)["checkpoint"] == "new.safetensors", "文件内容正确")

    print()
    print("【3】原子写：不能留下 .tmp 残件")
    check(not os.path.exists(fake + ".tmp"), "没有残留 .tmp")

    print()
    print("【4】原文件不存在时能创建")
    paths.CONFIG_PATH = os.path.join(tmpdir, "fresh.json")
    paths.CONFIG = {}
    paths.save_config({"checkpoint": "x.safetensors"})
    check(os.path.isfile(paths.CONFIG_PATH), "新文件写出来了")
    check(json.loads(io.open(paths.CONFIG_PATH, encoding="utf-8").read())
          ["checkpoint"] == "x.safetensors", "内容正确")

    print()
    print("【5】写回后 paths.CONFIG 要跟着更新（不然下次读的是旧的）")
    check(paths.CONFIG.get("checkpoint") == "x.safetensors",
          "内存里的 CONFIG 也更新了：%r" % paths.CONFIG.get("checkpoint"))
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
    paths.CONFIG_PATH = real

print()
print("【6】用户真实的 config.json 一个字节都没动")
now = io.open(real, encoding="utf-8", newline="").read() if os.path.isfile(real) else None
check(now == real_before, "真实 config.json 未被修改")

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
