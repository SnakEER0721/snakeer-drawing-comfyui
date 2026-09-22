"""回归：中文翻译的 sqlite 连接不许跨线程共用（P0-1 / P0-2）。

上游 Bug 排查报告（2026-09-20，用户机器上跑的）第一条就是它，实测复现：

    | 并发 | 失败率（修复前）|
    |   1  |  0.0%   ← 对照，证明不是输入问题 |
    |   2  |  0.0%   |
    |   4  |  5.0%   |
    |  16  | 46.0%   |   ← 272 次 InterfaceError + 170 次 IndexError

根因：`Translator` 是单例，服务端是 ThreadingHTTPServer，
而连接是 `sqlite3.connect(..., check_same_thread=False)` —— 一个连接被所有
线程共用且没有锁。`InterfaceError: bad parameter or other API misuse`
就是这个用法的标志性报错。

为什么这个测试不是"跑一遍看失败率"：
    失败率是**概率性**的。修复前 2 并发也测出 0%，用概率做断言会变成
    偶发变红的垃圾测试。改成**确定性**断言：
      1. 两个线程拿到的连接对象必须不同（结构性证据，不看运气）
      2. 单线程要连接复用（否则每个词都新建连接，性能白丢）
      3. 并发打一批查询，一条异常都不许有（真跑，但不拿百分比当断言）
      4. 词库缺失时中文翻译必须能降级工作（P0-2）

A/B 对照（修复前跑这个文件）：第 1 节必红 —— 两个线程拿到同一个 id。
"""
import io
import os
import sys
import threading

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cn_translate as CT  # noqa: E402

FAILED = []


def check(name, ok, detail=""):
    print("   %s %s%s" % ("OK  " if ok else "FAIL", name,
                          ("  " + str(detail)) if detail else ""))
    if not ok:
        FAILED.append(name)


print("=" * 74)
print("1. 每个线程必须拿到自己的连接（不能共用一个）")
print("=" * 74)
t = CT.Translator()
check("词库在线（这个测试需要真词库）", not t.db_missing, "db_missing=%s" % t.db_missing)

ids = {}


def grab(who):
    c = t.conn
    ids[who] = id(c) if c is not None else None


main_conn = t.conn
ids["main"] = id(main_conn) if main_conn is not None else None

th = [threading.Thread(target=grab, args=("t%d" % i,)) for i in range(3)]
for x in th:
    x.start()
for x in th:
    x.join()

print("   各线程拿到的连接 id: %r" % ids)
alive = [v for v in ids.values() if v is not None]
check("三个子线程的连接两两不同",
      len({ids["t0"], ids["t1"], ids["t2"]}) == 3,
      "%d 个不同 id" % len({ids["t0"], ids["t1"], ids["t2"]}))
check("子线程的连接 ≠ 主线程的连接", len(set(alive)) == len(alive),
      "%d 个 id 去重后 %d 个" % (len(alive), len(set(alive))))

print()
print("=" * 74)
print("2. 同一个线程要复用连接（每词新建连接会白丢性能）")
print("=" * 74)
a = t.conn
b = t.conn
check("连续两次取到同一个连接对象", a is b, "id %s vs %s" % (id(a), id(b)))

print()
print("=" * 74)
print("3. 并发查询：一条异常都不许有")
print("=" * 74)
# 互不相同的中文，强制走查库路径（_exact_cache 会吃掉重复的）
WORDS = ["银发", "长发", "短发", "双马尾", "呆毛", "侧脸", "微笑", "哭泣",
         "闭眼", "脸红", "眼镜", "耳环", "项链", "水手服", "和服", "旗袍",
         "围巾", "手套", "长靴", "丝袜", "领带", "蝴蝶结", "蕾丝", "披风",
         "站着", "坐着", "躺着", "跪着", "跑着", "跳着", "靠着", "蹲着",
         "窗边", "教室", "走廊", "天台", "公园", "海边", "森林", "雪地",
         "樱花", "落叶", "星空", "夕阳", "月光", "逆光", "阴影", "雾气"]
errors = []
seen = set()
lock = threading.Lock()
ROUNDS = 12


def hammer(tid):
    for k in range(ROUNDS):
        word = "%s%d" % (WORDS[(tid * ROUNDS + k) % len(WORDS)], tid * 100 + k)
        try:
            t.translate(word)
        except Exception as e:
            with lock:
                errors.append("%s: %s" % (type(e).__name__, e))
                seen.add(type(e).__name__)


threads = [threading.Thread(target=hammer, args=(i,)) for i in range(16)]
for x in threads:
    x.start()
for x in threads:
    x.join()

total = 16 * ROUNDS
print("   16 线程 × %d 次 = %d 次翻译" % (ROUNDS, total))
check("零异常", not errors,
      "%d 次异常: %s" % (len(errors), sorted(seen)) if errors else "0 次")

# 单独盯住这两个签名 —— 它们就是"连接被跨线程共用"的标志
check("没有 InterfaceError（共用连接的标志报错）",
      "InterfaceError" not in seen, sorted(seen))
check("没有 IndexError（同上，第二个签名）",
      "IndexError" not in seen, sorted(seen))

print()
print("=" * 74)
print("4. 词库缺失时中文翻译必须降级工作（P0-2）")
print("=" * 74)
# 这是全文件唯一漏掉 `if self.conn is None` 保护的地方，而它被 _segment 的
# 分词循环调用 —— 任何含中文的输入都必经此处。修复前 6000 次调用 0 次成功。
_orig = CT._DB_PATH
try:
    CT._DB_PATH = os.path.join(os.path.dirname(_orig), "_不存在的词库.sqlite3")
    t2 = CT.Translator()
    check("降级路径如期触发", t2.db_missing is True, "db_missing=%s" % t2.db_missing)
    try:
        out = t2.translate("银发少女 站在窗边")
        en = out[0] if isinstance(out, tuple) else out
        check("中文输入不再抛异常", True, repr(en)[:60])
        check("降级后仍能翻出东西（不是空框）", bool(en), repr(en)[:60])
    except Exception as e:
        check("中文输入不再抛异常", False, "%s: %s" % (type(e).__name__, e))
finally:
    CT._DB_PATH = _orig

print()
if FAILED:
    print("RESULT: FAIL  失败 %d 项: %s" % (len(FAILED), "; ".join(FAILED)))
    sys.exit(1)
print("RESULT: ALL PASS")
sys.exit(0)
