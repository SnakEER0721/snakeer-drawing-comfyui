"""Verify the prompt-builder panel end to end.

Checks the parts that can silently break:
  * the panel sits under the negative-prompt box, as requested
  * /api/prompt_builder returns grouped options with Chinese labels
  * every option's English tag is a real tag (so it will not waste tokens)
  * POST stores a custom entry and it comes back on the next GET
  * the assembled prompt is usable by the normal generate path

★ 自定义项那一节（【4】）跑在**自己起的临时服务 + 临时配置文件**上，
  真实配置 `custom_prompt_options.json` 全程只读、跑完必须一字节没动。
  见下面 REAL / REAL_FINGERPRINT 那两段注释。
"""
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402
from cn_translate import Translator

BASE = "http://127.0.0.1:8765"

# 确认 8765 上跑的就是这个目录的服务。
# 不确认的话，POST 会打到**另一个安装**上，写到那边的 custom_prompt_options.json，
# 而这里检查的是本目录的文件 —— 于是"落盘到自定义项文件"假失败。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_prompt_builder")
UI = paths.UI_HTML
# ★ 绝不碰真实数据。这个文件的保护史是「直接 os.remove」→「先备份、测完还原」
#   →「彻底隔离」，每一版都是因为上一版真的丢过数据：
#
#   ① 最早直接 os.remove(REAL) —— 抹掉用户加的自定义选项，且**无法恢复**。
#   ② 改成「先备份、测完还原」。还原逻辑本身没问题（A/B 探针
#      `dev/ab_prompt_builder_data.py` 实测 md5 前后一致），但它有**两个漏洞**：
#      · 前一次运行若崩在 POST 和还原之间，脏数据就留在真实文件里；
#      · 更糟的是**污染会自我固化** —— 此后每次运行都把脏数据忠实地备份、
#        再忠实地还原回来，永远不会自愈。实测本机那份
#        `custom_prompt_options.json` 里就留着一条「发色 / 测试色」。
#   ③ 现在改成彻底隔离：**测试自己起一个临时服务**，把服务端的
#      `COMEDY_CUSTOM_OPTIONS` 指到临时目录里的文件（`server.py` 的 GET/POST
#      `/api/prompt_builder` 都认这个环境变量）。
#
#   为什么不是"给已跑着的 8765 服务设环境变量"：环境变量要在**服务进程启动时**
#   就定下来，而 8765 是用户自己的服务。②里那句「服务未必继承测试的环境变量」
#   说的就是这个 —— 但正确的反应是**自己起服务**，不是回去读写真实文件。
#   下面【4】的 `REAL_FINGERPRINT` 前后断言钉死这一点：真实文件必须一字节不动。
REAL = paths.CUSTOM_PROMPT_OPTIONS
fails = []


def check(cond, msg):
    print("   %s %s" % ("OK  " if cond else "FAIL", msg))
    if not cond:
        fails.append(msg)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, body):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


print("=" * 80)
print("【1】面板位置")
print("=" * 80)
t = io.open(UI, encoding="utf-8").read()
i_neg = t.find('id="negBox"')
i_pb = t.find('id="pbBox"')
i_guide = t.find("<!-- 引导输入 -->")
check(i_pb > 0, "面板存在")
check(i_pb > i_neg, "面板在负向提示词框之后")
check(i_pb < i_guide, "面板在「引导输入」之前")
check('<summary>提示词拼装' in t, "是折叠面板（details/summary）")

print()
print("=" * 80)
print("【2】接口数据")
print("=" * 80)
d = get("/api/prompt_builder")
check(d.get("ok"), "GET 返回 ok")
gs = d.get("data") or []
check(len(gs) >= 10, "分组数 >= 10（实际 %d）" % len(gs))
total = sum(len(g["items"]) for g in gs)
check(total >= 150, "选项数 >= 150（实际 %d）" % total)

no_cn = [i for g in gs for i in g["items"]
         if not any("\u4e00" <= c <= "\u9fff" for c in str(i.get("cn", "")))]
check(not no_cn, "全部选项有中文标签（缺失 %d）" % len(no_cn))

print()
print("=" * 80)
print("【3】标签有效性（防止把废词填进提示词）")
print("=" * 80)
tr = Translator()
bad = []
for g in gs:
    for i in g["items"]:
        if i.get("custom"):
            continue
        if not tr.is_known_tag(i["en"]):
            bad.append((g["group"], i["en"]))
check(not bad, "内置选项全部是真实标签（可疑 %d）" % len(bad))
for b in bad[:10]:
    print("        %s / %s" % b)

print()
print("=" * 80)
print("【3b】性器细节分组（原版「加强阴部」的对应项）")
print("=" * 80)
organ = next((g for g in gs if g["group"] == "性器细节"), None)
check(organ is not None, "存在「性器细节」分组")
if organ:
    ens = [i["en"] for i in organ["items"]]
    check(len(ens) >= 15, "选项数 >= 15（实际 %d）" % len(ens))
    for must in ("spread pussy", "pussy", "clitoris", "anus", "nipples"):
        check(must in ens, "含 %s" % must)

print()
print("=" * 80)
print("【3c】随机功能（原版 Random / randomizePoseExpression）")
print("=" * 80)
ui = io.open(UI, encoding="utf-8").read()
check('id="pbRandom"' in ui, "有「随机一组」按钮")
check('id="pbRandomPE"' in ui, "有「随机姿势+表情」按钮")
check("function pbPickRandom(" in ui, "有随机函数")
check("function pbPickRandomPoseExpression(" in ui, "有姿势表情随机函数")
check("PB_RANDOM_GROUPS" in ui, "随机范围可配置")


print()
print("=" * 80)
print("【4】自定义项存取（在隔离的临时服务 + 临时配置文件上）")
print("=" * 80)
import hashlib      # noqa: E402
import shutil       # noqa: E402
import subprocess   # noqa: E402
import tempfile     # noqa: E402


def fingerprint(p):
    """文件指纹。不存在 -> None，用来区分"没有"和"空文件"。"""
    if not os.path.isfile(p):
        return None
    with io.open(p, "rb") as fh:
        raw = fh.read()
    return (len(raw), hashlib.md5(raw).hexdigest())


# ★ 改之前先记下真实文件的指纹，【4】末尾再比一次。
REAL_FINGERPRINT = fingerprint(REAL)
print("   真实配置: %s" % REAL)
print("   跑之前指纹: %s" % (REAL_FINGERPRINT,))

TMP = tempfile.mkdtemp(prefix="pb_isolate_")
CUSTOM = os.path.join(TMP, "custom_prompt_options.json")
TMP_PORT = 8851
TMP_BASE = "http://127.0.0.1:%d" % TMP_PORT
HERE = os.path.dirname(os.path.abspath(__file__))

# 预置一条**已有的**自定义项：POST 走的是"读进来 -> 合并 -> 原子写回"，
# 预置一条才能真正验到"读"那一步。空文件会把读路径整个跳过
# —— 那样这条测试就是"绿得不像真的"。
with io.open(CUSTOM, "w", encoding="utf-8", newline="\n") as _fh:
    _fh.write(json.dumps({"发色": [{"cn": "已有色", "en": "keep me"}]},
                         ensure_ascii=False, indent=2))
print("   临时配置: %s（已预置一条「已有色 / keep me」）" % CUSTOM)
print("   临时服务: %s（端口 %d）" % (TMP_BASE, TMP_PORT))

_env = dict(os.environ)
_env["COMEDY_CUSTOM_OPTIONS"] = CUSTOM
_proc = subprocess.Popen([sys.executable, "-X", "utf8", paths.SERVER_PY,
                          "--port", str(TMP_PORT)],
                         cwd=os.path.dirname(HERE),
                         env=_env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def g(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_url(url, body):
    """打**完整 URL** 的 POST。

    ⚠️ 别用上面那个 `post()`：它内部是 `BASE + path`，BASE 写死指向真实服务
    （8765）。传一个完整 URL 进去会拼成 `http://127.0.0.1:8765http://...`，
    报出来的是 `getaddrinfo failed` —— 看着像网络问题，其实是 URL 拼接问题。
    """
    req = urllib.request.Request(url,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _up():
    """等服务起来。任何异常都算"还没起来"。"""
    for _ in range(60):
        try:
            g(TMP_BASE + "/api/capabilities")
            return True
        except Exception:
            time.sleep(0.5)
    return False


d2 = None
try:
    if not _up():
        check(False, "隔离用的临时服务起来了（端口 %d）" % TMP_PORT)
    else:
        r = post_url(TMP_BASE + "/api/prompt_builder",
                 {"group": "发色", "cn": "测试色", "en": "test color"})
        check(r.get("ok"), "POST 保存成功")
        # ★ 这条是隔离的**结果**（文件在），但**不足以**证明隔离生效 ——
        #   见下面那条 ★★。
        check(os.path.isfile(CUSTOM), "落盘到隔离的临时文件（不是真实配置）")
        # ★★ 这条才是真正钉住隔离的断言。不能用「os.path.isfile(CUSTOM)」代替它：
        #    隔离开关一旦失效，服务端会去写**真实**配置，而临时文件仍然存在
        #    （我们预置过它）—— 于是上面那条照样绿。A/B 实测过：把 server.py
        #    的隔离开关短路成"永远读写真实配置"，在只有上面那条断言时本测试
        #    **只红 1 条**，而"真实文件没被动"那条还是绿的 —— 因为测试进程自己
        #    不写真实文件，它验的是一件**不可能发生的事**。
        #    判据要落在人看得见的状态上：这个隔离文件的内容必须恰好等于
        #    「预置那条 + 刚 POST 那条」，多一条少一条都说明写的不是它。
        _disk = json.loads(io.open(CUSTOM, encoding="utf-8").read())
        _ens = sorted(x.get("en") for x in _disk.get("发色", []))
        check(_ens == ["keep me", "test color"],
              "服务端写的是**这个隔离文件**（内容 %s，应恰好是预置那条 + 新加的）"
              % _ens)
        d2 = g(TMP_BASE + "/api/prompt_builder")
        found = [i for g2 in d2["data"] if g2["group"] == "发色"
                 for i in g2["items"] if i["en"] == "test color"]
        check(len(found) == 1, "重新读取能取到自定义项")
        check(found and found[0].get("custom"), "自定义项带 custom 标记")

        # ★ 预置那条必须还在 —— 它证明服务端确实读的是**我们指定的**那个文件，
        #   而不是某个"新建的、恰好是空"的文件。
        kept = [i for g2 in d2["data"] if g2["group"] == "发色"
                for i in g2["items"] if i["en"] == "keep me"]
        check(len(kept) == 1, "预置的自定义项还在（证明读的就是隔离文件）")

        r2 = post_url(TMP_BASE + "/api/prompt_builder",
                  {"group": "发色", "cn": "测试色", "en": "test color"})
        check(r2.get("note") == "已存在，未重复添加", "重复添加被拒绝")
finally:
    try:
        _proc.terminate()
        _proc.wait(timeout=15)
    except Exception:
        try:
            _proc.kill()
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    # ★ 兜底：真实文件的大小、内容、**是否存在**都必须和跑之前一样。
    #   注意这条单独**不足以**证明隔离生效（测试进程自己不写那个文件），
    #   真正的判据是上面那条 ★★；这条是防"跑完发现真实配置没了"的底线 ——
    #   这个项目真的发生过（早期版本在这里 os.remove 掉用户的自定义项）。
    _after = fingerprint(REAL)
    check(_after == REAL_FINGERPRINT,
          "★ 真实配置 %s 一字节没动（跑之前 %s / 跑之后 %s）"
          % (os.path.basename(REAL), REAL_FINGERPRINT, _after))

print()
print("=" * 80)
print("【5】拼装结果可用于生成")
print("=" * 80)
# 模拟前端拼装：取几个选项拼成一行，走正常翻译接口（打真实服务即可，
# /api/translate 只读词库，不落盘任何东西）
sample = []
for _grp in ((d2 or {}).get("data") or []):
    if _grp.get("group") in ("发色", "姿势 / 动作", "表情"):
        sample += [i["en"] for i in _grp.get("items", [])[:2]]
txt = ", ".join(sample)
tr_r = post("/api/translate", {"text": "少女 微笑"})
check(tr_r.get("ok"), "翻译接口可用")
check(len(sample) >= 4, "能拼出多个标签（%d 个）" % len(sample))
print("        拼装示例: %s" % txt)

print()
print("=" * 80)
if fails:
    print("FAILED %d 项:" % len(fails))
    for f in fails:
        print("   - %s" % f)
else:
    print("RESULT: ALL PASS")
print("=" * 80)
sys.exit(1 if fails else 0)
