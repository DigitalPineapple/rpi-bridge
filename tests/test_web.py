from contextlib import nullcontext, redirect_stdout
import http.client
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import rpi_bridge as bridge
import rpi_bridge_web as web


CONFIG = bridge.Config("eth0", "wlan0", "192.168.43.1", (8080, 8000, 5555))


class ControllerTests(unittest.TestCase):
    def test_scan_handles_escaped_ssids_and_deduplicates_by_strongest_signal(self):
        output = "FTC\\:one:40:WPA2\nFTC\\:one:85:WPA2\nFTC\\\\two:60:WPA2\n:10:--\n"
        with patch.object(bridge, "command", return_value=output):
            result = web.Controller(CONFIG).scan()
        self.assertEqual(result["networks"], [
            {"ssid": "FTC:one", "signal": 85, "security": "WPA2"},
            {"ssid": "FTC\\two", "signal": 60, "security": "WPA2"},
        ])
        for malformed in ["SSID:NaN:WPA2\n", "SSID:101:WPA2\n", "SSID:50\n"]:
            with patch.object(bridge, "command", return_value=malformed), \
                    self.assertRaises(bridge.BridgeError):
                web.Controller(CONFIG).scan()

    def test_status_uses_active_uuid_and_never_requests_secrets(self):
        with patch.object(bridge, "saved_hubs", return_value={"practice": "uuid1"}), \
                patch.object(bridge, "command", side_effect=[
                    "uuid1\n", "100 (connected)\n", "FTC-12345\n", "active\n",
                ]) as command:
            result = web.Controller(CONFIG).status()
        self.assertEqual(result["hubs"], [
            {"name": "practice", "ssid": "FTC-12345", "active": True},
        ])
        self.assertEqual(result["proxy_state"], "active")
        for call in command.call_args_list:
            self.assertNotIn("--show-secrets", call.args[0])

    def test_connect_uses_shared_switching_code_without_terminal_prompt(self):
        with patch.object(bridge, "network_operation", return_value=nullcontext()), \
                patch.object(bridge, "connect_hub") as connect:
            web.Controller(CONFIG).perform("/api/connect", {"name": "practice", "password": "testpass"})
        connect.assert_called_once_with(CONFIG, "practice", interactive=False, password="testpass")
        with self.assertRaises(ValueError):
            web.Controller(CONFIG).perform("/api/connect", {"name": "practice", "command": "bad"})

    def test_password_uses_stdin_not_argv_and_is_redacted_on_failure(self):
        calls = []
        with patch.object(bridge, "require_hub", return_value="uuid"), \
                patch.object(bridge, "pause_proxy", return_value=nullcontext()), \
                patch.object(bridge, "saved_hubs", return_value={"practice": "uuid"}), \
                patch.object(bridge, "command", side_effect=lambda *a, **kw: calls.append((a, kw))), \
                redirect_stdout(io.StringIO()):
            bridge.connect_hub(CONFIG, "practice", interactive=False, password="test:pass")
        activation = next((args, kw) for args, kw in calls if "up" in args[0])
        self.assertNotIn("--ask", activation[0][0])
        self.assertNotIn("test:pass", activation[0][0])
        self.assertEqual(activation[0][0][-2:], ["passwd-file", "/dev/stdin"])
        self.assertEqual(activation[1]["input_text"], "802-11-wireless-security.psk:test:pass\n")
        with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["nmcli"], 4, "test:pass", "test:pass"
        )), self.assertRaises(bridge.BridgeError) as raised:
            bridge.command(["nmcli"], input_text="test:pass")
        self.assertNotIn("test:pass", str(raised.exception))

    def test_password_validation_precedes_network_changes(self):
        for password in ["short", "validpass\ninject", "a" * 64, 123]:
            with self.subTest(password=password), \
                    patch.object(bridge, "pause_proxy") as pause, \
                    self.assertRaises(bridge.BridgeError):
                bridge.connect_hub(CONFIG, "practice", interactive=False, password=password)
            pause.assert_not_called()

    def test_shared_runtime_lock_rejects_concurrent_changes(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(bridge, "LOCK_PATH", Path(directory) / "control.lock"):
            with bridge.network_operation():
                with self.assertRaisesRegex(bridge.BridgeError, "Another hub operation"):
                    with bridge.network_operation():
                        self.fail("Concurrent operation acquired lock")
            with bridge.network_operation():
                pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.lock = threading.Lock()
        self.controller.status.return_value = {"hubs": [], "proxy_state": "active"}
        self.controller.scan.return_value = {"networks": []}
        self.controller.perform.return_value = {"ok": True}
        self.server = web.WebServer(
            CONFIG, bind_interface=False,
            address=("127.0.0.1", 0), controller=self.controller,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        self.addCleanup(connection.close)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def test_static_page_and_assets_have_no_cache_and_restrictive_csp(self):
        for path in ("/", "/app.js", "/style.css"):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_all_api_routes_work_without_credentials(self):
        for path, method in [
            ("/api/status", "GET"), ("/api/scan", "GET"),
            ("/api/connect", "POST"), ("/api/hubs", "POST"),
            ("/api/disconnect", "POST"),
        ]:
            with self.subTest(path=path):
                status, _, _ = self.request(path, method=method, body="{}")
                self.assertEqual(status, 200)
        self.controller.status.assert_called_once()
        self.controller.scan.assert_called_once()
        self.assertEqual(self.controller.perform.call_count, 3)

    def test_cross_origin_reads_and_mutations_are_rejected(self):
        for path, method in [("/api/status", "GET"), ("/api/connect", "POST")]:
            status, _, _ = self.request(
                path, method=method, body="{}", headers={"Origin": "http://evil.example"},
            )
            self.assertEqual(status, 403)
        self.controller.status.assert_not_called()
        self.controller.perform.assert_not_called()
        host, port = self.server.server_address
        status, _, _ = self.request("/api/status", headers={"Origin": f"http://{host}:{port}"})
        self.assertEqual(status, 200)

    def test_routes_connect_with_password_and_report_errors(self):
        status, body, _ = self.request(
            "/api/connect", method="POST", body=json.dumps({"name": "practice", "password": "testpass"}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.controller.perform.assert_called_once_with(
            "/api/connect", {"name": "practice", "password": "testpass"},
        )
        self.controller.perform.side_effect = bridge.BridgeError("Hub is offline")
        with self.assertLogs("rpi-bridge.web", level="WARNING"):
            status, body, _ = self.request("/api/connect", method="POST", body='{"name":"practice"}')
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {"error": "Hub is offline"})

    def test_busy_and_malformed_requests_do_not_start_another_operation(self):
        with self.controller.lock:
            status, _, _ = self.request("/api/scan")
            self.assertEqual(status, 409)
        for body in ("not-json", "a" * 4097):
            with self.assertLogs("rpi-bridge.web", level="WARNING"):
                status, _, _ = self.request("/api/connect", method="POST", body=body)
            self.assertEqual(status, 400)
        self.controller.perform.assert_not_called()
        status, _, _ = self.request("/api/unknown")
        self.assertEqual(status, 404)

    @unittest.skipUnless(sys.platform == "linux", "SO_BINDTODEVICE requires Linux")
    def test_linux_web_listener_is_lan_interface_bound(self):
        config = bridge.Config("lo", "wlan0", "192.168.43.1", (8080,), web_port=8090)
        with web.WebServer(config, address=("127.0.0.1", 0)) as server:
            self.assertEqual(
                server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16).rstrip(b"\0"),
                b"lo",
            )


if __name__ == "__main__":
    unittest.main()
