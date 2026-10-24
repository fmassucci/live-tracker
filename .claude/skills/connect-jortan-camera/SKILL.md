---
name: connect-jortan-camera
description: Connect a Jortan Wi-Fi camera (Yoosee app, e.g. the dual-lens JT-8695QJ) to the Portaria app. Covers Wi-Fi setup, turning its video on, the video address, and checking that frames arrive. Use when adding or troubleshooting one of these cameras.
---

# Connect a Jortan (Yoosee) camera

Learned on a JT-8695QJ on 2026-10-24. These cameras are set up with the Yoosee phone app. Their video is
closed to other programs until the camera is on Wi-Fi and *NVR connection* is turned on.

Jortan sells models built on different platforms, so first check that this one is a Yoosee camera: its box
or manual names the Yoosee app, and before setup it shows a Wi-Fi network named `GW_AP_<number>`. A model
made for another app (iCSee, V380, CamHi...) is set up differently and has a different address; ask which
app its box names. Only the Portaria side (steps 4 and 5, and splitting a two-lens picture) is the same.

The person types the camera password into the app's camera form themselves. If they share it in chat, you may
write the address text for them; don't enter it anywhere or use it to connect.

## 1. Put it on the Wi-Fi (Yoosee app)

- A camera not set up yet shows up as an open Wi-Fi network named `GW_AP_<number>`.
- In Yoosee: **+**, then add the camera in **AP mode** (hotspot), and connect the phone to `GW_AP_<number>`.
- Give it a **2.4 GHz** network; the camera has no 5 GHz.
- Prefer the router's own network over an extender. Behind an extender the router can't reserve the camera's
  address, and the video loses more packets.
- It worked when `GW_AP_…` disappears and the app shows the camera online.

## 2. Turn its video on

In Yoosee: camera, settings (gear), **NVR connection**: turn it on and create a password. Before this the
camera refuses every video request.

## 3. The address

```
rtsp://admin:PASSWORD@CAMERA-IP:554/onvif1
```

- The user is always `admin`. PASSWORD is the NVR connection password.
- Write `#` in the password as `%23`. A plain `#` cuts the address short and nothing reaches the camera.
- If a password typed in chat ends with punctuation, ask whether it belongs to it. The first camera's
  password ended in a period.
- When giving an address to copy, put it alone in a code block, with no placeholder the person could paste as is.
- **Procurar câmeras nesta rede** may not list it: type the address. The camera's IP is in the router's device
  list or the Yoosee app.

## 4. Add it in the app

**Câmeras**, **Adicionar câmera**: name, **Endereço de vídeo**, **Modelo de IA** (**Reconhecimento facial**)
and **Dispositivo ligado**: **Porta** for the camera at the door, **Nenhum** otherwise. Every camera records;
only one with the door opens it and counts entries on Today. Click **Testar conexão**, then **Salvar câmera**.

## 5. Check it streams

- The recognition service log says `camera connected: rtsp://***@<ip>:554/onvif1 over UDP`. These cameras
  only stream over UDP; the app tries TCP first and switches on its own.
- `http://127.0.0.1:<preview port>/status.json` shows the camera `online`, its `fps` and `size`
  (`[preview] port` in the config, 8080 by default).
- Both lenses arrive in one picture, one above the other: 1920x2160, about 17 frames per second.

| Log | Meaning |
|---|---|
| `camera unavailable`, every 2 s, no other message | password rejected: check `%23` and trailing punctuation |
| `Nonmatching transport in server reply` | password accepted, TCP refused; the app retries over UDP |
| 5 s timeouts | wrong IP, or the camera is off or changed address |

## After it works

- To use each lens as a camera of its own, open the camera's **Editar** page and click **Separar em duas
  câmeras**. Both lenses record; the top one keeps the camera's device, the bottom one starts with
  **Nenhum**, so a person seen by both lenses is one entry. The camera is still read once. Each lens then
  has its own name, settings and live image. The button shows only for a
  picture taller than wide, i.e. lenses one above the other; side-by-side lenses can't be split yet.
- The picture is upside down when the camera stands on a table: it's made to hang. Mount it, or flip the
  image in the Yoosee app.
- Reserve its address in the router so it doesn't change.
- Keep it on the local network: don't forward router ports to it.
