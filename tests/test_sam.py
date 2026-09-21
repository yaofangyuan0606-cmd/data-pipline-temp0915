"""SAM boundary protection, image coordinates, exact labels and reversible application."""
import time

import numpy as np
import pytest

from emqc.annotate.sam import SAMService, revision
from emqc.annotate.store import Block


@pytest.fixture
def block(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    np.save(source / "em.npy", np.arange(12 * 8 * 2, dtype=np.uint8).reshape(12, 8, 2))
    labels = np.zeros((12, 8, 2), dtype=np.uint64)
    labels[7:, :, :] = 9007199254740993
    np.save(source / "seg.npy", labels)
    return Block(source, tmp_path / "work")


def proposal(service, block, mask, token="a" * 32):
    service.proposals[token] = {"path": str(block.path.resolve()), "work": str(block.work.resolve()), "z": 0, "mask": mask,
                                "revision": revision(block), "created": time.monotonic(),
                                "score": .9, "points": [(3, 2)], "labels": [1], "box": None,
                                "only_background": True, "candidate": 0}
    return token


def test_mask_axis_exact_uint64_and_undo(block):
    original = np.load(block.path / "seg.npy").copy()
    # a mask is in the DISPLAYED frame, which is the transpose of the (12, 8, 2) array on disk
    mask = np.zeros((8, 12), dtype=bool)
    mask[2:5, 6:9] = True  # non-square, crosses a uint64 label boundary; on disk that is [6:9, 2:5]
    s = SAMService()
    token = proposal(s, block, mask)
    new_id = 9007199254741017
    rec = s.apply(block, token, new_id)
    edited = np.load(block.work / "seg_edit.npy")
    assert rec["kind"] == "sam" and rec["n_px"] == 9
    assert rec["new_id"] == str(new_id)
    assert np.all(edited[6:9, 2:5, 0] == new_id)
    assert np.array_equal(edited[:, :, 1], original[:, :, 1])
    assert np.array_equal(np.load(block.path / "seg.npy"), original)
    assert not (block.path / "seg_edit.npy").exists()
    with pytest.raises(ValueError, match="过期"):
        s.apply(block, token, new_id)
    block.undo()
    assert np.array_equal(np.load(block.work / "seg_edit.npy"), original)


def test_stale_expired_wrong_block_and_invalid_id(block):
    s = SAMService()
    mask = np.ones((8, 12), dtype=bool)
    token = proposal(s, block, mask)
    with pytest.raises(ValueError, match="dtype range"):
        s.apply(block, token, 2**64)
    assert not (block.work / "seg_edit.npy").exists()
    s.proposals[token]["path"] = "/other/block"
    with pytest.raises(ValueError, match="不属于"):
        s.apply(block, token, 12)
    token = proposal(s, block, mask)
    s.proposals[token]["work"] = "/other/work"
    with pytest.raises(ValueError, match="工作目录已变化"):
        s.apply(block, token, 12)
    token = proposal(s, block, mask)
    block.paint(0, [(2, 2)], 0, 12)
    block.undo()  # even an edit+undo invalidates the old proposal
    with pytest.raises(ValueError, match="已变化"):
        s.apply(block, token, 12)
    token = proposal(s, block, mask)
    s.proposals[token]["created"] -= 901
    with pytest.raises(ValueError, match="过期"):
        s.apply(block, token, 12)


def test_prediction_clips_existing_labels_and_preserves_em_axes(block, monkeypatch):
    pytest.importorskip("torch")
    from emqc.config import settings
    monkeypatch.setattr(settings, "sam_device", "cpu")
    class Predictor:
        image = None
        def set_image(self, image):
            self.image = image
        def predict(self, **kwargs):
            return np.ones((3, 12, 8), bool), np.array([.8, .9, .7]), None
    s = SAMService()
    s.predictor = Predictor()
    r = s.predict(block, 0, [(3, 2)], [1], None)
    assert r["n_px"] == 7 * 8 and r["candidate"] == 1
    assert np.array_equal(s.predictor.image[:, :, 0], block.em[:, :, 0])
    assert not (block.work / "seg_edit.npy").exists(), "preview must not write labels"
    s.apply(block, r["token"], 55)
    labels = np.load(block.work / "seg_edit.npy")
    assert np.all(labels[:7, :, 0] == 55)
    assert np.all(labels[7:, :, 0] == 9007199254740993)


def test_api_rejects_invalid_prompts_before_inference(block, monkeypatch):
    from fastapi.testclient import TestClient
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.annotate.sam import service
    monkeypatch.setattr(annotate, "_block", lambda _: block)
    def unexpected(*args, **kwargs):
        pytest.fail("invalid request reached inference")
    monkeypatch.setattr(service, "predict", unexpected)
    client = TestClient(app)
    for body in [
        {"z": 0}, {"z": 2, "points": [[1, 1]], "labels": [1]},
        {"z": 0, "points": [[1, 1]], "labels": []},
        {"z": 0, "points": [[12, 1]], "labels": [1]},
        {"z": 0, "points": [[1, -1]], "labels": [1]},
        {"z": 0, "box": [5, 5, 2, 7]},
        {"z": 0, "points": [[1, 1]], "labels": [2]},
        {"z": 0, "box": [0, 0, 8, 8], "candidate": 3},
    ]:
        assert client.post("/api/v1/annotate/blocks/example/sam/predict", json=body).status_code == 422
