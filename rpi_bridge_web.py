#!/usr/bin/env python3
"""Ethernet-only web controls for a trusted team LAN."""

import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlsplit

import rpi_bridge as bridge


LOG = logging.getLogger("rpi-bridge.web")
ASSETS = Path(__file__).parent / "web"


def split_nmcli(line):
    fields = [""]
    escaped = False
    for char in line:
        if escaped:
            fields[-1] += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("")
        else:
            fields[-1] += char
    if escaped:
        raise bridge.BridgeError("Malformed escaped NetworkManager response")
    return fields


class Controller:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()

    def status(self):
        active = bridge.command([
            "nmcli", "-g", "GENERAL.CON-UUID", "device", "show", self.config.wifi_interface,
        ], timeout=10).strip()
        state = bridge.command([
            "nmcli", "-g", "GENERAL.STATE", "device", "show", self.config.wifi_interface,
        ], timeout=10).strip()
        hubs = []
        for name, uuid in sorted(bridge.saved_hubs().items()):
            ssid = bridge.command([
                "nmcli", "--escape", "no", "-g", "802-11-wireless.ssid",
                "connection", "show", "uuid", uuid,
            ], timeout=10).rstrip("\n")
            hubs.append({"name": name, "ssid": ssid, "active": uuid == active})
        proxy_state = bridge.command([
            "systemctl", "show", "--property=ActiveState", "--value", bridge.SERVICE,
        ], timeout=10).strip()
        return {
            "hubs": hubs, "wifi_state": state, "proxy_state": proxy_state,
            "wifi_interface": self.config.wifi_interface, "ports": self.config.ports,
        }

    def scan(self):
        output = bridge.command([
            "nmcli", "--wait", "15", "--colors", "no", "--escape", "yes",
            "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
            "ifname", self.config.wifi_interface, "--rescan", "yes",
        ], timeout=20)
        networks = {}
        for line in output.splitlines():
            fields = split_nmcli(line)
            if len(fields) != 3 or not fields[1].isdigit() or not 0 <= int(fields[1]) <= 100:
                raise bridge.BridgeError("Malformed Wi-Fi scan response from NetworkManager")
            ssid, strength, security = fields
            if ssid and (ssid not in networks or int(strength) > networks[ssid]["signal"]):
                networks[ssid] = {"ssid": ssid, "signal": int(strength), "security": security}
        return {"networks": sorted(networks.values(), key=lambda item: (-item["signal"], item["ssid"]))}

    def perform(self, action, data):
        allowed = {
            "/api/hubs": ({"name", "ssid"}, {"name", "ssid"}),
            "/api/connect": ({"name"}, {"name", "password"}),
            "/api/disconnect": (set(), set()),
        }
        required, optional = allowed[action]
        if (
            not isinstance(data, dict) or not required <= data.keys() or data.keys() - optional
            or any(not isinstance(value, str) for value in data.values())
        ):
            raise ValueError("Invalid request fields")
        if "name" in data:
            bridge.profile_id(data["name"])
        if "ssid" in data and any(c in data["ssid"] for c in "\r\n\0"):
            raise ValueError("SSID cannot contain line breaks or NUL")
        with bridge.network_operation():
            if action == "/api/hubs":
                bridge.add_hub(self.config, data["name"], data["ssid"])
            elif action == "/api/connect":
                bridge.connect_hub(
                    self.config, data["name"], interactive=False,
                    password=data.get("password") or None,
                )
            else:
                bridge.disconnect_hub(self.config)
        return {"ok": True}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, config, *, bind_interface=True, address=None, controller=None):
        self.config = config
        self.controller = controller or Controller(config)
        super().__init__(
            address or ("0.0.0.0", config.web_port), Handler, bind_and_activate=False,
        )
        try:
            if bind_interface:
                if not hasattr(socket, "SO_BINDTODEVICE"):
                    raise bridge.BridgeError("The web service requires Linux interface binding")
                socket.if_nametoindex(config.lan_interface)
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                    config.lan_interface.encode() + b"\0",
                )
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise


class Handler(BaseHTTPRequestHandler):
    server_version = "rpi-bridge"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_request(self, code="-", size="-"):
        LOG.info("%s %s %s", self.client_address[0], self.command, code)

    def log_message(self, format, *args):
        LOG.warning("HTTP request error from %s", self.client_address[0])

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            self.reply(403, {"error": "Cross-origin requests are not allowed"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path in assets:
            name, content_type = assets[path]
            try:
                self.reply(200, (ASSETS / name).read_bytes(), content_type)
            except OSError:
                LOG.exception("Cannot read web asset %s", name)
                self.reply(500, {"error": "Web assets are missing; reinstall rpi-bridge"})
            return
        if not self.same_origin():
            return
        if path not in ("/api/status", "/api/scan"):
            self.reply(404, {"error": "Not found"})
            return
        self.run_operation(
            self.server.controller.status if path == "/api/status" else self.server.controller.scan
        )

    def do_POST(self):
        if not self.same_origin():
            return
        path = urlsplit(self.path).path
        if path not in ("/api/hubs", "/api/connect", "/api/disconnect"):
            self.reply(404, {"error": "Not found"})
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding is not supported")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("Expected application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("Request body must be 1-4096 bytes")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request body")
            data = json.loads(body)
        except (ValueError, UnicodeError, OSError) as exc:
            LOG.warning("Rejected malformed web request: %s", type(exc).__name__)
            self.reply(400, {"error": "Invalid JSON request (maximum 4096 bytes)"})
            return
        self.run_operation(partial(self.server.controller.perform, path, data))

    def run_operation(self, operation):
        controller = self.server.controller
        if not controller.lock.acquire(blocking=False):
            self.reply(409, {"error": "Another operation is running; please wait"})
            return
        try:
            result = operation()
        except ValueError as exc:
            self.reply(400, {"error": str(exc)})
        except (bridge.BridgeError, OSError) as exc:
            LOG.warning("Hub operation failed: %s", exc)
            self.reply(502, {"error": str(exc)})
        else:
            self.reply(200, result)
        finally:
            controller.lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=bridge.DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = bridge.Config.load(args.config)
        with WebServer(config) as server:
            LOG.info("Web UI on %s TCP %s", config.lan_interface, config.web_port)
            server.serve_forever()
    except (bridge.BridgeError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
