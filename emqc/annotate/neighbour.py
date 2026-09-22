"""跨片取色：这块地方在邻近的切片上是哪个细胞。

分割漏标是常事。一个细胞在某一片被挤扁了，分割没认出来，那一片上它就是 0；隔几片它又好端端地有颜色。
标注员看着那块空白，知道它就是上下片里的那个细胞，但**本片没有颜色可吸**——只能翻到另一片、把十几位的
id 抄下来、再翻回来手输。这个模块就是替这一步：回答「邻片在这块地方是谁」，把 id 交给标注员。

这里**只读**，一个像素都不写。边界画在哪、写哪一片，全部照旧由人决定：拿到 id 之后照常用画笔、填充，
或者用 SAM 点选给出边界再填。这不是自动补标注，是省掉抄 id 那一下。

交付数据里这个缺口有多常见：四个 512 块合计 393,615 个像素属于「上下两片是同一个细胞、中间那片却是 0」，
占全部体素的 0.38%，平均每片约 1,000 个像素。

取色读的是**工作副本**，不是交付原件——邻片如果已经被标注员改过，取到的就是改过之后的颜色，这才是他想要的。
"""
from __future__ import annotations

import numpy as np


def lookup(block, z: int, mask=None, x: int | None = None, y: int | None = None,
           radius: int = 6, top: int = 3) -> dict:
    """邻片在这块地方是哪个 id。给一块掩膜就按掩膜里的多数投票，给一个点就取那一点。

    由近及远地找：先看 z±1，再 z±2……同一距离上下都有标签时，取占比高的那一侧。找到就停，所以返回的
    永远是最近的那一片。没找到返回 {"found": False, ...}，而不是猜一个。"""
    nz, H, W = block.shape_zyx
    if not 0 <= int(z) < nz:
        raise IndexError(f"z {z} 超出数据块的 0..{nz - 1}")
    if not block.has_seg:
        raise ValueError("这个数据块没有标签")
    z = int(z)
    if mask is None:
        if x is None or y is None:
            raise ValueError("要么给一块掩膜，要么给一个点")
        if not (0 <= int(x) < W and 0 <= int(y) < H):
            raise IndexError(f"({x}, {y}) 超出这一片的 {W}x{H}")
        x, y = int(x), int(y)
    else:
        mask = np.asarray(mask, bool)
        if mask.shape != (H, W):
            raise ValueError("掩膜的形状和切片对不上")
        if not mask.any():
            raise ValueError("掩膜是空的")

    searched: list[int] = []
    for d in range(1, int(radius) + 1):
        best = None
        for k in (z - d, z + d):
            if not 0 <= k < nz:
                continue
            searched.append(k)
            plane = block.seg_slice(k)
            vals = plane[mask] if mask is not None else plane[y:y + 1, x:x + 1].ravel()
            ids, counts = np.unique(vals[vals != 0], return_counts=True)
            if not ids.size:
                continue
            order = np.argsort(counts)[::-1]
            share = float(counts[order[0]]) / max(1, vals.size)
            cand = {"id": str(int(ids[order[0]])), "z_src": int(k), "distance": int(abs(k - z)),
                    "share": round(share, 4), "px": int(counts[order[0]]),
                    # 同一块地方在邻片上可能横跨两个细胞；把次要的也报出来，让人看见再决定
                    "others": [{"id": str(int(ids[i])), "px": int(counts[i])} for i in order[1:max(1, top)]]}
            if best is None or cand["share"] > best["share"]:
                best = cand
        if best is not None:
            return {"found": True, **best, "searched": sorted(set(searched)), "radius": int(radius)}
    return {"found": False, "searched": sorted(set(searched)), "radius": int(radius),
            "reason": (f"前后 {radius} 片里，这块地方都没有标签"
                       if searched else "这一片没有相邻切片可看")}
