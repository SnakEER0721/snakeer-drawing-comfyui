# -*- coding: utf-8 -*-
"""端到端（真浏览器）：LoRA 面板上的「来源」角标和「改名字/分类」对话框。

为什么必须过浏览器：后端把 `kind_source` / `catalog_name` 发出来了，不代表
面板上真的渲染出来 —— 角标是 `refreshTrig()` 里一段纯 DOM 拼装，对话框是
一个靠 `.modal.on` 类切换的浮层。这些全是静态检查看不见的：
`test_frontend.py` 只能确认字符串在文件里，`test_static.py` 只能确认语法。

这个测试盯三件用户看得见的事：
  【1】选进面板的每个 LoRA，trig 里都显示「来源：xxx」（有据可查，不是空白）
  【2】C站目录真的接上了：有 catalog_name 的条目在行里看得到 C站真名
  【3】「改名字/分类」按钮真的能改：对话框弹得出来，存下去之后
       **面板上的名字变了、接口也真的写进了 lora_aliases.json**

★ 和前一个版本的区别：这个测试**能变红**，而且改的是**真文件**（用户的
  lora_aliases.json），所以 finally 里逐字节还原，并用接口复核一次。
  A/B 验过：把 `openLoraEdit` 里那行 `classList.add("on")` 换成 `void 0`，
  【3】立刻红。

服务没起 / ComfyUI 关着（没有 LoRA 列表）时**跳过**，不是失败。

用法：
    python server.py --port 8765 --no-browser
    python tests/test_lora_ui_e2e.py [BASE]
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import paths  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serverguard  # noqa: E402
serverguard.require(BASE, "test_lora_ui_e2e")

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


def api(path, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- 前置检查
caps = api("/api/capabilities").get("data") or {}
loras = caps.get("loras") or []
details = caps.get("lora_details") or []
print("ComfyUI 里的 LoRA：%d 个，详情 %d 条" % (len(loras), len(details)))
if not loras or not details:
    print("RESULT: SKIP（这台机器上 ComfyUI 没开或一个 LoRA 都没有，"
          "面板不会渲染任何行）")
    sys.exit(0)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过（端到端测试需要浏览器）")
    print("RESULT: SKIP")
    sys.exit(0)

import browser_helper as bh  # noqa: E402

ALIAS = paths.ALIAS_FILE if hasattr(paths, "ALIAS_FILE") else None
if ALIAS is None:
    # paths 里没有这个常量时走 server（它才是使用方）。用 getattr 而不是
    # 直接属性访问：属性名写错时这里会**立刻报错**，而不是静默拿到 None
    # 然后在还原环节悄悄什么都不做 —— 那会把用户的别名表留在测试状态。
    import server as S
    ALIAS = getattr(S, "ALIAS_FILE", None)
    if not ALIAS:
        print("RESULT: SKIP（找不到 ALIAS_FILE，没法保证测完能还原）")
        sys.exit(0)

orig = None
if os.path.isfile(ALIAS):
    with open(ALIAS, "rb") as fh:
        orig = fh.read()


def restore_alias():
    """逐字节还原（或删掉本来不存在的文件）。"""
    import glob
    if orig is None:
        for p in [ALIAS, ALIAS + ".tmp", ALIAS + ".bak"]:
            if os.path.isfile(p):
                os.remove(p)
        return
    with open(ALIAS, "wb") as fh:
        fh.write(orig)
    for p in glob.glob(ALIAS + ".tmp") + glob.glob(ALIAS + ".bak"):
        try:
            os.remove(p)
        except OSError:
            pass


# 挑一个有 C站真名、且当前分类稳定的 LoRA 当靶子。
target = None
for d in details:
    if d.get("catalog_name") and d.get("kind") in ("style", "character",
                                                   "quality", "other"):
        target = d
        break
if target is None:
    target = details[0]
print("靶子：%s（当前 kind=%s，C站名=%s）"
      % (target["file"], target.get("kind"), target.get("catalog_name") or "无"))
NEW_ALIAS = "端到端UI测试名"


def open_panel(pg):
    """等页面把 LoRA 面板渲染出来。"""
    pg.wait_for_selector("#loraGroups", timeout=30000)
    pg.wait_for_function(
        "() => document.querySelectorAll('#loraGroups button.addLora').length > 0",
        timeout=30000)


def add_first(pg):
    """点第一个可用的「＋ 添加」按钮 —— 面板上就会出现一行。"""
    pg.evaluate("""() => {
      const b = Array.from(document.querySelectorAll('#loraGroups button.addLora'))
                     .find(x => !x.disabled);
      if(b) b.click();
    }""")
    pg.wait_for_selector("#loraGroups .loraRow", timeout=15000)


def row_for(pg, fname):
    """把 fname 作为**一条新行**加进面板，返回那一行的文字。

    ★ 为什么不是"改某个已有行的下拉框"：改了下拉框只换了界面显示，`it.name`
      没变，于是「改名字/分类」按钮打开的还是**原来那个文件**的对话框 ——
      测试会拿着"对话框里是这个文件吗"去比一个根本不是它的候选，红了也说不清
      是谁的问题。这里直接走产品自己的入口（`addLoraItem`），保证选中的就是
      目标文件。
    """
    return pg.evaluate("""(fname) => {
      addLoraItem(loraKindOf(fname, loraMeta), fname);
      const rows = Array.from(document.querySelectorAll('#loraGroups .loraRow'));
      for(const row of rows){
        const sel = row.querySelector('select');
        if(sel && sel.value === fname) return row.textContent;
      }
      return null;
    }""", fname)


with sync_playwright() as pw:
    br = bh.launch(pw, headless=True, seed="loraui")
    try:
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        open_panel(pg)
        add_first(pg)

        print()
        print("【1】面板上每一行都有「来源：xxx」—— 不许是空白")
        # 把所有可添加的 LoRA 逐个加进来太慢；按分类各加一行就够覆盖
        # 三种来源（你标的 / C站数据 / 本地命名）。
        pg.evaluate("""() => {
          const bs = Array.from(document.querySelectorAll('#loraGroups button.addLora'))
                         .filter(x => !x.disabled);
          bs.slice(0, 6).forEach(b => b.click());
        }""")
        pg.wait_for_timeout(500)
        rows = pg.evaluate("""() => Array.from(
            document.querySelectorAll('#loraGroups .loraRow')).map(r => ({
              file: r.querySelector('select') ? r.querySelector('select').value : null,
              trig: r.querySelector('.trig') ? r.querySelector('.trig').textContent : ''
            }))""")
        check(len(rows) >= 1, "面板上出现 %d 行 LoRA" % len(rows))
        no_src = [r["file"] for r in rows
                  if "来源：" not in (r["trig"] or "")]
        check(not no_src,
              "每行都写着「来源：xxx」（缺的：%s）" % (no_src[:3] or "无"))
        unknown_src = [r["file"] for r in rows
                       if "来源：" in (r["trig"] or "")
                       and "来源： " in (r["trig"] or "")]
        check(not unknown_src, "没有空来源（'来源： ' 这种）")

        print()
        print("【2】C站目录接上了：行里看得到 C站真名、底模和标签")
        # ★ 必须**先把这个文件选到某一行上**再读那一行：这一节比的是
        #   "某个文件的 C站信息有没有渲染出来"，而面板上每行都可以换成
        #   别的 LoRA。第一版直接拿第 rows[0] 行的 trig 去比 target 的
        #   C站名，跑出来就是 `FAIL 行里显示 C站真名「Miyabi…」` 这种
        #   张冠李戴的红 —— 是测试自己的 bug，不是产品。
        d = [x for x in details if x["file"] == target["file"]][0]
        if not d.get("catalog_name"):
            print("   [跳过] 这个靶子在 C站目录里没有真名"
                  "（目录没生成？见 dev/make_lora_catalog.py）")
        else:
            tt = row_for(pg, target["file"]) or ""
            # ★ 判据必须跟 `ui.html` 的真实规则走，不能再要求那个前缀**总在**。
            #   ui.html 里是：
            #       (m.alias ? "C站真名 " : "") + m.catalog_name
            #   也就是**有本地别名才加「C站真名」四个字**（2026-09-20 故意改的：
            #   没别名时这个名字本身就是唯一的名字，再标"真名"没有信息量）。
            #   而靶子是按"有 catalog_name"挑的，**没要求它没别名** —— 于是
            #   "行里一定有 C站真名"是一句碰运气的话：同一个文件在自己机器上
            #   有没有别名，结果就不同。实测本机就红在这里（靶子 748cmSDXL
            #   .safetensors 当时别名是空的），红得毫无信息量。
            #   现在改成两条一起判：名字**必须**渲染出来；前缀则**按状态**要求
            #   —— 有别名就必须有前缀（原来那层"让用户看出是自动取的"的意思），
            #   没别名就不该有前缀。两边都比原来钉得死。
            check(d["catalog_name"] in tt,
                  "行里显示 C站真名「%s」" % d["catalog_name"])
            check(("C站真名" in tt) == bool(d.get("alias")),
                  "「C站真名」前缀跟着别名走（当前 alias=%r -> 前缀该%s）：%s"
                  % (d.get("alias"), "在" if d.get("alias") else "不在",
                     tt[:70].replace("\n", " ")))
            check("C站数据" in tt or "你标的" in tt,
                  "来源角标说的是 C站数据/你标的：%s"
                  % tt[:60].replace("\n", " "))
            if d.get("catalog_base"):
                check(("底模 " + d["catalog_base"]) in tt,
                      "行里显示底模 %s" % d["catalog_base"])
            tags = d.get("catalog_tags") or []
            if tags:
                check(("#" + tags[0]) in tt,
                      "行里显示 C站标签 #%s" % tags[0])
            # ★ 下拉框里那串中文名是哪来的，必须在名字后面写出来。
            #   实测用户的困惑点：`8be1e5d2….safetensors（Detailed anime style…）`
            #   看着像自己以前起的名字，于是搞不清"我改的名字记住了没有"。
            if d.get("alias"):
                lbl = pg.evaluate("""(fname) => {
                  const sels = Array.from(
                    document.querySelectorAll('#loraGroups .loraRow select'));
                  for(const s of sels){
                    const o = Array.from(s.options).find(x => x.value === fname);
                    if(o) return o.textContent;
                  }
                  return null;
                }""", target["file"])
                check(lbl and d["alias"] in lbl,
                      "下拉框里带着中文名：%r" % lbl)
                check(lbl and "·" in lbl,
                      "中文名后面写了它是哪来的：%r" % lbl)

        print()
        print("【3】「改名字/分类」对话框：弹得出来、存得下去、面板真的变")
        trig = row_for(pg, target["file"])
        check(trig is not None,
              "行里的下拉框能选到 %s" % target["file"])
        # 点这一行的「改名字/分类」
        pg.evaluate("""(fname) => {
          const btns = Array.from(document.querySelectorAll('#loraGroups .loraRow button'))
                        .filter(b => b.textContent.trim() === '改名字/分类');
          for(const b of btns){
            const row = b.parentElement.parentElement;
            const sel = row.querySelector('select');
            if(sel && sel.value === fname){ b.click(); return; }
          }
        }""", target["file"])
        pg.wait_for_timeout(400)
        check(pg.evaluate(
            "() => document.querySelector('#loraEditModal').classList.contains('on')"),
            "★ 对话框弹出来了（.modal.on）")
        box = pg.evaluate("""() => {
          const b = document.querySelector('#loraEditModal .modalBox');
          if(!b) return null;
          const r = b.getBoundingClientRect();
          return {w: Math.round(r.width), h: Math.round(r.height),
                  vis: getComputedStyle(b).display};
        }""")
        check(box and box["w"] > 200 and box["h"] > 100,
              "对话框真的占了地方（%s）" % box)
        shown = pg.text_content("#loraEditFile")
        check(target["file"] in shown,
              "对话框里写的是这个文件：%s" % shown[:80])
        if d.get("catalog_name"):
            # 文件名是哈希的时候，只有这一行能告诉用户这到底是个什么东西
            check(d["catalog_name"] in shown,
                  "对话框里带上了 C站真名「%s」" % d["catalog_name"])

        # 写入：改名字 + 改成「其他 / 特殊」
        pg.fill("#loraEditAlias", NEW_ALIAS)
        pg.select_option("#loraEditKind", "other")
        pg.click("#loraEditSave")
        pg.wait_for_timeout(1200)
        msg = pg.text_content("#loraEditMsg")
        check("已保存" in msg, "对话框自己报了「已保存」：%r" % msg)

        onDisk = {}
        if os.path.isfile(ALIAS):
            with open(ALIAS, encoding="utf-8") as fh:
                try:
                    onDisk = json.load(fh)
                except Exception:
                    onDisk = {}
        ent = onDisk.get(target["file"]) or {}
        check(ent.get("alias") == NEW_ALIAS,
              "★ lora_aliases.json 里真的写上了名字：%r" % ent.get("alias"))
        check(ent.get("kind") == "other",
              "★ 分类也写上了：%r" % ent.get("kind"))

        # 接口复核（不用刷新页面，因为前端本地已经改了）
        c2 = api("/api/capabilities").get("data") or {}
        d2 = [x for x in (c2.get("lora_details") or [])
              if x["file"] == target["file"]]
        check(bool(d2), "接口里还找得到这条")
        if d2:
            check(d2[0].get("alias") == NEW_ALIAS,
                  "接口报的名字是新的：%r" % d2[0].get("alias"))
            check(d2[0].get("kind") == "other",
                  "接口报的分类是 other：%r" % d2[0].get("kind"))
            check(d2[0].get("kind_source") == "你标的",
                  "来源变成「你标的」：%r" % d2[0].get("kind_source"))

        # 界面：等一下那 450ms 的自动关闭 + 重绘，然后看行里
        pg.wait_for_timeout(800)
        check(not pg.evaluate(
            "() => document.querySelector('#loraEditModal').classList.contains('on')"),
            "对话框自己关掉了")
        # 这个 LoRA 现在被归到「其他」组，行也跟着搬过去了 —— 找它的新位置
        trig2 = row_for(pg, target["file"])
        if trig2 is None:
            print("   [注意] 改完之后这一行搬到了别的分组，当前没显示在面板上")
        else:
            check("你标的" in trig2,
                  "改完之后行里的来源变成「你标的」：%s"
                  % trig2[:70].replace("\n", " "))

        print()
        print("【4】控制台没有报错")
        real = [e for e in errs if "favicon" not in e.lower()
                and "404" not in e and "Failed to load resource" not in e]
        check(not real, "无 JS 错误" if not real else "JS 报错: %s" % real[:3])
        pg.close()
    finally:
        br.close()
        bh.cleanup_profile()
        restore_alias()

# ---------------------------------------------------------------- 收尾复核
try:
    c3 = api("/api/capabilities").get("data") or {}
    d3 = [x for x in (c3.get("lora_details") or []) if x["file"] == target["file"]]
    if d3:
        check(d3[0].get("alias") != NEW_ALIAS,
              "★ lora_aliases.json 已还原（接口报的名字回到 %r）"
              % d3[0].get("alias"))
except Exception as e:
    fails.append("收尾复核接口失败：%s" % e)
    print("   FAIL 收尾复核接口失败：%s" % e)

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
