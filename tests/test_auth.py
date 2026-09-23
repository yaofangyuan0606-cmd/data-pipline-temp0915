"""登录系统：账号、角色、会话、页面与接口的门、改动记在登录用户名下、撤销权限。"""
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from emqc import auth
from emqc.db import init_db, session_scope
from emqc.db.models import AuthSession, User
from test_who import make_block

PW = "correct-horse-8"


@pytest.fixture
def auth_on(monkeypatch):
    from emqc.config import settings

    monkeypatch.setattr(settings, "auth_disabled", False)
    init_db()
    with session_scope() as s:
        s.execute(delete(AuthSession))
        s.execute(delete(User))
    auth._FAILS.clear()
    yield
    auth._FAILS.clear()


@pytest.fixture
def app(auth_on):
    from emqc.api.app import app as fastapi_app

    return fastapi_app


def make_user(username, role="annotator", display=None, password=PW, must_change=False):
    with session_scope() as s:
        u, _ = auth.create_user(s, username, password, display, role, must_change=must_change)
        return u.id


def login(c, username, password=PW):
    return c.post("/api/v1/auth/login", json={"username": username, "password": password})


def detail(r):
    d = r.json()["detail"]
    return d if isinstance(d, dict) else {"message": d}


def test_pages_and_api_are_gated_until_login(app):
    with TestClient(app) as c:
        r = c.get("/annotate", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fannotate")
        r = c.get("/annotate/compare?block=x&z=3", follow_redirects=False)
        assert r.headers["location"] == "/login?next=%2Fannotate%2Fcompare%3Fblock%3Dx%26z%3D3"
        r = c.get("/api/v1/annotate/blocks")
        assert r.status_code == 401 and detail(r)["code"] == "auth"
        assert c.get("/api/v1/annotate/blocks/any/edits").status_code == 401
        assert c.get("/login").status_code == 200 and 'id="login-form"' in c.get("/login").text
        assert c.get("/api/v1/auth/status").json() == {"auth_disabled": False, "auth_open": False, "has_users": False}
        assert c.get("/api/v1/auth/me").status_code == 401
        # the QC workspace is not part of the login system
        assert c.get("/api/v1/health").status_code == 200


def test_login_logout_cookie_and_throttle(app):
    make_user("zhang", display="张三", role="admin")
    with TestClient(app) as c:
        r = login(c, "zhang", "wrong-password")
        assert r.status_code == 401 and detail(r)["code"] == "bad_login"
        assert login(c, "nobody", PW).status_code == 401, "不存在的账号和密码错误对外一个样子"
        r = login(c, "zhang")
        assert r.status_code == 200 and r.json()["user"]["display_name"] == "张三" and r.json()["user"]["role_name"] == "管理员"
        assert c.cookies.get(auth.COOKIE) and "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"].replace("samesite", "SameSite")
        assert c.get("/api/v1/auth/me").json()["user"]["username"] == "zhang"
        assert c.get("/annotate", follow_redirects=False).status_code == 200
        assert "张三 · 管理员" in c.get("/annotate").text and 'id="an-who"' not in c.get("/annotate").text, "顶栏显示登录用户，不再让人自报姓名"
        assert c.get("/login", follow_redirects=False).status_code == 303, "登录了再开登录页直接跳走"
        with session_scope() as s:
            assert s.scalar(select(User).where(User.username == "zhang")).last_login_at is not None
            sess = list(s.scalars(select(AuthSession)))
            assert len(sess) == 1 and sess[0].token_hash != c.cookies.get(auth.COOKIE), "库里只有令牌的哈希"
        assert c.post("/api/v1/auth/logout").status_code == 200
        assert c.get("/api/v1/auth/me").status_code == 401
        with session_scope() as s:
            assert list(s.scalars(select(AuthSession))) == []
    with TestClient(app) as c:
        for _ in range(5):
            assert login(c, "zhang", "wrong-password").status_code == 401
        r = login(c, "zhang")
        assert r.status_code == 429 and detail(r)["code"] == "throttled", "错 5 次后连正确密码也要等一会"


def test_admin_creates_users_temp_password_must_change_and_roles(app):
    make_user("admin", role="admin", display="管理员甲")
    with TestClient(app) as admin, TestClient(app) as li, TestClient(app) as li2:
        login(admin, "admin")
        r = admin.post("/api/v1/auth/users", json={"username": "Li.Si", "display_name": "李四", "role": "annotator"})
        assert r.status_code == 200
        temp, uid = r.json()["temp_password"], r.json()["user"]["id"]
        assert r.json()["user"]["username"] == "li.si" and r.json()["user"]["must_change_password"] is True and len(temp) >= 12
        assert admin.post("/api/v1/auth/users", json={"username": "li.si", "role": "annotator"}).status_code == 422, "登录名唯一"
        assert admin.post("/api/v1/auth/users", json={"username": "bad name!", "role": "annotator"}).status_code == 422
        assert admin.post("/api/v1/auth/users", json={"username": "short", "role": "annotator", "password": "abc"}).status_code == 422
        # 李四 logs in with the temp password: pages send her to /account, the annotation API is closed until she changes it
        assert login(li, "li.si", temp).status_code == 200
        assert li.get("/annotate", follow_redirects=False).headers["location"] == "/account?force=1"
        assert li.get("/account", follow_redirects=False).status_code == 200
        r = li.get("/api/v1/annotate/blocks")
        assert r.status_code == 403 and detail(r)["code"] == "password"
        assert li.post("/api/v1/auth/password", json={"old_password": "nope-nope-1", "new_password": PW}).status_code == 422
        assert li.post("/api/v1/auth/password", json={"old_password": temp, "new_password": temp}).status_code == 422
        assert login(li2, "li.si", temp).status_code == 200, "第二个浏览器"
        assert li.post("/api/v1/auth/password", json={"old_password": temp, "new_password": PW}).status_code == 200
        assert li.get("/annotate", follow_redirects=False).status_code == 200
        assert li.get("/api/v1/annotate/blocks").status_code == 200
        assert li2.get("/api/v1/auth/me").status_code == 401, "改密码后其它登录全部退出，当前这个保留"
        # roles and account management
        assert li.get("/api/v1/auth/users").status_code == 403 and li.get("/annotate/users").status_code == 403
        assert admin.get("/annotate/users").status_code == 200
        assert admin.patch(f"/api/v1/auth/users/{uid}", json={"role": "reviewer", "display_name": "李四（审核）"}).json()["user"]["role"] == "reviewer"
        me = admin.get("/api/v1/auth/me").json()["user"]
        assert admin.patch(f"/api/v1/auth/users/{me['id']}", json={"is_active": False}).status_code == 422, "不能停用自己 / 最后一个管理员"
        assert admin.patch(f"/api/v1/auth/users/{me['id']}", json={"role": "annotator"}).status_code == 422
        r = admin.post(f"/api/v1/auth/users/{uid}/reset-password")
        assert r.status_code == 200 and r.json()["temp_password"] != temp
        assert li.get("/api/v1/auth/me").status_code == 401, "重置密码后她要重新登录"
        assert login(li, "li.si", PW).status_code == 401 and login(li, "li.si", r.json()["temp_password"]).status_code == 200
        assert admin.patch(f"/api/v1/auth/users/{uid}", json={"is_active": True, "display_name": "李四"}).status_code == 200
        assert admin.patch(f"/api/v1/auth/users/{uid}", json={"is_active": False}).status_code == 200
        assert li.get("/api/v1/auth/me").status_code == 401 and login(li, "li.si", r.json()["temp_password"]).status_code == 401, "停用立刻生效"
        assert [u["username"] for u in admin.get("/api/v1/auth/users").json()["users"]] == ["admin", "li.si"], "停用不是删除"


@pytest.fixture
def block_api(app, tmp_path, monkeypatch):
    from emqc.api.routers import annotate

    b = make_block(tmp_path)
    monkeypatch.setattr(annotate, "_block", lambda *_a, **_k: b)
    annotate._presence.clear()
    return app, b, "/api/v1/annotate/blocks/who"


def paint(c, url, z, x, label, **extra):
    return c.post(url + "/paint", json={"z": z, "points": [[x, 1]], "radius": 0, "new_id": label, **extra})


def test_edits_are_stamped_with_the_session_user_not_the_body(block_api):
    app, b, url = block_api
    uid = make_user("zhang", display="张三")
    with TestClient(app) as c:
        login(c, "zhang")
        r = paint(c, url, 0, 1, "7", annotator="冒充者", expect_rev=0)
        assert r.status_code == 200 and r.json()["rev"] == 1
        rec = r.json()["edit"]
        assert (rec["by"], rec["by_user"], rec["by_id"]) == ("张三", "zhang", uid), "身份只认会话，页面传的名字不认"
        e = b.audit_entries()[-1]
        assert (e["by"], e["by_user"], e["by_id"], e["action"]) == ("张三", "zhang", uid, "edit")
        assert c.get(url + "/edits?z=0").json()["editors"][0]["by"] == "张三"
        # a display-name change does not turn old records into someone else's
        with session_scope() as s:
            s.get(User, uid).display_name = "张三丰"
        assert b.conflicts(0, 0, auth.actor_for(type("U", (), {"display_name": "张三丰", "username": "zhang", "id": uid})())) is None
        r = c.post(url + "/presence", json={"z": 0})
        assert r.json()["n_online"] == 1 and r.json()["others"] == []


def test_undo_permissions_by_role_and_pinned_record(block_api):
    app, b, url = block_api
    make_user("zhang", display="张三", role="annotator")
    make_user("wang", display="王五", role="reviewer")
    with TestClient(app) as zhang, TestClient(app) as wang:
        login(zhang, "zhang"); login(wang, "wang")
        assert paint(wang, url, 0, 1, "7").status_code == 200                      # #1 by 王五
        r = zhang.post(url + "/undo?z=0", json={})
        assert r.status_code == 409 and detail(r)["code"] == "not_yours" and detail(r)["can_override"] is False
        r = zhang.post(url + "/undo?z=0", json={"force": True})
        assert r.status_code == 403 and detail(r)["code"] == "forbidden", "标注员不能撤别人的，哪怕带 force"
        assert b.pick(0, 1, 1) == 7
        assert paint(zhang, url, 0, 2, "8").status_code == 200                     # #2 by 张三
        r = wang.post(url + "/undo?z=0", json={})
        assert r.status_code == 409 and detail(r)["code"] == "not_yours" and detail(r)["can_override"] is True
        latest_n = detail(r)["latest"]["n"]
        assert paint(zhang, url, 0, 3, "8").status_code == 200                     # #3 by 张三 — after 王五's confirm box appeared
        r = wang.post(url + "/undo?z=0", json={"force": True, "n": latest_n})
        assert r.status_code == 409 and detail(r)["code"] == "stale", "确认的那一笔已不是最近一笔：拒绝，不撤错"
        r = wang.post(url + "/undo?z=0", json={"force": True, "n": 3})
        assert r.status_code == 200 and r.json()["undone"]["by"] == "张三" and r.json()["undone"]["n"] == 3
        undo = b.audit_entries()[-1]
        assert (undo["action"], undo["by"], undo["by_user"], undo["of"], undo["of_user"], undo["forced"]) == ("undo", "王五", "wang", "张三", "zhang", True)
        assert zhang.post(url + "/undo?z=0", json={}).json()["undone"]["n"] == 2, "自己的照常撤"


def test_presence_is_keyed_by_user(block_api):
    app, b, url = block_api
    make_user("zhang", display="张三"); make_user("li", display="李四")
    with TestClient(app) as zhang, TestClient(app) as zhang_tab2, TestClient(app) as li:
        login(zhang, "zhang"); login(zhang_tab2, "zhang"); login(li, "li")
        assert zhang.post(url + "/presence", json={"z": 0}).json()["n_online"] == 1
        assert zhang_tab2.post(url + "/presence", json={"z": 5}).json()["n_online"] == 1, "同一个人开两个标签页还是一个人"
        r = li.post(url + "/presence", json={"z": 5, "annotator": "假名字"}).json()
        assert [(o["by"], o["user"], o["same_slice"]) for o in r["others"]] == [("张三", "zhang", True)] and r["n_online"] == 2


def test_session_expiry_and_disabled_user(block_api):
    app, b, url = block_api
    uid = make_user("zhang", display="张三")
    with TestClient(app) as c:
        login(c, "zhang")
        assert c.get("/api/v1/auth/me").status_code == 200
        from datetime import datetime, timedelta
        with session_scope() as s:
            for sess in s.scalars(select(AuthSession)):
                sess.expires_at = datetime.utcnow() - timedelta(seconds=1)
        assert c.get("/api/v1/auth/me").status_code == 401, "过期的会话失效"
        login(c, "zhang")
        with session_scope() as s:
            s.get(User, uid).is_active = False
        assert c.get("/api/v1/auth/me").status_code == 401, "停用的账号即使有会话也进不来"


def test_cli_creates_users_with_password_on_stdin(app, monkeypatch, capsys):
    from emqc.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("cli-pass-word-9\n"))
    assert main(["create-user", "--username", "Ops", "--display", "运维", "--role", "admin", "--password-stdin"]) == 0
    assert "已创建 ops" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-pass-word-9\n"))
    assert main(["create-user", "--username", "ops", "--role", "admin", "--password-stdin"]) == 1 and "已存在" in capsys.readouterr().out
    with TestClient(app) as c:
        assert login(c, "ops", "cli-pass-word-9").status_code == 200
    assert main(["users"]) == 0 and "ops" in capsys.readouterr().out
    assert main(["reset-password", "--username", "ops"]) == 0
    out = capsys.readouterr().out
    assert "初始密码" in out
    with TestClient(app) as c:
        assert login(c, "ops", "cli-pass-word-9").status_code == 401, "重置后旧密码失效"


def test_auth_disabled_keeps_the_old_typed_name_flow(monkeypatch, tmp_path):
    from emqc.api.app import app
    from emqc.api.routers import annotate
    from emqc.config import settings

    monkeypatch.setattr(settings, "auth_disabled", True)
    b = make_block(tmp_path)
    monkeypatch.setattr(annotate, "_block", lambda *_a, **_k: b)
    with TestClient(app) as c:
        assert c.get("/login", follow_redirects=False).status_code == 303
        assert c.get("/annotate").status_code == 200 and 'id="an-who"' in c.get("/annotate").text
        assert c.get("/api/v1/auth/me").json() == {"user": None, "auth_disabled": True}
        assert c.get("/annotate/users").status_code == 404
        r = paint(c, "/api/v1/annotate/blocks/who", 0, 1, "7", annotator="张三")
        assert r.status_code == 200 and r.json()["edit"]["by"] == "张三" and "by_id" not in r.json()["edit"]


def test_open_mode_lets_anyone_in_by_name(block_api, monkeypatch):
    from emqc.config import settings

    app, b, url = block_api
    monkeypatch.setattr(settings, "auth_open", True)
    make_user("zhang", display="张三", role="annotator")
    with TestClient(app) as c, TestClient(app) as d:
        assert c.get("/api/v1/auth/status").json()["auth_open"] is True
        r = login(c, "zhang", "totally-wrong")
        assert r.status_code == 200 and r.json()["user"]["username"] == "zhang", "现有账号：密码不看"
        r = login(d, "李四", "")
        assert r.status_code == 200
        u = r.json()["user"]
        assert u["display_name"] == "李四" and u["username"].startswith("u-") and u["role"] == "reviewer" and u["must_change_password"] is False, "中文名当显示名，登录名用哈希，自动建成审核员"
        with TestClient(app) as e:
            assert login(e, "李四", "another").json()["user"]["id"] == u["id"], "同一个名字永远对上同一个账号"
        assert paint(d, url, 0, 1, "7").json()["edit"]["by"] == "李四", "改动照样记在名字下"
        assert d.get("/annotate", follow_redirects=False).status_code == 200
        with TestClient(app) as e:
            assert login(e, "   ", "x").status_code == 401
        with session_scope() as s:
            s.get(User, u["id"]).is_active = False
        with TestClient(app) as e:
            assert login(e, "李四", "").status_code == 401, "停用的账号试用模式也进不来"
    monkeypatch.setattr(settings, "auth_open", False)
    with TestClient(app) as c:
        assert login(c, "zhang", "totally-wrong").status_code == 401, "关掉试用模式立刻恢复校验"
        assert login(c, "zhang", "").status_code == 401
