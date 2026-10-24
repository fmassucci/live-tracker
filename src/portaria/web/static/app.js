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

  // Camera thumbnails: a fresh picture every few seconds.
  if (document.querySelector("img[data-refresh]")) {
    setInterval(() => {
      for (const img of document.querySelectorAll("img[data-refresh]")) {
        img.src = `${img.dataset.refresh}?t=${Date.now()}`;
      }
    }, 4000);
  }

  // Toasts, in the bottom-right corner. A result that went well goes away by itself; closing a
  // standing notice (test mode, default password) keeps it closed until the next sign-in.
  const toasts = document.getElementById("toasts");
  const dismiss = (toast) => {
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 200);
  };
  const autohide = (toast) => {
    if (!("autohide" in toast.dataset)) return;
    let timer = setTimeout(() => dismiss(toast), 6000);
    toast.addEventListener("mouseenter", () => clearTimeout(timer));
    toast.addEventListener("mouseleave", () => {
      timer = setTimeout(() => dismiss(toast), 3000);
    });
  };
  if (toasts) {
    toasts.querySelectorAll("[data-toast]").forEach(autohide);
    // toasts that come with a page fragment, such as a camera test
    new MutationObserver((changes) => {
      for (const change of changes) {
        for (const node of change.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && node.matches("[data-toast]")) autohide(node);
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
