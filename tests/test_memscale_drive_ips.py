"""IPSDriver.setup integrity (training-metrics review 0df4258 B1): a fresh-server
run refuses a busy port, fails when its server process exits before it is
ready, and fails when the answering server's report does not match the
configured pool size. Real processes and a real local HTTP server, no IPS."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale.drive_ips import IPSDriver  # noqa: E402

FAKE_SERVER = r"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, pool = int(sys.argv[1]), int(sys.argv[2])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ready", "max_pool_size": pool}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def driver(tmp_path, port, start_cmd, pool="40"):
    return IPSDriver(
        heap="HOL",
        imports=[],
        url=f"http://127.0.0.1:{port}",
        external=False,
        start_cmd=start_cmd,
        env={"ISABELLE_POOL_SIZE": pool, "ISABELLE_SERVER_LOG_DIR": str(tmp_path)},
        ready_timeout_s=20,
        repo_root=str(tmp_path),
        scope="no",
    )


def test_a_busy_port_is_refused(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = driver(tmp_path, srv.server_address[1], [sys.executable, "-c", "pass"])
        with pytest.raises(RuntimeError, match="port busy"):
            asyncio.run(d.setup())
        assert d.proc is None  # nothing was started
    finally:
        srv.shutdown()


def test_a_server_that_exits_before_ready_fails_the_setup(tmp_path):
    d = driver(
        tmp_path,
        free_port(),
        [sys.executable, "-c", "import sys; print('bind failed'); sys.exit(3)"],
    )
    with pytest.raises(RuntimeError, match=r"exited \(rc 3\)") as err:
        asyncio.run(d.setup())
    assert "bind failed" in str(err.value)  # the log tail is in the error
    asyncio.run(d.stop_all())


def test_the_answering_server_must_report_the_configured_pool(tmp_path):
    port = free_port()
    cmd = [sys.executable, "-c", FAKE_SERVER, str(port), "6"]
    d = driver(tmp_path, port, cmd, pool="40")
    with pytest.raises(RuntimeError, match="max_pool_size=6, configured 40"):
        asyncio.run(d.setup())
    asyncio.run(d.stop_all())
    port = free_port()
    d = driver(tmp_path, port, [sys.executable, "-c", FAKE_SERVER, str(port), "40"])
    try:
        asyncio.run(d.setup())
        facts = d.describe()
        assert facts["start_cmd"][0] == sys.executable
        assert facts["server_pgid"] == d.proc.pid
        json.dumps(facts)
    finally:
        asyncio.run(d.stop_all())
    assert d.proc is None
