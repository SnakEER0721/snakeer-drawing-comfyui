# -*- coding: utf-8 -*-
"""config.json 坏掉的时候，会发生什么？

背景（实测，见 dev/probe_broken_config.py）
==========================================
config.json 是用户**一定会改**的文件（README 让他在里面填产出目录、comfy_url）。
而手改 JSON 有三种常见的坏法，实测结果：

| 情况 | 修之前 | 后果 |
|---|---|---|
| 带 BOM（编辑器写的 UTF-8） | 整份读不出来 | 静默回退成"自动探测" |
| 多一个逗号 | 整份读不出来 | 同上 |
| 最外层写成 `[...]` | **完全不出声** | 同上 |

共同点：**用户填的 comfy_output 不见了，图跑到别处去，而界面上看不出任何异常。**
这正是这个项目最忌讳的「静默失败」。修完之后：

  · 带 BOM 的**能读了**（读文件用 utf-8-sig，不带 BOM 也一样正常）
  · 真的坏的：`paths.CONFIG_ERROR` 记下一句"给用户看的话"
  · 那句话一路走到 **check_env.py**（自检报红）和 **界面上**（页头红标签 +
    状态栏），不再只躺在黑窗口的一行 [warn] 里

这个测试钉四件事：
  【1】读 config.json 的六种情况（**全程在临时文件上，绝不碰真实 config.json**）
  【2】那句话得说清「哪份文件、怎么了、怎么修、后果是什么」
  【3】真的会走到界面上（真调一次 capabilities()）
  【4】静态：读文件仍然用 utf-8-sig，check_env 和界面都接了这个信号
"""
import os
import re
import sys
import tempfile

# ★ 必须在 import paths / server **之前**（见 AGENTS.md）
sys.dont_write_bytecode = True

import io  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
import server  # noqa: E402

fails = []
# ★ comfy_output 用**相对名**，不用 D:\... 那种盘符路径。
#   原因：test_path_hygiene.py 会扫全仓"写死的绝对路径"，它分不清"夹具值"和
#   "真的写死本机路径"（实测：第一版用了 D:\out，直接被判 5 处硬编码）。
#   这个测试只需要一个能原样往返的值，用相对名一样有效。
GOOD = {"comfy_url": "http://127.0.0.1:8188", "comfy_output": "some-out-dir",
        "checkpoint": "a.safetensors", "port": 8765}
ZH_OUT = "出图目录"
REAL = paths.CONFIG_PATH
TMP = tempfile.mkdtemp(prefix="test_cfg_")
# server.py 里那句 `from paths import CONFIG_ERROR` 是**导入时的快照**
# （字符串是不可变的，之后不会跟着变）。所以这里比的是"paths 在 import 那一刻
# 的值"，不是断言两者永远同一 —— 那才是真实的契约。
PATHS_ERR_AT_IMPORT = paths.CONFIG_ERROR


def check(name, cond, extra=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          "" if cond else "   " + str(extra)))
    if not cond:
        fails.append(name)


def load(text, encoding="utf-8", write=True):
    """把 text 写进临时 config.json，读一次，返回 (config, CONFIG_ERROR)。

    write=False 表示**不创建文件**（测"文件不存在"那一支）。
    """
    p = os.path.join(TMP, "config.json")
    if os.path.exists(p):
        os.remove(p)
    if write:
        io.open(p, "w", encoding=encoding, newline="").write(text)
    paths.CONFIG_PATH = p
    paths.CONFIG_ERROR = None
    try:
        cfg = paths._load_config()
        return cfg, paths.CONFIG_ERROR
    finally:
        paths.CONFIG_PATH = REAL
        # ★ 必须一起还原：不还原的话它会污染后面那些检查
        #   （第一版就漏了，`server.CONFIG_ERROR is paths.CONFIG_ERROR` 因此报红）
        paths.CONFIG_ERROR = PATHS_ERR_AT_IMPORT


real_hash_before = open(REAL, "rb").read() if os.path.isfile(REAL) else None

try:
    # ------------------------------------------------------------ 【1】
    print()
    print("【1】读 config.json 的六种情况")
    import json

    cfg, err = load(json.dumps(GOOD, ensure_ascii=False, indent=2))
    check("正常文件：读到 %d 条" % len(cfg), len(cfg) == 4 and err is None,
          (len(cfg), err))
    check("正常文件：comfy_output 拿到了",
          cfg.get("comfy_output") == "some-out-dir")

    cfg, err = load("\ufeff" + json.dumps(GOOD, ensure_ascii=False))
    check("带 BOM：**能读**（修之前整份读不出来）",
          len(cfg) == 4 and cfg.get("comfy_output") == "some-out-dir",
          (len(cfg), err))
    check("带 BOM：不算错误，不报 CONFIG_ERROR", err is None, err)

    cfg, err = load(json.dumps(GOOD, indent=2).replace('"port": 8765',
                                                       '"port": 8765,'))
    check("多一个逗号：配置为空", cfg == {}, cfg)
    check("多一个逗号：记下了 CONFIG_ERROR（不再静默）", bool(err), err)

    cfg, err = load("[1, 2, 3]")
    check("最外层是数组：配置为空", cfg == {}, cfg)
    check("最外层是数组：**也**记下 CONFIG_ERROR（修之前完全不出声）",
          bool(err), err)

    cfg, err = load("", write=False)
    check("文件不存在（首次运行）：返回空、且**不该**报错",
          cfg == {} and err is None, (cfg, err))

    # ------------------------------------------------------------ 【2】
    print()
    print("【2】那句话得说清「哪份文件、怎么了、后果、怎么修」")
    _cfg, err = load(json.dumps(GOOD, indent=2).replace('"port": 8765',
                                                        '"port": 8765,'))
    print("     实际消息：%s" % err)
    check("说出了是哪份文件（带路径）", "config.json" in err and TMP in err, err)
    check("说出了后果：填的路径没生效", "没有生效" in err, err)
    check("给了改法：删掉重跑 安装模型.bat", "安装模型.bat" in err, err)
    check("不是 Python 原文照抄（有中文解释）",
          "Expecting" not in err.split("——")[0], err[:60])

    _cfg, err2 = load("[1, 2, 3]")
    print("     数组那条：%s" % err2)
    check("数组那条说清了「最外层应该是 { }」", "{ }" in err2, err2)

    # 编码不是 UTF-8（中文 Windows 记事本存成 ANSI/GBK）
    # ★ 内容里必须**有中文**，否则 GBK 和 UTF-8 的字节完全一样，这个用例就是空的
    #   —— 第一版就是这么写的，测出来是 None、白过一场（这条断言自己抓到了）。
    zh = dict(GOOD, comfy_output=ZH_OUT)
    _cfg, err3 = load(json.dumps(zh, ensure_ascii=False), encoding="gbk")
    print("     GBK 那条：%s" % str(err3)[:90])
    check("GBK/ANSI 存的：提示改编码，而不是只报一串 UnicodeDecodeError",
          bool(err3) and ("UTF-8" in err3 or "utf-8" in err3.lower()), err3)

    # ------------------------------------------------------------ 【3】
    print()
    print("【3】真的会走到界面上")
    # ★ 这里原来断言的是"server 拿到的是 paths 在 import 时的那个值"——
    #   那其实**是在钉一个 bug**：import 快照在启动后永远是旧的，而
    #   "config.json 不见了"这条判据要到**启动时**才算得出来（它要看产出目录
    #   里有没有出图记录），算出来写回的是 `paths.CONFIG_ERROR`。接口读快照的话，
    #   界面上什么都不显示 —— 而这正是最该显示的那一种。
    #   现在钉的是新的（也是唯一对的）行为：**改 paths 里的值，必须能到接口上**；
    #   改那个快照名字，**必须没有任何作用**。
    check("server 仍然 import 了那个值（旧引用还在，但接口不再依赖它）",
          server.CONFIG_ERROR == PATHS_ERR_AT_IMPORT,
          (server.CONFIG_ERROR, PATHS_ERR_AT_IMPORT))
    dead = server.COMFY
    server.COMFY = "http://127.0.0.1:9"     # 死端口：capabilities 里那些探测会快速失败
    paths.CONFIG_ERROR = "config.json 读不出来（举例）"
    try:
        c = server.capabilities()
        cap = c.get("config_error")
    finally:
        server.COMFY = dead
        paths.CONFIG_ERROR = PATHS_ERR_AT_IMPORT
    check("capabilities() 把 config_error 带给前端",
          cap == "config.json 读不出来（举例）", cap)

    # 反向：只改快照、不改 paths → 接口**不该**跟着变。
    # 没有这条，上面那条在"两种读法都通"的实现下也会绿，就白测了。
    server.CONFIG_ERROR = "只改了快照，不该出现"
    try:
        c2 = server.capabilities()
        cap2 = c2.get("config_error")
    finally:
        server.CONFIG_ERROR = PATHS_ERR_AT_IMPORT
    check("只改 import 快照不起作用（接口读的是 paths 的实时值）",
          cap2 != "只改了快照，不该出现", cap2)
    check("真实 config.json 正常时它是 None（这样界面才不会无缘无故报红）",
          PATHS_ERR_AT_IMPORT is None, PATHS_ERR_AT_IMPORT)

    ui = io.open(os.path.join(paths.APP_DIR, "ui.html"), encoding="utf-8").read()
    check("ui.html 读了 caps.config_error", "caps.config_error" in ui)
    check("ui.html 用它加了红标签 .pill.err", 'className = "pill err"' in ui)
    check("ui.html 把原因写进了状态栏",
          re.search(r'setStatus\("config\.json 没生效', ui) is not None)
    check("ui.html 有 .pill.err 的样式", ".pill.err{" in ui)

    cen = io.open(os.path.join(paths.APP_DIR, "check_env.py"),
                  encoding="utf-8").read()
    check("check_env.py 也读 utf-8-sig（和应用一致，不会一边说好一边说坏）",
          'open(cfg_path, encoding="utf-8-sig")' in cen)
    check("check_env.py 接住了 S.CONFIG_ERROR（非对象的坏法也能报出来）",
          "S.CONFIG_ERROR" in cen)

    # ------------------------------------------------------------ 【4】
    print()
    print("【4】静态：读 config.json 必须仍然用 utf-8-sig")
    src = open(paths.SERVER_PY.replace("server.py", "paths.py"),
               encoding="utf-8").read()
    m = re.search(r"def _load_config.*?(?=\nCONFIG = )", src, re.S)
    check("_load_config 用的是 utf-8-sig（防有人改回 utf-8）",
          m is not None and 'encoding="utf-8-sig"' in m.group(0),
          "没找到，或不是 utf-8-sig")

    # ------------------------------------------------------------ 【5】
    print()
    print("【5】坏掉的 config.json 被覆盖前，必须先备份")
    # 真实路径：用户 config.json 里有个逗号写错了，他改天在界面上换底模 ——
    # 换底模会 save_config，而这时 CONFIG 是空的（读失败过），写回就只剩一个键，
    # 他手写的内容**连同那个错一起没了**。所以覆盖前必须留一份。
    broken_text = json.dumps(dict(GOOD, comfy_output=ZH_OUT), indent=2)\
        .replace('"port": 8765', '"port": 8765,')
    bp = os.path.join(TMP, "config.json")
    io.open(bp, "w", encoding="utf-8", newline="").write(broken_text)
    for f in ("config.json.bak", "config.json.tmp"):
        q = os.path.join(TMP, f)
        if os.path.exists(q):
            os.remove(q)
    old_path, old_cfg, old_err = paths.CONFIG_PATH, paths.CONFIG, paths.CONFIG_ERROR
    paths.CONFIG_PATH = bp
    paths.CONFIG = {}
    paths.CONFIG_ERROR = "config.json 读不出来（举例）"
    try:
        paths.save_config({"checkpoint": "new.safetensors"})
        bak = bp + ".bak"
        check("覆盖前留下了 config.json.bak", os.path.isfile(bak), bak)
        if os.path.isfile(bak):
            check("备份里是**原来的坏文件**，一个字都没改",
                  io.open(bak, encoding="utf-8", newline="").read() == broken_text)
        saved = json.load(io.open(bp, encoding="utf-8"))
        check("新文件是合法 JSON，且带上了要改的键",
              saved.get("checkpoint") == "new.safetensors", saved)
        check("config.json.tmp 没有留下来（原子写）",
              not os.path.exists(bp + ".tmp"))
    finally:
        paths.CONFIG_PATH, paths.CONFIG, paths.CONFIG_ERROR = \
            old_path, old_cfg, old_err
finally:
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    paths.CONFIG_PATH = REAL

# 结尾再确认一次：**真实的 config.json 一个字节都没动**
real_hash_after = open(REAL, "rb").read() if os.path.isfile(REAL) else None
print()
check("真实的 config.json 全程没被动过",
      real_hash_before == real_hash_after,
      "字节数 %s -> %s" % (len(real_hash_before or b""),
                           len(real_hash_after or b"")))

print()
print("=" * 74)
if fails:
    print("RESULT: %d 项失败: %s" % (len(fails), ", ".join(fails)))
    print("=" * 74)
    sys.exit(1)
print("RESULT: ALL PASS")
print("=" * 74)
