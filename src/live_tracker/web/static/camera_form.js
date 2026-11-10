// Camera form. People and objects: the sentence above the settings reads back what is recorded and when, each
// group counts its ticked kinds, and a search narrows the 80 kinds. When to record: how many photos an interval
// can make. Without this script the page still works, as plain fields.
(() => {
  const form = document.querySelector("form.camera-settings");
  if (!form) return;
  const field = (name) => form.elements.namedItem(name);
  const UNITS = { s: 1, min: 60, h: 3600 };
  const MIN_EVERY_S = 5; // as recording.py
  const MAX_EVERY_S = 24 * 3600;
  const PHOTO_MB = 0.25; // a record's photo and its thumbnail, about
  const numbers = new Intl.NumberFormat(document.documentElement.lang);
  const tenths = new Intl.NumberFormat(document.documentElement.lang, { maximumFractionDigits: 1 });

  const interval = () => {
    const value = Number(field("every_value")?.value);
    const unit = field("every_unit")?.value ?? "min";
    return { value, unit, seconds: Number.isInteger(value) ? value * (UNITS[unit] ?? 0) : NaN };
  };

  // People and objects
  const panel = form.querySelector("[data-objects]");
  const rule = panel?.querySelector("[data-rule]");
  const search = panel?.querySelector("[data-search]");
  const templates = panel ? JSON.parse(panel.dataset.templates) : {};

  const strong = (text) => {
    const element = document.createElement("strong");
    element.textContent = text;
    return element;
  };

  const writeRule = () => {
    const names = [...panel.querySelectorAll('input[name="kinds"]:checked')].map((box) => box.dataset.name);
    rule.classList.toggle("is-empty", names.length === 0);
    if (names.length === 0) {
      rule.textContent = panel.dataset.none;
      return;
    }
    const wait = Math.max(0, Math.round((Number(field("min_seconds")?.value) || 0) * 10) / 10); // tenths, as the field
    let mode = ["interval", "daily"].includes(field("record_mode")?.value) ? field("record_mode").value : "appear";
    if (mode === "interval" && field("record_while_present")?.checked === false) mode = "interval-back";
    const every = interval();
    const text = templates[wait ? mode : `${mode}-now`]
      .replace("{seconds} ", `${tenths.format(wait)}\u00a0`) // "0,5 s" stays on one line
      .replace("{seconds}", tenths.format(wait))
      .replace("{every}", `${every.value || 0}\u00a0${every.unit}`);
    const [before, after] = text.split("{kinds}");
    const parts = [before];
    names.forEach((name, i) => {
      parts.push(strong(name));
      if (i < names.length - 2) parts.push(", ");
      else if (i === names.length - 2) parts.push(` ${panel.dataset.and} `);
    });
    parts.push(after);
    rule.replaceChildren(...parts);
  };

  const countGroups = () => {
    for (const group of panel.querySelectorAll("[data-group]")) {
      const count = group.querySelector("[data-count]");
      const chosen = group.querySelectorAll('input[name="kinds"]:checked').length;
      count.textContent = count.dataset.template.replace("{chosen}", chosen).replace("{total}", count.dataset.total);
      count.classList.toggle("is-some", chosen > 0);
    }
  };

  // Accents don't matter when searching: "aviao" finds "Avião".
  const plain = (text) => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();

  const filter = () => {
    const wanted = plain(search.value);
    for (const kind of panel.querySelectorAll("[data-kind]")) {
      kind.hidden = Boolean(wanted) && !plain(kind.textContent).includes(wanted);
    }
    for (const group of panel.querySelectorAll("[data-group]")) {
      const any = group.querySelector("[data-kind]:not([hidden])") !== null;
      group.hidden = !any;
      if (wanted && any && !group.open) {
        group.open = true;
        group.dataset.searchOpened = ""; // closed again once the search is cleared, unless something in it was ticked
      } else if (!wanted && "searchOpened" in group.dataset) {
        group.open = group.querySelector("input:checked") !== null;
        delete group.dataset.searchOpened;
      }
    }
    panel.querySelector("[data-no-match]").hidden = !wanted || panel.querySelector("[data-kind]:not([hidden])") !== null;
  };

  if (panel) {
    search.hidden = false; // a search box only makes sense with this script
    search.addEventListener("input", filter);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault(); // Enter searches; it doesn't save the camera
    });
  }

  // When to record: what an interval adds up to in a day, for each person or kind of thing that stays
  const volume = form.querySelector("[data-volume]");

  const writeVolume = () => {
    const { seconds } = interval();
    volume.hidden = false;
    if (!(seconds >= MIN_EVERY_S && seconds <= MAX_EVERY_S)) {
      volume.classList.add("is-heavy");
      volume.textContent = volume.dataset.invalid;
      return;
    }
    const count = Math.floor(MAX_EVERY_S / seconds);
    const megabytes = count * PHOTO_MB;
    const size = megabytes >= 1000
      ? `${tenths.format(megabytes / 1000)} GB`
      : `${numbers.format(Math.max(1, Math.round(megabytes)))} MB`;
    volume.classList.toggle("is-heavy", megabytes >= 1000);
    volume.textContent = volume.dataset.template.replace("{count}", numbers.format(count)).replace("{size}", size);
  };

  const refresh = (event) => {
    if (event?.target === search) return;
    if (panel) {
      writeRule();
      countGroups();
    }
    if (volume) writeVolume();
  };
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  refresh();
})();

// Analysis region: drag a box on the live image to limit where the AI looks. The whole frame is still shown
// and recorded; only recognition is cropped to the box. Stored as "x,y,w,h" in fractions (camera.parse_region).
(() => {
  const input = document.querySelector("[data-region-input]");
  const overlay = document.querySelector("[data-region-overlay]");
  const rect = document.querySelector("[data-region-rect]");
  const tools = document.querySelector("[data-region-tools]");
  const clear = document.querySelector("[data-region-clear]");
  if (!input || !overlay || !rect) return;
  if (tools) tools.hidden = false; // the live image is on the page, so a box can be drawn
  const MIN = 0.02; // a box smaller than this, in either direction, means the whole picture
  const clamp = (n) => Math.min(Math.max(n, 0), 1);

  const parse = (text) => {
    const parts = (text || "").split(",").map(Number);
    if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n)) || parts[2] <= 0 || parts[3] <= 0) return null;
    return { x: parts[0], y: parts[1], w: parts[2], h: parts[3] };
  };

  const draw = (box) => {
    rect.hidden = !box;
    if (clear) clear.hidden = !box;
    if (!box) return;
    rect.style.left = `${box.x * 100}%`;
    rect.style.top = `${box.y * 100}%`;
    rect.style.width = `${box.w * 100}%`;
    rect.style.height = `${box.h * 100}%`;
  };

  const store = (box) => {
    input.value = box ? [box.x, box.y, box.w, box.h].map((n) => n.toFixed(4)).join(",") : "";
    draw(box);
  };

  const at = (event) => {
    const bounds = overlay.getBoundingClientRect();
    return { x: clamp((event.clientX - bounds.left) / bounds.width), y: clamp((event.clientY - bounds.top) / bounds.height) };
  };

  const boxFrom = (a, b) => ({ x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) });

  let start = null;
  store(parse(input.value));

  overlay.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    overlay.setPointerCapture(event.pointerId);
    start = at(event);
    draw(boxFrom(start, start));
  });
  overlay.addEventListener("pointermove", (event) => {
    if (start) draw(boxFrom(start, at(event)));
  });
  overlay.addEventListener("pointerup", (event) => {
    if (!start) return;
    const box = boxFrom(start, at(event));
    start = null;
    store(box.w < MIN || box.h < MIN ? null : box);
  });
  overlay.addEventListener("pointercancel", () => { start = null; store(parse(input.value)); });
  if (clear) clear.addEventListener("click", () => store(null));
})();
