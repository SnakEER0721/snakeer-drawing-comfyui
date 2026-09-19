# -*- coding: utf-8 -*-
"""LoRA 目录（随包发布的 C站事实）与「改名字/分类」写入口的行为。

分两半，都是**纯逻辑 + 临时文件**，不碰用户的真实数据：

  · `catalog_facts()` / `_cat_by_name()` / `_cat_by_tags()` / `lora_category()`
    —— 分类链的三层优先级。目录文件被指到临时目录再测，用完就撤。
  · `paths.save_json()` —— 写 `lora_aliases.json` 用的那个函数。用户那份文件里
    有他手写的全部备注，所以"坏文件先备份、写完能读回、换行风格不变"必须钉住。

★ 这些断言都**能变红**：
    · 把 `_cat_by_tags` 从 `lora_category` 的链里摘掉 -> 【2】红
    · 把 `catalog_facts` 的大小写兜底删掉           -> 【1】红
    · 让 `save_json` 不写 `.bak`                     -> 【4】红
  逐条 A/B 验过（见文件末尾的说明）。

跑法：
    python tests/test_lora_catalog.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import server as S          # noqa: E402
import paths                # noqa: E402

FAILS = []
OKS = []


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


# ------------------------------------------------------------------ 夹具
#
# 造一个和真目录同形状的临时文件。里面故意放三件真实存在、且各自的判据不同的
# 情况：
#   A. 有 style 标签，但**真名**里就能看出是工具（ADetailer）—— 真名要赢
#   B. 只有标签能说明问题（真名 `Illustrious Style Pack` 也偏画风，但标签
#      `game character` 才是作者自己贴的类别词）—— 这里 B 放的是纯标签场景
#   C. 目录里查不到 -> 必须干净地退回下一层，不许抛、不许把 None 当字典用
LORAS = {
    "tool_with_style_tag.safetensors": {
        "sha256": "a" * 64, "bytes": 1234,
        "civitai": {
            "model_name": "[Illustrious-XL] Nipple LORA for ADetailer",
            "version_name": "v1.0", "base_model": "Illustrious",
            "tags": ["style", "detail", "tool"],
            "trained_words": ["perfect nipples"], "weight_hint": [0.6, 1.0],
            "url": "https://civitai.com/models/1?modelVersionId=2",
        },
    },
    "mss_v2_il.safetensors": {
        "sha256": "b" * 64, "bytes": 2345,
        "civitai": {
            # ★ 这条真名必须**一个判类关键词都不含**。`_NAME_CATEGORY` 那 10 条
            #   正则不是只认 style/detail —— `character|girl|boy` 也在里面，所以
            #   "Cute Anime Girl Collection" 会先命中 Character（`girl`），
            #   那这一节又变成在测真名了（实际跑出来是 Character/None，
            #   标签那层压根没被走到）。
            "model_name": "Zz Art By Someone",
            "version_name": "v3", "base_model": "Illustrious",
            # ★ `anime` 出现两次：真站数据里 Aura_Phantasy_illu 就是这样。
            #   数标签个数的地方必须先 distinct，否则"风格标签占多数"这类
            #   判据会被重复值带偏。
            "tags": ["anime", "anime", "styles", "game character"],
            "trained_words": ["hs_style"], "weight_hint": None,
            "url": "https://civitai.com/models/3?modelVersionId=4",
        },
    },
    "no_civitai_row.safetensors": {
        "sha256": "c" * 64, "bytes": 3456,
        "civitai": None,          # by-hash 查到了文件、但 C站上没有对应模型
    },
}


def write_catalog(path, loras=None):
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps({"schema": 1, "loras": LORAS if loras is None else loras},
                   ensure_ascii=False, indent=1))


class CatalogProbe(object):
    """把 `S.CATALOG_FILE` 指到临时文件，退出时还原（含缓存）。

    缓存必须一起还原：`_CATALOG_CACHE` 里存着"上次读出来的那份数据"，
    不还原的话测试后面任何一次 `lora_details()` 都会继续用临时目录的数据。
    """

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="_dsh_catalog_test_")
        self.old_file = S.CATALOG_FILE
        self.old_cache = S._CATALOG_CACHE
        self.path = os.path.join(self.tmp, "lora_catalog.json")
        S.CATALOG_FILE = self.path
        S._CATALOG_CACHE = ({}, None, {})
        return self

    def __exit__(self, *exc):
        S.CATALOG_FILE = self.old_file
        S._CATALOG_CACHE = self.old_cache
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


print("=" * 74)
print("LoRA 目录 + 改名写入口")
print("=" * 74)

# ------------------------------------------------------------ 1 查得到吗
print()
print("【1】catalog_facts：按名字找条目，找不到要干净地返回 None")
with CatalogProbe() as pr:
    write_catalog(pr.path)
    check(S.catalog_facts("") is None, "空名字 -> None（不是抛异常）")
    check(S.catalog_facts("查无此人.safetensors") is None,
          "目录里没有 -> None")
    r = S.catalog_facts("mss_v2_il.safetensors")
    check(r and r["civitai_name"] == "Zz Art By Someone",
          "完整文件名查得到（%s）" % (r or {}).get("civitai_name"))
    check(r and r["civitai_tags"] == ["anime", "anime", "styles",
                                     "game character"],
          "tags 原样带出来（不去重，去重是判据自己的事）：%s"
          % (r or {}).get("civitai_tags"))
    # ComfyUI 会把子目录拼进名字，且分隔符一会儿反斜杠一会儿正斜杠
    check(S.catalog_facts("子目录\\mss_v2_il.safetensors") is not None,
          "带子目录（反斜杠）也能按纯文件名命中")
    check(S.catalog_facts("子目录/mss_v2_il.safetensors") is not None,
          "带子目录（正斜杠）也能命中")
    check(S.catalog_facts("MSS_V2_IL.SAFETENSORS") is not None,
          "大小写不同也能命中（用户把扩展名改成大写是常事）")
    check(S.catalog_facts("no_civitai_row.safetensors") is None,
          "有行、但 civitai 是 null -> None（不能返回半个空壳给上层判）")

print()
print("【1b】目录文件不在 / 内容是坏的 —— 两种降级要分得开")
with CatalogProbe() as pr:
    # ① 文件不存在（发布版用户没生成过）：正常情况，**不许**报警
    caught = []
    old_warn = S.warn
    S.warn = lambda where, err: caught.append(where)
    try:
        check(S.catalog_facts("mss_v2_il.safetensors") is None,
              "目录文件不存在 -> None（分类退回文件名/元数据）")
        check(S.catalog_facts("mss_v2_il.safetensors") is None,
              "连续两次都返回 None（缓存没把 None 变成异常）")
        check(not caught,
              "文件不存在时**不报警**（那是正常情况，不是错误）：%s" % (caught or "无"))
        # ② 文件在、内容坏了：这是真出事了，必须说一声
        io.open(pr.path, "w", encoding="utf-8", newline="\n").write("{ 坏 JSON")
        S._CATALOG_CACHE = ({}, None, {})
        check(S.catalog_facts("mss_v2_il.safetensors") is None,
              "坏 JSON -> 仍然返回 None（不拖垮整个 /api/capabilities）")
        check(caught, "坏 JSON **报警了**：%s" % (caught or "无（这是静默失败！）"))
        # ③ 形状不对（不是 {loras:{...}}）
        caught[:] = []
        io.open(pr.path, "w", encoding="utf-8", newline="\n").write(
            json.dumps({"loras": ["这不是字典"]}))
        S._CATALOG_CACHE = ({}, None, {})
        check(S.catalog_facts("mss_v2_il.safetensors") is None,
              "loras 是列表 -> None")
        check(caught, "形状不对**也报了警**：%s" % (caught or "无"))
        # ④ 空文件
        caught[:] = []
        io.open(pr.path, "w", encoding="utf-8", newline="\n").write("")
        S._CATALOG_CACHE = ({}, None, {})
        check(S.catalog_facts("mss_v2_il.safetensors") is None, "空文件 -> None")
    finally:
        S.warn = old_warn

print()
print("【1c】缓存：改了文件要能读到新的（按 mtime+size 失效）")
with CatalogProbe() as pr:
    write_catalog(pr.path, {"x.safetensors": dict(LORAS["mss_v2_il.safetensors"])})
    check(S.catalog_facts("x.safetensors") is not None,
          "第一次读到了 x.safetensors")
    check(S.catalog_facts("mss_v2_il.safetensors") is None, "此刻还没有它")
    # 重写成一份不同的（大小也不同），模拟用户重新生成目录
    write_catalog(pr.path)
    check(S.catalog_facts("mss_v2_il.safetensors") is not None,
          "文件换掉之后立刻读得到新内容（缓存失效了）")
    check(S.catalog_facts("x.safetensors") is None,
          "旧内容不再出现（缓存真的换了，不是两份都在）")

print()
print("【1d】真目录（如果这台机器上有一份）—— 形状和内容都要说得通")
if os.path.isfile(os.path.join(os.path.dirname(HERE), "lora_catalog.json")):
    cat = S._catalog_load()
    check(isinstance(cat, dict) and len(cat) > 0,
          "读出来 %d 条" % (len(cat) if isinstance(cat, dict) else -1))
    bad = [k for k, v in cat.items()
           if not isinstance(v, dict) or not v.get("sha256")]
    check(not bad, "每条都有 sha256（缺的：%s）" % (bad[:3] or "无"))
    n_cv = [k for k, v in cat.items() if (v.get("civitai") or {}).get("model_name")]
    print("       其中 %d 条带 C站真名" % len(n_cv))
    # ★ 发布包里**不许**出现本机绝对路径，目录文件是随包发的，注释里也不行
    raw = io.open(os.path.join(os.path.dirname(HERE), "lora_catalog.json"),
                  encoding="utf-8").read()
    import re  # noqa: E402
    # ★ 不区分大小写：Windows 盘符可以是小写（`s:/…`），第一版只写 `[A-Za-z]`
    #   又把盘符限定成大写 [A-Z]，于是自己造出来的小写盘符漏过去了。
    hits = re.findall(r"(?i)\b[a-z]:[\\/]", raw)
    check(not hits, "目录文件里没有本机绝对路径（%s）" % (hits[:3] or "无"))
else:
    print("   [跳过] 这台机器上没有 lora_catalog.json（发布包默认不带，"
          "要跑 dev/make_lora_catalog.py 生成）")

# ------------------------------------------------------------ 2 三层优先级
print()
print("【2】分类链的优先级：C站真名 > C站标签 > 文件名")


def kind_of(fn, facts=None, alias="", over=None):
    return S.classify_lora(os.path.join("不存在但没关系", fn), alias=alias,
                           alias_over=over, facts=facts)


# ① C站**真名**是工具，标签里却有 style —— 真名必须赢。
#    这是真站数据：`[Illustrious-XL] Nipple LORA for ADetailer` 的作者
#    给它的标签里有 style，但它实质是给 ADetailer 用的修补器。
#
# ★ 这一节测的是 `lora_category` 这几层的优先级，所以直接调它 —— 不走
#   `classify_lora`：那个函数第一步要 `os.path.getsize(路径)`，而这些是
#   夹具里的假文件名，真走进去会先抛 FileNotFoundError 然后静默返回一个
#   只有 kind=unknown 的壳（第一版就是这么写的，`r2["kind"]` 直接 KeyError）。
#   端到端那条路（真文件 + 真分类）由 tests/test_lora_alias_e2e.py 和下面的
#   【2c】负责。
FN = "tool_with_style_tag.safetensors"
with CatalogProbe() as pr:
    write_catalog(pr.path)
    facts = S.catalog_facts(FN)
    check(facts is not None, "先能查到这条目录记录（%s）" % FN)
    r = S.lora_category(FN, {}, FN, facts)
    check(r["category"] == "Tool",
          "真名里写着 ADetailer -> Tool/画质（实际 %s/%s）"
          % (r["category"], r.get("kind")))
    check("C站模型名" in str(r.get("source")),
          "而且来源写的是「C站模型名」：%r" % r.get("source"))

    # ② 真名看不出工具，标签里才有类别词。
    #    ★ 文件名的选择有讲究：**不能**含 style/detail 这些关键词，否则
    #      `_cat_by_name(真名)` 会先命中那一条 —— 那就变成在测"真名"而不是
    #      在测"标签"了。这个夹具模拟的正是真机上的 `MSS_v2_IL.safetensors`
    #      （它的真名是 `Illustrious Style Pack`，光看名字判不出用途）。
    facts2 = S.catalog_facts("mss_v2_il.safetensors")
    check(facts2 is not None, "目录里查得到 mss_v2_il.safetensors")
    r2 = S.lora_category("mss_v2_il.safetensors", {}, "mss_v2_il.safetensors",
                         facts2)
    check(r2.get("category") == "Character" and r2.get("kind") == "character",
          "真名和文件名都判不出，标签里有 game character -> Character/character"
          "（实际 %s/%s）" % (r2.get("category"), r2.get("kind")))
    check("C站标签" in str(r2.get("source")),
          "来源写明是「C站标签」：%r" % r2.get("source"))

    # ③ 目录查不到 -> 退回文件名，而且**要说这条是猜的**。
    #    ★ 这里必须用一个**有 facts、但真名和标签都判不出**的条目：只有这样
    #      `lora_category` 才会走到"只剩文件名猜的"那个分支。直接传 facts=None
    #      是测不到的 —— 那条分支要求 `facts` 非空（见 server.py 里那段注释）。
    facts3 = {"civitai_name": "Zzz", "civitai_tags": [], "civitai_words": [],
              "sha256": "e" * 64}
    r3 = S.lora_category("someone_artstyle.safetensors", {},
                         "someone_artstyle.safetensors", facts3)
    check("猜的" in str(r3.get("source")),
          "真名/标签都判不出时，来源标明「文件名（猜的）」：%r" % r3.get("source"))
    check(r3["category"] == "Style",
          "而且分类仍然按文件名给出来了（%s）" % r3["category"])
    # 完全不传 facts（目录里查不到）时来源是干净的文件名，不带"猜的"
    r3b = S.lora_category("someone_artstyle.safetensors", {},
                          "someone_artstyle.safetensors", None)
    check(r3b.get("source") == "文件名",
          "目录里查不到时来源就是「文件名」：%r" % r3b.get("source"))

# ④ 用户自己标的压过上面全部 —— 这条必须走**完整** `classify_lora`。
#    ★ 这里需要一个 ComfyUI 认得的 safetensors 文件头，所以现造一个最小的：
#      8 字节小端头长 + JSON 头 + 数据区。第一版偷懒拿了仓库自己的
#      `lora_catalog.json` 当"存在的文件"，**每次断言都失败** ——
#      `classify_lora` 第二步就是 `_read_safetensors_header`，读不到就直接
#      return 那个 `kind="unknown"` 的壳，后面所有判断压根没执行。
#      凑一个真头比借一个 JSON 文件更省事，也更诚实。
print()
print("【2c】classify_lora：用户标注的优先级和 kind_source 的取值")


def make_st(path, meta=None, unet=2, te=0):
    """造一个最小的合法 safetensors：头长(8B LE) + JSON 头 + 数据区。"""
    import struct
    hdr = {}
    if meta:
        hdr["__metadata__"] = meta
    for i in range(unet):
        hdr["lora_unet_input_blocks_%d.lora_down.weight" % i] = {
            "dtype": "F16", "shape": [2, 2], "data_offsets": [i * 8, i * 8 + 8]}
    for i in range(te):
        hdr["lora_te_text_model_encoder_layers_%d.lora_down.weight" % i] = {
            "dtype": "F16", "shape": [2, 2],
            "data_offsets": [unet * 8 + i * 8, unet * 8 + i * 8 + 8]}
    blob = json.dumps(hdr, ensure_ascii=False).encode("utf-8")
    blob += b" " * ((8 - len(blob) % 8) % 8)      # safetensors 要求 8 字节对齐
    with open(path, "wb") as fh:
        fh.write(struct.pack("<Q", len(blob)))
        fh.write(blob)
        fh.write(b"\0" * ((unet + te) * 8))
    return path


tmp2 = tempfile.mkdtemp(prefix="_dsh_st_test_")
try:
    st = make_st(os.path.join(tmp2, "x.safetensors"))
    check(os.path.getsize(st) > 8, "造出来的最小 safetensors 有 %d 字节"
          % os.path.getsize(st))
    check(S._read_safetensors_header(st) is not None,
          "★ 它真的被 `_read_safetensors_header` 认出来了（不认就等于什么都没测）")
    mark = {"kind": "quality"}
    info = S.classify_lora(st, alias="随便", alias_over=mark,
                           facts={"civitai_name": "x", "civitai_tags": [],
                                  "civitai_words": [], "sha256": "d" * 64})
    check(info["kind"] == "quality",
          "别名表里人工标了 quality -> 就用 quality（实际 %r）" % info["kind"])
    check(info.get("kind_source") == "你标的",
          "来源标成「你标的」：%r" % info.get("kind_source"))
    # 没有人工标注时，kind_source 也必须有值 —— 界面那个「来源」角标唯一的输入
    info2 = S.classify_lora(st)
    check(bool(info2.get("kind_source")),
          "没有任何目录/别名数据时 kind_source 也不为空（实际 %r）"
          % info2.get("kind_source"))
    # 空文件头 -> 明确的"读不到文件头"，不是静默返回一个空 dict
    bad = os.path.join(tmp2, "bad.safetensors")
    open(bad, "wb").write(b"\x00" * 4)
    info3 = S.classify_lora(bad)
    check(info3.get("note") == "读不到文件头" and info3.get("kind") == "unknown",
          "头不合法 -> note 明说「读不到文件头」（%r）" % info3.get("note"))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print()
print("【2b】标签判据本身：工具词优先、重复标签不算两票")
toolish = S._cat_by_tags(["style", "detail", "tool"], "测试")
check(toolish and toolish["category"] == "Tool",
      "style+detail 并存 -> Tool（工具词赢，实际 %s）" % (toolish or {}))
pure = S._cat_by_tags(["anime", "artstyle"], "测试")
check(pure and pure["category"] == "Style",
      "纯画风标签 -> Style（实际 %s）" % (pure or {}))
# ★ 重复的 style 不能把"工具词只有 1 个"这条压过去（就是 Aura_Phantasy_illu 的坑）
dup = S._cat_by_tags(["style", "style", "style", "detail"], "测试")
check(dup and dup["category"] == "Tool",
      "style 重复 3 次 + detail 1 次 -> 仍然是 Tool（实际 %s）" % (dup or {}))
check(S._cat_by_tags([], "测试") is None, "空标签 -> None（不能凭空判成 Tool）")
check(S._cat_by_tags(None, "测试") is None, "标签是 None -> None")
# 单标签 `style`：只有一个标签时不能被当成"工具词占多数"
one = S._cat_by_tags(["style"], "测试")
check(one and one["category"] == "Style",
      "只有一个 style 标签 -> Style（不是 Tool，实际 %s）" % (one or {}))
check(S._cat_by_tags(["tool"], "测试")["category"] == "Tool",
      "只有一个 tool 标签 -> Tool")
check(S._cat_by_tags(["hatsune miku"], "测试") is None,
      "只有角色名标签 -> None（这一层不该抢角色判断）")

# ------------------------------------------------------------ 3 save_json
print()
print("【3】paths.save_json：原子写、坏文件先备份、换行风格跟着原文件")
tmp = tempfile.mkdtemp(prefix="_dsh_savejson_test_")
try:
    p = os.path.join(tmp, "a.json")
    paths.save_json(p, {"k": "值"})
    check(json.loads(io.open(p, encoding="utf-8").read()) == {"k": "值"},
          "新文件写得进、读得回")
    check(not os.path.isfile(p + ".bak"),
          "全新写不产生 .bak（没坏东西可备份）")

    # 覆盖已有的正常文件
    paths.save_json(p, {"k": "新值", "b": 2})
    back = json.loads(io.open(p, encoding="utf-8").read())
    check(back == {"k": "新值", "b": 2}, "覆盖已有文件成功")
    check(not os.path.isfile(p + ".bak"),
          "覆盖**正常**文件也不留 .bak（不然目录里会攒一堆没用的备份）")

    # 换行风格：原文件是 CRLF，新写的也要是 CRLF
    pc = os.path.join(tmp, "crlf.json")
    io.open(pc, "w", encoding="utf-8", newline="").write(
        json.dumps({"old": 1}, ensure_ascii=False, indent=1)
        .replace("\n", "\r\n"))
    paths.save_json(pc, {"old": 1, "new": 2})
    raw = io.open(pc, encoding="utf-8", newline="").read()
    check("\r\n" in raw and raw.replace("\r\n", "").find("\n") < 0,
          "CRLF 文件写回还是 CRLF（不会混进裸 LF）")

    print()
    print("【4】原文件是坏的 —— 必须先备份再覆盖（用户备注不能凭一次点击就没了）")
    pb = os.path.join(tmp, "broken.json")
    io.open(pb, "w", encoding="utf-8", newline="\n").write(
        '{"手写的备注": "这段不能丢", 坏在这里}')
    paths.save_json(pb, {"k": 1})
    check(json.loads(io.open(pb, encoding="utf-8").read()) == {"k": 1},
          "坏文件被新内容覆盖（接口还能用）")
    check(os.path.isfile(pb + ".bak"),
          "★ 覆盖前留下了 .bak")
    bak = io.open(pb + ".bak", encoding="utf-8", newline="").read()
    check("手写的备注" in bak,
          "★ .bak 里是**原来那些字节**（用户手写的内容还找得回来）")

    # 空文件 / {} 不算"坏" —— 不该触发备份
    for name, content in (("empty.json", ""), ("brace.json", "{}")):
        pe = os.path.join(tmp, name)
        io.open(pe, "w", encoding="utf-8", newline="\n").write(content)
        paths.save_json(pe, {"k": 1})
        check(not os.path.isfile(pe + ".bak"),
              "%s 不算坏文件，不产生 .bak" % name)
        check(os.path.isfile(pe + ".tmp") is False, "临时文件已清理（%s）" % name)

    # 写不进去的时候要**抛**，不能静默
    bad = os.path.join(tmp, "no_such_dir", "x.json")
    threw = False
    try:
        paths.save_json(bad, {"k": 1})
    except Exception:
        threw = True
    check(threw, "目录不存在时抛异常（静默不写会让用户以为存上了）")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 74)
print("通过 %d 项，失败 %d 项" % (len(OKS), len(FAILS)))
if FAILS:
    print()
    for m in FAILS:
        print("  FAIL %s" % m)
print("RESULT: %s" % ("ALL PASS" if not FAILS else "FAIL"))
print("=" * 74)
sys.exit(1 if FAILS else 0)
