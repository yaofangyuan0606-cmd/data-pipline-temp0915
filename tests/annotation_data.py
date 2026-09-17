"""Small real label volumes with disconnected islands sharing the same ids."""
import json

import numpy as np


IDS = (9007199254740993, 22, 33, 44)
POINTS = ((4, 4), (20, 4), (36, 4), (52, 4))


def write_pairs(path):
    path.mkdir(parents=True)
    em = np.full((64, 32, 2), 128, dtype=np.uint8)
    seg = np.zeros(em.shape, dtype=np.uint64)
    for i, label in enumerate(IDS):
        x = 2 + i * 16
        seg[x:x + 8, 2:10, :] = label
        seg[x:x + 8, 18:26, :] = label
    seg[10, 10, :] = IDS[0]  # diagonal contact must not join a 4-connected island
    np.save(path / "em.npy", em)
    np.save(path / "seg.npy", seg)
    (path / "meta.json").write_text(json.dumps({"dataset": {"id": "pair-regression"}}))
    return seg
