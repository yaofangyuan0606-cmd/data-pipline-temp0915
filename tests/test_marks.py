"""对比页的标记与评论：钉坐标、线程、解决、删除权限、深链接。"""
import pytest
from fastapi.testclient import TestClient

from emqc import auth
from emqc.db import init_db, session_scope
from emqc.db.models import Mark, MarkComment, User
from test_who import make_block


@pytest.fixture
def marks_api(tmp_path, monkeypatch):
    from emqc.api.app import app
    from emqc.api.routers import annotate

    b = make_block(tmp_path)
    monkeypatch.setattr(annotate, "_block", lambda *_a, **_k: b)
    init_db()
    with session_scope() as s:
        s.query(MarkComment).delete(); s.query(Mark).delete()
    with TestClient(app) as c:
        yield c, b, "/api/v1/annotate/blocks/who/marks"


def test_marks_thread_resolve_and_links(marks_api):
    c, b, url = marks_api
    assert c.get(url).json() == {"marks": [], "n_open": 0, "by_z": {}}
    assert c.post(url, json={"z": 0, "x": 99, "y": 1, "text": "越界"}).status_code == 404
    assert c.post(url, json={"z": 0, "x": 1, "y": 1, "text": "   "}).status_code == 422
    r = c.post(url, json={"z": 0, "x": 3, "y": 2, "text": "这里膜断了\n看一下", "label_id": "18861616579", "annotator": "张三"})
    assert r.status_code == 200
    m = r.json()
    assert (m["z"], m["x"], m["y"], m["status"], m["created_by_name"], m["label_id"]) == (0, 3, 2, "open", "张三", "18861616579")
    assert m["link"] == f"/annotate/compare?block=who&z=0&mark={m['id']}"
    c.post(url, json={"z": 2, "x": 1, "y": 1, "text": "另一片的", "annotator": "李四"})
    lst = c.get(url).json()
    assert len(lst["marks"]) == 2 and lst["n_open"] == 2 and lst["by_z"] == {"0": 1, "2": 1}
    assert [x["id"] for x in c.get(url + "?z=2").json()["marks"]] == [lst["marks"][1]["id"]]
    r = c.post(f"/api/v1/annotate/marks/{m['id']}/comments", json={"text": "已经补上了", "annotator": "李四"})
    assert r.status_code == 200 and [(x["created_by_name"], x["kind"]) for x in r.json()["comments"]] == [("李四", "comment")]
    r = c.post(f"/api/v1/annotate/marks/{m['id']}/comments", json={"text": "膜的缺口在 (5, 2)，建议用画笔补 2 像素", "kind": "agent", "annotator": "审图助手"})
    assert r.json()["comments"][1]["kind"] == "agent"
    r = c.patch(f"/api/v1/annotate/marks/{m['id']}", json={"status": "resolved", "annotator": "李四"})
    assert r.json()["status"] == "resolved" and r.json()["resolved_by_name"] == "李四" and r.json()["resolved_at"]
    assert c.get(url).json()["n_open"] == 1 and c.get(url + "?status=open").json()["marks"][0]["z"] == 2
    assert c.patch(f"/api/v1/annotate/marks/{m['id']}", json={"status": "open"}).json()["resolved_at"] is None
    assert c.get(f"/api/v1/annotate/marks/{m['id']}").json()["comments"][0]["text"] == "已经补上了"
    assert c.get("/api/v1/annotate/marks/999999").status_code == 404
    assert c.delete(f"/api/v1/annotate/marks/{m['id']}").json() == {"ok": True, "id": m["id"]}
    assert c.get(f"/api/v1/annotate/marks/{m['id']}").status_code == 404
    with session_scope() as s:
        assert s.query(MarkComment).filter(MarkComment.mark_id == m["id"]).count() == 0, "评论跟着标记一起删"


def test_marks_carry_the_session_user_and_only_author_or_admin_deletes(tmp_path, monkeypatch):
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.config import settings

    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_open", False)
    b = make_block(tmp_path)
    monkeypatch.setattr(annotate, "_block", lambda *_a, **_k: b)
    init_db()
    with session_scope() as s:
        s.query(MarkComment).delete(); s.query(Mark).delete()
        s.query(User).delete()
        auth.create_user(s, "zhang", "pass-word-1", "张三", "annotator")
        auth.create_user(s, "li", "pass-word-1", "李四", "annotator")
        auth.create_user(s, "boss", "pass-word-1", "管理员", "admin")
    url = "/api/v1/annotate/blocks/who/marks"
    with TestClient(app) as zhang, TestClient(app) as li, TestClient(app) as boss, TestClient(app) as anon:
        assert anon.get(url).status_code == 401
        for c, u in ((zhang, "zhang"), (li, "li"), (boss, "boss")):
            assert c.post("/api/v1/auth/login", json={"username": u, "password": "pass-word-1"}).status_code == 200
        m = zhang.post(url, json={"z": 0, "x": 1, "y": 1, "text": "看这里", "annotator": "冒充者"}).json()
        assert (m["created_by_user"], m["created_by_name"]) == ("zhang", "张三"), "作者按会话记，不认请求里的名字"
        r = li.post(f"/api/v1/annotate/marks/{m['id']}/comments", json={"text": "收到"})
        assert r.json()["comments"][0]["created_by_user"] == "li"
        assert li.patch(f"/api/v1/annotate/marks/{m['id']}", json={"status": "resolved"}).json()["resolved_by_name"] == "李四", "谁都能标为已解决"
        assert li.patch(f"/api/v1/annotate/marks/{m['id']}", json={"text": "改你的字"}).status_code == 403
        assert li.delete(f"/api/v1/annotate/marks/{m['id']}").status_code == 403
        assert zhang.patch(f"/api/v1/annotate/marks/{m['id']}", json={"text": "看这里（补充）"}).json()["text"] == "看这里（补充）"
        assert boss.delete(f"/api/v1/annotate/marks/{m['id']}").status_code == 200


def test_compare_page_has_the_mark_ui(marks_api):
    c, _, _ = marks_api
    page = c.get("/annotate/compare").text
    for needle in ('id="cmp-mark"', 'id="cmp-pins"', 'id="cmp-mark-form"', 'id="cmp-marks-list"', 'id="cmp-marks-scope"'):
        assert needle in page
