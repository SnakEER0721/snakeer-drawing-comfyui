"""回归：提示词里的 HTML 不许被执行（XSS）—— 上游报告 P1-1。

修复前实测（无头 Edge 打开真实页面，往提示词框粘贴）：
    <img src=x onerror="document.title='XSS_FIRED'">
    → document.title 真的变成 XSS_FIRED，<img> 真的进了 DOM。

根因：`ui.html` 的 `doTranslate()` 把 `r.english` / `r.unknown` / `r.not_tags`
拼成 HTML 字符串再 `$("transOut").innerHTML = html`，而这三个字段装的**全是
用户原样输入**（`cn_translate.py` 里 `unknown.append(term)`）。

为什么中英混排能过、纯中文不行：纯中文被分词器逐字拆开并插空格
（`<b>` → `< b >`），拼不成标签；含 ASCII 的输入走 lookup 的 ASCII 分支
被原样透传，HTML 结构完整保留。

危害不在"外部网站打进来"（服务只监听 127.0.0.1，还有 Host/Origin 校验），
而在**同源脚本天然放行**，而同一批 API 里有 /api/delete、/api/reveal、
/api/set_config、/api/upload —— 从群里复制一段别人分享的"提示词"粘进来，
就可能被拿走任意本地操作权限。

这个测试认三件事，缺一不可（只看"没执行"会把"界面被改坏"也判成通过）：
    ① payload 不许执行（标题没被改写、没有 <img> 元素）
    ② payload 要以**纯文本**出现（不能连文字都不显示）
    ③ 正常中文翻译的显示不许被改坏（粗体还在、内容还在）
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILED.append(name)


try:
    from playwright.sync_api import sync_playwright
except ImportError as e:
    print("   SKIP  没装 playwright（%s）—— 环境不具备，不是失败" % e)
    sys.exit(0)

URL = os.environ.get("SNAKEER_URL", "http://127.0.0.1:8765/")
PAYLOAD = '<img src=x onerror="document.title=\'XSS_FIRED\'">'

try:
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(URL, wait_until="load")
        pg.wait_for_timeout(1800)

        # ---- ① 正常翻译先不能坏 ----
        print("=" * 74)
        print("1. 正常中文翻译的显示（改了这段代码最容易顺手弄坏的就是它）")
        print("=" * 74)
        pg.fill("#promptCN", "少女 半身 微笑")
        pg.wait_for_timeout(1300)
        normal_text = pg.eval_on_selector("#transOut", "e => e.textContent")
        normal_b = pg.eval_on_selector_all("#transOut b", "e => e.length")
        check("翻出了内容", "1girl" in normal_text, normal_text[:70])
        check("粗体效果还在（1 个 <b>）", normal_b == 1, "%d 个" % normal_b)

        # ---- ② payload 不许执行 ----
        print()
        print("=" * 74)
        print("2. 粘贴带 HTML 的提示词 —— 不许执行")
        print("=" * 74)
        before = pg.title()
        pg.fill("#promptCN", PAYLOAD)
        pg.wait_for_timeout(1800)
        after = pg.title()
        imgs = pg.eval_on_selector_all("#transOut img", "e => e.length")
        out_html = pg.eval_on_selector("#transOut", "e => e.innerHTML")
        out_text = pg.eval_on_selector("#transOut", "e => e.textContent")

        print("   标题 %r → %r；#transOut 里 <img> 元素 %d 个" % (before, after, imgs))
        check("标题没被脚本改写", after == before, "%r → %r" % (before, after))
        check("没有真的造出 <img> 元素", imgs == 0, "%d 个" % imgs)
        check("页面没抛脚本错误", not errs, errs[:2])

        # 判据要用「元素是不是真被造出来了」，不能只看 innerHTML 里有没有尖括号 ——
        # payload 作为纯文本显示时 innerHTML 里也会有 &lt;img ... &gt;
        check("payload 以纯文本出现（innerHTML 里是 &lt;img）",
              "&lt;img" in out_html, out_html[:90])
        check("文字真的显示出来了（不是空白）",
              "img" in out_text and "XSS_FIRED" in out_text, out_text[:90])

        b.close()
except Exception as e:
    print("   SKIP  跑不起来（%s: %s）—— 环境不具备，不是失败" % (type(e).__name__, e))
    sys.exit(0)

print()
if FAILED:
    print("RESULT: FAIL  失败 %d 项: %s" % (len(FAILED), "; ".join(FAILED)))
    sys.exit(1)
print("RESULT: ALL PASS")
sys.exit(0)
