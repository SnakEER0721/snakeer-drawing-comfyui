"""分层测试入口。取代「每次都把全部跑一遍」。

实测的问题：整套要跑 16 分钟以上，因为两个**实验脚本**（exp_all_parts 644s、
exp_blend_compare 345s）被当成回归测试跑了。它们不打印任何判定 —— 只是把图
生成出来给人眼看，**没有"通过/失败"可言**。

四层，按「要不要真实出图」和「有没有对错」分：

  fast     纯断言，不出图。改完任何东西都该先跑这个（约 100 秒）。
  api      真实出图但快。目前是空的 —— 历史上那几个真实出图的都因为太慢
           挪进 slow 了。
  slow     真实出图的**真测试**，会打印 RESULT / 结果: 那一行。要特意跑，
           别混进日常回归（约 10 分钟 GPU）。
  exp      **实验脚本，不是测试**。它们的文件名是 exp_*.py 而不是 test_*.py，
           原因很具体：**叫 test_*.py 的话，谁跑一次 pytest 就会把它们当测试
           执行** —— 16 分钟 GPU + 往产出目录写一堆图。它们不放进任何 --tier，
           要用的时候单独执行；--list 会把它们列出来。

           ★ **exp_*.py 不随发布包发布**（只在开发环境有）。用户的机器上
           它们不存在，所以 --list 在那边会显示 0 个。原因见
           dev/audit_exp_scripts.py：它们依赖 tests/_partstest/ 里的夹具图
           （被 .gitignore 的 tests/_* 挡着）和两个不随包发布的 LoRA ——
           发出去就是 12 个一跑就崩的脚本。

  default / all   fast + api（也就是「跑一下测试」应该有的意思）

用法：
    python run_tests.py                # fast + api
    python run_tests.py --tier fast    # 最快的自检，改完必跑
    python run_tests.py --tier slow    # 真实出图的真测试
    python run_tests.py --list         # 看四层各有哪些

分类的判据（能不能打印出判定行）和守卫见 dev/audit_slow_tests.py。
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")

# 分类依据：是否真实出图 + 是否带断言汇总
FAST = [
    "test_path_whitelist.py",   # 回归：不能读产出目录以外的文件（曾可读 C:\Windows\win.ini）
    "test_vocab_layers.py",     # 回归：词库四层不互相静默覆盖（曾改 A 层被 B 层挡掉）
    "test_tag_values_real.py",  # 回归：数据文件里的标签必须真实存在（曾把 双马尾 映射到不存在的 twin tails）
    "test_ascii_passthrough.py",  # 回归：手打英文原样透传，不被模糊匹配成别的标签
    "test_browser_isolation.py",  # 回归：跑浏览器测试绝不能杀掉用户自己的浏览器
    "test_smoke_page.py",   # 端到端：真实浏览器打开页面，检查每个区域是否填充
    "test_imports.py", "test_frontend.py", "test_static.py", "test_vocab.py", "test_vocab_values.py", "test_tag_normalize.py",
    "test_lora_logic.py", "test_lora_payload.py", "test_lora_strict.py", "test_subcategories.py",
    "test_dead_controls.py", "test_wiring.py", "test_prompt_builder.py", "test_prompt_fill.py",
    "test_depth_wiring.py",     # 回归：构图迁移勾上时 depth 链路要接上，没勾/没图时绝不能接
    "test_read_meta.py",        # 回归：从成品图读回参数（真图 + 构造图 + A1111 文本 + 坏输入）
    "test_read_meta_e2e.py",    # 端到端：/api/read_meta 真打一次（服务是旧代码则跳过）
    "test_read_meta_ui.py",     # 端到端：真浏览器丢图进去，看回填是否真的生效
    "test_defaults_parity.py",  # 回归：界面默认值必须等于 server 的兜底默认值
    "test_viewer_ui.py",        # 端到端：点图在站内打开、← → 能翻页、Esc 能关
    "test_layout.py",           # 回归：哪张卡在哪一栏、窄屏降级规则还在不在
    "test_upscale_size.py",     # 回归：放大目标必须等比（曾把 16:9 压成 4:3）
    "test_delete.py",           # 端到端：删除=移回收站、越界挡住、回收站不进列表
    "test_object_info_choices.py",  # 回归：ComfyUI 新旧两种 COMBO 格式都要认
    "test_path_hygiene.py",     # 回归：全仓不许再出现写死的绝对路径（带自检）
    "test_port.py",             # 回归：端口被占用时自动往上找；只监听 127.0.0.1
    "test_checkpoint_switch.py",  # 回归：底模选了哪个就用哪个（含 null 的坑）
    "test_save_config.py",      # 回归：写回 config.json 只改指定键、保换行、原子写
    "test_set_config_e2e.py",   # 端到端：界面上换底模真的写回配置文件（测完还原）
    "test_lora_alias_e2e.py",   # 端到端：给自己 LoRA 改名字/分类真的写进 lora_aliases.json（测完还原）
    "test_lora_catalog.py",     # 回归：随包发布的 lora_catalog.json —— 查找/降级/三层优先级/原子写
                                #       （临时目录里造目录文件与最小 safetensors，不碰真实数据）
    "test_lora_ui_e2e.py",      # 端到端：真浏览器 —— 面板上的「来源」角标、C站真名、
                                #       「改名字/分类」对话框真的写进了 lora_aliases.json（测完还原）
    "test_alias_source_label.py",  # 回归：中文名后面那个来源短标签（空串不许乱加、认不出不许猜）
    "test_origin_guard.py",     # 回归：跨源请求 / 假 Host 必须挡住，本机脚本放行
    "test_comfy_down.py",       # 回归：ComfyUI 没开时要给"照着改"的话，不是 URLError 原文
    "test_client_abort.py",     # 回归：浏览器关连接时不许往控制台吐 traceback（自带对照组）
    "test_broken_config.py",    # 回归：config.json 坏了不能静默回退；带 BOM 的要能读
    "test_version.py",          # 回归：版本号只有一个来源，页头显示的是它
    "test_install_models.py",   # 安装脚本：搬对了没有、认不出的不动、不碰真实数据
    "test_bats.py",             # 回归：两个 .bat 的编码/换行/找 Python（含真 cmd 试跑）
    "test_lora_category.py",    # 回归：LoRA 分类的优先级（画质词 vs 部位词、元数据 vs 默认）
    "test_null_params.py",      # 回归：参数送 null 不许抛 TypeError（数字框清空 + NaN 就是这条路径）
    "test_poisson_blend.py",    # 回归：融合不许动蒙版外；中文路径读写（cv2 自己读写不了）
    "test_browser_cleanup.py",  # 回归：残留的测试浏览器要能清掉（这一句曾经一直空转，
                                #       表现为"连跑两次 fast，第二次 4 个浏览器测试全红"）
]
# 实测修正：test_api 与 test_compare_feature 虽然带断言汇总，但同样真实出图，
# 分别耗时 417s / 141s —— 不能算「快」。统一放进 slow 层。
API = []

# quick = fast 里**去掉浏览器测试**的那部分。
#
# 为什么要单独一层：fast 层实测 107 秒，其中 4 个要真开浏览器的（playwright）
# 就占了 ~71 秒 —— 也就是说改一行注释也要等一分钟给浏览器启动。
# 日常改动（改文档、改一个函数、调个阈值）跑 quick 就够（约 35 秒）；
# **发布前**仍然必须跑完整的 fast。判据是"要不要真起浏览器"，不是"感觉快不快"。
BROWSER = [
    "test_smoke_page.py",       # 真浏览器打开页面
    "test_read_meta_ui.py",     # 真浏览器丢图进去
    "test_viewer_ui.py",        # 真浏览器点图/翻页
    "test_lora_ui_e2e.py",      # 真浏览器看 LoRA 面板的角标 / 改名字对话框
    "test_browser_isolation.py",  # 真浏览器（这条本身是查浏览器进程隔离的）
    "test_browser_cleanup.py",  # 真浏览器（这条本身是查残留清理的）
]
QUICK = [t for t in FAST if t not in BROWSER]
# slow = **真测试**：它们会打印 RESULT / 结果: 那一行，也就是有对错。
# 都要真实出图，所以慢。用 --tier slow 跑。
SLOW = [
    "test_api.py",              # 接口全量：38 条断言
    "test_compare_feature.py",  # 构图/特征对比，11 条断言
    "test_inpaint.py",          # 局部重绘只改蒙版区域（内联判定，打印 RESULT）
    # 真的放大一张图、量成品像素。单测只测算术，这条测整条链路
    # （算尺寸 -> 建图 ImageScale -> ComfyUI 出图）。用户报过"放大把尺寸拉伸了"。
    "test_upscale_e2e.py",
]
# exp = **实验脚本**，不是测试：它们只把图生成出来给人眼看，**没有任何判定**，
# 因此没有"通过/失败"可言。所以：
#   · 不放进任何 --tier，永远不自动跑（它们加起来 16 分钟以上）
#   · 文件名是 exp_*.py 而不是 test_*.py —— **否则谁跑一次 pytest 就会把它们
#     当测试执行**：16 分钟 GPU + 往产出目录写一堆图
#   · --list 会把它们列出来，方便要用的时候找
# 判据：能不能打印出 RESULT / 结果: 那一行（run_tests.py 读的就是它）。
# 见 dev/audit_slow_tests.py —— 那份审计用同一条判据分这两类。
EXP = [
    "exp_all_parts.py",         # 各部件（手/脚/眼…）逐项出图，人眼比对
    "exp_blend_compare.py",     # 局部重绘的边缘融合：base vs alpha，打印亮度/色度表
    "exp_controlnet_ab.py",     # 两种 ControlNet 的 A/B
    "exp_denoise_sweep.py",     # denoise 扫参
    "exp_highcontrast.py",      # 高对比场景
    "exp_inpaint_prompt.py",    # 修图提示词的几种写法
    "exp_lora_effect.py",       # LoRA 权重效果
    "exp_multi_character.py",   # 多角色
    "exp_poisson.py",           # Poisson 融合的接缝/细节指标
    "exp_shoe_params.py",       # 鞋子相关参数
    "exp_shoe_prompt.py",       # 鞋子相关提示词
    "exp_vagina.py",            # 特定部位的修复效果
]


def run(name, timeout):
    p = os.path.join(TESTS, name)
    if not os.path.isfile(p):
        return "SKIP", 0.0, "文件不存在"
    t0 = time.time()
    # 必须给子进程强制 UTF-8。
    # 实测踩过：在中文 Windows（cp936）上直接跑本脚本时，子进程的 stdout 是管道，
    # Python 会退回本地编码，凡是打印了 emoji 的测试（⚠️ / ✅ / ⚠）都会抛
    # UnicodeEncodeError 直接退出 1 —— 于是 test_vocab_values、test_subcategories、
    # test_lora_logic、test_imports、test_smoke_page 全被报成 FAIL，
    # 单独跑却全是 ALL PASS。同一批测试加 PYTHONUTF8=1 后 19/19 通过。
    # 这类假失败比真失败更贵：会让人怀疑自己刚改的代码。
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([sys.executable, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=child_env)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.time() - t0, "超过 %ds" % timeout
    dt = time.time() - t0
    out = (r.stdout or "") + (r.stderr or "")
    # 取最后一行带 RESULT / 结果 的当汇总。
    # （这里原来有一个重复的同样循环，纯冗余，删掉了。）
    verdict = None
    for line in out.splitlines():
        if "RESULT" in line or "结果:" in line:
            verdict = line.strip()
    # 退出码优先于打印内容。
    # 理由（实测）：曾出现「打印 RESULT: ALL PASS 之后崩溃」的脚本
    # （test_static.py 忘记 import sys，最后一行 NameError），
    # 只看文字会把崩溃读成通过。
    v_up = (verdict or "").upper()

    # 环境不满足时测试会打印 SKIP 并 exit 0（如没装 playwright）
    if "SKIP" in v_up and r.returncode == 0:
        return "SKIP", dt, verdict

    if r.returncode != 0:
        if "PASS" in v_up or "0 失败" in (verdict or "") or "0 项失败" in (verdict or ""):
            return "FAIL", dt, "打印成功但退出码 %d（脚本在打印后崩了）" % r.returncode
        return "FAIL", dt, (verdict or "退出码 %d，无汇总" % r.returncode)

    if verdict is None:
        return "NO-SUMMARY", dt, "没有汇总输出（实验脚本？）"

    if "FAIL" in v_up or ("失败" in verdict and "0 失败" not in verdict
                          and "0 项失败" not in verdict):
        return "FAIL", dt, verdict
    if "PASS" in v_up or "0 失败" in verdict or "0 项失败" in verdict or "100%" in verdict:
        return "PASS", dt, verdict
    return "PASS", dt, verdict + "（退出码 0）"


def preflight():
    """跑之前先确认 8765 上那个服务就是本安装的。

    为什么要有这一步：
        端到端测试都是去敲 127.0.0.1:<port>。如果那个端口上是**另一个目录**
        跑起来的实例（开发目录和发布目录各有一份很常见），测试拿到的路径和它
        自己从 paths.py 算出来的不一样，就会报出一堆和被测代码无关的失败。
        实测：在发布目录里跑 fast 层，7 个端到端测试全红，而开发目录里全绿。

        fast 层的那几个已经各自带了守卫（tests/serverguard.py），会自己 SKIP。
        **慢速层那 4 个还没有** —— 所以在这里提前说清楚，免得白等 7 分钟
        再看到一堆看不懂的红。

    只提示，不拦截：服务本来就没起也是常见情况（纯断言测试不需要它）。
    """
    try:
        sys.path.insert(0, os.path.join(TESTS))
        import serverguard  # noqa
        sys.path.insert(0, HERE)
        import paths  # noqa
        base = "http://127.0.0.1:%d" % paths.PORT
        good, why = serverguard.check(base)
    except Exception as e:
        print("（预检跳过：%s）" % e)
        return
    print("=" * 78)
    if good:
        print("预检：%s 上跑的是本目录的服务 ✅" % base)
    else:
        print("预检：%s 上**不是**本目录的服务 ⚠️" % base)
        print()
        for ln in str(why).splitlines():
            print("  %s" % ln)
        print()
        print("  影响：端到端测试会 SKIP 或报出与代码无关的失败。")
        print("  纯断言测试（大部分 fast 层）不受影响，可以照常看结果。")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="default",
                    choices=["quick", "fast", "api", "slow", "all", "default"])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    if a.list:
        for label, group in (("quick（fast 里去掉 %d 个浏览器测试，约 35 秒）"
                          % len(BROWSER), QUICK),
                             ("fast（纯断言，含浏览器测试，改完必跑）", FAST),
                             ("api（真实出图但快）", API),
                             ("slow（真测试，要真实出图，用 --tier slow 跑）", SLOW),
                             ("exp（实验脚本，**不是测试**，不自动跑，单独执行）", EXP)):
            # ★ exp_*.py **不随发布包发布**（用户的决定：个人实验脚本，且依赖
            #   没有一起发布的 LoRA）。在用户的机器上它们不存在 —— 那就别把它
            #   们列出来，否则用户会照着名字去跑、然后发现文件不存在。
            #   本地开发时它们在，照常列。
            here = [x for x in group if os.path.isfile(os.path.join(TESTS, x))]
            print("%s  %d 个%s" % (label, len(here),
                                   "" if len(here) == len(group)
                                   else "（另有 %d 个只在开发环境有，不进发布包）"
                                        % (len(group) - len(here))))
            for x in here:
                print("   %s" % x)
        return 0

    if a.tier == "quick":
        groups = [("quick", QUICK)]
    elif a.tier == "fast":
        groups = [("fast", FAST)]
    elif a.tier == "api":
        groups = [("api", API)]
    elif a.tier == "slow":
        groups = [("slow", SLOW)]
    elif a.tier == "all":
        groups = [("fast", FAST), ("api", API), ("slow", SLOW)]
    else:
        groups = [("fast", FAST), ("api", API)]

    total_fail = 0
    total_t = 0.0
    if a.tier in ("default", "all", "quick", "fast", "api", "slow"):
        preflight()
    for label, items in groups:
        print("=" * 78)
        print("【%s】%d 个" % (label, len(items)))
        print("=" * 78)
        for name in items:
            v, dt, msg = run(name, a.timeout)
            total_t += dt
            if v == "FAIL":
                total_fail += 1
            color = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "skip",
                     "TIMEOUT": "TIME", "NO-SUMMARY": "?   "}[v]
            print("  %s %-26s %6.1fs  %s" % (color, name, dt, msg[:48]))
        print()

    print("=" * 78)
    print("合计 %.1fs（%.1f 分钟）  失败 %d 个" % (total_t, total_t / 60, total_fail))
    if a.tier in ("default", "all"):
        print("已跳过 slow 层 %d 个（要真实出图，用 --tier slow 单独跑）" % len(SLOW))
        # 只数**本地存在的**：发布包里 exp_*.py 一个都没有，
        # 数全额会告诉用户"另有 12 个"、然后他一个也找不到。
        n_exp = len([x for x in EXP if os.path.isfile(os.path.join(TESTS, x))])
        if n_exp:
            print("另有 %d 个实验脚本（exp_*.py）不在这里 —— 它们没有对错，"
                  "要用的时候单独跑，见 --list" % n_exp)
    print("=" * 78)
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
