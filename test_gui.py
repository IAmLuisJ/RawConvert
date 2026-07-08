"""API tests for rawconvert_gui.py — live server on an ephemeral port."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import rawconvert_gui


class GuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = rawconvert_gui.create_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # each test starts with a clean job slate
        rawconvert_gui.JOBS.__init__()

    def request(self, path, body=None, token=rawconvert_gui.TOKEN,
                method=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token:
            req.add_header("X-RawConvert-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)

    def wait_for_job_end(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, state = self.request("/api/job")
            if not state["running"]:
                return state
            time.sleep(0.05)
        self.fail("job did not finish in time")


class TestAuth(GuiTestCase):
    def test_api_rejects_missing_token(self):
        status, body = self.request("/api/doctor", token=None)
        self.assertEqual(status, 401)

    def test_api_rejects_wrong_token(self):
        status, _ = self.request("/api/doctor", token="nope")
        self.assertEqual(status, 401)

    def test_ping_is_tokenless(self):
        status, body = self.request("/api/ping", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "rawconvert-gui")

    def test_no_delete_endpoint_exists(self):
        for path in ("/api/delete", "/api/trash/empty", "/api/remove"):
            status, _ = self.request(path, body={}, method="POST")
            self.assertEqual(status, 404, path)


class TestReadEndpoints(GuiTestCase):
    def test_doctor(self):
        status, body = self.request("/api/doctor")
        self.assertEqual(status, 200)
        self.assertIn("sips", body)
        self.assertIn("dng_converter_url", body)

    def test_scan(self):
        (self.root / "a.CR2").write_bytes(b"x" * 1000)
        status, body = self.request("/api/scan?folder=%s" % self.root)
        self.assertEqual(status, 200)
        self.assertEqual(body["files"], 1)
        self.assertEqual(body["total_bytes"], 1000)

    def test_scan_rejects_bad_folder(self):
        status, _ = self.request("/api/scan?folder=/nonexistent")
        self.assertEqual(status, 400)

    def test_trash_report(self):
        trash = self.root / "_rawconvert_trash" / "originals"
        trash.mkdir(parents=True)
        (trash / "a.CR2").write_bytes(b"x" * 500)
        status, body = self.request("/api/trash?folder=%s" % self.root)
        self.assertEqual(status, 200)
        self.assertEqual(body["files"], 1)
        self.assertEqual(body["bytes"], 500)


STUB_JOB = r"""
import json, sys, time
print(json.dumps({"type": "step", "name": "convert"}), flush=True)
print(json.dumps({"type": "start", "total": 2, "format": "dng",
                  "phase": "convert"}), flush=True)
print(json.dumps({"type": "progress", "index": 1, "total": 2,
                  "source": "a.CR2", "output": "a.dng", "src_bytes": 100,
                  "out_bytes": 10, "eta_seconds": 1}), flush=True)
print("human noise line that must be ignored", flush=True)
print(json.dumps({"type": "failed", "source": "b.CR2", "code": "RC03",
                  "name": "UNSUPPORTED_OR_CORRUPT",
                  "diagnosis": "test diagnosis"}), flush=True)
print(json.dumps({"type": "step", "name": "verify"}), flush=True)
print(json.dumps({"type": "start", "total": 1, "phase": "verify"}),
      flush=True)
print(json.dumps({"type": "progress", "index": 1, "total": 1,
                  "source": "a.CR2", "phase": "verify",
                  "eta_seconds": 0}), flush=True)
print(json.dumps({"type": "verify_failed", "source": "a.CR2",
                  "note": "dimension mismatch"}), flush=True)
print(json.dumps({"type": "summary", "converted": 1, "skipped": 0,
                  "verified": 0, "verify_failed": 1, "staged": 0,
                  "failed": 2, "trash": "/t", "error_log": "/e"}), flush=True)
"""

STUB_SLEEPER = r"""
import json, time
print(json.dumps({"type": "start", "total": 1, "format": "dng"}), flush=True)
time.sleep(30)
"""


class TestJobLifecycle(GuiTestCase):
    def start_job(self, stub):
        with mock.patch.object(rawconvert_gui, "build_job_cmd",
                               return_value=[sys.executable, "-c", stub]):
            return self.request("/api/job",
                                body={"folder": str(self.root),
                                      "options": {"format": "dng"}})

    def test_job_runs_and_reports_events(self):
        status, body = self.start_job(STUB_JOB)
        self.assertEqual(status, 200)
        self.assertTrue(body["started"])
        state = self.wait_for_job_end()
        # each phase resets the bar; the last start was verify (total 1)
        self.assertEqual(state["phase"], "verify")
        self.assertEqual(state["total"], 1)
        self.assertEqual(state["done"], 1)
        self.assertEqual(state["step"], "done")
        self.assertEqual(state["summary"]["verify_failed"], 1)
        self.assertEqual(len(state["failures"]), 2)
        self.assertEqual(state["failures"][0]["code"], "RC03")
        self.assertEqual(state["failures"][1]["code"], "VERIFY")
        self.assertIn("dimension mismatch",
                      state["failures"][1]["diagnosis"])
        self.assertEqual(state["returncode"], 0)

    def test_second_job_rejected_while_running(self):
        status, _ = self.start_job(STUB_SLEEPER)
        self.assertEqual(status, 200)
        status, body = self.start_job(STUB_JOB)
        self.assertEqual(status, 409)
        self.request("/api/job/cancel", body={}, method="POST")
        self.wait_for_job_end()

    def test_cancel_terminates_job(self):
        self.start_job(STUB_SLEEPER)
        time.sleep(0.3)
        status, body = self.request("/api/job/cancel", body={},
                                    method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(body["cancelled"])
        state = self.wait_for_job_end()
        self.assertTrue(state["cancelled"])

    def test_job_rejects_bad_folder(self):
        status, _ = self.request("/api/job",
                                 body={"folder": "/nonexistent",
                                       "options": {}})
        self.assertEqual(status, 400)


class TestQuitEndpoint(unittest.TestCase):
    """Own server instance — quitting shuts it down."""

    def test_quit_shuts_down_server(self):
        server = rawconvert_gui.create_server(0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/quit" % port, data=b"{}")
        req.add_header("X-RawConvert-Token", rawconvert_gui.TOKEN)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(json.load(resp)["ok"], True)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "serve_forever should return")
        server.server_close()


class TestFrozenCliDispatch(unittest.TestCase):
    def test_run_cli_env_dispatches_to_rawconvert_main(self):
        with mock.patch.dict(rawconvert_gui.os.environ,
                             {"RAWCONVERT_RUN_CLI": "1"}), \
             mock.patch.object(rawconvert_gui.rawconvert, "main",
                               return_value=7) as cli:
            rc = rawconvert_gui.main(["doctor"])
        self.assertEqual(rc, 7)
        cli.assert_called_once_with(["doctor"])

    def test_frozen_job_cmd_reinvokes_self(self):
        with mock.patch.object(rawconvert_gui, "FROZEN", True):
            cmd = rawconvert_gui.build_job_cmd("/photos",
                                               {"format": "dng"})
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "process")
        self.assertNotIn("rawconvert.py", " ".join(cmd))


if __name__ == "__main__":
    unittest.main()
