import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import rpi_bridge as bridge


CONFIG = bridge.Config("eth0", "wlan0", "192.168.43.1", (8080, 8000, 5555))


class ConfigTests(unittest.TestCase):
    def load(self, **updates):
        data = {
            "lan_interface": "eth0", "wifi_interface": "wlan0",
            "hub_address": "192.168.43.1", "ports": [8080, 8000, 5555],
        }
        data.update(updates)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data))
            return bridge.Config.load(path)

    def test_load_and_reject_unsafe_config(self):
        self.assertEqual(self.load(), CONFIG)
        for updates in [
            {"wifi_interface": "eth0"}, {"lan_interface": "invalid/name"},
            {"hub_address": "localhost"}, {"hub_address": True},
            {"hub_address": "127.0.0.1"}, {"hub_address": "224.0.0.1"},
            {"ports": []}, {"ports": [8000, 8000]}, {"ports": [True]},
            {"ports": [65536]}, {"extra": "typo"},
            {"web_port": 8080}, {"web_port": True}, {"web_port": 1023},
        ]:
            with self.subTest(updates=updates), self.assertRaises(bridge.BridgeError):
                self.load(**updates)


class HubTests(unittest.TestCase):
    def test_add_keeps_wifi_off_default_route_and_no_password_in_args(self):
        with patch.object(bridge, "command") as command, \
                patch.object(bridge, "saved_hubs", return_value={}), \
                redirect_stdout(io.StringIO()):
            bridge.add_hub(CONFIG, "practice", "FTC-12345")
        args = command.call_args.args[0]
        for key, expected in [
            ("connection.autoconnect", "no"), ("ipv4.never-default", "yes"),
            ("ipv4.ignore-auto-dns", "yes"), ("ipv6.method", "disabled"),
            ("wifi-sec.psk-flags", "0"),
        ]:
            self.assertEqual(args[args.index(key) + 1], expected)
        self.assertNotIn("wifi-sec.psk", args)

    def test_duplicate_hubs_and_invalid_names_are_errors(self):
        with patch.object(bridge, "saved_hubs", return_value={"practice": "uuid"}):
            with self.assertRaises(bridge.BridgeError):
                bridge.add_hub(CONFIG, "practice", "FTC-12345")
        for name in ["", "-x", "two hubs", "../hub"]:
            with self.subTest(name=name), self.assertRaises(bridge.BridgeError):
                bridge.profile_id(name)

    def test_listing_ignores_unrelated_profiles_and_detects_duplicate_aliases(self):
        with patch.object(bridge, "command", return_value=(
            "uuid1:rpi-bridge-practice\nuuid2:Home WiFi\nuuid3:Guest\\:Wifi\n"
        )):
            self.assertEqual(bridge.saved_hubs(), {"practice": "uuid1"})
        with patch.object(bridge, "command", return_value=(
            "uuid1:rpi-bridge-practice\nuuid2:rpi-bridge-practice\n"
        )), self.assertRaises(bridge.BridgeError):
            bridge.saved_hubs()

    def test_switch_closes_old_sessions_and_remembers_only_selected_hub(self):
        events = []

        def command(args, **kwargs):
            events.append(args)
            return ""

        with patch.object(bridge, "command", side_effect=command), \
                patch.object(bridge, "saved_hubs", return_value={
                    "practice": "selected", "other": "old",
                }), \
                patch.object(bridge.subprocess, "run", return_value=Mock(returncode=0)), \
                redirect_stdout(io.StringIO()):
            bridge.connect_hub(CONFIG, "practice")
        self.assertEqual(events[0], ["systemctl", "stop", bridge.SERVICE])
        self.assertEqual(events[-1], ["systemctl", "start", bridge.SERVICE])
        self.assertIn([
            "nmcli", "--ask", "--wait", "45", "connection", "up",
            "uuid", "selected", "ifname", "wlan0",
        ], events)
        self.assertIn([
            "nmcli", "connection", "modify", "uuid", "old",
            "connection.autoconnect", "no",
        ], events)
        self.assertIn([
            "nmcli", "connection", "modify", "uuid", "selected",
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
        ], events)

    def test_failed_switch_restores_proxy_but_does_not_claim_success(self):
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if "up" in args:
                raise bridge.BridgeError("wrong Wi-Fi password")
            return ""

        with patch.object(bridge, "command", side_effect=command), \
                patch.object(bridge, "saved_hubs", return_value={"practice": "uuid"}), \
                patch.object(bridge.subprocess, "run", return_value=Mock(returncode=0)), \
                self.assertRaisesRegex(bridge.BridgeError, "wrong Wi-Fi password"):
            bridge.connect_hub(CONFIG, "practice")
        self.assertEqual(calls[-1], ["systemctl", "start", bridge.SERVICE])
        self.assertFalse(any("connection.autoconnect" in args for args in calls))

    def test_disconnect_disables_boot_reconnect_before_disconnecting(self):
        with patch.object(bridge, "command") as command, \
                patch.object(bridge, "saved_hubs", return_value={"practice": "uuid"}), \
                patch.object(bridge.subprocess, "run", return_value=Mock(returncode=3)), \
                redirect_stdout(io.StringIO()):
            bridge.disconnect_hub(CONFIG)
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual(calls, [
            ["nmcli", "connection", "modify", "uuid", "uuid", "connection.autoconnect", "no"],
            ["nmcli", "device", "disconnect", "wlan0"],
        ])

    def test_command_failure_surfaces_stderr(self):
        with patch.object(bridge.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["nmcli"], 10, "", "NetworkManager is not running"
        )), self.assertRaisesRegex(bridge.BridgeError, "NetworkManager is not running"):
            bridge.command(["nmcli"])


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        if sys.platform != "linux":
            def unbound_socket(interface):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(False)
                return sock
            self.stack.enter_context(patch.object(bridge, "device_socket", unbound_socket))
        self.hub = None
        self.proxy = None
        self.hub_tasks = set()

    async def asyncTearDown(self):
        if self.proxy:
            await asyncio.wait_for(self.proxy.close(), 5)
        if self.hub:
            self.hub.close()
            await self.hub.wait_closed()
        tasks = list(self.hub_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def setup_proxy(self, handler):
        async def serve(reader, writer):
            task = asyncio.current_task()
            self.hub_tasks.add(task)
            try:
                await handler(reader, writer)
            finally:
                await bridge.close_writer(writer)
                self.hub_tasks.discard(task)

        self.hub = await asyncio.start_server(serve, "127.0.0.1", 0)
        hub_port = self.hub.sockets[0].getsockname()[1]
        original_open = bridge.open_hub

        async def connect(config, port):
            return await original_open(config, hub_port)

        self.stack.enter_context(patch.object(bridge, "open_hub", connect))
        self.proxy = bridge.Proxy(bridge.Config("lo", "lo", "127.0.0.1", (0,)))
        await self.proxy.start()
        self.port = self.proxy.servers[0].sockets[0].getsockname()[1]

    async def test_binary_roundtrip_concurrent_clients_and_half_close(self):
        async def hub(reader, writer):
            body = await reader.read()
            writer.write(body[::-1])
            await writer.drain()

        await self.setup_proxy(hub)

        async def client(index):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            payload = bytes(range(256)) * (1024 + index)
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), payload[::-1])
            await bridge.close_writer(writer)

        await asyncio.gather(*(client(index) for index in range(4)))

    async def test_http_websocket_upgrade_and_bidirectional_frames(self):
        request = (
            b"GET / HTTP/1.1\r\nHost: pi:8000\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        )
        response = (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
        )
        frame = b"\x81\x82\x01\x02\x03\x04ik"
        reply = b"\x81\x02hi"

        async def hub(reader, writer):
            self.assertEqual(await reader.readuntil(b"\r\n\r\n"), request)
            writer.write(response)
            await writer.drain()
            self.assertEqual(await reader.readexactly(len(frame)), frame)
            writer.write(reply)
            await writer.drain()

        await self.setup_proxy(hub)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(request)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5), response)
        writer.write(frame)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(reply)), 5), reply)
        await bridge.close_writer(writer)

    async def test_unreachable_hub_logs_error_and_closes_client(self):
        async def unused(reader, writer):
            pass

        await self.setup_proxy(unused)
        with patch.object(bridge, "open_hub", AsyncMock(side_effect=ConnectionRefusedError("offline"))), \
                self.assertLogs("rpi-bridge", level="WARNING") as logs:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
            await bridge.close_writer(writer)
        self.assertIn("offline", logs.output[0])

    async def test_shutdown_closes_established_connections(self):
        connected = asyncio.Event()

        async def hub(reader, writer):
            connected.set()
            await reader.read()

        await self.setup_proxy(hub)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        await asyncio.wait_for(connected.wait(), 5)
        await asyncio.wait_for(self.proxy.close(), 5)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
        await bridge.close_writer(writer)
        self.assertFalse(self.proxy.sessions)

    @unittest.skipUnless(sys.platform == "linux", "SO_BINDTODEVICE requires Linux")
    async def test_linux_listener_and_upstream_are_interface_bound(self):
        async def unused(reader, writer):
            pass

        await self.setup_proxy(unused)
        listener = self.proxy.servers[0].sockets[0]
        self.assertEqual(
            listener.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16).rstrip(b"\0"),
            b"lo",
        )
        _, writer = await bridge.open_hub(self.proxy.config, self.port)
        self.assertEqual(
            writer.get_extra_info("socket").getsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16
            ).rstrip(b"\0"),
            b"lo",
        )
        await bridge.close_writer(writer)


if __name__ == "__main__":
    unittest.main()
