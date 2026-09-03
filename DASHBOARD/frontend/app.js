"use strict";

const state = {
  snapshot: null,
  socket: null,
  reconnectTimer: null,
  refreshTimer: null,
  connected: false,
  operator: {
    access_mode: "read_only",
    authenticated: false,
    busy: false,
  },
};

const $ = (id) => document.getElementById(id);

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value ?? "Unknown";
}

function humanize(value) {
  if (value === null || value === undefined || value === "") return "Unknown";
  return String(value)
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function parseDate(value) {
  if (value === null || value === undefined) return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function timeLabel(value, includeDate = false) {
  const date = parseDate(value);
  if (!date) return "Time unavailable";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const options = includeDate || !sameDay
    ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Intl.DateTimeFormat(undefined, options).format(date);
}

function relativeTime(value) {
  const date = parseDate(value);
  if (!date) return "Never";
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 5) return "Just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return timeLabel(value, true);
}

function booleanLabel(value, trueText = "Detected", falseText = "Clear") {
  if (value === true) return trueText;
  if (value === false) return falseText;
  return "Unknown";
}

function onOff(value) {
  if (value === true) return "On";
  if (value === false) return "Off";
  return "Unknown";
}

function valueOf(reading) {
  return reading && Object.hasOwn(reading, "value") ? reading.value : null;
}

function statusDot(status) {
  const dot = document.createElement("span");
  dot.className = `status-dot status-${status || "unknown"}`;
  return dot;
}

function showView(viewName) {
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("is-visible", panel.dataset.viewPanel === viewName);
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === viewName;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  history.replaceState(null, "", `#${viewName}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setConnection(connected, label) {
  state.connected = connected;
  const pill = $("connection-pill");
  pill.replaceChildren(statusDot(connected ? "online" : "offline"));
  const text = document.createElement("span");
  text.id = "connection-label";
  text.textContent = label;
  pill.append(text);
}

function serviceRow(name, status, detail) {
  const row = document.createElement("div");
  row.className = "service-row";
  row.append(statusDot(status));

  const copy = document.createElement("div");
  copy.className = "service-row-copy";
  const heading = document.createElement("strong");
  heading.textContent = name;
  const secondary = document.createElement("small");
  secondary.textContent = detail;
  copy.append(heading, secondary);

  const stateLabel = document.createElement("span");
  stateLabel.className = "service-state";
  stateLabel.textContent = humanize(status);
  row.append(copy, stateLabel);
  return row;
}

function voicePresentation(snapshot) {
  const sidecarOnline = snapshot.system?.services?.voice_sidecar?.online === true;
  const voice = snapshot.voice || {};

  if (!sidecarOnline) {
    return {
      label: "Voice offline",
      stateClass: "is-offline",
      detail: "Conversational interface offline",
    };
  }
  const remoteDetail = voice.controller_online === true
    ? "Voice remote online"
    : "Voice remote offline";
  if (voice.microphone_enabled === true) {
    return {
      label: "Listening",
      stateClass: "is-listening",
      detail: `VENUS listening · ${remoteDetail}`,
    };
  }
  return {
    label: "Muted",
    stateClass: "is-muted",
    detail: `Microphone muted · ${remoteDetail}`,
  };
}

function renderVoice(snapshot) {
  const presentation = voicePresentation(snapshot);
  const pill = $("microphone-pill");
  pill.className = `microphone-pill ${presentation.stateClass}`;
  pill.title = presentation.detail;
  setText("microphone-label", presentation.label);

  const voice = snapshot.voice || {};
  const sidecarOnline = snapshot.system?.services?.voice_sidecar?.online === true;
  const panel = $("voice-controls");
  panel.classList.toggle("is-listening", sidecarOnline && voice.microphone_enabled === true);
  panel.classList.toggle("is-offline", !sidecarOnline);
  setText("operator-microphone-state", sidecarOnline ? presentation.label : "Voice offline");
  setText(
    "operator-voice-remote-state",
    `${voice.controller_online === true ? "Physical remote online" : "Physical remote offline"} · Last change: ${humanize(voice.changed_by || "unknown")}`
  );
}

function renderServices(snapshot) {
  const services = snapshot.system?.services || {};
  const nodes = snapshot.nodes || {};
  const voice = voicePresentation(snapshot);
  const definitions = [
    ["VENUS Core", services.core?.online ? "online" : "offline", "Monitoring and coordination runtime"],
    ["MQTT broker", services.mqtt?.online ? "online" : "offline", "UNS telemetry connection"],
    ["Voice sidecar", services.voice_sidecar?.online ? "online" : "offline", voice.detail],
    ["Box 1", nodes.box_1?.status || "unknown", nodeDetail(nodes.box_1)],
    ["Box 2", nodes.box_2?.status || "unknown", nodeDetail(nodes.box_2)],
  ];

  const list = $("overview-service-list");
  list.replaceChildren(...definitions.map((item) => serviceRow(...item)));
  const onlineCount = definitions.filter(([, status]) => status === "online").length;
  setText("service-count", `${onlineCount} of ${definitions.length} online`);
}

function nodeDetail(node) {
  if (!node?.last_seen) return "No telemetry received";
  if (node.status === "stale") return `Data delayed · ${relativeTime(node.last_seen)}`;
  if (node.status === "offline") return `Telemetry missing · ${relativeTime(node.last_seen)}`;
  return `Latest telemetry · ${relativeTime(node.last_seen)}`;
}

function renderEnvironment(snapshot) {
  const environment = snapshot.environment || {};
  const temperature = valueOf(environment.temperature);
  const humidity = valueOf(environment.humidity);
  const gas = valueOf(environment.gas);
  const flame = valueOf(environment.fire_detected);
  const valid = valueOf(environment.dht_valid);
  const boxStatus = snapshot.nodes?.box_1?.status || "unknown";

  setText("temperature-value", temperature ?? "—");
  setText("humidity-value", humidity ?? "—");
  setText("gas-summary", booleanLabel(gas));
  setText("flame-summary", booleanLabel(flame));
  setText("sensor-validity", valid === true ? "Valid" : valid === false ? "Invalid" : "Unknown");

  const chip = $("environment-chip");
  chip.replaceChildren(statusDot(boxStatus));
  const chipText = document.createElement("span");
  chipText.textContent = boxStatus === "online" ? "Live" : humanize(boxStatus);
  chip.append(chipText);

  setText("box1-temperature", temperature ?? "—");
  setText("box1-humidity", humidity ?? "—");
  setText("box1-gas", booleanLabel(gas));
  setText("box1-flame", booleanLabel(flame));
  setText("box1-dht", valid === true ? "Valid" : valid === false ? "Invalid" : "Unknown");
}

function applyLargeStatus(id, status) {
  const element = $(id);
  element.textContent = humanize(status);
  element.className = `large-status ${status || "unknown"}`;
}

function lightModeLabel(mode, stateValue) {
  if (stateValue === false || mode === "off") return "Off";
  const labels = {
    warm_white: "Warm Light · 2700K",
    natural_white: "Natural White · 3500K",
    neutral_white: "Natural White · 3500K",
    daylight: "Daylight · 5000K",
  };
  return labels[String(mode || "").toLowerCase()] || (stateValue === true ? "On" : "Unknown");
}

function renderActuators(snapshot) {
  const actuators = snapshot.actuators || {};
  const light = actuators.light || {};
  const door = actuators.door || {};
  const buzzer = actuators.buzzer || {};
  const mode = String(light.mode || "").toLowerCase();
  const lightLabel = lightModeLabel(mode, light.state);

  setText("light-state", onOff(light.state));
  setText("light-mode", lightLabel);
  setText("door-state", door.state === true ? "Open" : door.state === false ? "Closed" : "Unknown");
  setText("door-angle", door.angle === null || door.angle === undefined ? "Angle unavailable" : `${door.angle}° confirmed`);
  setText("buzzer-state", onOff(buzzer.state));
  setText("buzzer-source", buzzer.local_reflex_active ? "Local emergency reflex active" : "Local reflex ready");

  setText("box1-buzzer", onOff(buzzer.state));
  setText("box1-reflex", buzzer.local_reflex_active ? "Active" : "Armed");

  setText("box2-light-mode", lightLabel);
  setText("box2-light-description", light.state === false ? "The living-room light is off." : "Latest state reported by the Box 2 controller.");
  setText("box2-door-state", door.state === true ? "Open" : door.state === false ? "Closed" : "Unknown");
  setText("box2-door-angle", door.angle === null || door.angle === undefined ? "Angle unavailable" : `${door.angle}° reported position`);
  setText("box2-light-raw", `${onOff(light.state)} · ${humanize(light.mode)}`);
  setText("box2-door-raw", `${door.state === true ? "Open" : door.state === false ? "Closed" : "Unknown"} · ${door.angle ?? "—"}°`);

  const orb = $("light-orb");
  orb.className = "light-orb";
  if (light.state === true) {
    if (mode.includes("warm")) orb.classList.add("warm");
    else if (mode.includes("natural") || mode.includes("neutral")) orb.classList.add("natural");
    else if (mode.includes("daylight") || mode.includes("cool")) orb.classList.add("daylight");
  }
  $("door-panel").classList.toggle("open", door.state === true);

  setText("operator-light-state", lightLabel);
  setText("operator-door-state", door.state === true ? "Open · 90°" : door.state === false ? "Closed · 0°" : "Unknown");
  setText("operator-buzzer-state", onOff(buzzer.state));
}

function renderNodes(snapshot) {
  const boxOne = snapshot.nodes?.box_1 || {};
  const boxTwo = snapshot.nodes?.box_2 || {};
  applyLargeStatus("box-one-status", boxOne.status || "unknown");
  applyLargeStatus("box-two-status", boxTwo.status || "unknown");
  setText("box1-last-seen", boxOne.last_seen ? `${relativeTime(boxOne.last_seen)} · ${timeLabel(boxOne.last_seen)}` : "Never");
  setText("box2-last-seen", boxTwo.last_seen ? `${relativeTime(boxTwo.last_seen)} · ${timeLabel(boxTwo.last_seen)}` : "Never");
  setText("box1-path", "venus/living_room/sensor_node_01");
  setText("box2-path", "venus/living_room/actuator_node_01");
}

function safetyTitle(event) {
  const type = String(event?.event_type || "");
  const drill = event?.simulated === true;
  if (type.startsWith("GAS_")) return type.endsWith("CLEARED") ? (drill ? "Gas drill cleared" : "Gas hazard cleared") : (drill ? "Gas drill active" : "Gas detected");
  if (type.startsWith("FLAME_") || type.startsWith("FIRE_")) return type.endsWith("CLEARED") ? (drill ? "Fire drill cleared" : "Flame hazard cleared") : (drill ? "Fire drill active" : "Flame detected");
  return humanize(type || "Safety event");
}

function safetyDetail(event) {
  if (event?.simulated === true) {
    if (event?.active) return "Controlled emergency drill. Physical safety responses are being tested.";
    return "The controlled emergency drill ended and VENUS completed drill recovery.";
  }
  const source = event?.source ? ` at ${humanize(String(event.source).split("/").at(-1))}` : "";
  if (event?.active) return `Deterministic safety response is active${source}.`;
  return `VENUS recorded recovery and re-armed monitoring${source}.`;
}

function renderSafety(snapshot) {
  const safety = snapshot.safety || { status: "normal", active: [], recent: [] };
  const critical = safety.status === "critical";
  const drill = safety.status === "drill";
  const active = safety.active || [];
  const newest = critical ? (active.find((event) => event?.simulated !== true) || active[0]) : active[0];
  const banner = $("safety-banner");
  banner.classList.toggle("is-hidden", !critical && !drill);
  banner.classList.toggle("drill", drill);
  if ((critical || drill) && newest) {
    setText("safety-banner-eyebrow", drill ? "Controlled emergency drill" : "Critical safety alert");
    setText("safety-banner-title", safetyTitle(newest));
    setText("safety-banner-detail", safetyDetail(newest));
    setText("safety-banner-time", timeLabel(newest.timestamp));
  }

  applyLargeStatus("safety-page-status", safety.status || "normal");
  const overview = $("safety-overview");
  overview.classList.toggle("critical", critical);
  overview.classList.toggle("drill", drill);
  setText("safety-heading", (critical || drill) ? (active.length > 1 ? `${active.length} active safety conditions` : safetyTitle(newest)) : "No active emergencies");
  setText("safety-description", (critical || drill) ? safetyDetail(newest) : "Deterministic monitoring is armed. Cleared events remain available below.");
  renderTimeline("safety-timeline", safety.recent || [], "No safety events recorded yet.");
}

function eventPresentation(event) {
  const name = event?.event || "system_event";
  if (name === "safety_alert") {
    return {
      title: safetyTitle(event),
      detail: safetyDetail(event),
      tone: event.active ? (event.simulated === true ? "warning" : "danger") : "success",
    };
  }
  const target = humanize(event?.target || event?.device || "actuator");
  const requested = event?.state === true ? "on" : event?.state === false ? "off" : "updated";
  const commandId = event?.command_id ? ` · ${String(event.command_id).slice(0, 8)}` : "";
  const source = event?.source ? ` from ${humanize(event.source)}` : "";
  const presentations = {
    command_dispatched: [`${target} command sent`, `Requested ${requested}${source}${commandId}`, "info"],
    command_executed: [`${target} command completed`, `Hardware confirmed ${requested}${commandId}`, "success"],
    command_failed: [`${target} command failed`, `Hardware did not confirm the requested state${commandId}`, "danger"],
    command_timeout: [`${target} command timed out`, `No hardware acknowledgement was received${commandId}`, "warning"],
  };
  const item = presentations[name];
  if (item) return { title: item[0], detail: item[1], tone: item[2] };
  return {
    title: humanize(name),
    detail: event?.message || event?.source || "VENUS Core event",
    tone: "info",
  };
}

function timelineItem(event) {
  const presentation = eventPresentation(event);
  const item = document.createElement("article");
  item.className = "timeline-item";
  const marker = document.createElement("span");
  marker.className = `timeline-marker ${presentation.tone}`;
  marker.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  copy.className = "timeline-copy";
  const title = document.createElement("strong");
  title.textContent = presentation.title;
  const detail = document.createElement("p");
  detail.textContent = presentation.detail;
  copy.append(title, detail);
  const time = document.createElement("time");
  time.className = "timeline-time";
  time.dateTime = parseDate(event?.timestamp)?.toISOString() || "";
  time.textContent = timeLabel(event?.timestamp || event?.received_at);
  item.append(marker, copy, time);
  return item;
}

function renderTimeline(id, events, emptyMessage) {
  const container = $(id);
  container.replaceChildren();
  if (!events?.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = emptyMessage;
    container.append(empty);
    return;
  }
  container.append(...events.map(timelineItem));
}

function renderActivity(snapshot) {
  const events = snapshot.events || [];
  const commands = snapshot.commands || [];
  renderTimeline("overview-activity", events.slice(0, 5), "Activity will appear when VENUS receives telemetry or commands.");
  renderTimeline("command-timeline", commands, "No recent actuator commands.");
  renderTimeline("event-timeline", events, "No recent Core events.");
}

function render(snapshot) {
  state.snapshot = snapshot;
  setText("latest-update", timeLabel(snapshot.generated_at));
  renderVoice(snapshot);
  renderServices(snapshot);
  renderEnvironment(snapshot);
  renderActuators(snapshot);
  renderNodes(snapshot);
  renderSafety(snapshot);
  renderActivity(snapshot);
}

async function fetchSnapshot() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`Dashboard API returned ${response.status}`);
    render(await response.json());
    setConnection(true, "Live");
  } catch (error) {
    setConnection(false, "Disconnected");
    console.warn("VENUS dashboard refresh failed", error);
  }
}

function queueRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(fetchSnapshot, 80);
}

function connectWebSocket() {
  clearTimeout(state.reconnectTimer);
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
  state.socket = socket;

  socket.addEventListener("open", () => setConnection(true, "Live"));
  socket.addEventListener("message", (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.event_type === "snapshot") render(event.data);
      else if (event.event_type !== "read_only_notice") queueRefresh();
    } catch (error) {
      console.warn("Ignored malformed dashboard event", error);
    }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    setConnection(false, "Reconnecting");
    state.reconnectTimer = setTimeout(connectWebSocket, 2500);
  });
  socket.addEventListener("error", () => socket.close());
}

function setOperatorFeedback(message = "", tone = "") {
  const feedback = $("operator-feedback");
  feedback.textContent = message;
  feedback.className = `operator-feedback ${tone}`.trim();
}

function renderOperatorSession(session) {
  state.operator = { ...state.operator, ...session };
  const isOperatorAddress = session.access_mode === "operator";
  const unlocked = isOperatorAddress && session.authenticated === true;
  const pill = $("operator-pill");
  const accessCard = $("operator-access-card");
  const login = $("operator-login");
  const activeSession = $("operator-session");
  const controls = $("operator-controls");
  const voiceControls = $("voice-controls");
  const lockNote = $("control-lock-note");

  pill.className = "operator-pill";
  accessCard.classList.toggle("is-unlocked", unlocked);
  lockNote.classList.toggle("is-unlocked", unlocked);
  controls.disabled = !unlocked || state.operator.busy;
  voiceControls.disabled = !unlocked || state.operator.busy;
  login.classList.add("is-hidden");
  activeSession.classList.add("is-hidden");

  if (!isOperatorAddress) {
    pill.classList.add("is-read-only");
    setText("operator-pill-label", "Read only");
    setText("operator-page-status", "Read only");
    $("operator-page-status").className = "large-status locked";
    setText("operator-access-title", "Monitoring access only");
    setText(
      "operator-access-description",
      session.secure_url
        ? `Hardware controls are available only from the secure VENUS address: ${session.secure_url}`
        : "This HTTP address is permanently read-only. Open the configured HTTPS address to unlock controls."
    );
    setText("control-lock-message", "Controls are disabled on the HTTP monitoring address.");
    setText("footer-access-label", "Read-only monitoring");
    return;
  }

  if (!unlocked) {
    pill.classList.add("is-available");
    setText("operator-pill-label", "Unlock");
    setText("operator-page-status", "Locked");
    $("operator-page-status").className = "large-status locked";
    setText("operator-access-title", "Unlock Operator Mode");
    setText("operator-access-description", "Authenticate before VENUS accepts dashboard hardware commands.");
    setText("control-lock-message", "Controls will become available after operator authentication.");
    setText("footer-access-label", "Secure access · Operator locked");
    login.classList.remove("is-hidden");

    const passkeyButton = $("passkey-login");
    const passkeySupported = window.isSecureContext && "PublicKeyCredential" in window;
    passkeyButton.classList.toggle("is-hidden", !session.passkey_available);
    passkeyButton.disabled = !session.passkey_available || !passkeySupported;
    $("password-login-form").classList.toggle("is-hidden", !session.password_available);
    return;
  }

  pill.classList.add("is-unlocked");
  setText("operator-pill-label", "Operator");
  setText("operator-page-status", "Unlocked");
  $("operator-page-status").className = "large-status unlocked";
  setText("operator-access-title", "Operator Mode active");
  setText("operator-access-description", "Hardware commands are confirmed through MQTT; microphone changes are confirmed by the voice sidecar.");
  setText("control-lock-message", "Authenticated controls are ready.");
  setText("footer-access-label", "Secure access · Operator active");
  setText("operator-session-user", session.username || "Operator");
  const minutes = Math.max(1, Math.ceil((session.expires_in || 0) / 60));
  setText("operator-session-detail", `${humanize(session.method || "authenticated")} · locks in ${minutes} min`);
  activeSession.classList.remove("is-hidden");

  const canRegister = window.isSecureContext && "PublicKeyCredential" in window;
  $("register-passkey").disabled = !canRegister;
}

async function fetchOperatorSession() {
  try {
    const response = await fetch("/api/operator/session", { cache: "no-store" });
    if (!response.ok) throw new Error(`Session API returned ${response.status}`);
    renderOperatorSession(await response.json());
  } catch (error) {
    console.warn("Operator session refresh failed", error);
    renderOperatorSession({ access_mode: "read_only", authenticated: false });
  }
}

async function postOperator(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = { message: "VENUS returned an unreadable response." };
  }
  if (!response.ok) {
    const error = new Error(payload.message || `Request failed with ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function base64UrlToBytes(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/") + padding;
  const decoded = atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function bytesToBase64Url(value) {
  if (value === null || value === undefined) return null;
  const bytes = new Uint8Array(value);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function decodeRegistrationOptions(options) {
  return {
    ...options,
    challenge: base64UrlToBytes(options.challenge),
    user: { ...options.user, id: base64UrlToBytes(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((item) => ({
      ...item,
      id: base64UrlToBytes(item.id),
    })),
  };
}

function decodeAuthenticationOptions(options) {
  return {
    ...options,
    challenge: base64UrlToBytes(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((item) => ({
      ...item,
      id: base64UrlToBytes(item.id),
    })),
  };
}

function serializeRegistrationCredential(credential) {
  return {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      clientDataJSON: bytesToBase64Url(credential.response.clientDataJSON),
      attestationObject: bytesToBase64Url(credential.response.attestationObject),
      transports: credential.response.getTransports?.() || [],
    },
  };
}

function serializeAuthenticationCredential(credential) {
  return {
    id: credential.id,
    rawId: bytesToBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      authenticatorData: bytesToBase64Url(credential.response.authenticatorData),
      clientDataJSON: bytesToBase64Url(credential.response.clientDataJSON),
      signature: bytesToBase64Url(credential.response.signature),
      userHandle: bytesToBase64Url(credential.response.userHandle),
    },
  };
}

async function loginWithPasskey() {
  if (!window.isSecureContext || !("PublicKeyCredential" in window)) {
    setOperatorFeedback("Passkeys require the configured HTTPS address.", "error");
    return;
  }
  try {
    setOperatorFeedback("Waiting for Face ID, Touch ID, or device verification…", "pending");
    const options = await postOperator("/api/operator/passkeys/login/options");
    const credential = await navigator.credentials.get({
      publicKey: decodeAuthenticationOptions(options.publicKey),
    });
    await postOperator("/api/operator/passkeys/login/verify", {
      ceremony_id: options.ceremony_id,
      credential: serializeAuthenticationCredential(credential),
    });
    setOperatorFeedback("Operator Mode unlocked.", "success");
    await fetchOperatorSession();
  } catch (error) {
    setOperatorFeedback(error.name === "NotAllowedError" ? "Passkey request was cancelled." : error.message, "error");
  }
}

async function registerPasskey() {
  if (!window.isSecureContext || !("PublicKeyCredential" in window)) {
    setOperatorFeedback("Open VENUS through HTTPS before registering a passkey.", "error");
    return;
  }
  try {
    setOperatorFeedback("Follow the device prompt to create the VENUS passkey…", "pending");
    const options = await postOperator("/api/operator/passkeys/register/options");
    const credential = await navigator.credentials.create({
      publicKey: decodeRegistrationOptions(options.publicKey),
    });
    await postOperator("/api/operator/passkeys/register/verify", {
      ceremony_id: options.ceremony_id,
      credential: serializeRegistrationCredential(credential),
    });
    setOperatorFeedback("Passkey registered. Face ID or Touch ID can be used next time.", "success");
    await fetchOperatorSession();
  } catch (error) {
    setOperatorFeedback(error.name === "NotAllowedError" ? "Passkey registration was cancelled." : error.message, "error");
  }
}

async function submitOperatorCommand(button) {
  const target = button.dataset.commandTarget;
  const requestedState = button.dataset.commandState === "true";
  if (target === "buzzer" && requestedState && !window.confirm("Activate the physical emergency buzzer?")) return;

  state.operator.busy = true;
  renderOperatorSession(state.operator);
  setOperatorFeedback("Sending command through VENUS Core…", "pending");
  try {
    const payload = {
      target,
      state: requestedState,
    };
    if (button.dataset.commandMode) payload.mode = button.dataset.commandMode;
    const result = await postOperator("/api/operator/command", payload);
    const shortId = result.result?.command_id?.slice(0, 8);
    setOperatorFeedback(`${result.message}${shortId ? ` · ${shortId}` : ""}`, "success");
    setTimeout(fetchSnapshot, 350);
  } catch (error) {
    setOperatorFeedback(error.message, "error");
    if (error.status === 401) await fetchOperatorSession();
  } finally {
    state.operator.busy = false;
    renderOperatorSession(state.operator);
  }
}

async function submitMicrophoneCommand(button) {
  const requestedState = button.dataset.microphoneState === "true";
  state.operator.busy = true;
  renderOperatorSession(state.operator);
  setOperatorFeedback(
    requestedState ? "Requesting VENUS listening mode…" : "Requesting microphone mute…",
    "pending"
  );

  try {
    const result = await postOperator(
      "/api/operator/microphone",
      { state: requestedState }
    );
    const shortId = result.result?.command_id?.slice(0, 8);
    setOperatorFeedback(
      `${result.message}${shortId ? ` · ${shortId}` : ""}`,
      "success"
    );
    setTimeout(fetchSnapshot, 200);
  } catch (error) {
    setOperatorFeedback(error.message, "error");
    if (error.status === 401) await fetchOperatorSession();
  } finally {
    state.operator.busy = false;
    renderOperatorSession(state.operator);
  }
}

function initialiseOperatorMode() {
  $("operator-pill").addEventListener("click", () => showView("operator"));
  $("passkey-login").addEventListener("click", loginWithPasskey);
  $("register-passkey").addEventListener("click", registerPasskey);
  $("operator-logout").addEventListener("click", async () => {
    try {
      await postOperator("/api/operator/logout");
      setOperatorFeedback("Operator Mode locked.", "success");
    } catch (error) {
      setOperatorFeedback(error.message, "error");
    }
    await fetchOperatorSession();
  });
  $("password-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      setOperatorFeedback("Verifying operator credentials…", "pending");
      await postOperator("/api/operator/login/password", {
        username: $("operator-username").value,
        password: $("operator-password").value,
      });
      $("operator-password").value = "";
      setOperatorFeedback("Operator Mode unlocked.", "success");
      await fetchOperatorSession();
    } catch (error) {
      setOperatorFeedback(error.message, "error");
    }
  });
  document.querySelectorAll("[data-command-target]").forEach((button) => {
    button.addEventListener("click", () => submitOperatorCommand(button));
  });
  document.querySelectorAll("[data-microphone-state]").forEach((button) => {
    button.addEventListener("click", () => submitMicrophoneCommand(button));
  });
}

function configureTheme() {
  const saved = localStorage.getItem("venus-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  $("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme;
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = current ? (current === "dark" ? "light" : "dark") : (systemDark ? "light" : "dark");
    document.documentElement.dataset.theme = next;
    localStorage.setItem("venus-theme", next);
  });
}

function initialiseNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  document.querySelectorAll("[data-jump-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.jumpView));
  });
  const requested = window.location.hash.slice(1);
  const valid = [...document.querySelectorAll("[data-view-panel]")].some((panel) => panel.dataset.viewPanel === requested);
  showView(valid ? requested : "overview");
}

function updateClock() {
  setText("footer-clock", new Intl.DateTimeFormat(undefined, {
    weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date()));
}

configureTheme();
initialiseNavigation();
initialiseOperatorMode();
updateClock();
setInterval(updateClock, 1000);
setInterval(() => state.connected && fetchSnapshot(), 5000);
setInterval(fetchOperatorSession, 30000);
fetchSnapshot();
fetchOperatorSession();
connectWebSocket();
