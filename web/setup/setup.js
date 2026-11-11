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
  settings: null,
  listening: true,
};

const TIER_LABELS = { any: "Any", mild: "Mild", strong: "Strong" };

// Settings form definition. `path` is "section.key" in /api/settings.
const SETTINGS = [
  {
    title: "Display",
    fields: [
      { path: "display.transition", label: "Transition", type: "select",
        options: { fade: "Cross-fade", cut: "Cut", bounce: "Bounce" } },
      { path: "display.fade_ms", label: "Transition time (ms)", type: "number", min: 0, max: 5000, step: 50 },
      { path: "display.rotation", label: "With several images", type: "select",
        options: { random: "Pick randomly", cycle: "Cycle in order" } },
      { path: "display.new_image_each_utterance", label: "New image every sentence", type: "checkbox",
        help: "Switch to another image of the same emotion each time you speak." },
      { path: "display.strong_threshold", label: "Strong-tier threshold", type: "number", min: 0, max: 1, step: 0.05,
        help: "Strong images show at or above this confidence, Mild ones below." },
    ],
  },
  {
    title: "Detection",
    fields: [
      { path: "state.confidence_threshold", label: "Minimum confidence", type: "number", min: 0, max: 1, step: 0.05,
        help: "Below this the detection counts as unsure." },
      { path: "state.low_confidence", label: "When unsure", type: "select",
        options: { hold: "Keep current image", neutral: "Go to neutral" } },
      { path: "state.silence_timeout_s", label: "Back to neutral after silence (s)", type: "number",
        min: 0.5, max: 600, step: 0.5 },
      { path: "classifier.window_seconds", label: "Text window (s)", type: "number", min: 0, max: 60, step: 1,
        help: "How much recent speech is judged together. 0 = only the last sentence." },
      { path: "vad.min_silence_ms", label: "Pause that ends a sentence (ms)", type: "number",
        min: 50, max: 5000, step: 50, help: "Shorter reacts faster but may split sentences." },
      { path: "vad.max_utterance_s", label: "Longest chunk of speech (s)", type: "number", min: 1, max: 30, step: 0.5,
        help: "Non-stop talking is judged in chunks this long." },
      { path: "vad.threshold", label: "Voice detection threshold", type: "number", min: 0.05, max: 0.95, step: 0.05,
        help: "Higher ignores more background noise." },
    ],
  },
  {
    title: "Stability (anti-flicker)",
    note: "All off by default, so the image follows every sentence. Turn these on if it changes too often.",
    fields: [
      { path: "state.smoothing", label: "Smoothing", type: "number", min: 0.05, max: 1, step: 0.05,
        help: "1 = off. Lower values blend each sentence with the ones before." },
      { path: "state.persist_windows", label: "Sentences before switching", type: "number", min: 1, max: 10, step: 1,
        help: "1 = off. A new emotion must win this many sentences in a row." },
      { path: "state.instant_switch_margin", label: "Instant-switch lead", type: "number", min: 0, max: 1, step: 0.05,
        help: "0 = off. Skip the wait when the new emotion leads by this much." },
      { path: "state.min_display_s", label: "Minimum time on screen (s)", type: "number", min: 0, max: 60, step: 0.5,
        help: "0 = off." },
    ],
  },
];

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

  const tier = el(
    "select",
    { className: "tier", title: "Intensity tier" },
    ...Object.entries(TIER_LABELS).map(([value, label]) => el("option", { value, textContent: label })),
  );
  tier.value = img.tier;
  tier.addEventListener("change", () => updateImage(img.id, { tier: tier.value }));

  return el(
    "figure",
    { className: `thumb${isDefault ? " default" : ""}` },
    el("img", { src: img.url, alt: img.original_name, title: img.original_name, loading: "lazy" }),
    tier,
    el("div", { className: "thumb-actions" }, star, remove),
  );
}

async function updateImage(imageId, changes) {
  try {
    await sendJson(`/api/images/${imageId}`, "PUT", changes);
  } catch (err) {
    flash("image-msg", err.message, true);
  }
  await loadImages();
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

// --- settings -----------------------------------------------------------------

async function loadSettings() {
  ui.settings = await api("/api/settings");
  renderSettings();
}

function getSetting(path) {
  const [section, key] = path.split(".");
  return ui.settings[section][key];
}

async function saveSettings(patch) {
  try {
    ui.settings = await sendJson("/api/settings", "PUT", patch);
    flash("settings-msg", "Saved.");
  } catch (err) {
    flash("settings-msg", err.message, true);
  }
  renderSettings(); // also reverts a rejected value
}

function settingInput(field) {
  const value = getSetting(field.path);
  let input;
  if (field.type === "select") {
    input = el(
      "select",
      {},
      ...Object.entries(field.options).map(([v, label]) => el("option", { value: v, textContent: label })),
    );
    input.value = value;
  } else if (field.type === "checkbox") {
    input = el("input", { type: "checkbox", checked: value });
  } else {
    input = el("input", { type: "number", min: field.min, max: field.max, step: field.step, value });
  }
  input.addEventListener("change", () => {
    let next = input.value;
    if (field.type === "checkbox") next = input.checked;
    if (field.type === "number") {
      if (input.value === "" || Number.isNaN(Number(input.value))) return renderSettings();
      next = Number(input.value);
    }
    const [section, key] = field.path.split(".");
    saveSettings({ [section]: { [key]: next } });
  });
  return input;
}

function settingRow(label, input, help) {
  return el(
    "label",
    { className: `setting${input.type === "checkbox" ? " check" : ""}` },
    el("span", { className: "setting-label", textContent: label }),
    input,
    ...(help ? [el("span", { className: "setting-help", textContent: help })] : []),
  );
}

function fallbackGroup() {
  // For each emotion: which other emotion's images to borrow when it has none.
  const fallbacks = ui.settings.display.fallbacks;
  const rows = ui.emotions
    .filter((emotion) => emotion !== "neutral")
    .map((emotion) => {
      const choices = ui.emotions.filter((e) => e !== emotion);
      const input = el(
        "select",
        {},
        el("option", { value: "", textContent: "Default image" }),
        ...choices.map((e) => el("option", { value: e, textContent: `${e} images` })),
      );
      input.value = fallbacks[emotion] ?? "";
      input.addEventListener("change", () => {
        const next = { ...fallbacks };
        if (input.value) next[emotion] = input.value;
        else delete next[emotion];
        saveSettings({ display: { fallbacks: next } });
      });
      return settingRow(emotion, input);
    });
  return el(
    "fieldset",
    {},
    el("legend", { textContent: "Emotions without images" }),
    el("p", { className: "hint", textContent: "What to show when an emotion has no images of its own." }),
    ...rows,
  );
}

function renderSettings() {
  if (!ui.settings) return;
  const groups = SETTINGS.map((group) =>
    el(
      "fieldset",
      {},
      el("legend", { textContent: group.title }),
      ...(group.note ? [el("p", { className: "hint", textContent: group.note })] : []),
      ...group.fields.map((f) => settingRow(f.label, settingInput(f), f.help)),
    ),
  );
  $("settings").replaceChildren(...groups, fallbackGroup());
}

async function resetSettings() {
  if (!confirm("Reset all settings on this page to their defaults? Your images and microphone stay as they are.")) {
    return;
  }
  try {
    ui.settings = await api("/api/settings/reset", { method: "POST" });
    flash("settings-msg", "All settings reset to defaults.");
  } catch (err) {
    flash("settings-msg", err.message, true);
  }
  renderSettings();
}

// --- backup -------------------------------------------------------------------

async function importSetup() {
  const input = $("import-file");
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  if (!confirm(`Replace ALL current images and settings with "${file.name}"?`)) return;
  const form = new FormData();
  form.append("file", file);
  flash("backup-msg", "Importing…");
  try {
    const result = await api("/api/import", { method: "POST", body: form });
    flash("backup-msg", `Imported ${result.images} image(s) and settings.`);
    ui.settings = result.settings;
    renderSettings();
    await loadImages();
  } catch (err) {
    flash("backup-msg", err.message, true);
  }
}

// --- live view ------------------------------------------------------------------

function renderState(msg) {
  $("emotion").textContent = msg.emotion;
  $("confidence").textContent = msg.confidence ? `${Math.round(msg.confidence * 100)}%` : "";
  $("reason").textContent = msg.reason === "scores" ? `${msg.tier}` : msg.reason;
  const img = $("preview-img");
  img.hidden = !msg.image;
  $("preview-empty").hidden = Boolean(msg.image);
  if (msg.image) img.src = msg.image;
}

function renderStatus(msg) {
  const kind = { listening: "ok", loading: "warn", paused: "warn", error: "bad", mic_error: "bad" }[
    msg.state
  ];
  setPill("engine", msg.message, kind);

  const button = $("listen-toggle");
  ui.listening = msg.listening ?? true;
  button.disabled = msg.state === "disabled" || msg.state === "error";
  button.textContent = ui.listening ? "Stop listening" : "Start listening";
  button.classList.toggle("paused", !ui.listening);
  button.title = ui.listening
    ? "Turn the microphone off. The overlay shows your neutral image until you start again."
    : "Turn the microphone back on.";
}

async function toggleListening() {
  const button = $("listen-toggle");
  button.disabled = true;
  try {
    // The status message that follows over the WebSocket updates the button.
    await sendJson("/api/listening", "PUT", { listening: !ui.listening });
  } catch (err) {
    setPill("engine", err.message, "bad");
  } finally {
    button.disabled = false;
  }
}

function renderSpeaking(speaking) {
  $("speaking").classList.toggle("on", speaking);
  $("speaking-label").textContent = speaking ? "Speaking" : "Silent";
}

function renderDebug(msg) {
  $("transcript").textContent = msg.transcript || "—";
  $("window").textContent = msg.window || "—";
  // Smoothed scores are what the state machine decides on (equal to raw when smoothing is off).
  const shown = msg.smoothed ?? msg.scores;
  const rows = ui.emotions.map((emotion) => {
    const score = shown?.[emotion] ?? 0;
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
  $("listen-toggle").addEventListener("click", toggleListening);
  $("settings-reset").addEventListener("click", resetSettings);
  $("import-btn").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importSetup);

  ui.emotions = (await api("/api/emotions")).emotions;
  await Promise.all([loadImages(), loadDevices(), loadSettings()]);

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
