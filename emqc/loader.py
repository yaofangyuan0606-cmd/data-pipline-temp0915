"""Reference clients for the two delivery paths. Standard library + numpy only, so training / inference code can vendor it.

Training side (local shards written by an export job):

    from emqc.loader import ShardStore
    store = ShardStore("var/exports/mouse_30um")
    for meta, arr in store.shards():          # arr: (n, H, W) memory-mapped
        ...
    patch, meta = store.random_patch(np.random.default_rng(0), (8, 256, 256))

Inference side (stream session: whole volume block by block, nothing copied):

    from emqc.loader import StreamClient
    for item, arr in StreamClient("http://127.0.0.1:8765", "mouse_30um", z_chunk=16, client="unet-infer").items():
        pred = model(arr)                     # arr: (z1-z0, H, W) of one tile
        ...                                   # items are acked as you go; the session is closed at the end
"""
from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import numpy as np


class ShardStore:
    def __init__(self, export_dir: str | Path):
        self.dir = Path(export_dir)
        self.manifest = json.loads((self.dir / "export_manifest.json").read_text())
        self._shards = [s for s in self.manifest["shards"]]
        self._cache: dict[str, np.ndarray] = {}

    @property
    def shard_list(self) -> list[dict]:
        return self._shards

    def load(self, shard: dict) -> np.ndarray:
        f = shard["file"]
        if f not in self._cache:
            p = self.dir / f
            self._cache[f] = np.load(p, mmap_mode="r") if p.suffix == ".npy" else np.stack([np.asarray(__import__("PIL.Image", fromlist=["Image"]).open(q)) for q in sorted(p.glob("*.png"))])
        return self._cache[f]

    def shards(self):
        for s in self._shards:
            yield s, self.load(s)

    def random_patch(self, rng: np.random.Generator, size: tuple[int, int, int]) -> tuple[np.ndarray, dict]:
        """A random (dz, dy, dx) patch; shards are weighted by how many patches they can hold."""
        dz, dy, dx = size
        cands = [s for s in self._shards if s["n"] >= dz and (s["bbox"][3] - s["bbox"][1]) >= dy and (s["bbox"][2] - s["bbox"][0]) >= dx]
        if not cands:
            raise ValueError(f"no shard can hold a patch of size {size}")
        weights = np.array([s["n"] - dz + 1 for s in cands], dtype=float)
        s = cands[int(rng.choice(len(cands), p=weights / weights.sum()))]
        arr = self.load(s)
        z = int(rng.integers(0, s["n"] - dz + 1))
        y = int(rng.integers(0, arr.shape[1] - dy + 1))
        x = int(rng.integers(0, arr.shape[2] - dx + 1))
        meta = {"shard": s["file"], "block_id": s["block_id"], "z0": s["z_start"] + z, "z1": s["z_start"] + z + dz, "y0": s["bbox"][1] + y, "x0": s["bbox"][0] + x}
        return np.ascontiguousarray(arr[z : z + dz, y : y + dy, x : x + dx]), meta

    def iter_patches(self, size: tuple[int, int, int], stride: tuple[int, int, int] | None = None):
        """Deterministic tiling of every shard (for validation)."""
        dz, dy, dx = size
        sz, sy, sx = stride or size
        for s, arr in self.shards():
            for z in range(0, arr.shape[0] - dz + 1, sz):
                for y in range(0, arr.shape[1] - dy + 1, sy):
                    for x in range(0, arr.shape[2] - dx + 1, sx):
                        yield np.ascontiguousarray(arr[z : z + dz, y : y + dy, x : x + dx]), {"shard": s["file"], "block_id": s["block_id"], "z0": s["z_start"] + z, "y0": s["bbox"][1] + y, "x0": s["bbox"][0] + x}


class StreamClient:
    def __init__(self, api: str, dataset_id: str, z_chunk: int = 16, client: str | None = None, order: str = "z", skip_failed: bool = False, block_ids: list[str] | None = None, batch: int = 2):
        self.api = api.rstrip("/")
        self.batch = batch
        body = {"dataset_id": dataset_id, "z_chunk": z_chunk, "client": client, "order": order, "skip_failed": skip_failed, "block_ids": block_ids}
        self.session = self._post("/api/v1/streams", body)
        self.stream_id = self.session["stream_id"]

    def _post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.api + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())

    def fetch(self, item: dict) -> np.ndarray:
        with urllib.request.urlopen(self.api + item["url"], timeout=600) as r:
            return np.load(io.BytesIO(r.read()))

    def items(self):
        """Yield (item, array) for the whole plan, acking each item after the caller consumed it."""
        try:
            while True:
                r = self._post(f"/api/v1/streams/{self.stream_id}/next?n={self.batch}")
                if not r["items"]:
                    break
                for it in r["items"]:
                    arr = self.fetch(it)
                    yield it, arr
                    self._post(f"/api/v1/streams/{self.stream_id}/ack", {"indices": [it["i"]], "ok": True})
                if r["done"]:
                    break
            self._post(f"/api/v1/streams/{self.stream_id}/close", {"status": "done"})
        except BaseException:
            try:
                self._post(f"/api/v1/streams/{self.stream_id}/close", {"status": "aborted"})
            finally:
                raise


class PatchSetClient:
    """Training side of the Patch Factory: iterate a patch set, fetching EM and label cutouts on demand.

        from emqc.loader import PatchSetClient
        ps = PatchSetClient("http://127.0.0.1:8765", set_id=3)
        for em, label, meta in ps.iter(partition="train"):   # em (dz,dy,dx); label ids/boundary/mask or None
            ...
    Preprocessing / augmentation are *declared* in ps.manifest["preprocessing"] / ["augmentation"]; the platform
    serves raw voxels and the training code applies them, so the declaration is the lineage record, not a transform.
    """

    def __init__(self, api: str, set_id: int):
        self.api = api.rstrip("/")
        self.set_id = set_id
        with urllib.request.urlopen(f"{self.api}/api/v1/patchsets/{set_id}/manifest", timeout=600) as r:
            self.manifest = json.loads(r.read().decode())

    @property
    def patches(self) -> list[dict]:
        return self.manifest["patches"]

    def _get_npy(self, url: str) -> np.ndarray:
        with urllib.request.urlopen(self.api + url, timeout=600) as r:
            return np.load(io.BytesIO(r.read()))

    def fetch(self, patch: dict, with_label: bool = True) -> tuple[np.ndarray, np.ndarray | None]:
        em = self._get_npy(patch["url_em"])
        lab = self._get_npy(patch["url_label"]) if with_label and patch.get("url_label") else None
        return em, lab

    def iter(self, partition: str | None = None, with_label: bool = True, shuffle_seed: int | None = None):
        items = [p for p in self.patches if partition is None or p["partition"] == partition]
        if shuffle_seed is not None:
            rng = np.random.default_rng(shuffle_seed)
            items = [items[i] for i in rng.permutation(len(items))]
        for p in items:
            em, lab = self.fetch(p, with_label)
            yield em, lab, p
