# -*- coding: utf-8 -*-
"""poisson_blend 的单元测试 —— 合成图，不连服务、不占 GPU、秒级。

为什么补这一条：
    这个模块是**发布的**、在做像素运算，但此前**只有慢速测试碰到过它**
    （test_compare_feature.py 真实出图那一条）和实验脚本 exp_poisson.py。
    fast 层里引用它的两个文件（test_imports / test_static）只是审计导入和
    静默异常，**不验证它算得对不对**。
    也就是说：融合逻辑写错了，只有跑几分钟 GPU 的慢速测试才会发现。

钉住的不变量：
  · 输出尺寸 == 原图尺寸（不能因为裁剪而变小）
  · 蒙版**远处**的像素逐字节不变（这是 seamlessClone 最容易破坏的一条）
  · 蒙版内确实变了（不然"融合"等于没做）
  · 三种 mode 都能跑
  · 源图尺寸不一致时会 resize，不报错
  · 空蒙版 / 坏图 / 写不进去，都要**抛异常**而不是静默返回

最后那条来自代码里记着的一个真 bug：源图和目标图是同一个文件时，
seamlessClone 会改写它的目标参数，两次写入互相抵消，融合结果等于没变。

跑法：
    python tests/test_poisson_blend.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np  # noqa: E402
from PIL import Image, PngImagePlugin  # noqa: E402

import poisson_blend as PB  # noqa: E402

FAILS = []
OKS = []


def _pnginfo(text):
    """造一个带 prompt 文本块的 PngInfo（模拟 ComfyUI 写进 PNG 的东西）。"""
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", text)
    return info


def _png_text(path, key):
    """读一张 PNG 的某个文本块；没有就返回 None。"""
    with Image.open(path) as im:
        return im.info.get(key)


def check(cond, msg):
    (OKS if cond else FAILS).append(msg)
    print("  %s %s" % ("[OK]  " if cond else "[FAIL]", msg))


def make_base(w=200, h=160):
    """一张有渐变和纹理的原图 —— 纯色会让"变没变"测不出来。"""
    a = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        for x in range(w):
            a[y, x] = ((x * 3) % 256, (y * 5) % 256, ((x + y) * 2) % 256)
    return a


def make_mask(w=200, h=160, box=(80, 60, 120, 100)):
    m = np.zeros((h, w), np.uint8)
    x0, y0, x1, y1 = box
    m[y0:y1, x0:x1] = 255
    return m


print("=" * 74)
print("poisson_blend 单元测试")
print("=" * 74)

tmp = tempfile.mkdtemp(prefix="snakeer_pb_")
try:
    base = make_base()
    mask = make_mask()
    # 重绘内容：和原图明显不同（否则"变了"测不出来）
    redrawn = np.clip(255 - base, 0, 255).astype(np.uint8)

    p_base = os.path.join(tmp, "base.png")
    p_red = os.path.join(tmp, "redrawn.png")
    p_mask = os.path.join(tmp, "mask.png")
    Image.fromarray(base[:, :, ::-1]).save(p_base)      # cv2 是 BGR
    Image.fromarray(redrawn[:, :, ::-1]).save(p_red)
    Image.fromarray(mask).save(p_mask)

    # ---------------------------------------------------------------- 1
    print()
    print("【1】基本融合（mixed）")
    out = os.path.join(tmp, "out_mixed.png")
    PB.poisson_blend(p_base, p_red, p_mask, out, mode="mixed")
    check(os.path.isfile(out), "产出文件写出来了")

    import cv2
    o = PB.imread_u(out, cv2.IMREAD_COLOR)
    d = PB.imread_u(p_base, cv2.IMREAD_COLOR)
    check(o is not None, "产出能被 cv2 读回来")
    check(o.shape == d.shape,
          "输出尺寸 == 原图尺寸（%s）" % (o.shape,))

    # ★ 要检查的是「蒙版外、但**落在裁剪区内**」的像素 —— 那才是保护逻辑管的地方。
    #
    #   第一版我检查的是左上角 30×30。A/B 试出来它没用：故意把 poisson_blend 里
    #   那段 `keep = (m_soft < 8)` 的保护删掉，测试**照样全绿** —— 因为左上角离
    #   蒙版太远，seamlessClone 只在蒙版周围 pad=24 的裁剪区里工作，压根碰不到那里。
    #   断言必须落在**会被影响**的区域上，否则它只是永远为真。
    #
    #   裁剪区 = 蒙版外扩 24px：x[80-24, 120+24] = [56,144], y[60-24,100+24] = [36,124]
    #   下面取蒙版左侧、紧贴裁剪内边的一条：x[58,78], y[38,118]
    near_mask = (slice(38, 118), slice(58, 78))
    same_near = np.array_equal(o[near_mask], d[near_mask])
    check(same_near, "蒙版外、裁剪区内的像素逐字节不变（x58-78）")
    if not same_near:
        diff = np.abs(o[near_mask].astype(int) - d[near_mask].astype(int))
        check(False, "  最大差异 %d，改动像素 %d 个"
              % (diff.max(), int((diff.sum(axis=2) > 0).sum())))

    # 远处当然也不能变（这条容易通过，但便宜）
    far = (slice(0, 30), slice(0, 30))
    check(np.array_equal(o[far], d[far]), "蒙版远处（左上角 30×30）也不变")

    # 蒙版内必须变了
    inner = (slice(70, 90), slice(90, 110))
    inner_diff = np.abs(o[inner].astype(int) - d[inner].astype(int)).mean()
    check(inner_diff > 3.0,
          "蒙版内确实变了（平均差异 %.1f，应该明显 > 0）" % inner_diff)

    # ---------------------------------------------------------------- 1b
    print()
    print("【1b】蒙版贴边时，蒙版外的像素仍然不许被动（保护逻辑只管这里）")
    #
    # ★ 这一段是这次才补上的，来历值得记：
    #   我一开始只在蒙版**居中**的情况下断言"蒙版外不变"。做 A/B（故意把
    #   poisson_blend 里那段 `keep = (m_soft < 8)` 保护删掉）时，测试**照样全绿**
    #   —— 因为蒙版在中间时 seamlessClone 自己就不碰外面，那段保护根本没用上。
    #   用 dev/probe_poisson_keep.py 逐个位置量过：**蒙版贴边**时保护才起作用
    #   （实测最多改动 585 个像素、最大差 168）。代码注释里那句
    #   "seamlessClone can touch the border of its crop" 是对的。
    for label, box in (("贴左上角", (0, 0, 40, 40)),
                       ("贴右下角", (160, 120, 200, 160)),
                       ("贴上边界", (80, 0, 120, 40))):
        m2 = np.zeros((base.shape[0], base.shape[1]), np.uint8)
        m2[box[1]:box[3], box[0]:box[2]] = 255
        p_m2 = os.path.join(tmp, "mask_edge_%s.png" % label)
        Image.fromarray(m2).save(p_m2)
        o2p = os.path.join(tmp, "out_edge_%s.png" % label)
        try:
            PB.poisson_blend(p_base, p_red, p_m2, o2p)
            o2 = PB.imread_u(o2p, cv2.IMREAD_COLOR)
        except Exception as e:
            check(False, "%s: 抛异常 %s: %s" % (label, type(e).__name__, e))
            continue
        # 蒙版之外**离得远**的像素一个都不能动。
        #
        # ★ 不能断言"蒙版外逐字节不变" —— 那是错的期望值。
        #   实测：蒙版边缘**紧邻的 1 像素**会变（m_soft 是高斯模糊过的，
        #   边缘那圈模糊值 > 8，本来就不在 keep 的保护范围内，那是留给融合
        #   过渡用的）。所以正确的不变量是「改动**只允许**发生在蒙版边缘」。
        #   第一版断言写成"一个像素都不许动"，于是贴边用例全红 ——
        #   代码是对的，是我的期望值错了（这个项目为这类事专门吃过亏）。
        outside = (m2 == 0)
        dd = np.abs(o2.astype(int) - d.astype(int)).sum(axis=2)
        changed = (dd > 0) & outside
        # 蒙版外扩 4px（±2）：这个范围内允许过渡，之外一个都不许动
        near = cv2.dilate((m2 > 0).astype(np.uint8),
                          np.ones((5, 5), np.uint8)) > 0
        far_changed = int((changed & ~near).sum())
        if far_changed == 0:
            check(True, "%s：蒙版外 2px 以外的像素一个都没动"
                        "（边缘 %d 个过渡像素属正常）"
                  % (label, int(changed.sum())))
        else:
            check(False, "%s：蒙版外 2px 以外被改动了 %d 个像素（最大差 %d）"
                  % (label, far_changed,
                     int(dd[changed & ~near].max())))

    # ---------------------------------------------------------------- 2
    print()
    print("【2】三种 mode 都能跑，且结果互不相同")
    outs = {}
    for mode in ("normal", "mixed", "mono"):
        p = os.path.join(tmp, "out_%s.png" % mode)
        try:
            PB.poisson_blend(p_base, p_red, p_mask, p, mode=mode)
            outs[mode] = PB.imread_u(p, cv2.IMREAD_COLOR)
            check(True, "mode=%s 跑通" % mode)
        except Exception as e:
            check(False, "mode=%s 抛异常: %s: %s" % (mode, type(e).__name__, e))
    if len(outs) >= 2:
        ks = sorted(outs)
        a, b = outs[ks[0]], outs[ks[1]]
        check(a.shape == b.shape and not np.array_equal(a, b),
              "%s 和 %s 的结果不同（说明 mode 真的生效了）" % (ks[0], ks[1]))
    # 不认识的 mode 要退回 mixed，不能崩
    p = os.path.join(tmp, "out_weird.png")
    try:
        PB.poisson_blend(p_base, p_red, p_mask, p, mode="这不是一个mode")
        check(os.path.isfile(p), "未知 mode 退回默认（不崩）")
    except Exception as e:
        check(False, "未知 mode 抛异常了: %s" % e)

    # ---------------------------------------------------------------- 3
    print()
    print("【3】源图/蒙版尺寸和原图不一致时要能对上")
    small = np.zeros((40, 50, 3), np.uint8)
    small[:, :] = (30, 200, 90)
    p_small = os.path.join(tmp, "small.png")
    Image.fromarray(small[:, :, ::-1]).save(p_small)
    m_small = np.zeros((20, 20), np.uint8); m_small[5:15, 5:15] = 255
    p_msmall = os.path.join(tmp, "mask_small.png")
    Image.fromarray(m_small).save(p_msmall)
    p = os.path.join(tmp, "out_resize.png")
    try:
        PB.poisson_blend(p_base, p_small, p_msmall, p)
        o2 = PB.imread_u(p, cv2.IMREAD_COLOR)
        check(o2.shape == d.shape, "尺寸不一致时自动 resize，输出仍是原图尺寸")
    except Exception as e:
        check(False, "尺寸不一致时抛异常了: %s: %s" % (type(e).__name__, e))

    # ---------------------------------------------------------------- 4
    print()
    print("【4】源图和目标图是同一个文件（修过的回归）")
    # 代码注释记着：seamlessClone 会改写它的目标参数，同一个文件时两次写入
    # 互相抵消，融合结果等于没变。这里用同一张图当源和目标，要求**确实变了**。
    same_in = os.path.join(tmp, "same.png")
    shutil.copyfile(p_base, same_in)
    p = os.path.join(tmp, "out_same.png")
    try:
        PB.poisson_blend(same_in, same_in, p_mask, p)
        o3 = PB.imread_u(p, cv2.IMREAD_COLOR)
        src_now = PB.imread_u(same_in, cv2.IMREAD_COLOR)
        check(src_now is not None, "源文件还在、还能读（没被写坏）")
        # 源＝目标时，融合是在原图上做的，蒙版内应该有变化。
        #
        # 原来这里写的是 `check(inner_diff2 >= 0, ...)` —— 而 np.abs(...).mean()
        # **恒 ≥ 0**，这条断言永远为真。报告（E-2）抓到了它。
        # 更要紧的是：上一段测的正是「seamlessClone 改写目标参数导致两次写入互相
        # 抵消、融合结果等于没变」这个坑 —— 差异恰好为 0 时，`>= 0` 照样放行。
        # 阈值取 1.0 而不是 0：本机实测这个差异是 **10.50**，离 0 很远；
        # 取 1.0 既能挡住"完全没变"，又不会因为平台间浮点/编码微小差别误红。
        inner_diff2 = np.abs(o3[inner].astype(int) - src_now[inner].astype(int)).mean()
        check(inner_diff2 > 1.0,
              "源＝目标时蒙版内确实变了（平均差异 %.2f，要求 > 1.0）" % inner_diff2)
    except Exception as e:
        check(False, "源＝目标时抛异常了: %s: %s" % (type(e).__name__, e))

    # ---------------------------------------------------------------- 5
    print()
    print("【5】坏输入要抛异常，不能静默返回")
    # 空蒙版
    m_black = np.zeros((160, 200), np.uint8)
    p_mb = os.path.join(tmp, "mask_black.png")
    Image.fromarray(m_black).save(p_mb)
    try:
        PB.poisson_blend(p_base, p_red, p_mb, os.path.join(tmp, "x.png"))
        check(False, "空蒙版应该抛「蒙版是空的」")
    except ValueError as e:
        check("蒙版" in str(e), "空蒙版 → ValueError: %s" % str(e)[:30])
    except Exception as e:
        check(False, "空蒙版抛了意料外的异常: %s: %s" % (type(e).__name__, e))

    # 坏图
    p_bad = os.path.join(tmp, "bad.png")
    open(p_bad, "wb").write(b"this is not an image")
    try:
        PB.poisson_blend(p_bad, p_red, p_mask, os.path.join(tmp, "y.png"))
        check(False, "坏图应该抛「读图失败」")
    except ValueError as e:
        check("读图失败" in str(e), "坏图 → ValueError: %s" % str(e)[:30])
    except Exception as e:
        check(False, "坏图抛了意料外的异常: %s: %s" % (type(e).__name__, e))

    # 写不进去（目录不存在）—— cv2.imwrite / imwrite_u 都只返回 False，不抛，
    # 代码必须自己查；不查的话调用方以为写成功了，前端拿到一个不存在的路径。
    try:
        PB.poisson_blend(p_base, p_red, p_mask,
                         os.path.join(tmp, "不存在的目录", "z.png"))
        check(False, "写到不存在的目录应该抛 RuntimeError")
    except RuntimeError as e:
        check("写入失败" in str(e), "写不进去 → RuntimeError: %s" % str(e)[:40])
    except Exception as e:
        check(False, "写不进去抛了意料外的异常: %s: %s" % (type(e).__name__, e))

    # ---------------------------------------------------------------- 6
    print()
    print("【6】中文路径（cv2 自己读不了 / 写不出，实测过）")
    #
    # ★ 这一节是补这条测试时**意外发现的真 bug**：
    #   我随手给输出文件起名 `mask_edge_贴左上角.png`，cv2 就报
    #   "can't open/read file"。查下来（本机 cp936）：
    #       cv2.imread("中文.png")         -> None（读不了）
    #       cv2.imwrite("out_中文.png", x) -> **返回 True，但文件不存在**
    #   纯 ASCII 路径完全正常 —— 所以开发者自己机器上永远撞不到，
    #   用户的产出目录一旦是中文：融合要么报"读图失败"，
    #   要么**以为写成功了**、前端拿到不存在的路径显示裂图。
    #
    #   现在 poisson_blend 走 imread_u / imwrite_u
    #   （Python 按字节读写 + cv2 编解码）。
    d_cn = os.path.join(tmp, "中文目录")
    os.makedirs(d_cn, exist_ok=True)
    p_cn_in = os.path.join(d_cn, "原图 带空格.png")
    p_cn_out = os.path.join(d_cn, "融合结果.png")
    Image.fromarray(base[:, :, ::-1]).save(p_cn_in)
    try:
        PB.poisson_blend(p_cn_in, p_red, p_mask, p_cn_out)
        check(os.path.isfile(p_cn_out),
              "中文+空格路径：输出文件真的写出来了（cv2.imwrite 会假成功）")
        got = PB.imread_u(p_cn_out)
        check(got is not None, "中文路径的产物能读回来")
        if got is not None:
            check(got.shape == base.shape, "尺寸正确（%s）" % (got.shape,))
            far2 = (slice(0, 20), slice(0, 20))
            check(np.array_equal(got[far2], base[far2]),
                  "中文路径下融合结果和 ASCII 路径一致")
    except Exception as e:
        check(False, "中文路径抛异常了: %s: %s" % (type(e).__name__, e))

    p_probe = os.path.join(d_cn, "探针.png")
    okw = PB.imwrite_u(p_probe, base)
    check(okw and os.path.isfile(p_probe),
          "imwrite_u 写中文路径：返回 True **且**文件真的在")
    r_probe = PB.imread_u(p_probe)
    check(r_probe is not None and r_probe.shape == base.shape,
          "imread_u 读中文路径：读回来了且尺寸对")
    check(PB.imread_u(os.path.join(tmp, "根本不存在.png")) is None,
          "imread_u 读不存在的文件 -> None（不抛）")

    # 【7】融合结果要把 ComfyUI 的参数带过去
    #
    # ★ 起因（用户视角实测）：Poisson 融合那张图是本应用自己刚生成的，
    #   丢回「从成品图读回参数」却说"这张图里没有生成参数…被聊天软件/图床转过
    #   存？"—— 把责任推给了用户。根因：imwrite_u 走 cv2.imencode，
    #   写出来的 PNG **不带任何文本块**。见 dev/probe_read_meta.py。
    p_with = os.path.join(d_cn, "带参数.png")
    p_plain = os.path.join(d_cn, "没参数.png")
    work = '{"3": {"class_type": "KSampler"}}'
    Image.fromarray(base[:, :, ::-1]).save(p_with, pnginfo=_pnginfo(work))
    PB.imwrite_u(p_plain, base)
    check(_png_text(p_plain, "prompt") is None,
          "前提：cv2 写出来的 PNG 确实没有参数文本块（这就是 bug 的根）")
    moved = PB.copy_png_text(p_with, p_plain)
    check(moved == ["prompt"], "copy_png_text 搬走了 prompt（实际 %s）" % moved)
    check(_png_text(p_plain, "prompt") == work,
          "搬完之后能从融合结果里读回参数（用户点「读回参数」能用了）")
    check(os.path.isfile(p_plain) and not os.path.exists(p_plain + ".meta.png"),
          "没有留下 .meta.png 临时文件")
    # 反向：源图本来就没参数 -> 什么都不做，也不留临时文件
    p_none = os.path.join(d_cn, "源也没参数.png")
    PB.imwrite_u(p_none, base)
    check(PB.copy_png_text(p_none, p_plain) == [],
          "源图没参数时不搬、返回空列表")
    check(_png_text(p_plain, "prompt") == work, "也不会把已有的参数抹掉")
    check(not os.path.exists(p_plain + ".meta.png"), "依然没有临时文件残留")

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
