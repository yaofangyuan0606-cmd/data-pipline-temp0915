"""运行日志：请求编号、未处理异常、前端报错、请求记录、进程启停与崩溃检测、守护脚本、告警推送、管理员看日志。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emqc import logs

logs.setup_logging()


@pytest.fixture
def app():
    from emqc.api.app import app as fastapi_app

    return fastapi_app


def test_request_id_unhandled_error_and_problem_list(app):
    def boom():
        raise RuntimeError("故意的：测试未处理异常")

    app.add_api_route("/api/v1/_test_boom", boom, methods=["POST"])
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/api/v1/health")
            assert r.status_code == 200 and r.json()["db"] is True and r.headers["x-request-id"]
            r = c.post("/api/v1/_test_boom")
            assert r.status_code == 500
            rid = r.headers["x-request-id"]
            assert rid in r.json()["detail"]["message"], "页面上看到的编号就是日志里的编号"
            items = c.get("/api/v1/admin/logs?kind=problems&level=ERROR").json()["items"]
            hit = next(i for i in items if i["request_id"] == rid)
            assert "RuntimeError" in hit["exc"] and "故意的" in hit["exc"]
            acc = c.get("/api/v1/admin/logs?kind=access&only_errors=true").json()["items"]
            assert any(a["rid"] == rid and a["status"] == 500 and a["method"] == "POST" for a in acc)
            text = (Path(logs.log_dir()) / "emqc.log").read_text(encoding="utf-8")
            assert f"[{rid}]" in text and "Traceback" in text
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/_test_boom"]


def test_access_log_keeps_writes_errors_and_slow_reads_only(tmp_path):
    before = len(logs.read_access(2000))
    logs.access("GET", "/api/v1/annotate/blocks/b/em/3.png", "", 200, 40, "zhang", "1.2.3.4", "aaaa")
    logs.access("GET", "/static/app.js", "", 200, 5, None, "1.2.3.4", "bbbb")
    logs.access("POST", "/api/v1/annotate/blocks/b/presence", "", 200, 5, "zhang", "1.2.3.4", "cccc")
    logs.access("POST", "/api/v1/annotate/blocks/b/paint", "", 200, 30, "zhang", "1.2.3.4", "dddd")
    logs.access("GET", "/api/v1/annotate/blocks/b/labels/3.png", "", 200, 5000, "zhang", "1.2.3.4", "eeee")
    logs.access("GET", "/api/v1/annotate/blocks/nope", "", 404, 3, "zhang", "1.2.3.4", "ffff")
    got = [a["rid"] for a in logs.read_access(2000)[: len(logs.read_access(2000)) - before]]
    assert set(got) == {"dddd", "eeee", "ffff"}, "瓦片、静态文件、心跳不记；写操作、慢请求、出错的记"


def test_client_errors_are_logged_and_rate_limited(app):
    with TestClient(app) as c:
        r = c.post("/api/v1/logs/client", json={"message": "TypeError: x is undefined", "source": "/static/annotate.js",
                                                 "line": 12, "col": 3, "stack": "at foo", "page": "/annotate"})
        assert r.status_code == 200
        items = c.get("/api/v1/admin/logs?kind=problems").json()["items"]
        assert any("前端报错" in i["message"] and "x is undefined" in i["message"] for i in items)
        codes = [c.post("/api/v1/logs/client", json={"message": f"e{i}"}).status_code for i in range(40)]
        assert 429 in codes


def test_lifecycle_detects_a_previous_run_that_did_not_stop(monkeypatch):
    sent = []
    monkeypatch.setattr(logs, "alert", lambda key, text, **kw: sent.append((key, text)) or True)
    logs.lifecycle_event("start", version="x")                 # 上一次启动了、没有 stop：比如被 kill -9
    unclean = logs.on_startup(8791)
    assert unclean and unclean["event"] == "unclean" and "unclean" in [k for k, _ in sent]
    assert logs.read_lifecycle(1)[0]["event"] == "start"
    logs.on_shutdown()
    sent.clear()
    assert logs.on_startup(8791) is None and "unclean" not in [k for k, _ in sent], "上次正常退出：不告警"
    logs.on_shutdown()


def test_supervisor_records_crashes_and_restarts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    marker = tmp_path / "runs"
    child = f"import sys, pathlib; p = pathlib.Path({str(marker)!r}); p.write_text(p.read_text() + 'x' if p.exists() else 'x'); sys.exit(3)"
    r = subprocess.run([sys.executable, str(root / "scripts" / "supervise.py"), "--max-restarts", "2", "--delay", "0.1", "--",
                        sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert marker.read_text() == "xxx", "崩了两次都被拉起来，第三次后放弃"
    exits = [e for e in logs.read_lifecycle(20) if e["event"] == "exit"]
    assert len(exits) >= 3 and exits[-1]["code"] == 3 and "异常退出" in exits[-1]["note"]


def test_alert_payloads_and_rate_limit(monkeypatch):
    from emqc.config import settings

    posted = []

    class Resp:
        def read(self):
            return b"{}"

    monkeypatch.setattr(logs.urllib.request, "urlopen", lambda req, timeout=0: posted.append(json.loads(req.data)) or Resp())
    monkeypatch.setattr(settings, "alert_webhook", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
    logs._alerted.clear()
    assert logs.alert("t1", "服务挂了", wait=True) is True
    assert logs.alert("t1", "服务又挂了", wait=True) is False, "同一类 5 分钟内只推一次"
    assert posted == [{"msgtype": "text", "text": {"content": posted[0]["text"]["content"]}}] and "服务挂了" in posted[0]["text"]["content"]
    assert logs._payload("https://open.feishu.cn/open-apis/bot/v2/hook/x", "hi") == {"msg_type": "text", "content": {"text": "hi"}}
    monkeypatch.setattr(settings, "alert_webhook", "")
    assert logs.alert("t2", "没配地址", wait=True) is False
    logs._alerted.clear()


def test_admin_overview(app):
    with TestClient(app) as c:
        o = c.get("/api/v1/admin/overview").json()
        assert o["pid"] and o["uptime_s"] >= 0 and "errors" in o and isinstance(o["online"], list)
        assert c.get("/api/v1/admin/logs?kind=lifecycle").json()["items"][0]["event"] in ("start", "stop", "unclean", "exit")
        assert "people" in c.get("/api/v1/admin/activity").json()
