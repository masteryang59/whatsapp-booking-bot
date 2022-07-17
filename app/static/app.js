const $ = (id) => document.getElementById(id);
const chat = $("chat");
let lastId = 0;
let currentPhone = "";
let tz = "UTC";
let sending = false;

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// WhatsApp formatting: *bold*, _italic_, ~strike~.
function formatWhatsApp(text) {
  return escapeHtml(text)
    .replace(/\*([^*\n]+)\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?])/g, "$1<em>$2</em>")
    .replace(/~([^~\n]+)~/g, "<s>$1</s>")
    .replace(/\n/g, "<br>");
}

function timeLabel(iso) {
  return new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: tz }).format(new Date(iso));
}

function whenLabel(iso) {
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: tz,
  }).format(new Date(iso));
}

function addBubble(message) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${message.direction === "in" ? "mine" : "theirs"}`;
  bubble.innerHTML = `${formatWhatsApp(message.body)}<span class="time">${timeLabel(message.at)}</span>`;
  chat.appendChild(bubble);
  chat.scrollTop = chat.scrollHeight;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(typeof detail.detail === "string" ? detail.detail : `Request failed (${response.status})`);
  }
  return response.json();
}

async function pollMessages() {
  const phone = $("phone").value.trim();
  if (phone !== currentPhone) {
    currentPhone = phone;
    lastId = 0;
    chat.innerHTML = "";
  }
  if (!$("phone").checkValidity()) return;
  const messages = await api(`/api/sim/messages?phone=${encodeURIComponent(phone)}&after=${lastId}`);
  for (const message of messages) {
    addBubble(message);
    lastId = message.id;
  }
}

async function send(text) {
  if (!text.trim() || sending) return;
  if (!$("phone").checkValidity()) {
    alert("Enter the phone number in international format, e.g. +447700900123");
    return;
  }
  sending = true;
  try {
    await api("/api/sim/messages", {
      method: "POST",
      body: JSON.stringify({ phone: $("phone").value.trim(), name: $("name").value.trim(), text }),
    });
    await pollMessages();
    await refreshState();
  } catch (error) {
    alert(error.message);
  } finally {
    sending = false;
  }
}

const reminderNames = { 1440: "1 day", 120: "2 h", 60: "1 h" };
const leadLabel = (m) => reminderNames[m] || (m % 60 === 0 ? `${m / 60} h` : `${m} min`);

function renderBookings(state) {
  const body = $("bookings");
  if (!state.appointments.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">No bookings yet.</td></tr>`;
    return;
  }
  body.innerHTML = state.appointments.map((a) => {
    const cancelled = a.status === "cancelled";
    const sync = a.calendar_state === "synced"
      ? `<span class="badge ok">synced</span>`
      : `<span class="badge warn" title="${escapeHtml(a.calendar_error || "")}">pending</span>`;
    const reminders = state.business.reminders.map((m) =>
      `<span class="badge ${a.reminders_sent.includes(m) ? "ok" : "idle"}">${leadLabel(m)}</span>`).join(" ");
    return `<tr class="${cancelled ? "cancelled" : ""}">
      <td>${whenLabel(a.start)}–${timeLabel(a.end)}</td>
      <td>${escapeHtml(a.service)}${cancelled ? ` <span class="badge idle">cancelled</span>` : ""}</td>
      <td>${escapeHtml(a.name || a.phone)}<div class="muted small">${escapeHtml(a.phone)} · ${a.channel}</div></td>
      <td>${sync}</td>
      <td>${reminders}</td>
    </tr>`;
  }).join("");
}

function renderEvents(state) {
  const list = $("events");
  if (state.calendar !== "local") {
    list.innerHTML = `<li class="muted">Connected to Google Calendar: open it to see the events.</li>`;
    $("block").hidden = true;
    return;
  }
  if (!state.events.length) {
    list.innerHTML = `<li class="muted">Nothing in the calendar yet.</li>`;
    return;
  }
  list.innerHTML = state.events.map((e) => `
    <li class="${e.booking ? "booking" : "busy"}">
      <span class="when">${whenLabel(e.start)}–${timeLabel(e.end)}</span>
      <span>${escapeHtml(e.summary)}</span>
    </li>`).join("");
}

async function refreshState() {
  const state = await api("/api/sim/state");
  tz = state.business.timezone;
  $("business-name").textContent = state.business.name;
  $("avatar").textContent = state.business.name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("");
  $("calendar-kind").textContent = `calendar: ${state.calendar === "google" ? "Google Calendar" : "local"} · ${tz}`;
  $("reminder-leads").textContent = `sent ${state.business.reminders.map(leadLabel).join(" and ")} before`;
  renderBookings(state);
  renderEvents(state);
}

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("text").value;
  $("text").value = "";
  send(text);
});

$("quick").addEventListener("click", (event) => {
  if (event.target.tagName === "BUTTON") send(event.target.textContent);
});

$("block").addEventListener("submit", async (event) => {
  event.preventDefault();
  const start = $("block-start").value;
  const minutes = Number($("block-minutes").value);
  const end = new Date(new Date(`${start}:00Z`).getTime() + minutes * 60000).toISOString().slice(0, 16);
  try {
    await api("/api/sim/blocks", {
      method: "POST",
      body: JSON.stringify({ start, end, summary: $("block-summary").value || "Busy" }),
    });
    await refreshState();
  } catch (error) {
    alert(error.message);
  }
});

$("reminders").addEventListener("submit", async (event) => {
  event.preventDefault();
  const at = $("reminder-at").value || null;
  try {
    const result = await api("/api/sim/reminders", { method: "POST", body: JSON.stringify({ at }) });
    $("reminder-result").textContent =
      `Sent ${result.sent}, skipped ${result.skipped}, failed ${result.failed}.`;
    await pollMessages();
    await refreshState();
  } catch (error) {
    alert(error.message);
  }
});

async function tick() {
  try {
    await pollMessages();
    await refreshState();
  } catch (error) {
    console.error(error);
  }
}

tick();
setInterval(tick, 2500);

function syncRecords(data) {
  if (!data) return null;
  return data;
}

function validateRequest(data) {
  if (!data) return null;
  return data;
}

function syncRecords(data) {
  if (!data) return null;
  return data;
}