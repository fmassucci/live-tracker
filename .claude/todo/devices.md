# Devices: register them, then link them to cameras and to each other

Proposed on 2026-10-27. Steps 1 to 4 were built the same day (see Steps); rules between devices are next.
The roadmap's Phase 1 (`TODO.md`) and
the rules plan (`rules.md`) assumed only ESP32 boards running our firmware over MQTT. On 2026-10-27 the user paired a
ready-made module instead: a NovaDigital "Módulo Mini Inteligente Wi-Fi", made by Tuya, on the JCCP network with
the Smart Life app, to open a door after face recognition. This plan adds such modules beside the boards.

## What was planned already

- **Phase 1:** a Devices page where ESP32 boards appear by themselves, since they announce themselves over MQTT, to
  be named; status, IP, outputs, inputs and a test button per output; tables for devices, outputs and inputs; door
  access per person and per output.
- **rules.md:** cameras and devices are linked through rules ("when a face is recognized on camera 3, pulse
  esp32-a1b2c3/relay1"), and a board's button is an event like a face. Today's camera setting `device = "door"`
  becomes the first rule.

Missing: devices that don't speak our MQTT contract and so can't announce themselves, registering them by hand, and
how the app talks to each kind.

## The idea

- **Devices** gets its own place in the menu. Every device has outputs (kinds `lock`, `light`, `siren`, `relay`)
  and inputs (`button`, `contact`), whatever its brand, as in `firmware/README.md`.
- **One driver per kind of device**, behind one interface: switch an output (pulse, on, off), report its state,
  report input changes, online or offline. Rules, the Devices page and the audit never know the brand.
  - `esp32-mqtt`: our firmware, as in `firmware/README.md`.
  - `tuya-local`: Tuya-made Wi-Fi modules (NovaDigital and many other brands sold in Brazil), controlled directly
    over the local network with tinytuya (MIT), without Tuya's cloud.
  - Later, only if needed: Shelly, Sonoff, ESPHome, Zigbee through a gateway.
- **Linking goes through rules,** as rules.md plans, with shortcuts that write the rule for you:
  - a camera's Actions section: "when someone with access is recognized here, open this output";
  - a device's page: "when this button is pressed, pulse that output". Linking devices to each other is a rule
    whose *when* is a device event.

## Tuya modules

- **How they talk:** each module keeps a connection to Tuya's cloud for the phone app, and also answers on the local
  network (TCP port 6668) to anyone who has its *local key*. Live Tracker keeps one local connection per module:
  commands take a fraction of a second and work without the internet.
- **The local key:** Tuya gives it out only through its developer site (Tuya IoT Platform, a free account), once
  the Smart Life account is linked to it by QR code. That's why the module is paired with Smart Life and not the
  NovaDigital app, whose accounts can't be linked. Tuya's free cloud plan has to be renewed now and then, which only
  matters when importing keys. The key changes every time a module is paired again.
- **Registering,** with "Add Tuya devices" on the Devices page:
  1. Once: the developer project's Access ID, Secret and data center, kept like other secrets.
  2. The app lists the account's devices with their names and keys; you pick the ones to add and each output's kind.
  3. Without the cloud: a form for the ID, key and IP, as printed by `tinytuya wizard`.
  4. A module paired again shows "key out of date", and importing again fixes it.
- **Finding them:** modules broadcast their ID and IP every few seconds (UDP 6666 and 6667), so the app finds the
  address by itself and follows it when it changes. That works on the server's own network only; a module behind
  another router needs a fixed IP typed in.
- **Data points:** a module reports numbered values: a switch per channel, its countdown, how it starts after a
  power cut. The driver maps them from Tuya's standard switch set, and a device's page lets an administrator fix the
  mapping for unusual models.
- **The wall switch** ("paralelo físico", on S1 and S2) flips the relay itself. The module reports the new state, which
  the app shows and logs as an event; it can't be a separate button, since the relay has already moved.

## The first module, tested on 2026-10-27

- NovaDigital MS-107, 2 channels, Tuya category `tdq`, at 192.168.100.102 on JCCP, local protocol 3.4. Its key came
  from the developer project "Live Tracker" (data center Western America, tinytuya region `us`).
- Data points: `1` and `2` the relays; `9` and `10` their countdowns in seconds; `38` the power-on state (already
  `off`); `44` inching (base64); `47` the wall switch's type (`flip`).
- From this PC (behind the Mercusys): the module confirmed "on" in 0.19 s over an open connection, and its own
  countdown switched it off 2.2 s later, with nothing sent from here.
- **The module clears a channel's countdown whenever its switch changes, even when both come in one command**
  (`set_multiple_values`): the relay then stayed on, with its countdown at 0, until it was switched off by hand. So
  a pulse is two commands, the switch then the countdown, each confirmed (`devices/tuya.py`).
- Through the app, with the fix: Test answered in 0.3 to 0.5 s, and the module switched itself off a second later.
- Still to check: what Smart Life does while Live Tracker holds the connection; and the inching data point (`44`,
  `AAACAgAC` now, its format not yet known), which would end every pulse on the module even if the countdown command
  is lost.

## Safety: the firmware's rules, on Tuya modules

The five rules in `firmware/README.md` hold for every device. On a Tuya module:

1. **It times every output itself:** a pulse switches the output on, then sets the module's own countdown, so it
   switches off even if Live Tracker or the network goes down. Each command must be confirmed: a countdown that
   isn't is followed by "off" at once, and a watchdog in the service switches off a pulse still on 2 s past its
   time. An output without a countdown is never switched on.
2. **Everything starts off:** the import warns when a module's relays switch on after a power cut, to be set to off
   in Smart Life. Setting it from the app is still to do.
3. **Off is the rest state:** depends on the wiring and the lock (below).
4. **No late commands:** a command not picked up within 5 seconds (`LATE_S`) is dropped and logged, never sent late.
5. **Debounce:** not needed; the module reports states, not raw signals.

Before a module drives a real lock:
- **Its terminals:** `L N L1 S1 S2` switch mains (127/220 V) on their output, so they can't drive a 12 V lock
  directly; `COM NO NC` (NovaDigital MS-111) is a dry contact, the usual way to trigger a lock.
- **The lock:** an electric strike opens when it gets power (on the NO contact); a magnetic lock opens when its power
  is cut (on NC). Either way, relay off means locked.
- Mains wiring by an electrician.

## Data

Built (migration 0008):
- `devices`: name, kind (`esp32` or `tuya`), its ID on that kind (`uid`), a fixed address, the Tuya local key, model
  or firmware, and `new` for a board not named yet. Online, IP and state live in the service, not the database.
- `device_outputs` (key, name, kind, pulse time, longest time, Tuya countdown data point) and `device_inputs`.
- `device_commands`: the web UI's commands, which the service carries out, with their results; the action log
  rules.md plans can take it over.
- The Tuya developer project's Access ID, Secret and data center, in `settings`. Keys never appear in logs or pages.

## Where it is in the code

- `devices/`: `__init__.py` (shared types and error codes), `tuya.py`, `esp32.py`, and `manager.py`, which keeps
  their connections in the recognition service, beside the cameras, so a device doesn't depend on the web UI.
- `web/devices.py` and its templates. The page is htmx like the others, since the React move hasn't started; it
  moves with the rest.
- `switching.py`: a camera pulses its output (`Camera.output_id`) through the manager (`pulse_output`), for the
  output's own pulse time, when it recognizes someone with access (`Person.access`), with `[devices] mode` as the
  master switch: "shadow" only logs, "live" switches. The pulse runs on the camera's thread before the record, whose
  `Event.action` says "switched", "failed" or "denied". When rules land, `Camera.output_id` becomes the camera's
  first rule. Since migration 0010 nothing is a "door": no camera role, column, config section or route.

## Screens

- **Devices** (built): each device with online or offline, IP and protocol; its outputs with their state and a Test
  button (a 1-second pulse, for administrators, in the audit log); its inputs with their last event. New ESP32 boards
  wait at the top to be named, and "Add Tuya devices" opens the import. The states refresh every 3 seconds.
- **A device's page** (built: name, fixed address, outputs' names, kinds and pulse times, inputs' names, removal).
  Still to add: what links it to cameras and other devices, and its recent commands.
- **A camera's Attached device** (built): the output it switches, listed by device with its pulse time; the camera
  list shows it. One output per camera; more, and other kinds of actions, come with rules.

## Steps

- [x] 1. **Prove it by hand:** pulsed the NovaDigital module from this PC with tinytuya. Smart Life open at the same
  time is still to check.
- [x] 2. **Tuya in the app:** tables, the drivers and the manager in the recognition service, and the Devices page
  with the import and Test buttons. Tried end to end on the MS-107 from this PC.
- [x] 3. **A camera switches an output:** the camera's *Attached device* picks it; logged only until
  `[devices] mode` is "live". A record says "failed" when the output didn't switch.
- [x] 4. **ESP32 boards** on the same interface: MQTT with paho-mqtt, announcements, states, inputs, commands that
  wait for the board's `ack`. Tested with a stand-in broker only: no board or Mosquitto yet.
- [ ] 5. **Rules between devices,** once rules.md lands.

## Decisions to make

1. **Tuya keys:** imported from the developer account inside the app. Built that way; there is no form to type a key.
2. **Linking before rules exist:** decided on 2026-10-27: a column on the camera (`output_id`), turned into its first
   rule when the rules editor lands.
3. **Which network devices live on:** the Live Tracker server's own (JCCP now; recommended, so they're found by
   themselves), or a separate one for devices and cameras, as the roadmap suggests, with fixed addresses.

## Risks

- Tuya modules accept one local connection at a time (tinytuya's README); while Live Tracker holds it, Smart Life
  goes through the cloud. To confirm on the MS-107.
- A pulse is two commands: if the connection drops between them, the relay is on without its countdown until the
  watchdog reaches the module again. The inching data point would close that gap.
- A firmware update from Tuya can change the local protocol (versions 3.4 and 3.5 came that way); tinytuya has kept
  up so far. Updating a door module's firmware should be a choice, not automatic.
- Resetting or pairing a module again loses its key: it shows offline until the keys are imported again.
- Wi-Fi: a door module needs a strong signal where it's installed; NovaDigital's manual asks for a good network too.
