# -*- coding: utf-8 -*-
"""放大目标尺寸必须是**等比**的。

为什么专门测：下游是 ImageScale + crop="disabled"，那是拉伸语义 —— 传进去的
宽高比例和原图不一致，画面就被压扁/拉长，而且**不报任何错**。
原来的实现是 tw/th 各自 min(..., 4096)，16:9 的 1344×768 选 4× 时变成
4096×3072（比例 1.75 → 1.33），整张图横向压扁 24%，用户实测报过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from server import MAX_UPSCALE_SIDE, upscale_target  # noqa: E402

fails = []


def check(cond, label):
    print("   %s %s" % ("OK  " if cond else "FAIL", label))
    if not cond:
        fails.append(label)


print("=" * 74)
print("【1】比例必须保住（不允许为了塞进上限把某一维单独砍掉）")
CASES = [
    # 源尺寸, 倍率, 期望的长边或(宽,高)
    ((1344, 768), 2, (2688, 1536)),
    ((1344, 768), 4, (4096, 2341)),      # ★ 原实现在这里是 4096×3072，错的
    ((1024, 768), 4, (4096, 3072)),      # 4:3 正好顶到上限，应当原样
    ((896, 1152), 4, (3186, 4096)),
    ((1024, 1024), 4, (4096, 4096)),
    ((768, 1344), 4, (2341, 4096)),
    ((1344, 768), 1.5, (2016, 1152)),
    ((512, 512), 8, (4096, 4096)),
]
for (w0, h0), f, want in CASES:
    got = upscale_target(w0, h0, f)
    check(got == want, "%4d×%-4d @%s×  ->  %s×%s（期望 %s×%s）"
          % (w0, h0, f, got[0], got[1], want[0], want[1]))

print()
print("【2】长边不许超过上限，且不超得莫名其妙")
for (w0, h0, f) in ((1344, 768, 4), (1024, 768, 4), (896, 1152, 4),
                    (768, 1344, 4), (1024, 1024, 4), (2048, 2048, 2)):
    tw, th = upscale_target(w0, h0, f)
    check(max(tw, th) <= MAX_UPSCALE_SIDE,
          "%4d×%-4d @%s× -> %d×%d 长边 ≤ %d" % (w0, h0, f, tw, th,
                                                MAX_UPSCALE_SIDE))

print()
print("【3】比例误差要小到看不出来（< 0.5%）")
import itertools  # noqa: E402
worst = 0.0
worst_case = None
for (w0, h0) in itertools.product((512, 768, 896, 1024, 1152, 1344, 1360),
                                  (512, 768, 896, 1024, 1152, 1344, 1360)):
    for f in (1.5, 2, 3, 4):
        tw, th = upscale_target(w0, h0, f)
        err = abs((tw / th) - (w0 / h0)) / (w0 / h0)
        if err > worst:
            worst, worst_case = err, (w0, h0, f, tw, th)
check(worst < 0.005, "最大比例误差 %.4f%%（%s）" % (worst * 100, worst_case))

print()
print("【4】异常输入不能崩")
for bad in ((0, 0, 2), (100, 100, 0), (100, 100, -1), (100, 100, None)):
    try:
        r = upscale_target(*bad)
        check(all(isinstance(v, int) and v >= 8 for v in r),
              "upscale_target%s -> %s（合法值）" % (bad, r))
    except Exception as e:
        check(False, "upscale_target%s 抛了 %s: %s" % (bad, type(e).__name__, e))

print()
if fails:
    print("RESULT: FAIL（%d 项）" % len(fails))
    sys.exit(1)
print("RESULT: ALL PASS")
