// Small behaviors shared by the admin pages. Text shown here comes from the page, already translated.
(() => {
  const lang = document.documentElement.lang;

  // The clock on Today.
  const clock = document.getElementById("clock");
  if (clock) {
    const tick = () => {
      clock.textContent = new Date().toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", hour12: false });
    };
    tick();
    setInterval(tick, 1000);
  }

  // Live frames light up when the door decides, and the label says who or why.
  for (const frame of document.querySelectorAll("[data-state-url]")) {
    const label = frame.querySelector("[data-video-label]");
    const poll = async () => {
      try {
        const response = await fetch(frame.dataset.stateUrl, { cache: "no-store" });
        if (response.ok) {
          const state = await response.json();
          frame.dataset.tone = state.tone;
          if (label && state.label) label.textContent = state.label;
        }
      } catch {
        // the next poll tries again
      }
      setTimeout(poll, 700);
    };
    poll();
  }

  // Recording chips follow their camera while the page is open: a webcam takes a moment to start.
  // When a camera starts sending, its live video reconnects, or the page reloads to show it.
  const sending = (state) => ["recording", "live", "no-model"].includes(state);
  const reconnect = (img) => {
    img.src = `${img.src.split("?")[0]}?t=${Date.now()}`;
  };
  const known = {};
  for (const chip of document.querySelectorAll("[data-recording-for]")) {
    known[chip.dataset.recordingFor] = chip.dataset.state;
  }
  // every second while a camera that is on still has no image (it is starting), else every few
  const nextCheck = () => (Object.values(known).includes("no-image") ? 1000 : 3000);
  if (Object.keys(known).length) {
    const follow = async () => {
      try {
        const response = await fetch("/cameras-states", { cache: "no-store" });
        if (response.ok) {
          const states = await response.json();
          for (const [id, was] of Object.entries(known)) {
            const now = states[id];
            if (!now || now.state === was) continue;
            known[id] = now.state;
            for (const chip of document.querySelectorAll(`[data-recording-for="${id}"]`)) {
              chip.classList.replace(`camera-state-${chip.dataset.state}`, `camera-state-${now.state}`);
              chip.dataset.state = now.state;
              chip.lastElementChild.textContent = now.label;
            }
            if (sending(now.state) && !sending(was)) {
              if (document.querySelector(`[data-waiting-for="${id}"]`)) location.reload();
              document.querySelectorAll(`img[data-stream-for="${id}"]`).forEach(reconnect);
            }
          }
        }
      } catch {
        // the next round tries again
      }
      setTimeout(follow, nextCheck());
    };
    setTimeout(follow, nextCheck());
  }

  // A live video that couldn't load, because its camera was still starting, tries again.
  for (const img of document.querySelectorAll("img[data-stream-for]")) {
    img.addEventListener("error", () => setTimeout(() => reconnect(img), 3000));
  }

  // Today refreshes its panels every few seconds; their strips of faces keep their scroll position.
  const scrolled = {};
  document.addEventListener("htmx:oobBeforeSwap", () => {
    for (const row of document.querySelectorAll("[data-keep-scroll]")) scrolled[row.dataset.keepScroll] = row.scrollLeft;
  });
  document.addEventListener("htmx:oobAfterSwap", () => {
    for (const row of document.querySelectorAll("[data-keep-scroll]")) row.scrollLeft = scrolled[row.dataset.keepScroll] ?? 0;
  });

  // Today's list of cameras, beside the menu, follows the camera in view; a click jumps to one (a plain link,
  // smooth by the stylesheet). While the page glides past other cameras, the list keeps the chosen one.
  const index = document.querySelector("[data-camera-index]");
  if (index) {
    const links = [...index.querySelectorAll("a[data-for]")];
    const rows = links.map((link) => document.getElementById(link.dataset.for)).filter(Boolean);
    const inView = new Set();
    let quietUntil = 0;
    const mark = (id) => {
      for (const link of links) {
        if (link.dataset.for === id) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      }
    };
    const pick = () => {
      if (performance.now() < quietUntil) return;
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 24;
      const row = atBottom ? rows[rows.length - 1] : rows.find((r) => inView.has(r.id)); // the last can't climb higher
      if (row) mark(row.id);
    };
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) inView.add(entry.target.id);
        else inView.delete(entry.target.id);
      }
      pick();
    }, { rootMargin: "-80px 0px -45% 0px" }); // a camera counts once it reaches the upper half of the window
    rows.forEach((row) => observer.observe(row));
    window.addEventListener("scroll", pick, { passive: true });
    for (const link of links) {
      link.addEventListener("click", () => {
        mark(link.dataset.for);
        quietUntil = performance.now() + 900;
      });
    }
    mark(location.hash.slice(1) || rows[0]?.id);
  }

  // Camera pictures: a fresh one every few seconds, or as often as the picture asks (data-every, in ms).
  const refreshing = [...document.querySelectorAll("img[data-refresh]")];
  if (refreshing.length) {
    const every = (img) => Number(img.dataset.every || 4000);
    const due = new Map(refreshing.map((img) => [img, Date.now() + every(img)]));
    setInterval(() => {
      const now = Date.now();
      for (const img of refreshing) {
        if (now < due.get(img)) continue;
        due.set(img, now + every(img));
        img.src = `${img.dataset.refresh}?t=${now}`;
      }
    }, 250);
  }

  // Toasts, in the bottom-right corner. Every one goes away by itself, after the time its tone sets in the
  // stylesheet (--toast-time): good news soon, warnings and errors later. Pointing at a toast or moving into it
  // with the keyboard holds it, and so does the line along its bottom that shows the time left. Closing a
  // standing notice (test mode, default password) keeps it closed until the next sign-in.
  const toasts = document.getElementById("toasts");
  const dismiss = (toast) => {
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 200);
  };
  const time = (toast) => {
    let left = parseFloat(getComputedStyle(toast).getPropertyValue("--toast-time")) * 1000 || 6000;
    let since = performance.now();
    let timer = setTimeout(() => dismiss(toast), left);
    let holds = 0; // the pointer and the keyboard focus each hold it
    const hold = () => {
      if (holds++ > 0) return;
      clearTimeout(timer);
      left -= performance.now() - since;
    };
    const release = () => {
      if (holds === 0 || --holds > 0) return;
      since = performance.now();
      timer = setTimeout(() => dismiss(toast), left);
    };
    toast.addEventListener("mouseenter", hold);
    toast.addEventListener("mouseleave", release);
    toast.addEventListener("focusin", hold);
    toast.addEventListener("focusout", release);
  };
  if (toasts) {
    toasts.querySelectorAll("[data-toast]").forEach(time);
    // toasts that come with a page fragment, such as a camera test
    new MutationObserver((changes) => {
      for (const change of changes) {
        for (const node of change.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && node.matches("[data-toast]")) time(node);
        }
      }
    }).observe(toasts, { childList: true });
  }

  // A styled file button shows the chosen file's name next to it.
  document.addEventListener("change", (event) => {
    const input = event.target.closest("input[data-file-name]");
    if (input && input.files.length) {
      document.getElementById(input.dataset.fileName).textContent = [...input.files].map((f) => f.name).join(", ");
    }
  });

  // Slow forms (saving photos, taking photos): show that something is happening.
  document.addEventListener("submit", (event) => {
    const button = event.target.querySelector("button[data-busy-text]");
    if (button && !event.defaultPrevented) {
      button.setAttribute("aria-busy", "true");
      button.textContent = button.dataset.busyText;
    }
  });

  document.addEventListener("click", async (event) => {
    // Close a toast.
    const close = event.target.closest("[data-toast-close]");
    if (close) {
      const toast = close.closest("[data-toast]");
      if (toast.dataset.hideUrl) fetch(toast.dataset.hideUrl, { method: "POST" }).catch(() => {});
      dismiss(toast);
    }
    // Copy a field, such as the door screen address.
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const field = document.getElementById(copy.dataset.copy);
      field.select();
      try {
        await navigator.clipboard.writeText(field.value);
      } catch {
        document.execCommand("copy"); // the clipboard API needs HTTPS; plain HTTP on the network falls back here
      }
      copy.textContent = copy.dataset.copied;
    }
    // A video address found by the network search goes into the camera form.
    const use = event.target.closest("[data-use-url]");
    if (use) {
      const url = document.getElementById("url");
      url.value = use.dataset.useUrl;
      url.focus();
    }
  });
})();
