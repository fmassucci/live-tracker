// Door screen: asks for the door's state a few times a second and shows it in large type.
(() => {
  const body = document.body;
  const title = document.getElementById("title");
  const detail = document.getElementById("detail");
  const clock = document.getElementById("clock");
  const video = document.querySelector(".kiosk-frame img");
  const lang = document.documentElement.lang;

  const show = (state) => {
    body.dataset.tone = state.tone;
    if (title.textContent !== state.title) title.textContent = state.title;
    if (detail.textContent !== state.detail) detail.textContent = state.detail;
  };

  const poll = async () => {
    try {
      const response = await fetch(body.dataset.stateUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      show(await response.json());
    } catch {
      show({ tone: "bad", title: body.dataset.offlineTitle, detail: body.dataset.offlineDetail });
    }
    setTimeout(poll, 400);
  };

  const tick = () => {
    clock.textContent = new Date().toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", hour12: false });
  };

  // The video stops if the connection drops; start it again.
  const base = video.src;
  video.addEventListener("error", () => {
    setTimeout(() => { video.src = `${base}&t=${Date.now()}`; }, 2000);
  });

  tick();
  setInterval(tick, 1000);
  poll();
})();
