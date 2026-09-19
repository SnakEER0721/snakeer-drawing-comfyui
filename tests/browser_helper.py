"""Shared helper so browser tests never touch the user's own browser.

The bug being fixed: cleanup used
    Get-Process msedge | Where-Object { $_.MainWindowTitle -eq '' } | Stop-Process
intending to reap headless instances. In a Chromium browser, every normal
session also has dozens of child processes with an empty MainWindowTitle
(renderers, GPU, network, utility), so that filter killed the user's browser -
surfacing as RESULT_CODE_KILLED.

Fix: launch the test browser with its own --user-data-dir under the system temp
folder. Then "my" processes are identifiable by that exact flag and nothing else
matches, regardless of how many processes the user's own browser has.

★ 第二层修复（用户视角实测出来的）：**残留的实例会让下一轮全红，而真凶是上一轮。**
  实测：跑完一遍完整回归之后，`--tier fast` 再跑一次，4 个浏览器测试**全都在
  1–2 秒内失败**（`TargetClosedError: ... browser has been closed`，
  子进程 exitCode=21），而单独跑每一条又都是好的。查下来是上一次留下的一个
  headless Edge 还握着 `--user-data-dir=<测试 profile>`：Chromium 对同一个
  profile 目录只允许一个实例，第二个实例直接退出。
  于是「连跑两次 fast」这种最普通的动作，第二次会看到 4 个假失败 ——
  一个从 GitHub 克隆下来的人很容易据此认定这套代码是坏的。

  现在 launch() **先清残留再启动**，启动失败还会再清一次重试，两次都不行
  才报错，并且报错里说清"profile 被占了"和怎么办。清理判据仍然是那个
  profile 目录，不碰用户的浏览器。
"""
import os
import shutil
import subprocess
import tempfile
import time

# 专用目录名：带上它才说明是测试起的实例
PROFILE_DIR = os.path.join(tempfile.gettempdir(), "dsh_ui_test_profile")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def count_test_processes():
    """当前有多少个带测试 profile 标记的 Edge 进程（正常应为 0）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object { $_.CommandLine -like '*dsh_ui_test_profile*' }).Count"],
            capture_output=True, text=True, timeout=60)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1


def _reap_and_wait(wait_s=8.0):
    """清掉残留并**等它们真的退出**（杀完立刻启动还是会被占着）。"""
    reap_own_processes()
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if count_test_processes() == 0:
            return True
        time.sleep(0.3)
    return count_test_processes() == 0


def _launch_once(pw, headless, viewport):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        channel="msedge",
        headless=headless,
        viewport=viewport or {"width": 1400, "height": 900},
        args=["--no-first-run", "--no-default-browser-check"],
    )


def launch(pw, headless=True, viewport=None, seed=None):
    """启动测试用浏览器，带独立 profile。

    必须用 launch_persistent_context：Playwright 不接受通过 args 传
    --user-data-dir（会直接报错），只有这个入口能指定 profile 目录。
    profile 独立之后，进程才可以按目录标记识别，清理时不会碰到用户的浏览器。

    seed 是 Playwright 自带的稳定 profile 命名参数；不传则每次生成随机目录。
    这里自己固定一个目录，便于按路径匹配进程。

    ★ 启动前先清残留、启动失败再清一次重试：一个残留实例会握着 profile
      目录，让**这一次**的启动直接失败（Chromium exitCode=21）。实测就是这么
      看到"4 个浏览器测试全红、单独跑又全对"的。见文件头那段说明。
    """
    if count_test_processes() > 0:
        print("[browser_helper] 发现 %d 个残留的测试浏览器进程，先清掉"
              % count_test_processes())
        _reap_and_wait()
    try:
        return _launch_once(pw, headless, viewport)
    except Exception as e:
        first = e
        print("[browser_helper] 第一次启动失败（%s），清残留后重试一次"
              % type(e).__name__)
        if _reap_and_wait():
            try:
                return _launch_once(pw, headless, viewport)
            except Exception as e2:
                first = e2
        raise RuntimeError(
            "浏览器起不来，而且清掉残留之后还是起不来。\n"
            "  profile 目录: %s\n"
            "  原始错误: %s: %s\n"
            "  可以手动执行：关掉所有测试浏览器（命令行里带 %s 的那些），"
            "或者删掉上面那个 profile 目录再跑。\n"
            "  注意：不要去杀用户自己的浏览器 —— 判据只看命令行里有没有那个目录。"
            % (PROFILE_DIR, type(first).__name__, str(first)[:300], PROFILE_DIR)
        ) from first


def reap_own_processes():
    """只杀掉带测试 profile 标记的 Edge 进程。

    依据完整的 --user-data-dir 参数匹配，而不是"没有窗口标题"这种会误伤的判断。

    ★ 这里曾经**一直是个空转**：路径被 `PROFILE_DIR.replace("\\\\", "\\\\\\\\")`
      转义成双反斜杠，而它用的是 PowerShell 的 `-like`（通配符匹配，不是正则），
      于是模式里的 `C:\\\\Users\\\\...` 在真实命令行里永远匹配不到任何东西。
      后果实测：残留实例从来清不掉 → 下一轮浏览器测试全红（Chromium 对同一个
      profile 目录只允许一个实例，第二个直接 exitCode=21），而单独跑每一条都对。
      量法（dev/ 里那条同样的坑）：同一个 marker，`-like` 匹配到 0 个、不转义匹配到 9 个。
      → 通配符模式里**不要转义反斜杠**。这条现在有测试守着
      （tests/test_browser_cleanup.py 会真的起一个残留实例、再断言清掉了）。
    """
    if not os.path.isdir(PROFILE_DIR):
        return 0
    marker = PROFILE_DIR
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -like '*%s*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    ) % marker
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60)
    except Exception:
        return 0
    # 统计一下还剩多少（正常应为 0）
    left = count_test_processes()
    return left if left >= 0 else 0


def cleanup_profile():
    """删掉测试用的浏览器 profile 目录（可能被占用，失败就算了）"""
    try:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    except Exception:
        pass


def count_user_processes():
    """统计不属于测试 profile 的 Edge 进程数 —— 用于确认没误伤用户浏览器"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object { $_.CommandLine -notlike '*dsh_ui_test_profile*' }).Count"],
            capture_output=True, text=True, timeout=60)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return -1
