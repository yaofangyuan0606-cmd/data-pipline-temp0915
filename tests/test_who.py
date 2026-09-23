"""谁改的：标注人写进每条记录、审计流水、切片版本号与多人冲突、只能撤自己的、同块在线、工作目录进程锁。"""
import base64
import fcntl
import io
import json

import numpy as np
import pytest
from PIL import Image

from emqc.annotate.provenance import comparison, csv_report, report
from emqc.annotate.store import Block, UndoForbidden, hold_workdir


def make_block(tmp_path, name="who"):
    path = tmp_path / "blocks" / name
    path.mkdir(parents=True)
    # on disk (X, Y, z); displayed section = transpose → 6 rows × 8 columns, 3 sections, all background
    np.save(path / "em.npy", np.zeros((8, 6, 3), dtype=np.uint8))
    np.save(path / "seg.npy", np.zeros((8, 6, 3), dtype=np.uint64))
    return Block(path, tmp_path / "work")


def dot(block, z, x, y, label, by=None):
    return block.paint(z, [(x, y)], 0, label, by=by)


def test_record_carries_annotator_and_audit(tmp_path):
    b = make_block(tmp_path)
    rec = dot(b, 0, 1, 1, 7, by="张三")
    assert rec["by"] == "张三" and b.edits()[0]["by"] == "张三"
    audit = b.audit_entries()
    assert [(e["seq"], e["action"], e["by"], e["n"], e["z"], e["n_px"]) for e in audit] == [(1, "edit", "张三", 1, 0, 1)]
    assert json.loads((b.work / "audit.jsonl").read_text().splitlines()[0])["by"] == "张三", "流水里的名字按原样可读"
    assert b.slice_rev(0) == 1 and b.slice_rev(1) == 0, "版本号只跟被动过的那一片走"
    assert b.editors(0) == [{"by": "张三", "n": 1, "n_px": 1, "first": rec["ts"], "last": rec["ts"]}]
    assert b.last_edit()["by"] == "张三" and b.info()["editors"] == ["张三"] and b.info()["last_edit"]["n"] == 1
    anonymous = dot(b, 1, 1, 1, 8)
    assert anonymous["by"] is None and [r["by"] for r in b.editors()] == [None, "张三"], "脚本写入没有名字，归为一行 None"


def test_slice_rev_is_monotonic_and_only_others_conflict(tmp_path):
    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="张三")                   # seq 1
    dot(b, 0, 2, 1, 8, by="李四")                   # seq 2
    assert b.conflicts(0, since=1, by="张三")["by"] == "李四", "张三 看到的是版本 1，之后 李四 动过"
    assert b.conflicts(0, since=1, by="李四") is None, "自己的后续操作不算冲突"
    assert b.conflicts(0, since=2, by="张三") is None
    assert b.conflicts(1, since=0, by="王五") is None, "别的片没人动过"
    assert b.undo(0, by="李四")["n"] == 2            # seq 3: an undo moves the revision FORWARD
    assert b.slice_rev(0) == 3 and len(b.edits(0)) == 1
    c = b.conflicts(0, since=2, by="张三")
    assert c["action"] == "undo" and c["by"] == "李四" and c["of"] == "李四"
    dot(b, 2, 3, 3, 9, by="王五")                   # seq 4 on another section
    assert b.slice_rev(0) == 3 and b.slice_rev(2) == 4
    b.merge(7, 9, "block", by="王五")                # seq 5, block-wide: touches every section's revision
    assert b.slice_rev(0) == 5 and b.slice_rev(1) == 5


def test_undo_only_your_own_unless_forced(tmp_path):
    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="张三")
    with pytest.raises(UndoForbidden) as info:
        b.undo(0, by="李四")
    assert info.value.record["by"] == "张三" and "张三" in str(info.value)
    assert b.pick(0, 1, 1) == 7, "被拒绝的撤销什么都没改"
    assert b.undo(0, by="张三")["n"] == 1, "自己的可以撤"
    dot(b, 0, 1, 1, 7, by="张三")
    assert b.undo(0, by="李四", force=True)["n"] == 1, "明确 force 才能撤别人的（撤空后编号从 1 重新起）"
    dot(b, 0, 1, 1, 7, by="张三")
    assert b.undo(0)["n"] == 1, "不带名字的调用（脚本）照旧"
    dot(b, 0, 1, 1, 7)
    assert b.undo(0, by="李四")["n"] == 1, "没名字的旧记录谁都能撤"
    undos = [e for e in b.audit_entries() if e["action"] == "undo"]
    assert [(e["by"], e["of"], e["forced"]) for e in undos] == [("张三", "张三", False), ("李四", "张三", True), (None, "张三", False), ("李四", None, False)]


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from emqc.api.app import app
    from emqc.api.routers import annotate

    b = make_block(tmp_path)
    monkeypatch.setattr(annotate, "_block", lambda *_a, **_k: b)
    annotate._presence.clear()
    with TestClient(app) as c:
        yield c, b, "/api/v1/annotate/blocks/who"


def paint(c, url, z, x, label, **extra):
    return c.post(url + "/paint", json={"z": z, "points": [[x, 1]], "radius": 0, "new_id": label, **extra})


def test_api_two_people_one_section(api):
    c, b, url = api
    assert c.get(url + "/labels/0.json").json()["rev"] == 0, "标签表带着这一片的版本号下发"
    r = paint(c, url, 0, 1, "7", annotator="张三", expect_rev=0)
    assert r.status_code == 200 and r.json()["edit"]["by"] == "张三" and r.json()["rev"] == 1
    # 李四 loaded the section before 张三's stroke landed
    r = paint(c, url, 0, 2, "8", annotator="李四", expect_rev=0)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "stale" and d["latest"]["by"] == "张三" and d["rev"] == 1 and "张三" in d["message"]
    assert b.pick(0, 2, 1) == 0, "被拒绝的写入没落盘"
    assert paint(c, url, 0, 2, "8", annotator="李四", expect_rev=1).status_code == 200      # reloaded → fine
    assert paint(c, url, 0, 3, "8", annotator="李四", expect_rev=1).status_code == 200, "自己的连续几笔不算冲突"
    assert paint(c, url, 0, 4, "7", annotator="张三", expect_rev=1).status_code == 409, "张三 手里的版本已经旧了"
    assert paint(c, url, 1, 4, "7", annotator="张三", expect_rev=0).status_code == 200, "别的片各改各的"
    assert paint(c, url, 0, 5, "7", annotator="张三").status_code == 200, "不说自己看到哪个版本就不检查"
    assert paint(c, url, 0, 6, "9").status_code == 200, "匿名脚本照旧"
    # undo: the latest record on section 0 is anonymous → anyone may undo it; then 李四's → 张三 needs force
    assert c.post(url + "/undo?z=0", json={"annotator": "张三"}).json()["undone"]["by"] is None
    assert c.post(url + "/undo?z=0", json={"annotator": "张三"}).json()["undone"]["by"] == "张三"
    r = c.post(url + "/undo?z=0", json={"annotator": "张三"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_yours" and r.json()["detail"]["latest"]["by"] == "李四"
    r = c.post(url + "/undo?z=0", json={"annotator": "张三", "force": True})
    assert r.status_code == 200 and r.json()["undone"]["by"] == "李四" and r.json()["rev"] == b.slice_rev(0)
    history = c.get(url + "/edits?z=0").json()
    assert history["rev"] == b.slice_rev(0) and {e["by"] for e in history["editors"]} == {"张三", "李四"}
    assert all("by" in e for e in history["edits"])
    audit = c.get(url + "/audit?z=0").json()
    assert audit["n"] == len([e for e in b.audit_entries() if e.get("z") in (None, 0)])
    assert audit["entries"][0]["action"] == "undo" and audit["entries"][0]["forced"] is True and audit["entries"][0]["of"] == "李四"
    assert c.get(url + "/audit?z=1").json()["n"] == 1
    blocks = c.get("/api/v1/annotate/blocks/who").json()
    assert set(blocks["editors"]) >= {"张三", "李四"} and blocks["last_edit"]["by"] in {"张三", "李四"}


def test_api_presence_and_change_notice(api):
    c, b, url = api
    p = lambda z, who=None: c.post(url + "/presence", json={"z": z, "annotator": who}).json()
    assert p(0, "张三")["others"] == [] and p(0, "张三")["n_online"] == 1
    r = p(0, "李四")
    assert [(o["by"], o["z"], o["same_slice"]) for o in r["others"]] == [("张三", 0, True)]
    r = p(5, "王五")
    assert [(o["by"], o["same_slice"]) for o in r["others"]] == [("张三", False), ("李四", False)] and r["n_online"] == 3
    assert p(0)["n_online"] == 3, "匿名心跳不登记自己，但看得到别人"
    assert r["rev"] == 0 and r["latest"] is None
    paint(c, url, 0, 1, "7", annotator="张三")
    r = p(0, "李四")
    assert r["rev"] == 1 and r["latest"]["by"] == "张三" and r["latest"]["action"] == "edit" and r["latest"]["n_px"] == 1


def test_api_name_is_normalised_validated_and_optionally_required(api, monkeypatch):
    c, b, url = api
    assert paint(c, url, 0, 1, "7", annotator="  张 \n 三  ").json()["edit"]["by"] == "张 三"
    assert paint(c, url, 0, 2, "7", annotator="   ").json()["edit"]["by"] is None, "空白名字等于没填"
    assert paint(c, url, 0, 3, "7", annotator="a\x07b").status_code == 422
    assert paint(c, url, 0, 3, "7", annotator="x" * 65).status_code == 422
    assert paint(c, url, 0, 3, "7", annotator="<b>x</b>").json()["edit"]["by"] == "<b>x</b>", "原样保存，页面显示时再转义"
    from emqc.config import settings
    monkeypatch.setattr(settings, "annotate_require_annotator", True)
    r = paint(c, url, 0, 4, "7")
    assert r.status_code == 422 and "标注人" in r.json()["detail"]
    assert paint(c, url, 0, 4, "7", annotator="李四").status_code == 200
    r = c.post(url + "/undo?z=0", json={})
    assert r.status_code == 422, "撤销同样要署名"


def png(url):
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))))


def test_provenance_reports_who_wrote_each_pixel(tmp_path):
    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="张三")
    dot(b, 0, 2, 1, 7, by="张三")
    dot(b, 0, 5, 4, 8, by="李四")
    dot(b, 0, 0, 5, 9)                              # anonymous script, not touching 李四's patch
    dot(b, 0, 2, 1, 0, by="李四")                    # 李四 erases one of 张三's pixels: that pixel is now 李四's
    result = comparison(block=b, z=0)
    rep = result["report"]
    by_name = {e["by"]: e for e in rep["editors"]}
    assert by_name["张三"]["pixels"] == 1 and by_name["李四"]["pixels"] == 2 and by_name[None]["pixels"] == 1
    assert by_name["李四"]["label_pixels"] == 1 and by_name["李四"]["operations"] == 2, "擦成背景的像素归写它的人，但不算标签像素"
    who = png(result["editors_png"])
    names = result["editors"]
    assert names[who[1, 1] - 1] == "张三" and names[who[4, 5] - 1] == "李四" and names[who[1, 2] - 1] == "李四"
    assert names[who[5, 0] - 1] is None and who[0, 0] == 0, "没记名字的记录是 None；没动过的像素是 0"
    label7 = next(l for l in rep["labels"] if l["id"] == "7")
    assert label7["editors"] == [{"by": "张三", "px": 1}]
    label0 = next(l for l in rep["labels"] if l["id"] == "0")
    assert {"by": "李四", "px": 1} in label0["editors"]
    regions = {(g["cx"], g["cy"]): g["by"] for g in result["changes"]["regions"]}
    assert regions[(1, 1)] == "张三" and regions[(5, 4)] == "李四" and regions[(0, 5)] is None
    assert all("by" in op for op in rep["operations"]) and {op["by"] for op in rep["operations"]} == {"张三", "李四", None}
    csv = csv_report(rep).splitlines()
    assert csv[0].endswith(",editors")
    row7 = next(r for r in csv if r.startswith("who,0,7,"))
    assert row7.endswith(",张三:1")
    row0 = next(r for r in csv if r.startswith("who,0,0,"))
    assert "李四:1" in row0
    whole = report(b)
    assert {e["by"]: e["pixels"] for e in whole["editors"]} == {"张三": 1, "李四": 2, None: 1}
    assert next(l for l in whole["labels"] if l["id"] == "7")["editors"] == [{"by": "张三", "px": 1}]
    assert b.undo(0, by="李四")["n"] == 5
    rep2 = comparison(b, 0)["report"]
    assert {e["by"]: e["pixels"] for e in rep2["editors"]} == {"张三": 2, "李四": 1, None: 1}, "撤销后按剩余记录重算"


def test_csv_guards_formula_like_names(tmp_path):
    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="=HYPERLINK(1)")
    rep = comparison(b, 0)["report"]
    row = next(r for r in csv_report(rep).splitlines() if r.startswith("who,0,7,"))
    assert row.endswith(",'=HYPERLINK(1):1"), "名字也是用户输入，进表格前加引号防公式"


def test_workdir_is_claimed_by_one_process_only(tmp_path):
    b = make_block(tmp_path, "locked")
    root = b.work.parent
    root.mkdir(parents=True, exist_ok=True)
    other = open(root / ".server.lock", "a+")          # another process would hold exactly this lock
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError, match="另一个服务进程"):
            dot(b, 0, 1, 1, 7, by="张三")
        assert not (b.work / "seg_edit.npy").exists() and not (b.work / "edits.jsonl").exists(), "拒绝发生在任何写入之前"
    finally:
        fcntl.flock(other, fcntl.LOCK_UN)
        other.close()
    assert dot(b, 0, 1, 1, 7, by="张三")["n"] == 1, "锁释放后正常写"
    hold_workdir(root)                                   # second claim from the same process is a no-op
    assert (root / ".server.lock").read_text().strip().isdigit()
    assert not (b.path / ".server.lock").exists(), "数据目录里什么都不留"


def test_pages_carry_the_annotator_ui(api):
    c, _, _ = api
    page = c.get("/annotate").text
    for needle in ('id="an-who"', 'id="an-who-dialog"', 'id="an-undo-confirm"', 'id="an-editors"', 'id="an-presence"'):
        assert needle in page
    compare = c.get("/annotate/compare").text
    for needle in ('id="cmp-audit"', "<th>标注人</th>"):
        assert needle in compare


# ---------------------------------------------------------------- 审查补上的覆盖：SAM 按片判断过期、修补记名、在线 TTL、钉住撤销、残缺流水
def test_sam_apply_is_stale_only_when_its_own_section_changed(tmp_path):
    import time

    from emqc.annotate.sam import revision, service

    b = make_block(tmp_path)
    mask = np.zeros(b.shape_zyx[1:], bool)
    mask[2, 2] = True
    token = "a" * 32
    proposal = {"path": str(b.path.resolve()), "work": str(b.work.resolve()), "z": 0, "mask": mask, "revision": revision(b),
                "slice_rev": b.slice_rev(0), "created": time.monotonic(), "score": 0.9, "points": [[2, 2]], "labels": [1], "box": None,
                "only_background": True, "candidate": 0, "snap_boundary": False}
    service.proposals[token] = dict(proposal)
    dot(b, 1, 1, 1, 7, by="李四")                                       # someone else edits ANOTHER section
    rec = service.apply(b, token, 9, by="张三")
    assert rec is not None and rec["by"] == "张三" and rec["kind"] == "sam" and b.pick(0, 2, 2) == 9
    b.undo(0, by="张三")
    service.proposals[token] = dict(proposal, slice_rev=b.slice_rev(0))
    dot(b, 0, 5, 5, 8, by="李四")                                       # now THIS section changes after the preview
    with pytest.raises(ValueError, match="本片标注已变化"):
        service.apply(b, token, 9, by="张三")
    legacy = {k: v for k, v in proposal.items() if k != "slice_rev"}     # proposals from before this change fall back to the block fingerprint
    service.proposals[token] = dict(legacy, revision=revision(b))
    assert service.apply(b, token, 9, by="张三")["by"] == "张三"


def test_repair_apply_stamps_the_actor(tmp_path):
    import time

    from emqc.annotate.interpolate import service as repair
    from emqc.annotate.sam import revision

    b = make_block(tmp_path)
    labels = np.zeros(b.shape_zyx[1:], np.uint64)
    labels[3, 3] = 42
    hole = np.zeros(b.shape_zyx[1:], bool)
    hole[3, 3] = True
    token = "b" * 32
    repair.proposals[token] = {"path": str(b.path.resolve()), "work": str(b.work.resolve()), "z": 1, "labels": labels, "hole": hole,
                               "revision": revision(b), "created": time.monotonic(), "sources": [0, 2], "dark": 12}
    rec = repair.apply(b, token, by="张三")
    assert rec["kind"] == "repair" and rec["by"] == "张三" and b.pick(1, 3, 3) == 42
    assert b.audit_entries()[-1]["by"] == "张三" and b.editors(1)[0]["by"] == "张三"


def test_presence_forgets_people_after_the_ttl(api):
    from emqc.api.routers import annotate

    c, b, url = api
    p = lambda z, who: c.post(url + "/presence", json={"z": z, "annotator": who}).json()
    p(0, "张三")
    assert p(0, "李四")["others"][0]["by"] == "张三"
    with annotate._presence_lock:
        annotate._presence[b.id]["n:张三"]["at"] -= annotate.PRESENCE_TTL_S + 1
    r = p(0, "李四")
    assert r["others"] == [] and r["n_online"] == 1, "60 秒没心跳算离线"


def test_undo_is_pinned_to_the_confirmed_record(tmp_path):
    from emqc.annotate.store import UndoMismatch

    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="张三")
    dot(b, 0, 2, 1, 8, by="张三")
    with pytest.raises(UndoMismatch):
        b.undo(0, by="张三", expect_n=1)
    assert b.undo(0, by="张三", expect_n=2)["n"] == 2


def test_truncated_audit_tail_is_dropped_and_the_log_keeps_going(tmp_path):
    b = make_block(tmp_path)
    dot(b, 0, 1, 1, 7, by="张三")
    dot(b, 0, 2, 1, 8, by="张三")
    with open(b.work / "audit.jsonl", "a") as f:
        f.write('{"seq": 3, "action": "edit", "by": "李四", "n": 3')   # crashed mid-append: no closing brace, no newline
    fresh = Block(b.path, b.work.parent)
    assert [e["seq"] for e in fresh.audit_entries()] == [1, 2], "残缺的末行被丢掉，前面的完整保留"
    assert fresh.paint(0, [(3, 1)], 0, 9, by="王五")["n"] == 3
    lines = (b.work / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["seq"] == 3 and json.loads(lines[-1])["by"] == "王五", "新行另起一行，不粘在残行后面"
    assert [e["seq"] for e in Block(b.path, b.work.parent).audit_entries()] == [1, 2, 3]
    (b.work / "audit.jsonl").write_text('{"seq": 1, "action": "edit"}\nGARBAGE\n{"seq": 3, "action": "edit"}\n')
    with pytest.raises(ValueError, match="审计日志格式损坏"):
        Block(b.path, b.work.parent).audit_entries()
