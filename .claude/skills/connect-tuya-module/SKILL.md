---
name: connect-tuya-module
description: Connect a Tuya-made Wi-Fi relay module (NovaDigital "Módulo Mini Inteligente Wi-Fi", such as the MS-107, and many other brands sold in Brazil) to Live Tracker's Devices page. Covers pairing in Smart Life, the Tuya developer project that gives the module's local key, importing and testing it, and what to check before it drives a door lock. Use when adding or troubleshooting one of these modules.
---

# Connect a Tuya Wi-Fi module (NovaDigital and others)

Learned on a NovaDigital MS-107 (2 channels) on 2026-10-27. NovaDigital sells Tuya hardware: its manuals name
"HangZhou Tuya" as the maker. Live Tracker talks to the module on the local network with tinytuya, using the
module's local key, which only Tuya's developer site hands out. The design and the module's details are in
`.claude/todo/devices.md`; the code is in `src/live_tracker/devices/`.

The person types the Access Secret into the app's form themselves; never ask for it in chat. To run tinytuya by
hand, write a `tinytuya.json` in the scratchpad with a placeholder, open it in Notepad for them to paste the Secret,
and never print it or the local key.

## 1. Pair it in Smart Life

- Use **Smart Life - Smart Living** (developer Volcano Technology Limited); Tuya Smart works too. Not the NovaDigital
  app: its accounts can't be linked to Tuya's developer site, so there would be no local key.
- **2.4 GHz only.** Use the same network as the computer that runs Live Tracker, not a guest network: guest networks
  keep devices apart, and the module's broadcasts don't cross routers.
- On the module, hold the button on top for about 5 seconds, until the light blinks fast.
- On the phone, turn on Bluetooth and location. In Smart Life: **+**, **Adicionar dispositivo**. The module shows up
  over Bluetooth within seconds: **Adicionar**, then the Wi-Fi network and its password.
- If it doesn't show up: **Adicionar manualmente**, **Eletricista**, **Interruptor (Wi-Fi)**. If that fails, hold the
  button again until the light blinks slowly and choose **Modo AP** at the top right; the phone joins a Wi-Fi
  named `SmartLife-XXXX`.
- A router with one name on 2.4 and 5 GHz is fine: the module joins the 2.4 GHz side by itself.
- Unplugging keeps the pairing. Holding the button 5 seconds resets it, and pairing it again changes its local key.

## 2. The Tuya developer project (once per Smart Life account)

- https://iot.tuya.com: sign up; at "Account Type", **Skip this step**.
- **Cloud**, **Create Cloud Project**. Data center: **Western America** for accounts made in Brazil. If the project's
  Overview shows "Data Center:" empty afterwards, click **Edit** there and tick it.
- **Authorize API Services**: keep the ones offered; **IoT Core** and **Authorization Token Management** are the two
  that matter. Turn off popup blockers first: a blocked subscription fails without a message.
- **Devices**, **Link App Account**, **Add App Account**: a QR code. In Smart Life, **Eu** (Me), the scan icon at the
  top. Choose **Automatic** and **Read Only Status** if asked. The module is then listed, online.
- **Overview**: **Access ID/Client ID** and **Access Secret/Client Secret** (the eye icon shows it).
- The free IoT Core plan lasts a month; extend it from the project. It's needed only to import keys, not to switch
  the relays.

## 3. Import it in Live Tracker

- **Dispositivos**, **Adicionar dispositivos Tuya** (administrators): Access ID, Access Secret, **Data center**, then
  **Procurar dispositivos**. Tick the module and **Adicionar os dispositivos escolhidos**. Each channel becomes an
  output (**Canal 1**, **Canal 2**) with the module's own countdown.
- **Editar**: name the device and its outputs, choose what each switches and for how long. Type a **Endereço fixo**
  only when the module is on another network than the server; otherwise the server finds it by its broadcasts.
- The first time, Windows Firewall asks whether Python may use the network: allow it, or broadcasts (UDP 6666 and
  6667) never arrive.
- After the module is paired again: **Adicionar dispositivos Tuya** again; the module keeps its names and gets the
  new key.

## 4. Test it

- **Testar** on an output: a 1-second pulse. The toast says "ligou por um segundo e o próprio dispositivo desligou".
- `http://127.0.0.1:<preview port>/status.json` (8080 by default), under `devices`: `online`, `ip`, `protocol`, and
  each output's state.
- The recognition service log: `<name>: connected at <ip>, protocol 3.4`, then `<name>: 1 on for 1 s`.

| On the Devices page | Meaning |
|---|---|
| Não encontrado na rede | its broadcasts don't reach the server: another network or router; type its fixed address |
| Recusa a chave | it was paired again: import the Tuya devices again |
| Não responde | no power or no Wi-Fi, or a wrong fixed address |
| Conectando, for long | the recognition service is still trying each protocol version |

- Smart Life can't reach the module on the local network while the service holds its one connection; it goes
  through Tuya's cloud instead. Likewise, tinytuya by hand fails while the service runs: stop it first.
- To try a module from a computer behind another router: run `live-tracker run` and `web` with a scratch config
  (other ports, a scratch `data_dir`), and give the module its fixed address.

## 5. Link it to a camera

- **Câmeras**, the camera, **Dispositivo ligado**, **Saída**: the channel it switches when it recognizes someone with
  access (only with face recognition). It stays on for that output's time on the device's **Editar** page; the
  camera list shows "Aciona <device>, <channel>".
- Nothing switches until `[devices] mode = "live"` in `config.toml` (then Stop the system and start it again);
  "shadow" only logs "would switch output". In live mode the log says `<device> switched for <person>`, and the
  record says "dispositivo acionado", or "dispositivo não acionou" when the module didn't confirm.
- Only people with access switch it (**Tem acesso**, in People); anyone else's record says "sem acesso, nada
  acionado".

## The MS-107's quirk: don't merge its two commands

The module clears a channel's countdown whenever that channel's switch changes, even when both come in one command:
sent together, the relay stays on with its countdown at 0. So a pulse is "on", then the countdown, each confirmed,
with "off" at once if the countdown isn't, and a watchdog for pulses that outlast their time (`TuyaModule.pulse`).
Keep it that way. Its data points: `1` and `2` the switches, `9` and `10` their countdowns in seconds, `38` the
state after a power cut (`off`), `44` inching (base64, format not known yet), `47` the wall switch's type.

## Before it drives a door lock

- Read the module's label. Terminals `L`, `N` and `L1`/`L2` (with `S1`/`S2` for wall switches) put mains voltage
  (127/220 V) on the outputs: they can't go straight to a 12 V lock. A dry contact (`COM`, `NO`, `NC`), such as
  NovaDigital's MS-111, is the usual way to trigger a lock.
- An electric strike opens when it gets power: wire it on `NO`. A magnetic lock opens when its power is cut: `NC`.
  Either way, relay off must mean locked.
- If the import warns that the relays switch on after a power cut, set them to stay off in Smart Life.
- Mains wiring is an electrician's job.
