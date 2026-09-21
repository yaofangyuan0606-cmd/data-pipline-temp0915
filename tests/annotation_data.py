"""Small real label volumes with disconnected islands sharing the same ids."""
import json

import numpy as np


IDS = (9007199254740993, 22, 33, 44)
# screen points (x = column, y = row). A displayed section is the transpose of the array, so the islands this
# fixture writes at array rows 2+16i..+8 / columns 2..10 appear at screen x = 2+16i.. and screen y = 2..10.
POINTS = ((4, 4), (20, 4), (36, 4), (52, 4))


def write_pairs(path):
    path.mkdir(parents=True)
    em = np.full((64, 32, 2), 128, dtype=np.uint8)
    seg = np.zeros(em.shape, dtype=np.uint64)
    for i, label in enumerate(IDS):
        r = 2 + i * 16  # axis 0 (displayed as screen x); the two islands sit at axis 1 = 2..10 and 18..26
        seg[r:r + 8, 2:10, :] = label
        seg[r:r + 8, 18:26, :] = label
    seg[10, 10, :] = IDS[0]  # diagonal contact must not join a 4-connected island
    np.save(path / "em.npy", em)
    np.save(path / "seg.npy", seg)
    (path / "meta.json").write_text(json.dumps({"dataset": {"id": "pair-regression"}}))
    return seg
