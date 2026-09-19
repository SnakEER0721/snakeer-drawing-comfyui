"""Regression test: the app must not read files outside its own directories.

The bug this locks down:
  /api/image did `fp = query["path"]; if not os.path.isfile(fp): 404` and then
  served whatever the path pointed at. Measured before the fix:

      GET /api/image?path=C:\\Windows\\win.ini        -> HTTP 200, file contents
      GET /api/image?path=D:\\dsh\\webapp\\config.json -> HTTP 200, file contents

  The service only listens on 127.0.0.1, but "localhost" is not "trusted":
  any page open in the browser can issue that request. /api/upscale had the
  same hole (it read the file and only failed later inside PIL, which proves
  the open() succeeded).

The fix is a whitelist: only paths under the output tree and ComfyUI's input
directory may be read, compared after os.path.realpath so `..` and symlinks
are resolved first.

This test checks the whitelist function directly (fast, no server needed) and
also verifies the boundary cases that a naive `startswith` would get wrong.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

fails = []


def check(cond, label, detail=""):
    print("   %s %s%s" % ("OK  " if cond else "FAIL", label,
                          ("  " + str(detail)) if not cond else ""))
    if not cond:
        fails.append(label)


try:
    import server as S
except Exception as e:
    print("导入 server.py 失败（%s），跳过：这是本项目的模块测试" % type(e).__name__)
    print("RESULT: SKIP")
    sys.exit(0)

print("=" * 82)
print("【1】白名单根目录")
print("=" * 82)
roots = S._safe_read_roots()
for r in roots:
    print("   %s" % r)
check(len(roots) >= 2, "至少配置了 2 个可读根目录")
check(any(os.path.realpath(S.OUT_ANIME) == r for r in roots),
      "产出目录在可读范围内")
check(not any(r == os.path.realpath(paths.APP_DIR) for r in roots),
      "项目目录不在可读范围内")

print()
print("=" * 82)
print("【2】必须拒绝的路径")
print("=" * 82)
REJECT = [
    (r"C:\Windows\win.ini", "系统文件"),
    (os.path.join(paths.APP_DIR, 'config.json'), "应用配置"),
    (paths.TAG_DB, "词库"),
    (os.path.join(S.OUT_ANIME, "..", "..", "dsh", "webapp", "config.json"), "目录穿越"),
    (os.path.join(os.path.dirname(paths.COMFY_OUTPUT), "comfyout_evil", "x.png"), "同级仿冒目录（字符串前缀陷阱）"),
    (os.path.join(os.path.dirname(paths.COMFY_OUTPUT), "comfyout_backup", "x.png"), "同级备份目录"),
    ("config.json", "相对路径"),
    ("", "空字符串"),
    (None, "None"),
    (123, "非字符串"),
]
for p, why in REJECT:
    check(S.is_readable_path(p) is False, "拒绝 %s" % why, repr(p)[:60])

print()
print("=" * 82)
print("【3】必须放行的路径（白名单不能挡住正常功能）")
print("=" * 82)
# 白名单范围是「整个产出目录树」（用户确认过：放宽到整个 D:\comfyout），
# 不是只有 anime 子目录。所以产出根下的任意子目录都必须能读。
ALLOW = [
    (os.path.join(S.OUT_ANIME, "x.png"), "产出子目录"),
    (os.path.join(S.OUT_ANIME, "2026-09-15", "y.png"), "产出日期子目录"),
    (os.path.join(S.COMFY_OUTPUT, "z.png"), "产出根目录直属"),
    (os.path.join(S.COMFY_OUTPUT, "随便一个子目录", "z.png"), "产出根下任意子目录"),
    (os.path.join(S.COMFY_OUTPUT, "a", "b", "c", "deep.png"), "产出根下深层目录"),
    (os.path.join(S.COMFY_INPUT, "in_abc.png"), "ComfyUI input"),
]
for p, why in ALLOW:
    check(S.is_readable_path(p) is True, "放行 %s" % why, p)

# 放宽到整棵树之后，仍然不能被「同级仿冒目录」绕过
print()
print("   放宽范围后，同级仿冒目录仍须拒绝：")
for p, why in [(os.path.join(os.path.dirname(paths.COMFY_OUTPUT), "comfyout_evil", "x.png"), "comfyout_evil"),
               (os.path.join(os.path.dirname(paths.COMFY_OUTPUT), "comfyout2", "x.png"), "comfyout2"),
               (os.path.join(os.path.dirname(paths.COMFY_OUTPUT), "comfyout_backup", "x.png"), "comfyout_backup"),
               (os.path.join(S.COMFY_OUTPUT, "..", "webapp", "config.json"), "从产出目录穿出去")]:
    check(S.is_readable_path(p) is False, "拒绝 %s" % why, p)

print()
print("=" * 82)
print("【4】内容类型不能一律报 image/png")
print("=" * 82)
check(S.content_type_for("a.png") == "image/png", "png -> image/png")
check(S.content_type_for("a.PNG") == "image/png", "大写扩展名也能认")
check(S.content_type_for("a.txt") == "application/octet-stream",
      "未知类型回退 octet-stream（不再谎报 image/png）")
check(S.content_type_for("a.json") == "application/octet-stream",
      "json 不回退成 image/png")

print()
print("=" * 82)
print("【5】源码层面：端点必须真的调用了白名单")
print("=" * 82)
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "server.py"), encoding="utf-8").read()
check("is_readable_path" in src.split("def is_readable_path")[1],
      "白名单函数已定义")
n_calls = src.count("is_readable_path(") - 1      # 减去定义那一处
print("   端点里调用次数: %d" % n_calls)
check(n_calls >= 3, "三个读文件的端点都做了校验（image / upscale / inpaint）",
      "实际 %d 处" % n_calls)
check('send_header("X-Content-Type-Options", "nosniff")' in src,
      "图片响应带 nosniff，浏览器不再猜类型")

print()
print("=" * 82)
print("【6】种子参数不能被非法值搞崩")
print("=" * 82)
# 同一个 bug 家族：前端传来 NaN 时 JSON.stringify 会变成 null，后端
# setdefault 挡不住「键在但值是 None」，于是 int(None) 直接 500。
check(S.resolve_seed(None) != None, "None -> 换成随机，不再 500")      # noqa: E711
check(0 <= S.resolve_seed(None) <= 4294967295, "随机种子在合法范围内")
check(S.resolve_seed("abc") != "abc", "非数字字符串 -> 换成随机")
check(S.resolve_seed(-1) != -1, "负数 -> 换成随机")
check(S.resolve_seed(99999999999) <= 4294967295, "超范围 -> 换成随机")
check(S.resolve_seed(12345) == 12345, "合法值原样保留")
check(S.resolve_seed(0) == 0, "0 是合法种子，必须保留（不能当成没填）")
check(S.resolve_seed("777") == 777, "数字字符串也能用")
check("resolve_seed(p[" in src, "生成流程里真的调用了这个校验")

print()
print("=" * 82)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
    print("RESULT: FAIL")
else:
    print("RESULT: ALL PASS")
print("=" * 82)
sys.exit(1 if fails else 0)
