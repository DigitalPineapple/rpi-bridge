"use strict";

const el = (id) => document.getElementById(id);
let busy = false;
let selectedHub = "";
let savedHubs = [];

function message(text, error = false) {
  el("message").textContent = text;
  el("message").classList.toggle("error", error);
  el("message").hidden = !text;
}

async function api(path, data) {
  const options = { headers: {}, cache: "no-store" };
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

async function action(work, pending) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button, input").forEach((item) => { item.disabled = true; });
  message(pending);
  try {
    await work();
  } catch (error) {
    let detail = error.message;
    try {
      await refresh();
    } catch (statusError) {
      detail += ` Status refresh failed: ${statusError.message}`;
    }
    message(detail, true);
  } finally {
    busy = false;
    document.querySelectorAll("button, input").forEach((item) => { item.disabled = false; });
  }
}

function row(title, description, buttonLabel, onClick, active = false, onRemove = null) {
  const item = document.createElement("div");
  item.className = "item";
  const text = document.createElement("div");
  const heading = document.createElement("strong");
  heading.textContent = title;
  if (active) heading.className = "active";
  const detail = document.createElement("small");
  detail.textContent = description;
  text.append(heading, detail);
  const button = document.createElement("button");
  button.textContent = buttonLabel;
  button.className = "secondary";
  button.addEventListener("click", onClick);
  const controls = document.createElement("div");
  controls.className = "actions";
  controls.append(button);
  if (onRemove) {
    const remove = document.createElement("button");
    remove.textContent = "Remove";
    remove.className = "danger";
    remove.addEventListener("click", onRemove);
    controls.append(remove);
  }
  item.append(text, controls);
  return item;
}

function chooseSaved(name) {
  selectedHub = name;
  el("connect-title").textContent = `Connect ${name}`;
  el("saved-password").value = "";
  el("connect-dialog").showModal();
}

function removeHub(hub) {
  if (busy) return;
  const warning = hub.active
    ? "\nThis hub is connected. It will disconnect and its Dashboard, tuning, and ADB sessions will close."
    : "";
  if (!window.confirm(`Remove "${hub.name}" and its saved Wi-Fi password?${warning}\nYou can add it again later.`)) return;
  action(async () => {
    await api("/api/remove", { name: hub.name });
    await refresh();
    el("nearby-hubs").replaceChildren();
    el("nearby-hubs").textContent = "Saved hubs changed. Scan networks to refresh nearby hubs.";
    message(`Removed ${hub.name} and its saved Wi-Fi password.`);
  }, `Removing ${hub.name}...`);
}

async function refresh() {
  const status = await api("/api/status");
  savedHubs = status.hubs;
  const current = savedHubs.find((hub) => hub.active);
  el("disconnect").hidden = !current;
  el("current-hub").textContent = current ? current.name : "No saved hub connected";
  el("connection-state").textContent =
    `${status.wifi_interface}: ${status.wifi_state} | Proxy: ${status.proxy_state}`;
  el("saved-hubs").replaceChildren();
  if (!savedHubs.length) el("saved-hubs").textContent = "No saved hubs yet. Add one below.";
  for (const hub of savedHubs) {
    el("saved-hubs").append(row(
      hub.name, `${hub.ssid}${hub.active ? " | Connected" : ""}`,
      hub.active ? "Reconnect" : "Connect", () => chooseSaved(hub.name), hub.active,
      () => removeHub(hub),
    ));
  }
  const host = window.location.hostname;
  el("dashboard-link").href = `http://${host}:8080/dash`;
  el("pedro-link").href = `http://${host}:10158/`;
  el("management-link").href = `http://${host}:8080/`;
  el("dashboard-link").hidden = !status.ports.includes(8080);
  el("pedro-link").hidden = !status.ports.includes(10158) || !status.ports.includes(12649);
  el("management-link").hidden = !status.ports.includes(8080);
  el("adb").textContent = status.ports.includes(5555) ? `adb connect ${host}:5555` : "";
}

async function scan() {
  const result = await api("/api/scan");
  el("nearby-hubs").replaceChildren();
  if (!result.networks.length) el("nearby-hubs").textContent = "No visible Wi-Fi networks found.";
  for (const network of result.networks) {
    const saved = savedHubs.find((hub) => hub.ssid === network.ssid);
    el("nearby-hubs").append(row(
      network.ssid, `${network.signal}% signal | ${network.security || "Open network"}`,
      saved ? "Connect" : "Add hub",
      () => {
        if (saved) {
          chooseSaved(saved.name);
        } else {
          el("ssid").value = network.ssid;
          el("name").focus();
          el("add-form").scrollIntoView({ behavior: "smooth", block: "center" });
        }
      },
    ));
  }
}

el("refresh").addEventListener("click", () => action(async () => {
  await refresh();
  message("Status updated.");
}, "Refreshing status..."));
el("scan").addEventListener("click", () => action(async () => {
  await scan();
  message("Scan complete.");
}, "Scanning nearby Wi-Fi..."));

el("add-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = el("name").value.trim();
  const ssid = el("ssid").value;
  const password = el("password").value;
  el("password").value = "";
  action(async () => {
    await api("/api/hubs", { name, ssid });
    try {
      await api("/api/connect", { name, password });
    } catch (error) {
      await refresh();
      throw new Error(`Hub saved, but connection failed: ${error.message}`);
    }
    await refresh();
    el("add-form").reset();
    message(`Connected to ${name}. Refresh Dashboard and reconnect ADB.`);
  }, `Saving and connecting to ${name}...`);
});

el("cancel-connect").addEventListener("click", () => {
  el("saved-password").value = "";
  el("connect-dialog").close();
});
el("connect-dialog").addEventListener("cancel", () => { el("saved-password").value = ""; });
el("connect-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = selectedHub;
  const password = el("saved-password").value;
  el("saved-password").value = "";
  el("connect-dialog").close();
  action(async () => {
    await api("/api/connect", { name, password });
    await refresh();
    message(`Connected to ${name}. Refresh Dashboard and reconnect ADB.`);
  }, `Connecting to ${name} (this may take up to a minute)...`);
});
el("disconnect").addEventListener("click", () => action(async () => {
  await api("/api/disconnect", {});
  await refresh();
  message("Disconnected. Automatic hub reconnection is disabled.");
}, "Disconnecting hub..."));

setInterval(() => {
  if (
    !busy && !el("connect-dialog").open &&
    document.activeElement.tagName !== "INPUT"
  ) {
    action(refresh, "");
  }
}, 15000);

action(async () => {
  await refresh();
  message("");
}, "Loading hub status...");
