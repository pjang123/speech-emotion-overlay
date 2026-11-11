// OBS overlay: shows the image for the current emotion with a cross-fade.
// URL options: ?fit=cover (fill and crop instead of letterbox), ?label=1 (show emotion name).
import { connect } from "/static/common/socket.js";

const params = new URLSearchParams(location.search);
const layers = [document.getElementById("layer-a"), document.getElementById("layer-b")];
const label = document.getElementById("label");

if (params.get("fit") === "cover") layers.forEach((l) => l.classList.add("cover"));
label.hidden = params.get("label") !== "1";

let front = 0;
let wanted = null;

// Two stacked <img> layers: load the new image into the hidden one, then swap
// opacities so the old image fades out while the new one fades in.
// transition: "fade" (cross-fade), "cut" (instant) or "bounce" (cross-fade + pop).
async function show(src, fadeMs, transition) {
  if (src === wanted) return;
  wanted = src;
  const duration = transition === "cut" ? 0 : fadeMs;
  for (const layer of layers) layer.style.transitionDuration = `${duration}ms`;

  if (!src) {
    layers[front].classList.remove("visible");
    return;
  }
  const back = layers[1 - front];
  back.src = src;
  try {
    await back.decode(); // wait until it's ready so we never fade in a blank frame
  } catch {
    // decode() can reject for some GIFs; show it anyway
  }
  if (wanted !== src) return; // a newer change arrived while loading
  back.classList.remove("bounce");
  if (transition === "bounce") {
    void back.offsetWidth; // restart the animation even if this layer bounced last time
    back.style.animationDuration = `${Math.max(fadeMs, 250)}ms`;
    back.classList.add("bounce");
  }
  back.classList.add("visible");
  layers[front].classList.remove("visible");
  front = 1 - front;
}

// `play` goes up whenever an image (re)starts showing. Putting it in the URL makes
// the browser treat it as a fresh image, so animated GIFs start from their first
// frame instead of resuming wherever their loop happened to be.
const playable = (msg) => (msg.image ? `${msg.image}?play=${msg.play ?? 0}` : null);

// The overlay connects without ?debug=1, so it never receives transcripts.
connect("/ws", {
  onMessage(msg) {
    if (msg.type !== "state") return;
    label.textContent = msg.trigger ? `“${msg.trigger}”` : msg.emotion;
    show(playable(msg), msg.fade_ms ?? 300, msg.transition ?? "fade");
  },
});
