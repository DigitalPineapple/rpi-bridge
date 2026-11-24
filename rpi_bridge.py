#!/usr/bin/env python3
"""LAN-only Control Hub TCP proxy and NetworkManager profile manager."""

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import logging
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys


DEFAULT_CONFIG = Path("/etc/rpi-bridge/config.json")
LOCK_PATH = Path("/run/rpi-bridge/control.lock")
PROFILE_PREFIX = "rpi-bridge-"
SERVICE = "rpi-bridge.service"
LOG = logging.getLogger("rpi-bridge")


class BridgeError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    lan_interface: str
    wifi_interface: str
    hub_address: str
    ports: tuple[int, ...]
    web_port: int = 8090

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise BridgeError(f"Cannot read config {path}: {exc}") from exc
        expected = {"lan_interface", "wifi_interface", "hub_address", "ports"}
        if (
            not isinstance(data, dict)
            or not expected <= data.keys()
            or data.keys() - expected - {"web_port"}
        ):
            raise BridgeError(f"Config requires {', '.join(sorted(expected))}; web_port is optional")
        for key in ("lan_interface", "wifi_interface"):
            value = data[key]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value):
                raise BridgeError(f"Invalid {key}")
        if data["lan_interface"] == data["wifi_interface"]:
            raise BridgeError("LAN and hub Wi-Fi must use different interfaces")
        if not isinstance(data["hub_address"], str):
            raise BridgeError("hub_address must be an IPv4 address string")
        try:
            address = ipaddress.IPv4Address(data["hub_address"])
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise BridgeError("hub_address must be an IPv4 address") from exc
        if address.is_multicast or address.is_unspecified or address.is_loopback or int(address) == 0xFFFFFFFF:
            raise BridgeError("hub_address must be a unicast, non-loopback IPv4 address")
        ports = data["ports"]
        if (
            not isinstance(ports, list)
            or not ports
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports)
        ):
            raise BridgeError("ports must be a nonempty list of unique TCP ports (1-65535)")
        web_port = data.get("web_port", 8090)
        if type(web_port) is not int or not 1024 <= web_port <= 65535 or web_port in ports:
            raise BridgeError("web_port must be 1024-65535 and not overlap proxy ports")
        return cls(data["lan_interface"], data["wifi_interface"], str(address), tuple(ports), web_port)


def command(args, *, capture=True, input_text=None, timeout=None):
    try:
        result = subprocess.run(
            args, text=True, capture_output=capture, check=False,
            input=input_text, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"{args[0]} timed out") from exc
    except OSError as exc:
        raise BridgeError(f"Cannot run {args[0]}: {exc}") from exc
    if result.returncode:
        if input_text is not None:
            detail = "Wi-Fi activation failed; check the password and hub availability"
        else:
            detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise BridgeError(f"{args[0]} exited with status {result.returncode}: {detail}")
    return result.stdout if capture else ""


@contextmanager
def network_operation():
    """Serialize browser and CLI changes to the shared Wi-Fi interface."""
    LOCK_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK_PATH.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("Another hub operation is running; try again when it finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def profile_id(alias):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", alias):
        raise BridgeError("Hub name must be 1-48 letters, digits, underscores or hyphens")
    return PROFILE_PREFIX + alias


def saved_hubs():
    output = command(["nmcli", "-t", "-f", "UUID,NAME", "connection", "show"], timeout=10)
    hubs = {}
    for line in output.splitlines():
        uuid, separator, name = line.partition(":")
        if separator and name.startswith(PROFILE_PREFIX):
            alias = name[len(PROFILE_PREFIX):]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", alias):
                if alias in hubs:
                    raise BridgeError(f"Duplicate NetworkManager profile: {name}")
                hubs[alias] = uuid
    return hubs


def require_hub(alias):
    profile_id(alias)
    hubs = saved_hubs()
    if alias not in hubs:
        raise BridgeError(f"Unknown hub {alias!r}; use 'rpi-bridge add' first")
    return hubs[alias]


@contextmanager
def pause_proxy():
    """Drop old robot sessions before changing networks with identical hub IPs."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE], check=False
        )
    except OSError as exc:
        raise BridgeError(f"Cannot query service: {exc}") from exc
    if result.returncode not in (0, 3):
        raise BridgeError("Cannot query rpi-bridge service; install it first")
    active = result.returncode == 0
    if active:
        command(["systemctl", "stop", SERVICE])
    try:
        yield
    finally:
        if active:
            command(["systemctl", "start", SERVICE])


def add_hub(config, alias, ssid):
    name = profile_id(alias)
    if not ssid or len(ssid.encode("utf-8")) > 32 or "\x00" in ssid:
        raise BridgeError("SSID must contain 1-32 UTF-8 bytes and no NUL")
    if alias in saved_hubs():
        raise BridgeError(f"Hub {alias!r} already exists")
    command([
        "nmcli", "connection", "add", "type", "wifi",
        "ifname", config.wifi_interface, "con-name", name, "ssid", ssid,
        "connection.autoconnect", "no",
        "ipv4.method", "auto", "ipv4.never-default", "yes",
        "ipv4.ignore-auto-dns", "yes", "ipv6.method", "disabled",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk-flags", "0",
    ])
    print(f"Saved {alias}. Run 'sudo rpi-bridge connect {alias}' to enter its Wi-Fi password.")


def connect_hub(config, alias, *, interactive=True, password=None):
    if password is not None and not (
        isinstance(password, str)
        and (
            (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password))
            or re.fullmatch(r"[0-9a-fA-F]{64}", password)
        )
    ):
        raise BridgeError("Password must be 8-63 printable ASCII characters or 64 hex digits")
    uuid = require_hub(alias)
    with pause_proxy():
        # Reapply routing constraints even if someone edited the saved profile.
        command([
            "nmcli", "connection", "modify", "uuid", uuid,
            "connection.interface-name", config.wifi_interface,
            "ipv4.never-default", "yes", "ipv4.ignore-auto-dns", "yes",
            "ipv6.method", "disabled",
        ])
        up = [
            "nmcli", *(["--ask"] if interactive else []), "--wait", "45", "connection", "up",
            "uuid", uuid, "ifname", config.wifi_interface,
        ]
        if password is not None:
            up.extend(["passwd-file", "/dev/stdin"])
            command(
                up, input_text=f"802-11-wireless-security.psk:{password}\n",
                timeout=60,
            )
        elif interactive:
            command(up, capture=False)
        else:
            command(up, input_text="", timeout=60)
        for other_uuid in saved_hubs().values():
            if other_uuid != uuid:
                command([
                    "nmcli", "connection", "modify", "uuid", other_uuid,
                    "connection.autoconnect", "no",
                ])
        command([
            "nmcli", "connection", "modify", "uuid", uuid,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "100",
        ])
    print(f"Connected to {alias}; it will reconnect after reboot.")


def disconnect_hub(config):
    with pause_proxy():
        for uuid in saved_hubs().values():
            command([
                "nmcli", "connection", "modify", "uuid", uuid,
                "connection.autoconnect", "no",
            ])
        command(["nmcli", "device", "disconnect", config.wifi_interface])
    print("Disconnected hub Wi-Fi and disabled saved-hub autoconnect.")


def remove_hub(config, alias):
    uuid = require_hub(alias)
    active = command([
        "nmcli", "-g", "GENERAL.CON-UUID", "device", "show", config.wifi_interface,
    ], timeout=10).strip()
    with pause_proxy() if active == uuid else nullcontext():
        command(["nmcli", "--wait", "15", "connection", "delete", "uuid", uuid], timeout=20)


def device_socket(interface):
    if not hasattr(socket, "SO_BINDTODEVICE"):
        raise BridgeError("The proxy requires Linux SO_BINDTODEVICE (run it on the Pi)")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


async def open_hub(config, port):
    sock = device_socket(config.wifi_interface)
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_connect(sock, (config.hub_address, port)),
            timeout=5,
        )
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()


async def relay(client_reader, client_writer, hub_reader, hub_writer):
    tasks = [
        asyncio.create_task(copy_stream(client_reader, hub_writer)),
        asyncio.create_task(copy_stream(hub_reader, client_writer)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=3)
    except TimeoutError:
        LOG.warning("Timed out closing TCP connection; aborting transport")
        writer.transport.abort()
    except OSError as exc:
        LOG.debug("Socket closed with error: %s", exc)


class Proxy:
    def __init__(self, config):
        self.config = config
        self.sessions = set()
        self.servers = []

    async def handle(self, reader, writer, port):
        task = asyncio.current_task()
        self.sessions.add(task)
        hub_writer = None
        try:
            hub_reader, hub_writer = await open_hub(self.config, port)
            await relay(reader, writer, hub_reader, hub_writer)
        except (OSError, TimeoutError, BridgeError) as exc:
            LOG.warning("TCP %s from %s failed: %s", port, writer.get_extra_info("peername"), exc)
        finally:
            if hub_writer is not None:
                await close_writer(hub_writer)
            await close_writer(writer)
            self.sessions.discard(task)

    async def start(self):
        try:
            for port in self.config.ports:
                sock = device_socket(self.config.lan_interface)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("0.0.0.0", port))
                    sock.listen(socket.SOMAXCONN)
                    server = await asyncio.start_server(
                        lambda r, w, p=port: self.handle(r, w, p), sock=sock
                    )
                except BaseException:
                    sock.close()
                    raise
                self.servers.append(server)
                LOG.info(
                    "TCP %s on %s -> %s:%s via %s",
                    port, self.config.lan_interface, self.config.hub_address,
                    port, self.config.wifi_interface,
                )
        except BaseException:
            await self.close()
            raise

    async def close(self):
        for server in self.servers:
            server.close()
        sessions = list(self.sessions)
        for task in sessions:
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)
        await asyncio.gather(*(server.wait_closed() for server in self.servers))
        self.servers.clear()


async def serve(config):
    for interface in (config.lan_interface, config.wifi_interface):
        try:
            socket.if_nametoindex(interface)
        except OSError as exc:
            raise BridgeError(f"Network interface {interface!r} does not exist") from exc
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    proxy = Proxy(config)
    try:
        await proxy.start()
        await stop.wait()
    finally:
        await proxy.close()


def status(config):
    command([
        "systemctl", "--no-pager", "show",
        "--property=ActiveState,SubState,Result", SERVICE,
    ], capture=False)
    command([
        "nmcli", "-f", "GENERAL.DEVICE,GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY",
        "device", "show", config.wifi_interface,
    ], capture=False)
    output = command(["ip", "-j", "-4", "address", "show", "dev", config.lan_interface])
    try:
        addresses = [
            entry["local"]
            for device in json.loads(output)
            for entry in device.get("addr_info", [])
            if entry.get("scope") == "global"
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise BridgeError(f"Cannot parse LAN addresses: {exc}") from exc
    if not addresses:
        raise BridgeError(f"No LAN IPv4 address on {config.lan_interface}")
    for address in addresses:
        print(f"Pi hub controls: http://{address}:{config.web_port}/")
        if 8080 in config.ports:
            print(f"Dashboard / management: http://{address}:8080/ (Dashboard: /dash)")
        if 10158 in config.ports and 12649 in config.ports:
            print(f"Pedro Pathing tuning: http://{address}:10158/")
        if 5555 in config.ports:
            print(f"ADB: adb connect {address}:5555")
    print("Checking hub TCP services through Wi-Fi:")
    unreachable = []
    for port in config.ports:
        async def probe():
            _, writer = await open_hub(config, port)
            await close_writer(writer)
        try:
            asyncio.run(probe())
        except (OSError, TimeoutError, BridgeError) as exc:
            print(f"  {port}: UNREACHABLE ({exc})")
            unreachable.append(str(port))
        else:
            print(f"  {port}: reachable")
    if unreachable:
        raise BridgeError(f"Hub services unreachable: {', '.join(unreachable)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add", help="Save a WPA2 Control Hub Wi-Fi profile")
    add.add_argument("name")
    add.add_argument("ssid")
    connect = sub.add_parser("connect", help="Switch hubs; prompt for password if needed")
    connect.add_argument("name")
    sub.add_parser("disconnect", help="Disconnect hub Wi-Fi and disable hub autoconnect")
    sub.add_parser("list", help="List saved hub names and NetworkManager UUIDs")
    sub.add_parser("status", help="Show service, Wi-Fi, LAN endpoints and hub port reachability")
    sub.add_parser("check-config", help="Validate the configuration")
    sub.add_parser("run", help="Run the TCP proxy (normally managed by systemd)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        if args.action == "add":
            with network_operation():
                add_hub(config, args.name, args.ssid)
        elif args.action == "connect":
            with network_operation():
                connect_hub(config, args.name)
        elif args.action == "disconnect":
            with network_operation():
                disconnect_hub(config)
        elif args.action == "list":
            for alias, uuid in sorted(saved_hubs().items()):
                print(f"{alias}\t{uuid}")
        elif args.action == "status":
            status(config)
        elif args.action == "check-config":
            print("Configuration OK")
        elif args.action == "run":
            asyncio.run(serve(config))
    except (BridgeError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
