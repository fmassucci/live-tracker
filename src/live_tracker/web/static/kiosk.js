// Recognition screen: asks for the camera's state a few times a second and shows it in large type. On a camera a
// browser films for, it also shows this device's own camera and sends its pictures to the system.
// Tablets stay in use long after their browser stops updating: this has to run on Chrome 74 (Android 5.1), which
// doesn't know ?. or ??, and a browser that can't parse one line runs none of the file. tests/test_web.py checks.
(() => {
  const body = document.body;
  const title = document.getElementById("title");
  const detail = document.getElementById("detail");
  const clock = document.getElementById("clock");
  const stream = document.querySelector(".kiosk-frame img");
  const own = document.querySelector(".kiosk-frame video");
  const lang = document.documentElement.lang;
  const data = body.dataset;
  let note = null; // what the device's own camera has to say, in place of the state

  const show = (state) => {
    const shown = note || state;
    body.dataset.tone = shown.tone;
    if (title.textContent !== shown.title) title.textContent = shown.title;
    if (detail.textContent !== shown.detail) detail.textContent = shown.detail;
  };

  const say = (tone, heading, line = "") => {
    note = { tone, title: heading, detail: line };
    show(note);
  };

  const poll = async () => {
    try {
      const response = await fetch(data.stateUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      show(await response.json());
    } catch {
      show({ tone: "bad", title: data.offlineTitle, detail: data.offlineDetail });
    }
    setTimeout(poll, 400);
  };

  const tick = () => {
    clock.textContent = new Date().toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", hour12: false });
  };

  // The video stops if the connection drops; start it again.
  const watchStream = () => {
    const base = stream.src;
    stream.addEventListener("error", () => {
      setTimeout(() => { stream.src = `${base}&t=${Date.now()}`; }, 2000);
    });
  };

  const filmHere = () => {
    const PAUSE_MS = 120; // about 6 pictures a second: a decision takes 3 that agree within 1.5 s
    const RETRY_MS = 3000;
    const canvas = document.createElement("canvas");

    const start = async () => {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { // no camera on a plain http address
        say("bad", data.blockedTitle, data.blockedDetail);
        return;
      }
      try {
        // Plenty for a face at arm's length; on an old tablet, 720p leaves too few pictures a second for a decision.
        const video = { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } };
        const camera = await navigator.mediaDevices.getUserMedia({ video, audio: false });
        camera.getVideoTracks()[0].addEventListener("ended", start); // another app took it, say
        own.srcObject = camera;
        say("busy", data.starting);
      } catch (error) {
        if (error.name === "NotAllowedError") {
          say("bad", data.deniedTitle, data.deniedDetail);
        } else {
          say("bad", data.failedTitle, data.failedDetail);
          setTimeout(start, RETRY_MS);
        }
      }
    };

    const send = async () => {
      // A hidden page's picture stands still, and a camera that stopped shows a blank one.
      if (!document.hidden && own.srcObject && own.srcObject.active && own.videoWidth) {
        canvas.width = own.videoWidth;
        canvas.height = own.videoHeight;
        canvas.getContext("2d").drawImage(own, 0, 0);
        const jpeg = await new Promise((done) => { canvas.toBlob(done, "image/jpeg", 0.85); });
        try {
          await fetch(data.frameUrl, { method: "POST", body: jpeg });
        } catch {
          // No connection: the state says so, and the next picture tries again.
        }
        if (note && note.tone === "busy") note = null; // started: the system's state takes over
      }
      setTimeout(send, PAUSE_MS);
    };

    // A screen that sleeps stops filming, so it stays on, where the browser can: Chrome 84 on. It lets go of this
    // whenever the page is hidden.
    const keepAwake = () => {
      if (navigator.wakeLock) navigator.wakeLock.request("screen").catch(() => {});
    };
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      keepAwake();
      own.play().catch(() => {});
    });

    say("busy", data.starting);
    keepAwake();
    start();
    send();
  };

  // Browsers only go full screen after a tap, so the first one on the screen does it. Leaving, with the device's
  // back button or gesture, is up to the browser, and the next tap comes back.
  document.addEventListener("click", () => {
    const page = document.documentElement;
    if (!document.fullscreenElement && page.requestFullscreen) page.requestFullscreen().catch(() => {});
  });

  tick();
  setInterval(tick, 1000);
  if (own) filmHere();
  else watchStream();
  poll();
})();
