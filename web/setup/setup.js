// Setup page: mic selection, image management and live debug view.
// All DOM is built with textContent/properties (never innerHTML), so file names
// and transcripts can't inject markup.
import { connect } from "/static/common/socket.js";

const $ = (id) => document.getElementById(id);

const ui = {
  emotions: [],
  imageGroups: [],
  triggers: [],
  newTriggerImage: null,
  images: [],
  defaultImage: null,
  maxBytes: 10 * 1024 * 1024,
  settings: null,
  listening: true,
  calibration: null,
  voiceModel: null,
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
      { path: "classifier.interim_s", label: "Update while talking", type: "select",
        // A list, not an object: JS would put the integer-like keys first.
        options: [["auto", "Automatic"], ["0", "Off"], ["0.5", "Every 0.5 s"], ["1", "Every 1 s"],
          ["1.5", "Every 1.5 s"], ["2", "Every 2 s"], ["3", "Every 3 s"]],
        toInput: (v) => (v === null ? "auto" : String(v)),
        fromInput: (v) => (v === "auto" ? null : Number(v)),
        help: "Re-checks your words while you're still talking, so the image can change before you pause. Automatic: every 0.5 s on a GPU, every 1 s on a CPU." },
      { path: "classifier.slang", label: "Understand slang", type: "checkbox",
        help: "Reads slang as plain English before judging the emotion, e.g. \"lol\" as \"haha\" and \"that was fire\" as \"that was amazing\"." },
      { path: "vad.min_silence_ms", label: "Pause that ends a sentence (ms)", type: "number",
        min: 50, max: 5000, step: 50, help: "Shorter reacts faster but may split sentences." },
      { path: "vad.max_utterance_s", label: "Longest chunk of speech (s)", type: "number", min: 1, max: 30, step: 0.5,
        help: "Non-stop talking is judged in chunks this long." },
      { path: "vad.threshold", label: "Voice detection threshold", type: "number", min: 0.05, max: 0.95, step: 0.05,
        help: "Higher ignores more background noise." },
    ],
  },
  {
    title: "Tone of voice",
    note: "A second model listens to how you sound, not just your words, e.g. to catch sarcasm or a laugh.",
    fields: [
      { path: "audio_emotion.enabled", label: "Use tone of voice", type: "checkbox",
        help: "The first time you turn this on it downloads a ~1.3 GB model." },
      { path: "audio_emotion.weight", label: "Voice weight", type: "number", min: 0, max: 1, step: 0.05,
        help: "How much the voice counts compared with the words. 0 = words only, 1 = voice only." },
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
    if (data.missing) {
      flash("device-msg", `Your saved mic "${data.missing}" isn't connected, so the system default is used.`, true);
    } else {
      flash("device-msg", "");
    }
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

// --- processor (CPU / GPU) ---------------------------------------------------------

const DEVICE_NAMES = { cuda: "GPU", cpu: "CPU" };

function renderRunning(devices, switching) {
  if (!devices) return;
  const parts = [["speech-to-text", devices.speech], ["words", devices.text], ["voice", devices.voice]]
    .filter(([, device]) => device)
    .map(([what, device]) => `${what} ${DEVICE_NAMES[device] ?? device}`);
  $("compute-running").textContent = switching
    ? "Switching... the current models keep running until the new ones are loaded."
    : parts.length ? `Running now: ${parts.join(" · ")}` : "";
}

async function loadCompute() {
  const info = await api("/api/compute");
  const select = $("compute");
  select.value = info.device;
  select.querySelector('option[value="cuda"]').disabled = !info.gpu.available;
  $("compute-gpu").textContent = info.gpu.available
    ? `GPU found: ${info.gpu.name}.`
    : `GPU not available: ${info.gpu.reason}.`;
  renderRunning(info.running, false);
}

async function applyCompute() {
  const select = $("compute");
  select.disabled = true;
  try {
    // Status messages over the WebSocket report the switch as it happens.
    await sendJson("/api/compute", "PUT", { device: select.value });
    flash("compute-msg", select.value === "cpu" ? "Moving the models to the CPU..." : "Loading the models...");
  } catch (err) {
    flash("compute-msg", err.message, true);
    await loadCompute();
  } finally {
    select.disabled = false;
  }
}

// --- OBS scenes and filters ---------------------------------------------------------

function obsSelect(names, value, emptyLabel, onChange) {
  const options = [["", emptyLabel], ...names.map((n) => [n, n])];
  if (value && !names.includes(value)) options.push([value, `${value} (not found in OBS)`]);
  const select = el("select", {}, ...options.map(([v, label]) => el("option", { value: v, textContent: label })));
  select.value = value ?? "";
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

async function saveObsMappings(patch) {
  try {
    renderObs(await sendJson("/api/obs/mappings", "PUT", patch));
    flash("obs-msg", "Saved.");
  } catch (err) {
    flash("obs-msg", err.message, true);
  }
}

function renderObs(info) {
  ui.obs = info;
  $("obs-enabled").checked = info.enabled;
  $("obs-host").value = info.host;
  $("obs-port").value = String(info.port);
  $("obs-password").value = "";
  $("obs-password").placeholder = info.password_set
    ? "Password (saved; type to change)"
    : "Password (if OBS asks for one)";
  if (info.status) renderObsStatus(info.status);
  const connected = info.status?.state === "connected";
  $("obs-mappings").hidden = !connected;
  if (!connected) return;

  const source = $("obs-filter-source");
  const sources = [["", "No filters"], ...info.obs_sources.map((n) => [n, n])];
  if (info.filter_source && !info.obs_sources.includes(info.filter_source)) {
    sources.push([info.filter_source, `${info.filter_source} (not found in OBS)`]);
  }
  source.replaceChildren(...sources.map(([v, label]) => el("option", { value: v, textContent: label })));
  source.value = info.filter_source;

  const head = el(
    "div",
    { className: "obs-row head" },
    el("span", { textContent: "Emotion" }),
    el("span", { textContent: "Switch to scene" }),
    el("span", { textContent: info.filter_source ? `Turn on filter (on ${info.filter_source})` : "Turn on filter" }),
  );
  const rows = ui.emotions.map((emotion) => {
    const scene = obsSelect(info.obs_scenes, info.scenes[emotion], "Don't switch", (value) => {
      const next = { ...info.scenes };
      if (value) next[emotion] = value;
      else delete next[emotion];
      saveObsMappings({ scenes: next });
    });
    const filter = obsSelect(info.obs_filters, info.filters[emotion], "None", (value) => {
      const next = { ...info.filters };
      if (value) next[emotion] = value;
      else delete next[emotion];
      saveObsMappings({ filters: next });
    });
    filter.disabled = !info.filter_source;
    return el("div", { className: "obs-row" }, el("span", { textContent: emotion }), scene, filter);
  });
  $("obs-rows").replaceChildren(head, ...rows);
}

function renderObsStatus(status) {
  const was = ui.obsState;
  ui.obsState = status.state;
  $("obs-status").textContent = status.message;
  // Just connected: fetch the scene, source and filter names to choose from.
  if (status.state === "connected" && was !== "connected" && was !== undefined) loadObs();
  if (status.state !== "connected") $("obs-mappings").hidden = true;
}

async function loadObs() {
  try {
    const info = await api("/api/obs");
    if (!info.available) {
      $("obs-status").textContent = "OBS control isn't available in this mode.";
      return;
    }
    renderObs(info);
  } catch (err) {
    flash("obs-msg", err.message, true);
  }
}

async function saveObsConnection() {
  const body = {
    enabled: $("obs-enabled").checked,
    host: $("obs-host").value.trim() || "127.0.0.1",
    port: Number($("obs-port").value) || 4455,
  };
  const password = $("obs-password").value;
  if (password) body.password = password; // left empty: keep the saved one
  try {
    renderObs(await sendJson("/api/obs/connection", "PUT", body));
    flash("obs-msg", body.enabled ? "Saved. Connecting..." : "Saved. Disconnected.");
  } catch (err) {
    flash("obs-msg", err.message, true);
  }
}

// --- images -------------------------------------------------------------------

async function loadImages() {
  const data = await api("/api/images");
  ui.images = data.images;
  ui.defaultImage = data.default_image;
  ui.maxBytes = data.max_bytes;
  renderImages();
  await loadTriggers(); // deleting an image also deletes its triggers
}

// --- word triggers ---------------------------------------------------------------

async function loadTriggers() {
  ui.triggers = (await api("/api/triggers")).triggers;
  renderTriggers();
  await loadHotkeys(); // hotkeys pick from the images and triggers, and warn when one is deleted
}

// A dropdown that shows a small preview of each image or GIF next to its name
// (a native <select> can't show images). Trigger-only images are listed first.
function imagePicker(selectedId, onPick, { compact = false } = {}) {
  const groupOrder = (img) => (img.emotion === "trigger" ? -1 : ui.imageGroups.indexOf(img.emotion));
  const images = [...ui.images].sort((a, b) => groupOrder(a) - groupOrder(b));
  const current = images.find((img) => img.id === selectedId);

  const toggle = el(
    "button",
    {
      type: "button",
      className: `picker-toggle${compact ? " compact" : ""}`,
      title: current ? `${current.original_name} (click to change)` : "Choose an image",
    },
    current ? el("img", { src: current.url, alt: "" }) : el("span", { className: "picker-blank" }),
    ...(compact
      ? []
      : [el("span", { className: "picker-name", textContent: current?.original_name ?? "Choose an image…" })]),
    el("span", { className: "picker-caret", textContent: "▾" }),
  );
  toggle.setAttribute("aria-haspopup", "listbox");

  const menu = el("div", { className: "picker-menu", hidden: true });
  menu.setAttribute("role", "listbox");
  if (!images.length) {
    menu.append(el("p", { className: "muted small", textContent: "Upload an image first." }));
  }
  let group = null;
  for (const img of images) {
    if (img.emotion !== group) {
      group = img.emotion;
      menu.append(el("div", { className: "picker-group", textContent: GROUP_LABELS[group] ?? group }));
    }
    const option = el(
      "button",
      { type: "button", className: `picker-option${img.id === selectedId ? " selected" : ""}`, title: img.original_name },
      el("img", { src: img.url, alt: "", loading: "lazy" }),
      el("span", { textContent: img.original_name }),
    );
    option.setAttribute("role", "option");
    option.addEventListener("click", () => {
      close();
      onPick(img.id);
    });
    menu.append(option);
  }

  const wrap = el("div", { className: "picker" }, toggle, menu);
  const outside = (e) => !wrap.contains(e.target) && close();
  function close() {
    menu.hidden = true;
    document.removeEventListener("click", outside, true);
  }
  toggle.addEventListener("click", () => {
    if (!menu.hidden) return close();
    menu.hidden = false;
    document.addEventListener("click", outside, true);
    menu.querySelector(".selected, .picker-option")?.focus();
  });
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      close();
      toggle.focus();
    }
  });
  return wrap;
}

function renderNewTriggerPicker() {
  // Keep the choice if it still exists; otherwise default to the first trigger-only image.
  if (!ui.images.some((img) => img.id === ui.newTriggerImage)) {
    const preferred = ui.images.find((img) => img.emotion === "trigger") ?? ui.images[0];
    ui.newTriggerImage = preferred?.id ?? null;
  }
  $("trigger-image").replaceChildren(
    imagePicker(ui.newTriggerImage, (id) => {
      ui.newTriggerImage = id;
      renderNewTriggerPicker();
    }),
  );
}

function renderTriggers() {
  renderNewTriggerPicker();
  const rows = ui.triggers.map((t) => {
    const duration = el("input", { type: "number", min: 0.5, max: 60, step: 0.5, value: t.duration_s });
    duration.addEventListener("change", () => updateTrigger(t.id, { duration_s: Number(duration.value) }));
    const remove = el("button", { className: "icon danger", title: "Delete trigger", textContent: "✕" });
    remove.addEventListener("click", () => deleteTrigger(t));
    return el(
      "div",
      { className: "trigger-row" },
      imagePicker(t.image_id, (id) => updateTrigger(t.id, { image_id: id }), { compact: true }),
      el("span", { className: "trigger-phrases", textContent: t.phrases.map((p) => `“${p}”`).join(", ") }),
      el("label", { className: "inline", title: "Seconds to show the image" }, duration, " s"),
      button("Test", () => testTrigger(t.id)),
      remove,
    );
  });
  $("triggers").replaceChildren(
    ...(rows.length ? rows : [el("p", { className: "muted small", textContent: "No triggers yet." })]),
  );
}

async function addTrigger() {
  const body = {
    phrases: $("trigger-phrases").value,
    image_id: ui.newTriggerImage,
    duration_s: Number($("trigger-duration").value) || 3,
  };
  try {
    await sendJson("/api/triggers", "POST", body);
    $("trigger-phrases").value = "";
    flash("trigger-msg", "Trigger added. Say it, or click Test.");
  } catch (err) {
    flash("trigger-msg", err.message, true);
  }
  await loadTriggers();
}

async function updateTrigger(id, changes) {
  try {
    await sendJson(`/api/triggers/${id}`, "PUT", changes);
    flash("trigger-msg", "Saved.");
  } catch (err) {
    flash("trigger-msg", err.message, true);
  }
  await loadTriggers();
}

async function deleteTrigger(trigger) {
  if (!confirm(`Delete the trigger for ${trigger.phrases.map((p) => `"${p}"`).join(", ")}?`)) return;
  try {
    await api(`/api/triggers/${trigger.id}`, { method: "DELETE" });
  } catch (err) {
    flash("trigger-msg", err.message, true);
  }
  await loadTriggers();
}

async function testTrigger(id) {
  try {
    await api(`/api/triggers/${id}/test`, { method: "POST" });
    flash("trigger-msg", "Showing it on the overlay now.");
  } catch (err) {
    flash("trigger-msg", err.message, true);
  }
}

const GROUP_LABELS = { trigger: "Word triggers only" };

function renderImages() {
  const cards = ui.imageGroups.map((emotion) => {
    const mine = ui.images.filter((img) => img.emotion === emotion);
    const input = el("input", {
      type: "file",
      accept: "image/png,image/jpeg,image/webp,image/gif",
      multiple: true,
      hidden: true,
    });
    input.addEventListener("change", () => upload(emotion, input.files));
    const add = el("button", { className: "secondary", textContent: "Add", title: "Upload image or GIF files" });
    add.addEventListener("click", () => input.click());
    const link = el("button", { className: "secondary", textContent: "Link", title: "Add a GIF from a link" });
    link.addEventListener("click", () => {
      const url = prompt(
        `Paste a link to a GIF or image for "${GROUP_LABELS[emotion] ?? emotion}".\n` +
          "Direct links work, and so do Tenor/Giphy page links.",
      );
      if (url && url.trim()) addFromLink(emotion, url.trim());
    });

    const thumbs = el("div", { className: "thumbs" }, ...mine.map(thumbnail));
    if (!mine.length) {
      const hint = emotion === "trigger" ? "Images here are only shown by word triggers" : "No images";
      thumbs.append(el("p", { className: "muted small", textContent: hint }));
    }

    const card = el(
      "div",
      { className: `emotion${emotion === "trigger" ? " trigger-group" : ""}` },
      el(
        "div",
        { className: "emotion-head" },
        el("h3", { textContent: GROUP_LABELS[emotion] ?? emotion }),
        el("div", { className: "head-buttons" }, add, link),
      ),
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
      if (e.dataTransfer.files.length) {
        upload(emotion, e.dataTransfer.files);
        return;
      }
      // An image dragged from another browser tab arrives as a link.
      const url = (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain"))
        .split("\n")
        .find((line) => line.startsWith("http"));
      if (url) addFromLink(emotion, url.trim());
    });
    return card;
  });
  $("emotions").replaceChildren(...cards);
}

async function addFromLink(emotion, url) {
  flash("image-msg", "Downloading…");
  try {
    const image = await sendJson("/api/images/from-url", "POST", { url, emotion });
    flash("image-msg", `Added ${image.original_name}.`);
  } catch (err) {
    flash("image-msg", err.message, true);
  }
  await loadImages();
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
  tier.hidden = img.emotion === "trigger"; // tiers only apply to emotion images

  // Editable name, so links that all arrive as "giphy.gif" can be told apart.
  const name = el("input", {
    type: "text",
    className: "thumb-name",
    value: img.original_name,
    maxLength: 120,
    title: "Rename (press Enter)",
  });
  name.setAttribute("aria-label", "Image name");
  name.addEventListener("keydown", (e) => e.key === "Enter" && name.blur());
  name.addEventListener("change", () => {
    const value = name.value.trim();
    if (!value) {
      name.value = img.original_name;
      return;
    }
    if (value !== img.original_name) updateImage(img.id, { original_name: value });
  });

  return el(
    "figure",
    { className: `thumb${isDefault ? " default" : ""}` },
    el("img", { src: img.url, alt: img.original_name, title: img.original_name, loading: "lazy" }),
    ...(img.format === "GIF" ? [el("span", { className: "badge", textContent: "GIF" })] : []),
    tier,
    name,
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
    const shown = field.toInput ? field.toInput(value) : value;
    const options = Array.isArray(field.options) ? [...field.options] : Object.entries(field.options);
    if (!options.some(([v]) => v === String(shown))) options.push([String(shown), `${shown} (from config.yaml)`]);
    input = el(
      "select",
      {},
      ...options.map(([v, label]) => el("option", { value: v, textContent: label })),
    );
    input.value = shown;
  } else if (field.type === "checkbox") {
    input = el("input", { type: "checkbox", checked: value });
  } else {
    input = el("input", { type: "number", min: field.min, max: field.max, step: field.step, value });
  }
  input.addEventListener("change", () => {
    let next = field.fromInput ? field.fromInput(input.value) : input.value;
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

const EFFECT_NAMES = {
  "": "None",
  shake: "Shake",
  pulse: "Pulse",
  jump: "Jump",
  tremble: "Tremble",
  droop: "Droop",
};

function effectsGroup() {
  // For each emotion: a short animation the overlay plays when its image appears.
  const effects = ui.settings.display.effects ?? {};
  const rows = ui.emotions.map((emotion) => {
    const input = el(
      "select",
      {},
      ...Object.entries(EFFECT_NAMES).map(([v, label]) => el("option", { value: v, textContent: label })),
    );
    input.value = effects[emotion] ?? "";
    input.addEventListener("change", () => {
      const next = { ...effects };
      if (input.value) next[emotion] = input.value;
      else delete next[emotion];
      saveSettings({ display: { effects: next } });
    });
    return settingRow(emotion, input);
  });
  return el(
    "fieldset",
    {},
    el("legend", { textContent: "Effects" }),
    el("p", {
      className: "hint",
      textContent:
        "A short animation the overlay plays when an emotion's image appears. Try one with the Hold an emotion buttons while OBS is open.",
    }),
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
  $("settings").replaceChildren(...groups, fallbackGroup(), effectsGroup());
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

// --- calibration --------------------------------------------------------------

const SAY_IT = {
  joy: "happy",
  anger: "angry",
  sadness: "sad",
  surprise: "surprised",
  fear: "scared",
  disgust: "disgusted",
  neutral: "plain and calm",
};

const percent = (value) => (value === null || value === undefined ? "n/a" : `${Math.round(value * 100)}%`);

function button(text, onClick, secondary = true) {
  const node = el("button", { className: secondary ? "secondary" : "", textContent: text });
  node.addEventListener("click", onClick);
  return node;
}

async function calibrationAction(action, method = "POST") {
  try {
    const path = action ? `/api/calibration/${action}` : "/api/calibration";
    renderCalibration(await api(path, { method }));
    flash("calibration-msg", "");
  } catch (err) {
    flash("calibration-msg", err.message, true);
  }
}

async function saveCalibration(applyPaths) {
  try {
    const msg = await sendJson("/api/calibration/save", "POST", { apply: applyPaths });
    renderCalibration(msg);
    if (applyPaths.length) {
      await loadSettings(); // show the new values in the Settings panel
      flash("calibration-msg", `Saved, and applied ${applyPaths.length} setting(s).`);
    } else {
      flash("calibration-msg", "Calibration saved.");
    }
  } catch (err) {
    flash("calibration-msg", err.message, true);
  }
}

const formatValue = (value) =>
  typeof value === "boolean" ? (value ? "on" : "off") : Number.isInteger(value) ? `${value}` : value.toFixed(2);

function profileLines(profile) {
  if (!profile) return [];
  const lines = [];
  if (profile.pauses_measured !== undefined) {
    lines.push(
      profile.pauses_measured
        ? `Pauses inside your lines: most (75%) were ${profile.pause_p75_ms} ms or shorter (${profile.pauses_measured} measured).`
        : "Pauses inside your lines: none, you spoke each line in one go.",
    );
  }
  if (profile.speech_level !== undefined) {
    lines.push(
      `Voice detection: your speech scored ${profile.speech_level.toFixed(2)}+, ` +
        `background up to ${profile.background_level.toFixed(2)} (0 = silence, 1 = clear speech).`,
    );
  }
  const weights = profile.voice_weight_accuracy;
  if (weights) {
    const best = Object.entries(weights).sort((a, b) => b[1] - a[1])[0];
    lines.push(`Tone of voice: your lines scored best at voice weight ${best[0]} (${percent(best[1])} correct).`);
  }
  return lines.map((text) => el("li", { textContent: text }));
}

function recommendationList(recommendations) {
  // One checkbox per recommendation, all ticked; the user can untick any.
  const rows = recommendations.map((rec) => {
    const box = el("input", { type: "checkbox", checked: true, value: rec.path });
    return el(
      "label",
      { className: "recommendation" },
      box,
      el(
        "span",
        {},
        el("strong", { textContent: `${rec.label}: ` }),
        el("span", { className: "mono", textContent: `${formatValue(rec.current)} → ${formatValue(rec.value)}` }),
        el("span", { className: "setting-help", textContent: rec.reason }),
      ),
    );
  });
  return el("div", { className: "recommendations" }, ...rows);
}

function accuracyTable(accuracy) {
  const rows = [
    ["Words only", accuracy.text],
    ["Voice only", accuracy.voice],
    ["Combined, before calibration", accuracy.before],
    ["Combined, after calibration*", accuracy.after],
  ];
  return el(
    "table",
    { className: "accuracy" },
    ...rows.map(([label, value]) =>
      el("tr", {}, el("td", { textContent: label }), el("td", { className: "mono", textContent: percent(value) })),
    ),
  );
}

function renderCalibration(msg) {
  ui.calibration = msg;
  const box = $("calibration");
  const voiceState = ui.voiceModel ? `Tone-of-voice model: ${ui.voiceModel}.` : "";

  if (msg.active) {
    const s = msg.session;
    const last = s.last
      ? `Last line: the words read as ${s.last.heard ?? "(no words heard)"}` +
        (s.last.voice ? `, the voice as ${s.last.voice}.` : ".")
      : "Waiting for you to speak…";
    box.replaceChildren(
      el(
        "div",
        { className: "prompt" },
        el("p", { className: "label", textContent: `Line ${s.index + 1} of ${s.total}` }),
        el("p", { className: "prompt-how", textContent: `Say it ${SAY_IT[s.prompt.emotion] ?? s.prompt.emotion}:` }),
        el("p", { className: "prompt-line", textContent: `“${s.prompt.text}”` }),
        el("p", { className: "muted small", textContent: last }),
      ),
      el(
        "div",
        { className: "row" },
        button("Back", () => calibrationAction("back")),
        button("Skip line", () => calibrationAction("skip")),
        button("Finish now", () => calibrationAction("finish")),
        button("Cancel", () => calibrationAction("cancel")),
      ),
    );
    return;
  }

  if (msg.result || msg.error) {
    const parts = [];
    if (msg.error) parts.push(el("p", { className: "msg error", textContent: msg.error }));
    let list = null;
    if (msg.result) {
      parts.push(
        el("p", { textContent: `Recorded ${msg.result.samples} lines. How often each part picked the right emotion:` }),
        accuracyTable(msg.result.accuracy),
        el("p", {
          className: "muted small",
          textContent:
            "* Each line is scored by a calibration fitted on the other lines only, so this isn't inflated. " +
            "It already uses the voice weight recommended below. With so few lines, treat these numbers as rough.",
        }),
      );
      const profile = profileLines(msg.result.profile);
      if (profile.length) {
        parts.push(el("h3", { className: "subhead", textContent: "How you speak" }), el("ul", {}, ...profile));
      }
      parts.push(el("h3", { className: "subhead", textContent: "Recommended settings for you" }));
      if (msg.recommendations.length) {
        list = recommendationList(msg.recommendations);
        parts.push(list);
      } else {
        parts.push(el("p", { className: "muted", textContent: "Your current settings already suit you; no changes needed." }));
      }
    }
    const checkedPaths = () => (list ? [...list.querySelectorAll("input:checked")].map((box) => box.value) : []);
    parts.push(
      el(
        "div",
        { className: "row" },
        ...(msg.result && msg.recommendations.length
          ? [
              button("Save and apply ticked settings", () => saveCalibration(checkedPaths()), false),
              button("Save calibration only", () => saveCalibration([])),
            ]
          : msg.result
            ? [button("Save calibration", () => saveCalibration([]), false)]
            : []),
        button(msg.result ? "Discard" : "Close", () => calibrationAction("cancel")),
        button("Start over", () => calibrationAction("start")),
      ),
    );
    box.replaceChildren(...parts);
    return;
  }

  const saved = msg.saved;
  const summary = saved
    ? `Calibrated ${new Date(saved.created).toLocaleString()} from ${saved.samples} lines ` +
      `(combined accuracy ${percent(saved.accuracy.before)} → ${percent(saved.accuracy.after)}).`
    : "Not calibrated yet. The app works without it, but calibration helps the voice model fit your mic.";
  const applied = (msg.applied ?? []).map((rec) =>
    el("li", { textContent: `${rec.label}: ${formatValue(rec.current)} → ${formatValue(rec.value)}` }),
  );
  box.replaceChildren(
    el("p", { textContent: summary }),
    ...(applied.length ? [el("p", { textContent: "Settings adjusted for you:" }), el("ul", {}, ...applied)] : []),
    ...(saved ? [el("ul", { className: "muted small" }, ...profileLines(saved.profile))] : []),
    ...(voiceState ? [el("p", { className: "muted small", textContent: voiceState })] : []),
    el(
      "div",
      { className: "row" },
      button(saved ? "Recalibrate" : "Start calibration", () => calibrationAction("start"), false),
      ...(saved
        ? [button("Remove calibration", () => confirm("Remove your calibration?") && calibrationAction("", "DELETE"))]
        : []),
    ),
  );
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
  $("confidence").textContent = msg.confidence && !msg.manual ? `${Math.round(msg.confidence * 100)}%` : "";
  $("reason").textContent = msg.trigger
    ? `${msg.trigger_source === "hotkey" ? "hotkey image" : "word trigger"} “${msg.trigger}”`
    : msg.held_image
      ? `holding “${msg.held_image}” (detected: ${msg.detected})`
      : msg.manual
        ? `held by hand (detected: ${msg.detected})`
        : msg.reason === "scores"
          ? `${msg.tier}`
          : msg.reason;
  renderManual(msg.held_image ? "held" : (msg.manual ?? null)); // "held" lights no button
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
  renderRunning(msg.devices, msg.switching);
  if (ui.wasSwitching && !msg.switching) {
    const failed = (msg.message ?? "").startsWith("Couldn't");
    flash("compute-msg", failed ? msg.message : "Done.", failed);
  }
  ui.wasSwitching = Boolean(msg.switching);

  if (msg.voice_model && msg.voice_model !== ui.voiceModel) {
    ui.voiceModel = msg.voice_model;
    if (ui.calibration) renderCalibration(ui.calibration);
  }

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
  $("transcript").textContent = msg.transcript || "(no words heard)";
  if (msg.calibrating) {
    $("window").textContent = "Calibrating: the overlay isn't changed by these lines.";
    return;
  }
  $("window").textContent = msg.window || "—";
  if (msg.interim) $("transcript").textContent = `${msg.transcript} …(still talking)`;
  const voice = msg.voice ?? {};
  const topVoice = Object.entries(voice).sort((a, b) => b[1] - a[1])[0];
  $("voice").textContent = topVoice
    ? `Voice: ${topVoice[0]} ${Math.round(topVoice[1] * 100)}%${msg.calibrated ? " · calibrated" : ""}`
    : "Voice: off";
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
  if (msg.interim) {
    $("voice").textContent = "Voice: checked when the sentence ends";
    $("timings").textContent = `${msg.duration_s}s so far · words checked in ${msg.took_ms} ms`;
    return;
  }
  $("timings").textContent =
    `${msg.duration_s}s utterance${msg.forced ? " (cut)" : ""} · ASR ${t.asr_ms} ms · ` +
    `text ${t.classify_ms} ms · voice ${t.voice_ms ?? "–"} ms · ` +
    `speech end → state ${t.speech_end_to_state_ms} ms` +
    (t.words_shown_ms != null ? ` (words-only result shown at ${t.words_shown_ms} ms)` : "");
}

// --- hold an emotion by hand ------------------------------------------------------

function renderManual(active) {
  for (const button of $("manual-buttons").children) {
    const on = button.dataset.emotion === (active ?? "");
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", String(on));
  }
}

async function setManual(emotion) {
  try {
    // The state message that follows over the WebSocket highlights the button.
    await sendJson("/api/manual", "PUT", { emotion });
  } catch (err) {
    flash("manual-hint", err.message, true);
  }
}

async function loadManual() {
  const auto = el("button", { type: "button", className: "secondary", textContent: "Automatic" });
  auto.dataset.emotion = "";
  auto.addEventListener("click", () => setManual(null));
  const buttons = ui.emotions.map((emotion) => {
    const button = el("button", { type: "button", className: "secondary", textContent: emotion });
    button.dataset.emotion = emotion;
    button.addEventListener("click", () => setManual(emotion));
    return button;
  });
  $("manual-buttons").replaceChildren(auto, ...buttons);

  const info = await api("/api/manual");
  renderManual(info.image_id ? "held" : info.emotion);
  flash(
    "manual-hint",
    "Keeps showing that emotion until you press Automatic. To do this from any app, set up keys under Hotkeys below.",
  );
}

// --- hotkeys ----------------------------------------------------------------------

const HOTKEY_ACTIONS = [
  ["emotion", "Hold an emotion"],
  ["image", "Show an image"],
  ["trigger", "Fire a word trigger"],
  ["auto", "Back to automatic"],
];

// KeyboardEvent.code -> the key names the server understands (see hotkeys.py).
// Codes name physical keys, so this doesn't depend on the keyboard layout.
const CODE_KEYS = {
  Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\",
  Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`",
  Space: "space", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  Home: "home", End: "end", PageUp: "pageup", PageDown: "pagedown", Insert: "insert",
  Delete: "delete", Pause: "pause", ScrollLock: "scrolllock",
  NumpadMultiply: "nummultiply", NumpadAdd: "numadd", NumpadSubtract: "numsubtract",
  NumpadDecimal: "numdecimal", NumpadDivide: "numdivide",
};
const MODIFIER_CODE = /^(Control|Alt|Shift|Meta|OS)(Left|Right)?$/;

function keyName(code) {
  let m;
  if ((m = /^Key([A-Z])$/.exec(code))) return m[1].toLowerCase();
  if ((m = /^Digit(\d)$/.exec(code))) return m[1];
  if ((m = /^Numpad(\d)$/.exec(code))) return `num${m[1]}`;
  if (/^F([1-9]|1\d|2[0-4])$/.test(code)) return code.toLowerCase();
  return CODE_KEYS[code] ?? null;
}

// Waits for a key combination and resolves with it ("ctrl+alt+k"), or null on Esc.
// The hotkeys are switched off meanwhile: Windows would hand an already-used
// combination to this app instead of the browser.
async function captureKeys(target) {
  const before = target.textContent;
  target.textContent = "Press the keys… (Esc cancels)";
  target.classList.add("capturing");
  try {
    await sendJson("/api/hotkeys/capture", "PUT", { capturing: true });
  } catch {
    // capturing still works for combinations that aren't in use
  }
  const combo = await new Promise((resolve) => {
    const swallow = (e) => e.preventDefault(); // e.g. Alt focusing the browser menu
    function done(value) {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("keyup", swallow, true);
      window.removeEventListener("blur", cancel);
      resolve(value);
    }
    const cancel = () => done(null);
    function onKey(e) {
      e.preventDefault();
      e.stopPropagation();
      if (MODIFIER_CODE.test(e.code)) return; // wait for the actual key
      const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "win"].filter(Boolean);
      if (e.code === "Escape" && !mods.length) return done(null);
      const key = keyName(e.code);
      if (!key) {
        target.textContent = `${e.key} can't be used; try another key (Esc cancels)`;
        return;
      }
      done([...mods, key].join("+"));
    }
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("keyup", swallow, true);
    window.addEventListener("blur", cancel);
  });
  target.classList.remove("capturing");
  target.textContent = before;
  try {
    await sendJson("/api/hotkeys/capture", "PUT", { capturing: false });
  } catch {
    // they come back by themselves after a short while
  }
  return combo;
}

const plainBinding = ({ keys, action, emotion, image_id, trigger_id, duration_s }) => ({
  keys, action, emotion, image_id, trigger_id, duration_s,
});

async function saveHotkeys(bindings, enabled = ui.hotkeys.enabled, message = "Saved.") {
  try {
    renderHotkeys(await sendJson("/api/hotkeys", "PUT", { enabled, bindings }));
    flash("hotkeys-msg", message);
    return true;
  } catch (err) {
    flash("hotkeys-msg", err.message, true);
    renderHotkeys(ui.hotkeys); // undo what the controls show
    return false;
  }
}

function saveHotkey(index, changes) {
  const bindings = ui.hotkeys.bindings.map(plainBinding);
  bindings[index] = { ...bindings[index], ...changes };
  return saveHotkeys(bindings);
}

const triggerLabel = (t) => t.phrases.map((p) => `“${p}”`).join(", ");
const firstImage = () => (ui.images.find((img) => img.emotion === "trigger") ?? ui.images[0])?.id;

// What a hotkey gets when it's switched to `action`, or null (with a message) if it can't be.
function defaultsFor(action) {
  if (action === "emotion") return { emotion: ui.emotions.includes("neutral") ? "neutral" : ui.emotions[0] };
  if (action === "image") {
    const id = firstImage();
    if (!id) flash("hotkeys-msg", "Upload an image first (Images above).", true);
    return id ? { image_id: id, duration_s: null } : null;
  }
  if (action === "trigger") {
    if (!ui.triggers.length) flash("hotkeys-msg", "Add a word trigger first (Word triggers above).", true);
    return ui.triggers.length ? { trigger_id: ui.triggers[0].id } : null;
  }
  return {};
}

function select(options, value, onChange) {
  const node = el("select", {}, ...options.map(([v, label]) => el("option", { value: v, textContent: label })));
  node.value = value;
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

function hotkeyTarget(b, index) {
  if (b.action === "emotion") {
    return select(ui.emotions.map((e) => [e, e]), b.emotion, (emotion) => saveHotkey(index, { emotion }));
  }
  if (b.action === "trigger") {
    const options = ui.triggers.map((t) => [t.id, triggerLabel(t)]);
    if (!ui.triggers.some((t) => t.id === b.trigger_id)) options.unshift([b.trigger_id, "(deleted trigger)"]);
    return select(options, b.trigger_id, (trigger_id) => saveHotkey(index, { trigger_id }));
  }
  if (b.action === "image") {
    const seconds = el("input", { type: "number", min: 0.5, max: 60, step: 0.5, value: b.duration_s ?? 3 });
    const secondsLabel = el("label", { className: "inline", title: "Seconds to show the image" }, seconds, " s");
    secondsLabel.hidden = b.duration_s === null;
    seconds.addEventListener("change", () => saveHotkey(index, { duration_s: Number(seconds.value) || 3 }));
    const mode = select(
      [["hold", "until pressed again"], ["timed", "for a few seconds"]],
      b.duration_s === null ? "hold" : "timed",
      (value) => saveHotkey(index, { duration_s: value === "hold" ? null : Number(seconds.value) || 3 }),
    );
    return el(
      "div",
      { className: "hotkey-image" },
      imagePicker(b.image_id, (image_id) => saveHotkey(index, { image_id })),
      mode,
      secondsLabel,
    );
  }
  return el("span", { className: "muted small", textContent: "Shows detected emotions again" });
}

function hotkeyRow(b, index) {
  const keys = el("button", {
    type: "button",
    className: "secondary hotkey-keys",
    textContent: b.label,
    title: "Click, then press the new keys",
  });
  keys.addEventListener("click", async () => {
    const combo = await captureKeys(keys);
    if (combo) await saveHotkey(index, { keys: combo });
  });
  const action = select(HOTKEY_ACTIONS, b.action, (value) => {
    const defaults = defaultsFor(value);
    if (defaults) saveHotkey(index, { action: value, ...defaults });
    else action.value = b.action;
  });
  const test = button("Test", () => testHotkey(index));
  const remove = el("button", { className: "icon danger", title: "Delete hotkey", textContent: "✕" });
  remove.addEventListener("click", () => {
    const bindings = ui.hotkeys.bindings.map(plainBinding).filter((_, i) => i !== index);
    saveHotkeys(bindings, ui.hotkeys.enabled, "Hotkey deleted.");
  });
  const row = el("div", { className: "hotkey-row" }, keys, action, hotkeyTarget(b, index), test, remove);
  if (b.problem) row.append(el("p", { className: "hotkey-problem small", textContent: `Not working: ${b.problem}.` }));
  return row;
}

function renderHotkeys(info) {
  ui.hotkeys = info;
  $("hotkeys-enabled").checked = info.enabled;
  flash("hotkeys-problem", info.problem && info.enabled ? `Hotkeys are off: ${info.problem}.` : "", true);
  const rows = info.bindings.map(hotkeyRow);
  $("hotkeys").replaceChildren(
    ...(rows.length ? rows : [el("p", { className: "muted small", textContent: "No hotkeys yet." })]),
  );
}

async function loadHotkeys() {
  try {
    renderHotkeys(await api("/api/hotkeys"));
  } catch (err) {
    flash("hotkeys-msg", err.message, true);
  }
}

async function addHotkey() {
  if (!ui.hotkeys) return;
  const combo = await captureKeys($("hotkey-add"));
  if (!combo) return;
  // New hotkeys hold an image if there is one: emotions already have keys by default.
  const id = firstImage();
  const binding = id
    ? { keys: combo, action: "image", image_id: id, duration_s: null }
    : { keys: combo, action: "emotion", emotion: "neutral" };
  const bindings = [...ui.hotkeys.bindings.map(plainBinding), binding];
  if (await saveHotkeys(bindings, ui.hotkeys.enabled, "Hotkey added. Pick what it does.")) {
    $("hotkeys").lastElementChild?.scrollIntoView({ block: "nearest" });
  }
}

async function testHotkey(index) {
  try {
    await api(`/api/hotkeys/${index}/press`, { method: "POST" });
    flash("hotkeys-msg", "Done; check the Live preview.");
  } catch (err) {
    flash("hotkeys-msg", err.message, true);
  }
}

async function resetHotkeys() {
  if (!confirm("Replace all hotkeys with the defaults (Ctrl+Alt+Shift + 1–7 for the emotions, + 0 for automatic)?")) return;
  try {
    renderHotkeys(await api("/api/hotkeys/reset", { method: "POST" }));
    flash("hotkeys-msg", "Hotkeys reset.");
  } catch (err) {
    flash("hotkeys-msg", err.message, true);
  }
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
  $("compute").addEventListener("change", applyCompute);
  $("obs-save").addEventListener("click", saveObsConnection);
  $("obs-filter-source").addEventListener("change", () =>
    saveObsMappings({ filter_source: $("obs-filter-source").value }),
  );
  $("listen-toggle").addEventListener("click", toggleListening);
  $("settings-reset").addEventListener("click", resetSettings);
  $("import-btn").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importSetup);

  $("hotkey-add").addEventListener("click", addHotkey);
  $("hotkeys-reset").addEventListener("click", resetHotkeys);
  $("hotkeys-enabled").addEventListener("change", () =>
    saveHotkeys(ui.hotkeys.bindings.map(plainBinding), $("hotkeys-enabled").checked,
      $("hotkeys-enabled").checked ? "Hotkeys on." : "Hotkeys off."),
  );
  $("trigger-add").addEventListener("click", addTrigger);
  $("trigger-phrases").addEventListener("keydown", (e) => e.key === "Enter" && addTrigger());

  const groups = await api("/api/emotions");
  ui.emotions = groups.emotions;
  ui.imageGroups = groups.image_groups ?? groups.emotions;
  await Promise.all([loadImages(), loadDevices(), loadSettings(), loadManual(), loadCompute(), loadObs()]);

  connect("/ws?debug=1", {
    onOpen: () => setPill("conn", "connected", "ok"),
    onClose: () => setPill("conn", "disconnected (retrying)", "bad"),
    onMessage(msg) {
      if (msg.type === "state") renderState(msg);
      else if (msg.type === "status") renderStatus(msg);
      else if (msg.type === "vad") renderSpeaking(msg.speaking);
      else if (msg.type === "debug") renderDebug(msg);
      else if (msg.type === "calibration") renderCalibration(msg);
      else if (msg.type === "obs") renderObsStatus(msg);
    },
  });
}

init().catch((err) => flash("image-msg", `Setup failed to load: ${err.message}`, true));
