"""Chinese -> English Danbooru tag translation, offline.

Two-layer lookup, mirroring how the Bilingual Prompt Inspector itself works:

  layer 1  base_tags.json  - 139 hand-curated entries (quality words, hair,
           expression, lighting...). Small but exact and verified.
  layer 2  danbooru_tags.sqlite3 - 328k ffdkj entries, used as a fallback.

Layer 2 is filtered by the Danbooru category code so that a Chinese term
resolves to a general descriptive tag rather than an unrelated character or
series name:

    0 = general   1 = artist   3 = copyright   4 = character   5 = meta

Only 0 and 5 are accepted; 1/3/4 are rejected. That single rule is what stops
"红眼" from resolving to "red-eye_effect" and "哥特萝莉" from resolving to
"aki_rosenthal_(gothic_lolita)".

No network, no API key, no cost.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

# ---------------------------------------------------------------------------
# 词库路径解析（可移植）
#
# 原来这里写死了本机的绝对路径，换一台电脑就找不到词库。现在按顺序找：
#   1. 环境变量 COMEDY_VOCAB_DB / COMEDY_VOCAB_BASE（应急覆盖）
#   2. 应用目录下的 data\（打包自带，推荐）
#   3. 同级 ../data/ 与 ./（手动放置）
#   4. 本机原有的 Bilingual-Prompt-Inspector 节点目录（兼容旧环境）
# 找不到时不会崩：只有内置的补充词表可用，翻译会退化但服务仍能启动。
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_file(name: str, env_key: str) -> str:
    cands = []
    env = os.environ.get(env_key)
    if env:
        cands.append(env)
    cands += [
        os.path.join(_HERE, "data", name),
        os.path.join(_HERE, name),
        os.path.join(os.path.dirname(_HERE), "data", name),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Comfy-Desktop", "ComfyUI-Installs", "ComfyUI",
                     "ComfyUI", "custom_nodes",
                     "ComfyUI-Bilingual-Prompt-Inspector", "data", name),
        os.path.join(os.environ.get("APPDATA", ""), "..", "Local",
                     "Comfy-Desktop", "ComfyUI-Installs", "ComfyUI",
                     "ComfyUI", "custom_nodes",
                     "ComfyUI-Bilingual-Prompt-Inspector", "data", name),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    # 没找到就返回首选位置，便于报错时提示放哪
    return os.path.join(_HERE, "data", name)


_BASE_JSON = _find_file("base_tags.json", "COMEDY_VOCAB_BASE")
_DB_PATH = _find_file("danbooru_tags.sqlite3", "COMEDY_VOCAB_DB")

# Danbooru category codes that are safe to use as visual descriptors.
ALLOWED_CATEGORY_IDS = (0, 5)   # general, meta
# 只排除方括号写法；不再按圆括号过滤。
# 原因（实测）：本索引只从 category 0/5（通用+元数据）构建，角色/作品的限定名
# （hatsune_miku_(vocaloid) 之类，category 3/4）本来就被 SQL 排除了。在 0/5 范围内
# 带圆括号的是 star_(symbol)、photoshop_(medium)、chips_(food) 这类**正常标签**，
# 旧规则把它们全部丢弃：53255 个里挡掉 10923 个（20.5%），其中 281 个热度超过 1000。
# 后果是同一中文释义会落到冷门的替代标签上（星形符号 选中热度 23 的 star_symbol，
# 而正确的 star_(symbol) 热度 360329）。
REJECTED_NAMES = re.compile(r"[\[\]]")


def _good_fragment(cn: str) -> bool:
    """这个中文碎片能不能单独当反向索引的键。

    释义里的分隔符（、，,;；/|）会拆出碎片，但**不是每个碎片都能独立成词**。
    实测两类必须挡掉：

      1. 单字符。`k/da_(league)` 的释义是「K/DA（英雄联盟）」，拆出个 `K`；
         `butch/femme_couple` 拆出 `T`；`mfd_threesome` 拆出 `男`、`女`。
         拿单个字母/单字当中文输入键没有意义，而且会跟正常的单字匹配抢位置。

      2. 括号不配平。标签名里带 `/` 的（`fate/grand_order` 之类）会让释义
         在括号中间被切开：`'巴比伦尼亚（Fate/Grand Order）'` 拆成
         `巴比伦尼亚（Fate` 和 `Grand Order）` —— 两个都是垃圾键。

    ★ 不要用「碎片长度差很大就是缩写」当判据。试过：170 条命中里绝大多数是
      真同义词（`药物|软性毒品`、`圣杯|高脚金属杯`、`养父与养子|养父母与养子女`），
      那个规则会误伤一大片。见 dev/probe_abbrev_rule.py。
      「站在」被 on_box 抢走那种情况（多义项释义拆出残缺短语）没法用通用规则
      判，只能逐条钉在 _SUPPLEMENT 里。
    """
    if not cn or len(cn) > 12:
        return False
    if len(cn) == 1:
        return False
    if cn.startswith("("):
        return False
    if cn.count("（") != cn.count("）") or cn.count("(") != cn.count(")"):
        return False
    return True



def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def warn_local(where: str, err: BaseException) -> None:
    """Report a recovered error to stderr (module-local, no import cycle)."""
    import sys as _sys
    try:
        print("[warn] %s: %s: %s" % (where, type(err).__name__, err),
              file=_sys.stderr, flush=True)
    except Exception:
        pass


# A plausible English Danbooru tag: letters, digits, spaces, and the few marks
# real tags use ( ) _ - ! . Plus at least 2 letters, so a stray "z" is rejected.
_TAG_RE = re.compile(r"^[A-Za-z0-9()_\-!.\s']+$")
_HAS_REAL_WORD = re.compile(r"[A-Za-z]{2,}|\d")


def _LOOKS_LIKE_TAG(s: str) -> bool:  # noqa: N802 - used as a predicate
    s = s.strip()
    if not s or len(s) > 80:
        return False
    if not _TAG_RE.match(s):
        return False
    if not _HAS_REAL_WORD.search(s):
        return False
    # a long unbroken consonant run is almost certainly not a tag
    if re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", s.lower()):
        return False
    return True


# The dictionary packs several Chinese synonyms into one cell, separated by
# these marks (e.g. 双马尾、双尾辫).
_SPLIT_SYNONYMS = re.compile(r"[、，,;；/|]+")


def _pretty(tag: str) -> str:
    """Danbooru stores tags with underscores; the prompt reads better with
    spaces, and both forms tokenize to the same thing."""
    return tag.replace("_", " ")


# Chinese varies systematically for the same visual concept, and the dictionary
# is inconsistent about which form it stores:
#   eye colour  -> stored as 「X瞳」  (red_eyes == 红瞳, NOT 红眼)
#   hair colour -> stored as 「X发」  (blue_hair == 蓝发), but 银色/银发 absent
# Canonicalise the common spellings before querying.
_CANON = (
    ("眼睛", "瞳"), ("瞳孔", "瞳"), ("眼瞳", "瞳"),
    ("的眼", "瞳"), ("眼", "瞳"),
    ("色的头发", "发"), ("头发", "发"), ("长发", "发"), ("短发", "发"),
    ("色的发", "发"), ("色发", "发"),
)


def _canonical(term: str) -> list[str]:
    """Candidate canonical forms, most specific first.

    Rule sets are applied twice because stripping a noun can expose a colour
    suffix that was not adjacent before: 蓝色头发 -> 蓝色发 -> 蓝发.
    """
    out = [term]
    for _ in range(2):
        for base in list(out):
            for a, b in _CANON:
                if a in base:
                    cand = base.replace(a, b)
                    if cand not in out:
                        out.append(cand)
    return [v for v in out if v.strip()]


# Concepts people type that the 328k dictionary simply does not carry.
# English tags are the authoritative side, so these map straight to them.
#
# The dictionary uses descriptive glosses (it stores "1girl" as 单人女性) while
# people write everyday words (少女/女孩/半身), so the everyday forms have to be
# supplied here or they resolve to nothing at all.
_SUPPLEMENT = {
    # 手部动作（值均经词库验证为真实标签）
    "剪刀手": "v",
    "比耶": "v",
    "双手比耶": "double v",
    "双V": "double v",
    "OK手势": "ok sign",
    "OK 手势": "ok sign",
    "竖起拳头": "raised fist",
    "举起拳头": "raised fist",
    "招手": "beckoning",
    "挥手": "waving",
    "竖大拇指": "thumbs up",
    "敬礼": "salute",
    "手枪手势": "finger gun",
    "指点": "pointing",
    "向上指": "pointing up",
    "握拳": "clenched hand",
    "张开手指": "spread fingers",
    "十指交叉": "interlocked fingers",
    "合十": "palms together",
    "祈祷": "praying",
    "手按胸口": "hand on own chest",
    "单手叉腰": "hand on own hip", "双手叉腰": "hands on own hips",
    "双手背在身后": "arms behind back",
    "猫爪姿势": "paw pose",
    "爪形手势": "claw pose",
    "手指比心": "finger heart",
    "双手比心": "double finger heart",
    "单手比心": "half-heart hands",
    "比心": "heart hands",
    "手指过多": "too many fingers",
    "多指": "extra digits",
    "手指过少": "fewer digits",
    "手部崩坏": "bad hands",
    "手部特写": "hand focus",
    "手扶脸": "hand on own face",
    "手托脸颊": "hand on own cheek",
    # --- subject / count ---
    "少女": "1girl", "女孩": "1girl", "女生": "1girl", "女": "1girl",
    "女孩子": "1girl", "妹子": "1girl", "一个女孩": "1girl",
    "少年": "1boy", "男孩": "1boy", "男生": "1boy",
    "两人": "2girls", "两个女孩": "2girls", "多个女孩": "multiple girls",
    "单人": "solo",
    # 局部修复时最常用的词。这些是修手/脚/眼睛时必打的，之前却查不到 ——
    # Danbooru 用复数形式（hands/fingers/eyes），单数中文要映射到复数标签。
    "手": "hands", "双手": "hands", "一只手": "hand", "单手": "single hand",
    "手腕": "wrist", "手掌": "palms", "手背": "hand", "手臂": "arms",
    "手指甲": "fingernails",
    "脚": "feet", "双脚": "feet", "一只脚": "foot", "足部": "feet",
    "脚踝": "ankle", "脚掌": "soles", "脚背": "feet", "脚趾甲": "toenails",
    "腿": "legs", "双腿": "legs", "大腿": "thighs", "小腿": "calves",
    "眼睛": "eyes", "双眼": "eyes", "一只眼": "one eye", "一只眼睛": "one eye",
    "两只眼睛": "eyes", "眼睛特写": "close-up, eyes",
    "瞳孔": "pupils", "虹膜": "iris", "眼眶": "eyes",
    "眉毛": "eyebrows", "眼睑": "eyelids",
    "脸": "face", "脸部": "face", "面部": "face", "五官": "face",
    "鼻子": "nose", "嘴巴": "mouth", "嘴唇": "lips", "牙齿": "teeth",
    "耳朵": "ears",
    "腰": "waist",
    "背部": "back", "肚子": "stomach", "肘部": "elbows",
    "关节": "joints",
    # 动作 / 状态
    "握住": "holding", "握着": "holding", "张开": "spread", "并拢": "together",
    "伸展": "outstretched", "自然": "nature", "自然下垂": "relaxed",
    "精细": "detailed", "精细的": "detailed", "清晰": "clear",
    "完整": "complete", "干净的": "clean", "细致的": "detailed",
    "对称": "symmetry", "平滑": "smooth", "自然弯曲": "bent",
    # 修复类说法：Danbooru 没有"fix"标签，最接近的是 detailed。这样
    # "修手" 不会因为"修"查不到而整句失败（未收录字符会让验证拦下请求）。
    "修": "detailed", "修复": "detailed", "重画": "detailed",
    "修手": "hands, detailed", "修脚": "feet, detailed",
    "修脸": "face, detailed", "修眼": "eyes, detailed",
    # ---- 修复常用（身体部位 + 修饰）----
    "一只手指": "finger", "一根手指": "finger",
    "头发": "hair", "发梢": "hair tips", "发根": "hair roots",
    "发量": "voluminous hair", "蓬松": "fluffy hair",
    "乳房": "breasts", "贫乳": "small breasts", "美乳": "beautiful breasts",
    "挺拔": "perky breasts",
    # 词库把 curvy 和 plump 的官方注释都写成「丰满」，倒排索引按帖子数取了
    # plump，而界面「体型」面板给的是 curvy —— 同一个词两条路径不同结果。
    # 这里对齐成面板的值（curvy 帖子更多：76750 vs 44147）。
    "丰满": "curvy",
    "阴道": "vagina", "阴唇": "labia", "小穴": "pussy", "私处": "pussy",
    "生殖器": "genitals", "阴道口": "vaginal", "宫颈": "cervix",
    "湿润": "wet", "潮湿": "wet", "黏液": "mucus",
    # ---- NSFW 行为 ----
    "手交": "handjob", "插入": "penetration", "抽插": "sex",
    "深喉": "deepthroat", "舔": "licking", "舔阴": "cunnilingus",
    "舔乳": "nipple licking", "法式接吻": "french kiss",
    "内射": "creampie", "颜射": "facial", "射在脸上": "facial",
    "中出": "creampie", "灌肠": "enema",
    # ---- NSFW 姿势 ----
    "后入": "doggystyle", "后背位": "doggystyle",
    "正常位": "missionary", "侧位": "spooning",
    "站立位": "standing sex", "张开双腿": "spread legs",
    "抬起双腿": "legs up", "屈膝": "knees up", "跨坐": "straddling",
    "面对面": "face-to-face", "背对镜头": "from behind",
    # ---- 二次元身体 ----
    "大腿内侧": "inner thigh", "肚脐下方": "lower navel",
    "腰部曲线": "waist", "臀部": "ass", "臀部曲线": "ass focus",
    "身材": "body", "丰满的身材": "plump",
    # ---- 二次元表情 ----
    "愉快": "happy", "陶醉的表情": "swoon", "愉悦": "pleasure",
    "失神": "empty eyes", "翻白眼": "rolling eyes", "流口水": "drooling",
    "吐舌": ":p", "咬唇": "biting own lip",
    # ---- 画风 ----
    "美漫风格": "western comics (style)", "美式漫画": "western comics (style)",
    "像素风": "pixel art", "复古风格": "retro artstyle",
    # 视角（修局部时常要指定）
    "特写镜头": "close-up",
    # Quantifiers must be listed whole. Without these, 两个少女 splits into
    # 两 + 少女 and produces "1girl" - the opposite of what was asked, which is
    # why multi-character prompts silently rendered a single figure.
    "两个少女": "2girls", "两个": "2girls", "二位": "2girls",
    "两位": "2girls", "俩": "2girls",
    "三个少女": "3girls", "三个": "3girls", "三位": "3girls",
    "四个少女": "4girls", "四个": "4girls",
    "多个少女": "multiple girls", "多个": "multiple girls",
    "一群少女": "multiple girls", "一群": "multiple girls",
    "多个人物": "multiple girls", "两个人": "2girls",
    "两个男孩": "2boys", "两个男生": "2boys", "两个少年": "2boys",
    "一个少年": "1boy", "一位": "1girl", "一个": "1girl", "一位少女": "1girl",
    "一个少女": "1girl", "一位男孩": "1boy", "一个男孩": "1boy",
    # --- framing ---
    "半身": "upper body", "上半身": "upper body", "半身像": "upper body",
    "全身": "full body", "全身像": "full body", "特写": "close-up",
    "七分身": "cowboy shot", "头像": "portrait",
    # --- hair colour ---
    "银发": "silver hair", "银色": "silver hair",
    "金发": "blonde hair", "棕发": "brown hair", "粉发": "pink hair",
    "红发": "red hair", "黑发": "black hair", "紫发": "purple hair",
    "绿发": "green hair", "橙发": "orange hair", "灰发": "grey hair",
    "白发": "white hair", "蓝发": "blue hair",
    # --- eyes ---
    "红眼": "red eyes", "红眼睛": "red eyes", "蓝眼": "blue eyes",
    "绿眼": "green eyes", "紫眼": "purple eyes", "金眼": "yellow eyes",
    "异色瞳": "heterochromia",
    # --- very common verbs/poses ---
    "长头发": "long hair", "短头发": "short hair",
    "站立": "standing", "坐着": "sitting", "躺着": "lying",
    # --- second pass: terms the dictionary does not carry at all ---
    # (verified by _verify_vocab.py against the real lookup chain)
    "女人": "1girl", "女性": "1girl",
    "看镜头": "looking at viewer", "回头看": "looking back",
    "回头": "looking back", "不看镜头": "looking away",
    "俯视": "from above", "从上往下": "from above",
    "仰视": "from below", "从下往上": "from below",
    "侧面": "from side", "背面": "from behind", "背对": "from behind",
        "渐变色头发": "gradient hair", "渐变色": "gradient hair",
    "双色头发": "two-tone hair", "双色发": "two-tone hair",
    "单马尾": "ponytail", "马尾": "ponytail",
    "麻花辫": "braid", "辫子": "braid",
    "丸子头": "hair bun", "齐刘海": "blunt bangs",
    "呆毛": "ahoge", "翘发": "ahoge",
    "卷发": "curly hair", "大波浪": "wavy hair", "齐肩发": "medium hair",
    "闭眼": "closed eyes", "半睁眼": "half-closed eyes",
    "张嘴": "open mouth", "大笑": "grin", "咧嘴笑": "grin",
    "笑": "smile", "微笑": "smile", "笑容": "smile",
    "哭": "crying", "生气": "angry", "惊讶": "surprised",
    "舔嘴唇": "licking lips", "媚眼": "wink", "眨眼": "blinking",
    "严肃": "serious", "脸红": "blush", "害羞": "embarrassed",
    "白裙": "white dress", "连衣裙": "dress", "裙子": "skirt",
    "丝袜": "pantyhose", "裸足": "barefoot", "赤脚": "barefoot",
    "手套": "gloves", "长手套": "elbow gloves",
    "眼镜": "glasses", "帽子": "hat", "贝雷帽": "beret",
    "泳装": "swimsuit", "比基尼": "bikini", "兔女郎": "playboy bunny",
    "兽耳": "animal ears", "项圈": "collar",
    "领结": "bowtie", "蝴蝶结": "bow", "高跟鞋": "high heels",
    "跪着": "kneeling", "走路": "walking", "跑": "running",
    "举手": "arms up",
    "抱臂": "crossed arms", "交叉双臂": "crossed arms",
    # 「手放脸旁」和「比心」是完全不同的动作，早期版本把两者都写成
    # "hand on face"（而且 hand on face 根本不是 Danbooru 标签）。
    # 现按词库的权威标签拆开：手放脸旁 -> hand on own face；比心 -> heart hands
    # （比心的条目见上方 SUPPLEMENT 区，此处不再重复定义）
    "手放脸旁": "hand on own face",
    "拿东西": "holding", "手持": "holding",
    "白底": "white background", "灰底": "grey background",
    "海边": "beach", "海滩": "beach",
    "夜空": "night sky", "星空": "starry sky", "晚上": "night",
    "室外": "outdoors", "室内": "indoors",
    "雨天": "rain", "下雨": "rain", "雪": "snow",
    "窗边": "window", "天空": "sky", "云": "cloud",
    "柔光": "soft lighting", "柔和光线": "soft lighting",
    "逆光": "backlighting", "轮廓光": "rim lighting",
    "电影感光线": "cinematic lighting", "体积光": "volumetric lighting",
    # 丁达尔光原本也映射到 volumetric lighting，但那个标签在本地词库里**不存在**
    # （库里只有 volumetric_flask 容量瓶）。词库对 sunbeam 的官方注释正是
    # 「丁达尔效应」，posts=13346，而且界面「光照」面板给的也是 sunbeam ——
    # 面板点选和手打必须得到同一个结果，所以这里对齐成 sunbeam。
    "丁达尔光": "sunbeam", "景深": "depth of field", "戏剧性阴影": "dramatic shadow",
    "明亮": "bright", "阳光": "sunlight",
    "杰作": "masterpiece", "最佳质量": "best quality",
    "超高分辨率": "absurdres", "细节丰富": "highly detailed",
    "精致": "highly detailed",
    # 「动漫风格」原本指向 anime style —— 但那个标签在 Danbooru 上是 0 帖，
    # 根本不存在（anime / cartoon / 2d / cel shading 也都不存在）。
    # 改指 anime coloring（56661 帖），它才是"动漫画风"真正能落地的标签，
    # 和「赛璐璐 / 赛璐珞上色」指向同一条，所以词表里并成了同一行的别名。
    "动漫风格": "anime coloring",
    # 「厚涂」原本指向 impasto（油画颜料堆叠法，536 帖），但中文板绘说的厚涂
    # 是笔触融合的绘制风格；词库里 painterly 的官方中文正是「厚涂风格」
    # （10405 帖），差 20 倍，改指 painterly。
    "水彩": "watercolor", "厚涂": "painterly",
    # 「赛璐璐」原本指向 cel shading —— 该标签在 Danbooru 上 post_count=0
    # （空标签、无 wiki），写进去等于写空气。真标签是 anime_coloring
    # （56661 帖），它的官方中文注释就是「赛璐珞上色」。
    # cel（36 帖）是赛璐珞片实物、cel_rendering（555 帖）是 3D 赛璐璐渲染，
    # 都不能替代画风本身。
    "赛璐璐": "anime coloring", "赛璐璐上色": "anime coloring",
    "草图风": "sketch",   # 误删恢复
    "素描": "sketch", "线稿": "lineart",
    # --- third pass: extended vocabulary (probed by _probe_more_vocab.py) ---
    # 体型
    "苗条": "slim", "高挑": "tall", "成熟": "mature female",
    "肌肉": "muscular", "幼": "child",
    # 身体部位
    "腰部": "waist", "肩膀": "shoulders", "颈部": "neck", "手指": "fingers",
    # 姿势（扩展）
    "蹲着": "squatting", "翘腿": "crossed legs", "抬起腿": "leg up",
    "举起双手": "arms up", "扭头": "head turned", "仰头": "looking up",
    "趴着": "on stomach", "弯腰": "bent over",
    # 视线（扩展）
    "闭上一只眼": "one eye closed", "看着观众": "looking at viewer",
    "眯眼": "squinting", "向上看": "looking up",
    # 服装（扩展）
    "吊带袜": "garter belt", "泳衣": "swimsuit",
    "内衣": "underwear", "胸罩": "bra", "内裤": "panties",
    "网袜": "fishnets", "皮衣": "leather", "旗袍": "qipao",
    "运动服": "sportswear", "睡衣": "pajamas", "围裙": "apron",
    "披风": "cape", "头饰": "hair ornament", "耳环": "earrings",
    "项链": "necklace", "戒指": "ring", "纹身": "tattoo", "绷带": "bandages",
    # 状态（扩展）
    "湿身": "wet", "出汗": "sweat", "流泪": "tears",
    "受伤": "injury", "绑缚": "bound", "被绑": "bondage",
    "睡觉": "sleeping",
    # 镜头构图（扩展）
    "低角度": "from below", "高角度": "from above", "斜角": "dutch angle",
    "超广角": "wide angle", "正面全身": "full body, from front",
    "侧面全身": "full body, from side", "背面全身": "full body, from behind",
    "镜中": "mirror", "倒影": "reflection", "前景虚化": "blurry foreground",
    # 氛围（扩展）
    "朦胧": "hazy", "梦幻": "dreamy", "复古": "retro artstyle",
    "暗黑": "dark theme", "明快": "vibrant", "宁静": "calm",
    "浪漫": "romantic", "神秘": "mysterious",
    # 画质（扩展）
    "极高细节": "extremely detailed", "杰作级别": "masterpiece",
    "精美": "beautiful", "鲜艳": "vivid colors", "柔和配色": "muted colors",
    "高对比": "high contrast", "低饱和": "desaturated",
    # --- fourth pass: body / framing terms used by mature character art ---
    "大屁股": "wide hips", "宽胯": "wide hips", "细腰": "narrow waist",
    "胸口": "chest", "膝盖": "knees", "脖子": "neck", "腹部": "stomach",
    "暴露": "skimpy", "露出": "exposed", "半裸": "semi-nude",
    "开胸": "cleavage", "露肚脐": "midriff", "露背": "bare back",
    "露肩": "bare shoulders",
    "亲吻": "kiss", "接吻": "kiss", "拥抱": "hug", "牵手": "holding hands",
    "舌吻": "french kiss",
    "白肤": "pale skin", "黑肤": "dark skin", "小麦色": "tan",
    "小麦色皮肤": "dark-skinned female", "光滑皮肤": "smooth skin",
    "油亮": "shiny skin",
    "娇喘": "huffing", "呻吟": "moaning", "喘息": "huffing",
    "局部特写": "close-up",
    # --- 第五批：界面「提示词拼装」面板里的说法 ---
    # 起因：面板的显示名和词库的官方中文注释用词不同，导致同一个概念
    # 点面板能出正确标签、手打中文却查不到（词被静默丢掉）。
    # 每一条的英文都是**词库里真实存在**的标签，且中文对应关系取自词库自己
    # 的官方注释，不是我自己配的：
    #   衣物下拉(108780) 下拉胸罩(9444) 床上(174381) 抬腿(47577)
    #   双手置于大腿上(5071) 掰开阴部(41239) 皱缩的肛门(3602)
    "拉下衣物": "clothes pull", "拉下衣服": "clothes pull",
    "拉下胸罩": "bra pull", "拉下内衣": "bra pull",
    "躺在床上": "on bed", "在床上": "on bed",
    "双手放大腿": "hands on own thighs", "手放大腿": "hands on own thighs",
    "张开的阴部": "spread pussy", "掰开阴部": "spread pussy",
    "皱缩肛门": "puckered anus", "皱缩的肛门": "puckered anus",
    "微胖": "plump",
    # --- 第六批：词条整体映射（避免逐字拆解）---
    # 起因：并入「双手插入自己头发中」时，_segment 把「自己」「中」判为未收录，
    # 整条词就废了 —— 词表里有、界面里打不出来。
    # 修法用**整条映射**而不是补单字：单字补充会改变语义
    # （比如「插入」在别处已经映射为 penetration，再加一条就重复且覆盖）。
    "双手插入头发": "hands in own hair",
    "插入自己头发": "hands in own hair",
    "双手插入自己头发": "hands in own hair",
    "双手抱头": "hands on own head",
    # 方位字单独给个落点，避免它变成「未收录」把整条词拖死。
    # 不能用「分词时跳过」的办法：跳过会切坏「中出」（它以中开头）。
    "中": "inside", "内": "inside", "里": "inside",
    "上": "on", "下": "down",
    # --- 第七批：桌面「提示词映射」新资料的中文名 ---
    # 来源：画风.md / 腿部姿势与移动.md。这些中文名在词库没有官方注释，
    # 素材里的中文写法必须在这里显式映射，否则界面里打不出来。
    "传统媒材": "traditional media", "数码仿传统媒材": "faux traditional media", "混合媒材": "mixed media",
    "素描/草图": "sketch", "绘画媒材": "painting (medium)", "水粉画": "gouache (medium)",
    "丙烯画": "acrylic paint (medium)", "石墨铅笔画": "graphite (medium)", "彩铅画": "colored pencil (medium)",
    "笔墨绘画": "pen (medium)", "针管笔画": "millipen (medium)", "蘸水笔画": "nib pen (medium)",
    "圆珠笔画": "ballpoint pen (medium)", "墨水画": "ink (medium)", "彩色墨水画": "color ink (medium)",
    "马克笔画": "marker (medium)", "毛笔画": "calligraphy brush (medium)", "水溶彩铅": "watercolor pencil (medium)",
    "粉彩画": "pastel (medium)", "纸艺": "papercraft (medium)", "胶带画": "masking tape (medium)",
    "厚涂颜料": "impasto", "外轮廓描边": "outline", "粗外轮廓": "thick outlines",
    "黑色描边": "black outline", "排线阴影": "hatching (texture)", "直线排线": "linear hatching",
    "明显笔刷痕": "brush stroke", "水彩效果": "watercolor effect", "柔和混色": "blending",
    "单色画面": "monochrome", "重点色": "spot color", "多组单色块": "multiple monochrome",
    "限定色板": "limited palette", "暗色调": "dark", "低饱和色": "muted colors",
    "浅淡色": "pale colors", "棕褐色调": "sepia", "高饱和": "saturated",
    "多彩": "colorful", "邻近色配色": "analogous colors", "互补色配色": "complementary colors",
    "色相偏移上色": "hue shifting", "彩线贴色": "color trace", "运动线": "motion lines",
    "强调线": "emphasis lines", "喊叫强调线": "shout lines", "漫画网点": "screentones",
    "抖动网点": "dithering", "3D渲染": "3d", "赛璐璐式3D渲染": "cel rendering",
    "网页涂鸦风": "oekaki", "PC-98风格": "pc-98 (style)", "绘画感渲染": "painterly",
    "仿照片": "fake photograph",  
    "超现实主义": "surreal",  
    "西式卡通风": "toon (style)", "欧美漫画风": "western comics (style)", "动漫化": "animification",
    "名画仿作": "fine art parody", "现代仿复古": "faux retro artstyle", "1960年代画风": "1960s (style)",
    "1970年代画风": "1970s (style)", "1980年代画风": "1980s (style)", "1990年代画风": "1990s (style)",
    "2000年代画风": "2000s (style)", "散景光斑": "bokeh", "焦散光纹": "caustics",
    "色差效果": "chromatic aberration", "VHS录像带伪影": "vhs artifacts", "镜头眩光": "lens flare",
    "星芒": "diffraction spikes",  
    "徒步": "hiking", "蹦跳前进": "skipping", "迈步": "stepping",
    "绳降": "rappelling", "追赶": "chasing", 
    "扑跃": "pouncing", "落地": "landing", "俯冲落地": "dive",
     "昏倒": "fainting", "拍动": "flapping",
     "摆荡": "swinging", "乱挥挣动": "flailing",
    "发抖": "trembling", "抽动": "twitching", 
    "滑行运动": "skating", "滑雪": "skiing", "踢击": "kicking",
     "裆部踢击": "crotch kick", "高踢": "high kick",
    "战斗架势": "fighting stance", "踩向观众": "stomping viewer", "踩到观众": "stepping on viewer",
     "站在他人身上": "standing on person", "交叉双腿": "crossed legs",
    "双腿折叠": "legs folded", "单腿抬起": "leg up", "抬腿动作": "leg lift",
    "单腿伸直": "outstretched leg", "双腿伸直": "outstretched legs", "单腿站立": "standing on one leg",
    "单膝抬起": "knee up", "双膝抬起": "knees up", "屈膝靠胸": "knees to chest",
    "膝分脚并": "knees apart feet together", "膝并脚分": "knees together feet apart", 
      "抓住自己的腿": "holding own leg",
    "手放在自己腿上": "hand on own leg", "手放在自己膝盖上": "hand on own knee", 
    "大幅分腿": "spread legs", "坐姿劈叉": "sitting split", "双腿越过头部": "legs over head",
    "高柔韧姿势": "flexible", "直身分腿": "upright straddle", "手倒立": "handstand",
     "绷脚背": "plantar flexion", "勾脚": "dorsiflexion",
    "双脚抬起": "feet up", "双脚后翘": "feet up heels up",

    # --- 第十批：带括号后缀的媒材名 ---
    "圆珠笔": "ballpoint pen (medium)",
    "水溶性彩色铅笔": "watercolor pencil (medium)",
    # --- 第十一批：与界面拼装面板对齐 ---
    "双腿抬起": "legs up",
    # --- 第十二批：拆分含 / 的死键（这类键永远匹配不上） ---
    "新艺术": "art nouveau", "穆夏风格": "art nouveau", "墨绘": "sumi-e",
    "水墨写意风": "sumi-e", "塔罗牌画面": "tarot (medium)", "塔罗牌风格": "tarot (medium)",
    "拼贴画": "collage", "拼贴风格": "collage", "投影": "drop shadow",
    "下落阴影": "drop shadow", "行进": "marching", "齐步走": "marching",
    "单脚跳": "hopping", "小跳": "hopping", "跳水": "diving",
    "潜入": "diving", "腾空": "launching", "发射": "launching",
    "扑水": "splashing", "溅水": "splashing", "飞身双脚踢": "drop kick",
    "被踩": "stepped on", "踩踏互动": "stepped on",
    "女子坐": "wariza", "W坐": "wariza", "横坐": "yokozuwari",
    "侧坐": "yokozuwari", "抱腿": "hugging own legs", "抱膝": "hugging own legs",
    "双手扶脚": "hands on feet", "抓脚": "hands on feet", "沃森交叉腿": "watson cross",
    # --- 第九批：补齐缺失的少量映射 ---
    "着陆": "landing",
    # --- 第八批：新并入素材里的中文名对齐 ---
    # 词库对同一个概念常有多个标签，官方注释却写成同一个中文。
    # 素材希望用的是下面这个，所以在这里显式指定，避免倒排索引按
    # 帖子数挑走另一个。
    "水彩画": "watercolor (medium)", "平涂": "flat color", "厚涂风格": "painterly",
    "水墨画": "sumi-e", "故障艺术": "glitch art", "踩踏": "stomping",
    "挥舞手臂": "flailing", "飞踢": "flying kick",     "单膝跪": "one knee", "摩根船长姿势": "captain morgan pose", "脚跟抬起": "heels up",
    "绷脚尖": "toe-point", "弹腿": "leg lift",
    # --- 第十批：构图 / 取景 / 画风的既有错误映射修正 ---
    # 「透视」在词库里有两条 row 同挂中文「透视」：x-ray(17509) 和
    # perspective(9761)。_resolve 按帖子数取大，于是永远选中 x-ray ——
    # 可构图语境下「透视」是 perspective（透视法），x-ray 是"看到内部"。
    # Danbooru 自己把两条都写成「透视」（perspective 的 other_names 里也有
    # 「透视画法」「三点透视」），所以只能在这里显式指定。
    # x-ray 按用户要求保持原样、不加中文入口：要它就直接写 x-ray
    # （注意必须带连字符，写成 xray 是 0 帖的空标签）。
    "透视": "perspective", "透视画法": "perspective", "三点透视": "perspective",
    # 取景阶梯 —— 「中景」「中近景」有 Danbooru 官方依据：
    #   cowboy shot 的 other_names 含「中景镜头」
    #   upper body  的 other_names 含「中近景镜头」
    # 原值 medium shot 是 0 帖空标签；中近景则会退化成逐字匹配拿到 inside。
    "中景": "cowboy shot", "中景镜头": "cowboy shot", "中近景": "upper body",
    # 单字被吞：这些复合词不在词库里，_segment 会退化成逐字贪心匹配，
    # 「角」命中 horns（犄角）、「中」命中 inside。逐条给出落点。
    "仰角": "from below", "俯角": "from above",
    "主观视角": "pov", "第一人称视角": "pov",
    "等距视角": "isometric", "斜投影": "isometric",
    "鱼眼": "fisheye", "鸟瞰": "bird's eye view",
    "明暗对比": "chiaroscuro",
    # 扩充「视角与透视」时补的取景/构图词，落点全是词库里核实过的真标签。
    "大远景": "very wide shot",
    "分屏裁切": "split crop",
    "信箱式": "letterboxed", "竖屏信箱": "pillarboxed",
    # --- 第十三批：「动词 + 方位」被多义项释义抢走 ---
    # on_box 的释义是「站在/坐在箱子上」。反向索引按 / 拆开，于是**「站在」
    # 单独成了一个键、指向 on_box** —— 打「站在窗边」会翻出 `on box, window`。
    # 而 standing（133 万帖，全站最高频之一）的释义是「站立」，对不上「站在」。
    #
    # 结果就是：一个 1007 帖的冷门标签抢走了一个最常用的动作词。
    # 这类「多义项释义被拆出残缺短语」的问题没法用通用规则判（试过按碎片长度
    # 差过滤，会误伤 150 多条真同义词，见 dev/probe_abbrev_rule.py），
    # 所以逐条显式钉住。核实：standing / sitting / lying 都是真实存在的高频标签。
    #
    # ★ 只加**这里还没有的键**。「站立」「坐着」「躺着」在 L334 已有、
    #   「趴着」在 L416 已有 —— 重复的字典键会被 Python 静默去重，
    #   test_vocab_layers 的【4】专门查这个。
    "站在": "standing", "站着": "standing",
    "坐在": "sitting",
    "躺在": "lying",
    # 「站在X上」这类带宾语的完整说法留给词库自己（on_box / standing_on_object
    # 等的释义本来就是完整的），这里只补**光秃秃的动词**。
}


class Translator:
    # Common English tags are indexed by their Chinese gloss so that a term the
    # dictionary stores only in a descriptive form still resolves. "1girl" is
    # stored as 单人女性; indexing it lets 女/单人女性 both reach it, and gives
    # a safety net for other high-frequency concept tags.
    REVERSE_TAGS = (
        # subject & count
        "1girl", "2girls", "1boy", "multiple girls", "multiple boys", "solo",
        "solo focus",
        # framing
        "upper body", "lower body", "full body", "close-up", "cowboy shot",
        "portrait", "cowboy shot", "headshot",
        # common pose / state
        "standing", "sitting", "lying", "kneeling", "walking", "running",
        "arms up", "hands on own hips", "crossed arms", "looking at viewer",
        "looking away", "looking back",
        # common scene words
        "simple background", "white background", "grey background",
        "outdoors", "indoors", "night", "day", "sky", "cloud",
    )

    def __init__(self):
        self.base: dict[str, str] = {}
        self._load_base()
        # 词库缺失时不能让整个服务起不来：连接失败就退化成"只有内置补充词表"，
        # 并留下 db_missing 标记，界面/自检脚本据此提示用户放词库。
        self.db_missing = False
        try:
            if not os.path.isfile(_DB_PATH):
                raise FileNotFoundError(_DB_PATH)
            self.conn = sqlite3.connect(
                "file:%s?mode=ro" % _DB_PATH.replace("\\", "/"),
                uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        except Exception as e:
            self.db_missing = True
            self.conn = None
            warn_local("词库不可用，已退化为内置词表（路径: %s）" % _DB_PATH, e)
        self._cache: dict[str, str | None] = {}
        self._max_cn = 8
        self.reverse: dict[str, str] = {}
        self._known_cache: dict[str, bool] = {}
        self._exact_cache: dict[str, bool] = {}
        self._load_reverse()

    def _load_reverse(self) -> None:
        """Index English tags by their stored Chinese gloss.

        Root cause of the historical "term not found" churn: only ~52k of the
        dictionary's 328k rows are general-purpose tags (the other 77% are
        artist and character names), and many of those store a descriptive
        gloss instead of the word people type anyway. Building the index from
        the whole general-tag set up front fixes the class of problem instead
        of adding one word at a time.
        """
        # 1) all general + meta tags, most-used first so common words win
        try:
            if self.conn is None:
                raise RuntimeError("no db")
            rows = self.conn.execute(
                "SELECT name, chinese, post_count FROM tags "
                "WHERE category_id IN (0,5) AND chinese IS NOT NULL "
                "AND TRIM(chinese) <> '' ORDER BY post_count DESC")
            for r in rows:
                en = _pretty(r["name"])
                if REJECTED_NAMES.search(r["name"]):
                    continue
                for cn in _SPLIT_SYNONYMS.split(str(r["chinese"])):
                    cn = cn.strip()
                    if _good_fragment(cn):
                        self.reverse.setdefault(cn, en)
                self.reverse.setdefault(str(r["name"]), en)
                self.reverse.setdefault(_pretty(r["name"]), en)
        except Exception as e:
            warn_local("构建反向索引失败", e)

        # 2) a curated subset keeps its short, canonical form
        for tag in self.REVERSE_TAGS:
            key = tag.replace(" ", "_")
            try:
                if self.conn is None:
                    raise RuntimeError("no db")
                row = self.conn.execute(
                    "SELECT chinese FROM tags WHERE name = ? LIMIT 1", (key,)).fetchone()
            except Exception:
                continue
            if row and row["chinese"]:
                for cn in _SPLIT_SYNONYMS.split(str(row["chinese"])):
                    cn = cn.strip()
                    if _good_fragment(cn):
                        self.reverse[cn] = tag
            self.reverse[tag] = tag
            self.reverse[key] = tag

    def _load_base(self) -> None:
        if not os.path.isfile(_BASE_JSON):
            return
        data = json.load(open(_BASE_JSON, encoding="utf-8"))
        for t in data.get("tags", []):
            en = t.get("english")
            if not en:
                continue
            for cn in [t.get("chinese")] + list(t.get("aliases") or []):
                if cn:
                    self.base.setdefault(str(cn).strip(), en)

    # ------------------------------------------------------------------ lookup
    def lookup(self, term: str) -> str | None:
        term = term.strip()
        if not term:
            return None
        if term in self._cache:
            return self._cache[term]

        if _is_ascii(term):
            # Pass English through verbatim so users can mix in raw tags - but
            # only when it is plausibly a real tag. Without this, stray letters
            # like "zzzz" are echoed back as if they were valid tags, and an
            # all-garbage prompt sails past the "nothing recognised" check.
            #
            # Short real tags exist and must survive that check: "v" (heat
            # 238081) is the tag for 剪刀手, and the shape predicate rejects it
            # because a single character cannot look like a word. So the
            # dictionary gets the final say - it is direct evidence, whereas the
            # shape test is only a heuristic.
            low = term.lower()
            # Danbooru 标签全是小写；先把输入归一化再判断，
            # 否则 "V" 会因为 is_known_tag 内部转小写而通过，却把大小写原样返回。
            if _LOOKS_LIKE_TAG(low) or self.is_known_tag(low):
                self._cache[term] = low
                return low
            self._cache[term] = None
            return None

        # layer 1: curated, then supplement, then layer 2 exact match on
        # canonicalised forms.
        # Deliberately NOT falling back to LIKE '%term%': a fuzzy match on a
        # short Chinese term produces confident nonsense (红眼 -> "red-eye
        # effect", 蓝色 -> "blue helmet"). Unmatched terms are reported to the
        # user instead of silently mistranslated.
        en = None
        for cand in [term] + _canonical(term):
            if cand in self.base:
                en = self.base[cand]
                break
            if cand in _SUPPLEMENT:
                en = _SUPPLEMENT[cand]
                break
            if cand in self.reverse:
                en = self.reverse[cand]
                break
            hit = self._resolve(cand)
            if hit:
                en = hit
                break

        self._cache[term] = en
        return en

    # -------------------------------------------------------------- translate
    def translate(self, text: str):
        resolved: list[tuple[str, str]] = []
        unknown: list[str] = []
        for chunk in re.split(r"[,，、;；\n]+", text):
            chunk = chunk.strip()
            if not chunk:
                continue
            for term in self._segment(chunk):
                en = self.lookup(term)
                if en:
                    resolved.append((term, en))
                else:
                    unknown.append(term)
        return ", ".join(en for _t, en in resolved), resolved, unknown

    def translate_detailed(self, text: str) -> dict:
        """translate() 的完整版：额外给出「手打英文里不是真标签的」。

        translate() 保持原样返回三元组，因为它的调用点很多（生成、局部重绘、
        测试），不改签名更安全。需要提示信息的地方改用这个。
        """
        english, resolved, unknown = self.translate(text)
        return {"english": english,
                "resolved": resolved,
                "unknown": unknown,
                "not_tags": self.unknown_ascii_terms(resolved)}

    # ------------------------------------------------------------- validation
    def is_known_tag(self, tag: str) -> bool:
        """Does this English tag actually exist in the Danbooru dictionary?

        Used to tell a real prompt from garbage. Guessing from string shape is
        unreliable (a 3-letter consonant run looks like a word); the local
        328k-entry table is authoritative and costs one indexed lookup.
        """
        t = str(tag).strip().lower()
        if not t:
            return False
        cached = self._known_cache.get(t)
        if cached is not None:
            return cached
        hit = False
        try:
            key = t.replace(" ", "_")
            if self.conn is None:
                raise RuntimeError("no db")
            row = self.conn.execute(
                "SELECT 1 FROM tags WHERE name_key = ? OR name = ? LIMIT 1",
                (t.replace("_", " "), key)).fetchone()
            hit = row is not None
            if not hit and t in self.base:
                hit = True
            if not hit and t in _SUPPLEMENT.values():
                hit = True
            if not hit and t in self.reverse:
                hit = True
        except Exception:
            hit = False
        self._known_cache[t] = hit
        return hit

    def unknown_ascii_terms(self, resolved: list) -> list:
        """用户手打的英文里，哪些**任何一层都查不到**。

        界面用它显示「不是词库里的标签」提示。这个判断之所以可信，
        是因为 is_known_tag 会依次查四条路径：词库(name/name_key)、
        base、_SUPPLEMENT 的值、倒排索引。

        踩过的坑：我第一次做这个检查时用「词库直查」当依据，得出
        「79 个值查不到」的结论，并据此以为会对 hands / eyes / face 这种
        正常提示词误报 —— 于是把功能撤回了。实际 is_known_tag 有兜底，
        这些词都返回 True。**拿一个函数的标准去评判另一个函数的输出**，
        是这次误判的根源。

        误报率实测（撤回后又量了一遍才恢复这个功能）：
          * 面板标签随机组合 400 条提示词 -> 0 条误报
          * 手打常见提示词（含 hands/eyes/face/waist 等）-> 0 条误报
          * 只有 nsfw 这类"分级标签"会被标出来，而它确实不是 Danbooru 语义标签

        只统计「用户自己打进来的英文」，不统计中文翻出来的结果：
        中文 -> 英文是词库权威给出的映射，不该拿同一个词库去质疑它。
        """
        out = []
        seen = set()
        for src, en in resolved or []:
            if not en:
                continue
            # 源词是中文 => 这是翻译结果，不是手打的英文
            if re.search(r"[\u4e00-\u9fff]", str(src)):
                continue
            low = str(en).strip().lower()
            if not low or low in seen:
                continue
            seen.add(low)
            if not self.is_known_tag(low):
                out.append(low)
        return out

    def count_known(self, english: str) -> tuple:
        """(known, total) tag counts for an assembled English prompt.

        NOTE: 目前没有调用点。保留是因为它和 unknown_ascii_terms 是同一件事的
        两个角度（一个给计数、一个给名单），数值口径一致，将来要用不会再写一遍。
        """
        parts = [p.strip() for p in str(english).split(",") if p.strip()]
        known = sum(1 for p in parts if self.is_known_tag(p))
        return known, len(parts)

    # ---------------------------------------------------------------- private
    def _exact(self, variant: str) -> bool:
        """True when the dictionary carries this exact Chinese term.

        带缓存：_segment 在切分时对每个候选都要问一次，而这里每次都查库。
        实测「一个女孩蓝色头发微笑」这种 12 字串要跑约 40 次查询 = 25ms/次，
        是整个翻译链最慢的一环（界面每敲一次都要走一遍）。
        判定结果只取决于词库内容，进程内不会变，所以缓存是安全的 ——
        和 lookup 的 _cache 同一个道理。
        """
        cached = self._exact_cache.get(variant)
        if cached is not None:
            return cached
        if variant in self.base or variant in _SUPPLEMENT or variant in self.reverse:
            self._exact_cache[variant] = True
            return True
        row = self.conn.execute(
            "SELECT name FROM tags WHERE chinese = ? AND category_id IN (0,5) LIMIT 1",
            (variant,),
        ).fetchone()
        hit = bool(row)
        self._exact_cache[variant] = hit
        return hit

    def _resolve(self, variant: str) -> str | None:
        """One candidate form -> English tag, no normalization applied."""
        if variant in self.base:
            return self.base[variant]
        if self.conn is None:
            return
        for r in self.conn.execute(
            "SELECT name FROM tags WHERE chinese = ? AND category_id IN (0,5) "
            "ORDER BY post_count DESC LIMIT 4", (variant,),
        ):
            if not REJECTED_NAMES.search(r["name"]):
                return _pretty(r["name"])
        return None

    def _segment(self, chunk: str) -> list[str]:
        if _is_ascii(chunk):
            return [chunk] if self.lookup(chunk) else chunk.split()

        out: list[str] = []
        i, n = 0, len(chunk)
        while i < n:
            # skip whitespace and pure filler particles
            # ★ 只放「几乎不参与构词」的字。实测教训：
            #   「着」不能放 —— 着陆/着衣 会被拆成 ['陆']、['衣']；
            #   「中」「内」更不能放 —— 中出/内衣/内裤 都以它们开头。
            # 定位/助词改用 _SUPPLEMENT 给落点，整条词仍然优先匹配。
            if chunk[i].isspace() or chunk[i] in "的了个":
                i += 1
                continue

            best: str | None = None
            for L in range(min(self._max_cn, n - i), 0, -1):
                cand = chunk[i:i + L]
                if not self.lookup(cand):
                    continue
                if best is None:
                    best = cand
                # Authoritative whole-word entries are never split: 猫耳 is a
                # real dictionary entry and must not degrade to 猫 + 耳.
                if (cand in _SUPPLEMENT or cand in self.base
                        or cand in self.reverse or self._exact(cand)):
                    best = cand
                    break
                # A long match may be swallowing two separate concepts
                # (银色长发 = 银色 + 长发). Prefer the longest prefix that still
                # resolves and continue from there.
                if L >= 2 and self.lookup(chunk[i:i + L - 1]):
                    best = chunk[i:i + L - 1]
                    break
                best = cand
                break

            if best:
                out.append(best)
                i += len(best)
            else:
                out.append(chunk[i])
                i += 1
        return out


if __name__ == "__main__":
    import sys

    tr = Translator()
    print("整理词库:", len(tr.base), "条")
    print("大型词库:", _DB_PATH)
    print()
    probes = sys.argv[1:] or [
        "双马尾",
        "白发红眼",
        "微笑",
        "哥特萝莉装",
        "一个女孩蓝色头发微笑",
        "黑丝过膝袜 校服 猫耳",
    ]
    for p in probes:
        en, res, unk = tr.translate(p)
        print("输入:", p)
        print("英文:", en or "(未解析)")
        if unk:
            print("未收录:", " ".join(unk))
        print()
