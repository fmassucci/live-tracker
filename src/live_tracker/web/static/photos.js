// Photo picker: previews, drag and drop, and a check of each photo as soon as it is chosen,
// so a bad photo can be replaced before saving. The server checks every photo again on save.
(() => {
  for (const picker of document.querySelectorAll("[data-picker]")) {
    const input = picker.querySelector("[data-picker-input]");
    const tiles = picker.querySelector("[data-picker-tiles]");
    const text = picker.dataset;
    const form = picker.closest("form");
    let files = [];

    // The form sends whatever the file input holds, so keep it in step with the tiles.
    const sync = () => {
      const transfer = new DataTransfer();
      for (const file of files) transfer.items.add(file);
      input.files = transfer.files;
    };

    const check = async (file, tile, status) => {
      const body = new FormData();
      body.append("photo", file);
      if (text.personId) body.append("person_id", text.personId);
      try {
        const response = await fetch(text.checkUrl, { method: "POST", body });
        if (!response.ok) throw new Error(String(response.status));
        const result = await response.json();
        tile.classList.add(result.ok ? (result.warn ? "is-warn" : "is-ok") : "is-bad");
        status.textContent = result.message;
      } catch {
        status.textContent = text.failed;
      }
    };

    const addTile = (file) => {
      const tile = document.createElement("li");
      tile.className = "tile";
      const img = document.createElement("img");
      img.src = URL.createObjectURL(file);
      img.alt = file.name;
      const status = document.createElement("p");
      status.textContent = text.checking;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "tile-remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `${text.remove}: ${file.name}`);
      remove.addEventListener("click", () => {
        files = files.filter((f) => f !== file);
        URL.revokeObjectURL(img.src);
        tile.remove();
        sync();
      });
      tile.append(img, status, remove);
      tiles.append(tile);
      check(file, tile, status);
    };

    const add = (chosen) => {
      for (const file of chosen) {
        if (!file.type.startsWith("image/")) continue;
        files.push(file);
        addTile(file);
      }
      sync();
    };

    input.addEventListener("change", () => add([...input.files]));
    picker.addEventListener("dragover", (event) => {
      event.preventDefault();
      picker.classList.add("is-over");
    });
    picker.addEventListener("dragleave", () => picker.classList.remove("is-over"));
    picker.addEventListener("drop", (event) => {
      event.preventDefault();
      picker.classList.remove("is-over");
      add([...event.dataTransfer.files]);
    });

    // Photos need the LGPD confirmation; say so before sending rather than after.
    const informed = form.querySelector('input[name="informed"]');
    if (informed) {
      informed.addEventListener("change", () => informed.setCustomValidity(""));
      form.addEventListener("submit", (event) => {
        if (files.length && !informed.checked) {
          event.preventDefault();
          informed.setCustomValidity(text.informedRequired);
          informed.reportValidity();
        }
      });
    }
  }
})();
