"""Poisson (gradient-domain) blending: does it fix the colour drift?

Measured baseline: a VAE round-trip alone shifts colour by ~1.2/255 mean with
peaks of 31/255, and the redrawn mask interior never matches the original's
chroma. Plain alpha compositing (ImageCompositeMasked) cannot correct that -
it just pastes, so any global shift inside the mask shows up as a visible
rectangle and as changed eye colour.

Poisson blending solves for new pixel values whose GRADIENTS match the redrawn
content (so texture/detail survive) while their absolute values match the
surrounding original (so colour stops drifting). cv2.seamlessClone implements
it, and cv2 is already present - no ComfyUI node install needed.
"""
import os

import cv2
import numpy as np


def imread_u(path, flags=cv2.IMREAD_COLOR):
    """读图，**支持含中文/空格的路径**。

    ★ 为什么不能直接用 cv2.imread：cv2 在 Windows 上用 ANSI 代码页打开文件，
      路径里有非 ASCII 字符就读不出来。实测（本机 cp936）：
          cv2.imread("中文.png")       -> None（读不了）
          cv2.imread("图片 带空格.png") -> None
      而**纯 ASCII 路径正常** —— 所以这个 bug 在开发者自己机器上不会出现，
      用户的产出目录一旦是中文（很常见）才会撞上。

      做法：让 Python 按字节读文件（它认 Unicode 路径），再交给 cv2 解码。
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_u(path, img):
    """写图，**支持含中文/空格的路径**。成功返回 True。

    ★ 为什么不能直接用 cv2.imwrite：它在中文路径下**返回 True 但什么都不写**。
      实测：
          cv2.imwrite("out_中文.png", img) -> True，但文件根本不存在
      这是最坏的一种失败 —— 调用方以为写成功了，用户看到的却是一张裂图。
      （纯 ASCII 路径正常，所以同样是"只有用户会撞上"。）

      做法：先编码到内存，再用 Python 按字节写盘。
    """
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    try:
        buf.tofile(path)
    except OSError:
        return False
    return True


def copy_png_text(src_path, dst_path, keys=("prompt", "workflow", "parameters")):
    """把 src 的 PNG 文本块搬到 dst 上，返回搬过去的键名列表。

    ★ 为什么必须有这个：`imwrite_u` 走的是 `cv2.imencode`，**写出来的 PNG 不带
      任何文本块**。于是 Poisson 融合的结果——本应用自己刚生成的那张图——在
      「从成品图读回参数」里被判定成"这张图里没有生成参数…被聊天软件/图床转过
      存？"，把责任推给了用户。实测见 dev/probe_read_meta.py。

      只搬文本块，不重编码像素（PIL 打开后原样另存，PNG 本身无损）。
      搬不动就算了：这是锦上添花，不该让一次成功的重绘整体失败。
    """
    from PIL import Image, PngImagePlugin

    moved = []
    try:
        with Image.open(src_path) as s:
            info = PngImagePlugin.PngInfo()
            for k in keys:
                v = s.info.get(k)
                if isinstance(v, str) and v:
                    info.add_text(k, v)
                    moved.append(k)
        if not moved:
            return []
        tmp = dst_path + ".meta.png"
        with Image.open(dst_path) as d:
            d.save(tmp, pnginfo=info)
        os.replace(tmp, dst_path)
    except Exception:
        try:
            if os.path.exists(dst_path + ".meta.png"):
                os.remove(dst_path + ".meta.png")
        except OSError:
            pass
        return []
    return moved


def poisson_blend(original_path, redrawn_path, mask_path, out_path,
                  mode="mixed"):
    """Blend the redrawn region into the original using Poisson editing."""
    dst = imread_u(original_path, cv2.IMREAD_COLOR)
    src = imread_u(redrawn_path, cv2.IMREAD_COLOR)
    m = imread_u(mask_path, cv2.IMREAD_GRAYSCALE)
    if dst is None or src is None or m is None:
        raise ValueError("读图失败")
    if src.shape != dst.shape:
        src = cv2.resize(src, (dst.shape[1], dst.shape[0]))
    if m.shape != dst.shape[:2]:
        m = cv2.resize(m, (dst.shape[1], dst.shape[0]),
                       interpolation=cv2.INTER_NEAREST)

    # seamlessClone needs a mask whose bounding rect fits inside the image, so
    # work on a padded crop around the mask instead of the whole canvas.
    ys, xs = np.where(m > 127)
    if len(xs) == 0:
        raise ValueError("蒙版是空的")
    pad = 24
    x0 = max(0, xs.min() - pad); y0 = max(0, ys.min() - pad)
    x1 = min(dst.shape[1], xs.max() + pad); y1 = min(dst.shape[0], ys.max() + pad)
    d_crop = dst[y0:y1, x0:x1].copy()
    s_crop = src[y0:y1, x0:x1].copy()
    m_crop = m[y0:y1, x0:x1].copy()
    # Keep a pristine copy of the destination BEFORE seamlessClone runs. The
    # earlier version reused d_crop afterwards, but seamlessClone mutates its
    # destination argument, so when source and destination were the same file
    # the two writes cancelled out and the blend measured as 0 change.
    orig_crop = d_crop.copy()

    # soften the mask edge so the gradient solve has room to transition
    k = max(3, (min(m_crop.shape) // 40) | 1)
    m_soft = cv2.GaussianBlur(m_crop, (k, k), 0)
    m_hard = (m_crop > 127).astype(np.uint8) * 255

    centre = (m_crop.shape[1] // 2, m_crop.shape[0] // 2)
    flags = {"normal": cv2.NORMAL_CLONE,
             "mixed": cv2.MIXED_CLONE,
             "mono": cv2.MONOCHROME_TRANSFER}.get(mode, cv2.MIXED_CLONE)
    try:
        blended = cv2.seamlessClone(s_crop, d_crop, m_hard, centre, flags)
    except cv2.error as e:
        raise RuntimeError("seamlessClone 失败: %s" % e)

    out = dst.copy()
    out[y0:y1, x0:x1] = blended
    # outside the mask keep the ORIGINAL pixels exactly: seamlessClone can
    # touch the border of its crop, and those pixels must not move
    keep = (m_soft < 8)
    region = out[y0:y1, x0:x1]
    region[keep] = orig_crop[keep]
    out[y0:y1, x0:x1] = region
    # imwrite_u 失败时返回 False（cv2.imwrite 则可能在中文路径下"假成功"）。
    # 不检查的话调用方会以为写成功了，用户看到的却是一张裂图。
    if not imwrite_u(out_path, out):
        raise RuntimeError("写入失败（路径不可写或格式不支持）: %s" % out_path)
    return out_path


if __name__ == "__main__":
    print("模块加载 OK；seamlessClone 可用:", hasattr(cv2, "seamlessClone"))
