// Setup page: mic selection, image management and live debug view.
// All DOM is built with textContent/properties (never innerHTML), so file names
// and transcripts can't inject markup.
import { connect } from "/static/common/socket.js";

const $ = (id) => document.getElementById(id);

const ui = {
  emotions: [],
  images: [],
  defaultImage: null,
  maxBytes: 10 * 1024 * 1024,
};

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // non-JSON error body
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

function sendJson(path, method, body) {
  return api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function flash(id, text, isError = false) {
  const node = $(id);
  node.textContent = text;
  node.classList.toggle("error", isError);
}

function setPill(id, text, kind) {
  const pill = $(id);
  pill.textContent = text;
  pill.className = `pill ${kind ?? ""}`;
}

// --- microphone -------------------------------------------------------------

async function loadDevices() {
  const select = $("device");
  try {
    const data = await api("/api/devices");
    const options = data.devices.map((d) =>
      el("option", {
        value: String(d.index),
        textContent: `${d.name} · ${d.hostapi}${d.is_default ? " (system default)" : ""}`,
      }),
    );
    select.replaceChildren(el("option", { value: "", textContent: "System default input" }), ...options);
    select.value = data.selected === null ? "" : String(data.selected);
    flash("device-msg", "");
  } catch (err) {
    flash("device-msg", `Could not list microphones: ${err.message}`, true);
  }
}

async function applyDevice() {
  const value = $("device").value;
  try {
    await sendJson("/api/device", "PUT", { device: value === "" ? null : Number(value) });
    flash("device-msg", "Microphone saved.");
  } catch (err) {
    flash("device-msg", err.message, true);
  }
}

// --- images -------------------------------------------------------------------

async function loadImages() {
  const data = await api("/api/images");
  ui.images = data.images;
  ui.defaultImage = data.default_image;
  ui.maxBytes = data.max_bytes;
  renderImages();
}

function renderImages() {
  const cards = ui.emotions.map((emotion) => {
    const mine = ui.images.filter((img) => img.emotion === emotion);
    const input = el("input", {
      type: "file",
      accept: "image/png,image/jpeg,image/webp,image/gif",
      multiple: true,
      hidden: true,
    });
    input.addEventListener("change", () => upload(emotion, input.files));
    const add = el("button", { className: "secondary", textContent: "Add" });
    add.addEventListener("click", () => input.click());

    const thumbs = el("div", { className: "thumbs" }, ...mine.map(thumbnail));
    if (!mine.length) thumbs.append(el("p", { className: "muted small", textContent: "No images" }));

    const card = el(
      "div",
      { className: "emotion" },
      el("div", { className: "emotion-head" }, el("h3", { textContent: emotion }), add),
      thumbs,
      input,
    );
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      card.classList.add("drop");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drop"));
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      card.classList.remove("drop");
      upload(emotion, e.dataTransfer.files);
    });
    return card;
  });
  $("emotions").replaceChildren(...cards);
}

function thumbnail(img) {
  const isDefault = img.id === ui.defaultImage;
  const star = el("button", {
    className: `icon${isDefault ? " on" : ""}`,
    title: isDefault ? "Default image (click to unset)" : "Make this the default image",
    textContent: "★",
  });
  star.addEventListener("click", () => setDefault(isDefault ? null : img.id));
  const remove = el("button", { className: "icon danger", title: "Delete", textContent: "✕" });
  remove.addEventListener("click", () => deleteImage(img));

  return el(
    "figure",
    { className: `thumb${isDefault ? " default" : ""}` },
    el("img", { src: img.url, alt: img.original_name, title: img.original_name, loading: "lazy" }),
    el("div", { className: "thumb-actions" }, star, remove),
  );
}

async function upload(emotion, files) {
  const errors = [];
  for (const file of files) {
    if (file.size > ui.maxBytes) {
      errors.push(`${file.name}: larger than ${Math.round(ui.maxBytes / 1048576)} MB`);
      continue;
    }
    const form = new FormData();
    form.append("emotion", emotion);
    form.append("file", file);
    try {
      await api("/api/images", { method: "POST", body: form });
    } catch (err) {
      errors.push(`${file.name}: ${err.message}`);
    }
  }
  flash("image-msg", errors.join(" · "), errors.length > 0);
  await loadImages();
}

async function deleteImage(img) {
  if (!confirm(`Delete "${img.original_name}"?`)) return;
  try {
    await api(`/api/images/${img.id}`, { method: "DELETE" });
  } catch (err) {
    flash("image-msg", err.message, true);
  }
  await loadImages();
}

async function setDefault(imageId) {
  try {
    await sendJson("/api/images/default", "PUT", { image_id: imageId });
  } catch (err) {
    flash("image-msg", err.message, true);
  }
  await loadImages();
}

// --- live view ------------------------------------------------------------------

function renderState(msg) {
  $("emotion").textContent = msg.emotion;
  $("confidence").textContent = msg.confidence ? `${Math.round(msg.confidence * 100)}%` : "";
  $("reason").textContent = msg.reason;
  const img = $("preview-img");
  img.hidden = !msg.image;
  $("preview-empty").hidden = Boolean(msg.image);
  if (msg.image) img.src = msg.image;
}

function renderStatus(msg) {
  const kind = { listening: "ok", loading: "warn", error: "bad", mic_error: "bad" }[msg.state];
  setPill("engine", msg.message, kind);
}

function renderSpeaking(speaking) {
  $("speaking").classList.toggle("on", speaking);
  $("speaking-label").textContent = speaking ? "Speaking" : "Silent";
}

function renderDebug(msg) {
  $("transcript").textContent = msg.transcript || "—";
  $("window").textContent = msg.window || "—";
  const rows = ui.emotions.map((emotion) => {
    const score = msg.scores?.[emotion] ?? 0;
    const fill = el("div", { className: "fill" });
    fill.style.width = `${(score * 100).toFixed(1)}%`;
    return el(
      "div",
      { className: `score${emotion === msg.emotion ? " top" : ""}` },
      el("span", { textContent: emotion }),
      el("div", { className: "bar" }, fill),
      el("span", { className: "mono", textContent: score.toFixed(2) }),
    );
  });
  $("scores").replaceChildren(...rows);
  const t = msg.timings;
  $("timings").textContent =
    `${msg.duration_s}s utterance${msg.forced ? " (cut)" : ""} · ASR ${t.asr_ms} ms · ` +
    `classify ${t.classify_ms} ms · speech end → state ${t.speech_end_to_state_ms} ms`;
}

// --- init -----------------------------------------------------------------------

async function init() {
  const overlayUrl = `${location.origin}/overlay`;
  $("overlay-url").textContent = overlayUrl;
  $("copy-url").addEventListener("click", async () => {
    await navigator.clipboard.writeText(overlayUrl);
    $("copy-url").textContent = "Copied";
    setTimeout(() => ($("copy-url").textContent = "Copy"), 1500);
  });
  $("device-apply").addEventListener("click", applyDevice);
  $("device-refresh").addEventListener("click", loadDevices);

  ui.emotions = (await api("/api/emotions")).emotions;
  await Promise.all([loadImages(), loadDevices()]);

  connect("/ws?debug=1", {
    onOpen: () => setPill("conn", "connected", "ok"),
    onClose: () => setPill("conn", "disconnected (retrying)", "bad"),
    onMessage(msg) {
      if (msg.type === "state") renderState(msg);
      else if (msg.type === "status") renderStatus(msg);
      else if (msg.type === "vad") renderSpeaking(msg.speaking);
      else if (msg.type === "debug") renderDebug(msg);
    },
  });
}

init().catch((err) => flash("image-msg", `Setup failed to load: ${err.message}`, true));
