# rpi-bridge technical guide

This reference is for team members and mentors configuring, maintaining, or
developing the bridge. For first-time setup and everyday use, start with the
[student quick start](../README.md).

Access an FTC Control Hub from your home network without leaving your home Wi-Fi.
A Raspberry Pi connects to the router over Ethernet and to **one hub at a time**
over Wi-Fi. Save several hubs and switch between them in a browser or from the
command line.

```text
Laptop -- home Wi-Fi -- Router -- Ethernet -- Raspberry Pi -- Wi-Fi -- Control Hub
                      internet              eth0 / wlan0            192.168.43.1
```

This is a TCP proxy, not a Layer 2 bridge. It does not enable NAT, IP forwarding,
or change the Control Hub. It forwards the same port numbers on the Pi:

| TCP port | Service | Laptop endpoint |
|----------|---------|-----------------|
| 8090 | Pi's hub-management web UI | `http://PI_IP:8090/` |
| 8080 | Hub management / Robot Controller web server | `http://PI_IP:8080/` |
| 8080 | FTC Dashboard web page | `http://PI_IP:8080/dash` |
| 8000 | Dashboard live WebSocket connection | Used automatically by Dashboard |
| 5555 | Android Debug Bridge | `adb connect PI_IP:5555` |
| 10158 | Pedro Pathing AutoTune web page | `http://PI_IP:10158/` |
| 12649 | Pedro Pathing AutoTune WebSocket connection | Used automatically by AutoTune |

Dashboard and/or Pedro Pathing AutoTune must already be included and running in
your robot application to use their respective web pages. ADB must already be
enabled and listening on the hub; the proxy does not enable it or bypass
authorization. Only the configured TCP services are exposed, not all hub
networking.

## Requirements

- Raspberry Pi OS Bookworm or newer, Python 3.11+, systemd, and NetworkManager.
- Ethernet connected to the home router, with a working internet route.
- A Wi-Fi adapter supported by NetworkManager (the Pi's built-in Wi-Fi works).
- A home LAN **outside** `192.168.43.0/24`, e.g. `192.168.1.0/24`.
- WPA2-Personal Control Hub SSIDs and their passwords.

Use a trusted development LAN only. Anyone who can reach the proxy can access
the exposed robot services, including ADB and robot-control functions. There is
no authentication on the Pi's web controls, and the bridge adds no authentication
to forwarded robot ports; existing robot-side authorization still applies.
Anyone on the LAN can switch hubs, change connections, and access robot services.
The web UI uses unencrypted HTTP: Wi-Fi passwords travel over the LAN without
TLS. Do not configure internet/router port forwarding. This is not intended for
competition use or as a substitute for a Driver Station.

## Install on the Pi

Run these commands **on the Raspberry Pi**, not on your laptop:

```sh
sudo apt update
sudo apt install git python3 network-manager iproute2
git clone https://github.com/DigitalPineapple/rpi-bridge.git
cd rpi-bridge
nmcli device status
ip -4 route
```

NetworkManager must already be running and managing the Wi-Fi interface. Recent
Raspberry Pi OS releases use it by default. Do not switch network managers over
an unattended remote connection; migrate older images locally or use a current
Raspberry Pi OS image.

The defaults are `eth0` (home LAN) and `wlan0` (hub). If your interface names
differ, create the configuration **before** installation:

```sh
sudo install -d -m 755 /etc/rpi-bridge
sudo install -m 644 config.example.json /etc/rpi-bridge/config.json
sudo nano /etc/rpi-bridge/config.json
```

These copy commands are for a new installation only. If the configuration
already exists, edit it in place instead of replacing it with the example.

Otherwise the installer creates the default configuration for you:

```sh
sudo ./install.sh
```

The installer installs both systemd services, enables them at boot, and starts
them immediately. It does not connect Wi-Fi or modify your existing home-network
profiles. No Python package installation, Node.js, or browser is needed on the Pi.

Find the Pi's Ethernet address with `ip -4 address show dev eth0`, then open
**`http://PI_IP:8090/`** from any team device on the home LAN. No login or token
is required. Use **Scan networks** to discover nearby Wi-Fi, select your hub's
SSID, and enter a saved name and Wi-Fi password under **Add a hub**.
**Save & connect** creates the profile and connects it. A failed connection
leaves the profile saved so you can retry with the correct password. All visible
Wi-Fi networks are shown; select only your Control Hubs.

For command-line setup instead:

```sh
sudo rpi-bridge add practice 'FTC-12345'
sudo rpi-bridge connect practice
sudo rpi-bridge status
```

Saved hubs have **Connect** buttons. Leave the password field blank to reuse a
saved password, or enter it to supply/update the password. The page shows the
active saved hub and proxy state, with links to Dashboard, Pedro Pathing tuning,
and hub management. **Disconnect** is hidden until a saved hub is connected.
Disconnecting leaves its profile/password saved but disables automatic
reconnection for all saved bridge hubs.

The page refreshes connection status approximately every 15 seconds when idle;
use **Refresh status** for an immediate update. Nearby Wi-Fi results update only
when you click **Scan networks**. Only one hub connects at a time; refresh
Dashboard and Pedro tuning, and reconnect ADB after a switch.

Wi-Fi password inputs are cleared when submitted. Do not use the web UI on
untrusted/shared Wi-Fi. For an encrypted UI connection, tunnel it over SSH:

```sh
ssh -N -L 8090:PI_ETHERNET_IP:8090 PI_USER@PI_HOST
# Open http://localhost:8090/ for Pi controls.
```

With this UI-only tunnel, use the Pi's LAN address (not the UI's localhost
service links) for Dashboard, Pedro tuning, and ADB, or forward their ports
separately. The tunnel encrypts your UI connection only; it does not disable or
restrict the unauthenticated portal that remains accessible on the LAN.

Use the actual SSID broadcast by your hub, not necessarily the example above.
`connect` prompts through `nmcli` for a missing password. NetworkManager manages
the saved Wi-Fi secrets; passwords are not command-line arguments, part of this
project's configuration, or written to its logs. Browser-supplied passwords are
passed to `nmcli` over stdin, not process arguments. Run interactive commands in a
terminal (for remote use: `ssh -t PI_HOST`).

The selected hub reconnects automatically on reboot. The proxy starts at boot
and can run while the hub is offline; client connections fail with an explicit
log message until the hub is reachable.

### Updating an existing installation

From the cloned project directory on the Pi:

```sh
git pull --ff-only
sudo ./install.sh
```

Re-running the installer updates the installed files and restarts both services,
dropping current Dashboard, Pedro tuning, and ADB sessions. Existing
configuration and saved hubs/passwords are preserved; obsolete admin-token
files are removed. Older
configurations without `web_port` use port 8090 automatically. Updating the
clone alone does not update the installed copy in `/opt/rpi-bridge`.
Refresh the browser page after updating to load the latest portal controls.

If upgrading an installation that predates Pedro Pathing support, add **10158
and 12649** to the `ports` list in `/etc/rpi-bridge/config.json` before restarting
the services. The installer intentionally does not replace existing settings.

## Use from your laptop

Find the Pi's Ethernet IPv4 address using `sudo rpi-bridge status` on the Pi.
Reserve this address in your router's DHCP settings so it stays stable.

```text
http://192.168.1.50:8080/
http://192.168.1.50:8080/dash
http://192.168.1.50:10158/
```

The last URL opens **Pedro Pathing AutoTune**. Its page uses the browser's host
address for the live WebSocket connection on port **12649**, so both tuning
ports must be forwarded unchanged. The bridge UI shows its tuning link only
when both ports are configured. This is AutoTune, not the separate Pedro Panels
application, which may use different ports.

For Android Studio, run from the laptop with Android platform-tools installed:

```sh
adb connect 192.168.1.50:5555
adb devices
```

Accept any required ADB authorization on the robot side using the normal FTC/REV
workflow. The hub should then appear as an ADB deployment target.

The portal displays an ADB connection command, not a laptop connection indicator.
`rpi-bridge status` checks TCP port reachability, not ADB authorization or whether
a laptop is connected. Use `adb devices -l` on the laptop: `device` means ready;
`offline` or `unauthorized` means it is not ready.

## Save and switch hubs

```sh
sudo rpi-bridge add competition 'FTC-67890'
rpi-bridge list
sudo rpi-bridge connect competition
sudo rpi-bridge connect practice
sudo rpi-bridge disconnect
```

Only one hub is connected at a time. Switching first stops the proxy to close
existing sessions, then changes Wi-Fi and restarts the previously running
service. Refresh Dashboard and Pedro tuning, and reconnect ADB after switching:

```sh
adb disconnect 192.168.1.50:5555
adb connect 192.168.1.50:5555
```

The Pi's address is the same for every hub: verify the selected hub before
deploying code or running an OpMode. If Wi-Fi activation fails, the command
reports failure and restarts the previously running proxy, but does not
guarantee restoration of the previous Wi-Fi connection. Reconnect the desired
hub explicitly.

Use this CLI or the Pi web UI to switch hubs, not an external Wi-Fi menu: both
close stale sessions before changing robots that share the same IP address.
Leave `wlan0` dedicated to hub connectivity and disable autoconnect on any
unrelated saved Wi-Fi profiles that could take it over. This tool only changes
its own `rpi-bridge-*` profiles.
`disconnect` disables their autoconnect, but leaves profiles/passwords saved.
In the web UI, use **Remove** next to a saved hub and confirm to forget its
profile and stored Wi-Fi password. Removing the connected hub also disconnects
it and closes its Dashboard, tuning, and ADB sessions. Other saved hubs are left
unchanged. This does not reset or change the Control Hub's own password.
Removing a disconnected hub does not interrupt the current hub's proxy sessions.
After removal, scan again to refresh nearby-network actions if needed.

To remove a disconnected saved hub from the command line:

```sh
sudo nmcli connection delete id rpi-bridge-competition
```

There is no `rpi-bridge remove` CLI command; use the portal for removal of a
connected hub so proxy sessions are closed before its profile is deleted.

## Configuration and operation

Edit `/etc/rpi-bridge/config.json`:

```json
{
  "lan_interface": "eth0",
  "wifi_interface": "wlan0",
  "hub_address": "192.168.43.1",
  "ports": [8080, 8000, 5555, 10158, 12649],
  "web_port": 8090
}
```

Remove `5555` if you do not need ADB. Keep **both 8080 and 8000** for Dashboard.
Keep **both 10158 and 12649** for Pedro Pathing AutoTune; remove them if your
robot application does not provide that service.
The stock Dashboard client expects WebSockets on port 8000; changing the browser
URL alone does not change that port. Configuration supports forwarding the
listed ports unchanged, not remapping them.
`web_port` is optional, defaults to 8090, and must not overlap a forwarded port.

```sh
sudo rpi-bridge check-config
sudo systemctl restart rpi-bridge
sudo systemctl restart rpi-bridge-web
sudo rpi-bridge status
sudo journalctl -u rpi-bridge -n 50 --no-pager
```

`status` checks upstream port reachability and exits nonzero if any configured
hub port is unavailable. This can be expected for 5555 when ADB is disabled or
10158/12649 when AutoTune is not running.
It does not prove that your laptop can reach the Pi; use the browser and
`adb connect` from the laptop for that.

The proxy binds listening sockets to the **Ethernet interface only** using Linux
`SO_BINDTODEVICE`, even though socket addresses are displayed as `0.0.0.0`. Hub
connections are likewise bound to the Wi-Fi interface. DHCP address changes
do not require restarting the proxy. NetworkManager hub profiles exclude Wi-Fi
default routes and DNS, preserving the existing Ethernet internet connection.
The service runs as a temporary unprivileged systemd user with `CAP_NET_RAW`
solely to allow interface binding; CLI network changes require `sudo`.
Normal password-prompting sudo is sufficient; passwordless sudo is not required
for installation, CLI use, or operation of either background service.

The separate `rpi-bridge-web` service stays available while the TCP proxy stops
and restarts during hub switching. It binds only to Ethernet, like the proxy.
It runs as root to manage NetworkManager profiles and the proxy's systemd unit,
with systemd filesystem/capability restrictions. Its API is open to the LAN and
rejects cross-origin browser requests. Browser operations are
serialized, and a shared runtime lock prevents concurrent CLI/web Wi-Fi changes.
Scan/status failures and connection errors are shown in the page rather than
reported as successful switches. Check its logs with:

```sh
sudo journalctl -u rpi-bridge-web -n 50 --no-pager
```

If you change the Wi-Fi interface in the configuration, reconnect the selected
hub with the CLI so its NetworkManager profile is updated too.

### Troubleshooting

- **Dashboard loads but stays disconnected:** check TCP 8000 and ensure Dashboard
  is actually running in the robot application.
- **Cannot reach the Pi:** check Ethernet DHCP, guest-Wi-Fi/client isolation, and
  your Pi firewall. If enabled, permit only needed TCP ports from the home LAN on
  `eth0`, including 8090 for the web controls; do not disable the firewall or
  forward ports on the router.
- **Pedro tuning loads but cannot connect:** check port 12649, and ensure
  AutoTune's live server is running on the hub. Forwarding 10158 alone only
  serves the page. The bridge does not start tuning or modify the robot code.
- **Hub unreachable:** check SSID/password, hub power, Wi-Fi band compatibility,
  `nmcli device status`, and the service logs. For a blocked adapter, use
  `sudo rfkill unblock wifi`. Configure the Pi's WLAN country through
  `sudo raspi-config` when required.
- **Internet stops working:** confirm `ip -4 route` has an Ethernet default
  route. The tool does not create or repair your home Ethernet connection.
- **ADB fails but Dashboard works:** check the hub's ADB availability and
  authorization. An enabled proxy port cannot make a disabled hub service work.
- **Permission denied / unknown interface on startup:** use the installed
  systemd service on Linux and verify interface names. Running `run` as an
  ordinary user lacks the interface-binding capability.
- **Portal reports another operation is running:** wait for the current scan
  or connection change to finish, then refresh. Operations from different team
  devices share the same controller and cannot run simultaneously.

To stop the proxy without changing the hub Wi-Fi connection:

```sh
sudo systemctl disable --now rpi-bridge
sudo systemctl disable --now rpi-bridge-web
```

## Development

No third-party Python packages are required.

```sh
python3 -m unittest discover -s tests -v
python3 rpi_bridge.py --config config.example.json check-config
sh -n install.sh
node --check web/app.js # Optional, if Node.js is available.
```

On non-Linux hosts the network tests use ordinary loopback sockets. On Linux
they exercise actual `SO_BINDTODEVICE` listeners and upstream sockets on `lo`;
run with root or `CAP_NET_RAW` for those tests. These checks cover TCP byte
forwarding, WebSocket upgrade/frames, concurrent clients, half-closes, offline
hubs, shutdown, and NetworkManager command sequencing. When changing network
behavior, also verify radio association, reboot reconnection, and physical
Control Hub services on a Pi; those are not simulated by the tests.

The web tests cover login-free HTTP requests, origin protection, password
redaction/stdin transport, Wi-Fi scan parsing, operation conflicts, saved-hub
removal (including active-hub proxy shutdown and deletion failures), and error
responses. No web framework, external assets, or Python packages are needed.
