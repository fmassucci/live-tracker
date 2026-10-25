# Roadmap

Where the app is going: several cameras, each running one AI model or none, driving ESP32 boards that open
doors, switch lights and sound alarms. Planned with the user on 2026-10-24; tick items off as they land.

## Decisions

- **Devices:** ESP32 boards with the user's own firmware, wired by the user, talking MQTT through Mosquitto on
  the server. The message contract and an example sketch are in `firmware/`.
- **Cameras:** each runs at most one AI model, or none. Doors, lights and sirens are attached through rules, so
  there are no fixed "door cameras".
- **Model order:** face recognition first, then person detection, then the others.
- **Door access:** set per person and per device output, in the person's settings.
- **Alert recipients:** set in each person's settings.
- **Alerts:** WhatsApp first and e-mail second, but WhatsApp is skipped for now.
- **Hardware:** a GPU on the bakery's server if needed.

## Phase 1: devices and rules, no new AI

- [x] MQTT message contract and example ESP32 sketch (`firmware/`)
- [ ] Install `paho-mqtt` in the project environment and Mosquitto on this PC (waiting for the user's OK)
- [ ] MQTT in the recognition service: board announcements, online/offline, relay states, button presses,
      commands with confirmations, and a log of every command
- [ ] Devices page: new boards appear by themselves to be named; status, IP, outputs, inputs, and a test
      button per output
- [ ] New data structure: each camera's AI model with its own settings; devices with their outputs and inputs;
      rules; records for any kind of event; the action log
- [ ] Camera settings in sections: Video, AI model, Actions, Recording
- [ ] Rules: "when this happens, do that on this output", each with a test mode, a cooldown and a schedule.
      Today's hard-wired door becomes the first rule, in test mode
- [ ] Door access per person and per output, in place of the single "door access" setting
- [ ] Face recognition settings per camera; thresholds and voting are still global in `config.toml`
- [ ] (user) Firmware: install PubSubClient, set the Wi-Fi, server IP and pins, flash, test with
      `mosquitto_pub`

## Before any rule drives a real lock

- [x] Liveness check (MiniFASNet, RGB), so a photo or a screen can't open a door: part of face recognition once
      downloaded; a spoof is recorded as one and never opens. Still to test with photos and phones at the door
- [ ] Manual override, and an alert when a device goes offline
- [ ] Wiring checked: relay off means door locked, and each output's off timer runs on the board

## Phase 2: GPU and person detection

- [x] Settings, AI models: every model with its state, download in the background with checksum check, removal
      when no camera uses it; each camera chooses among the downloaded models, or none
- [x] People and objects with YOLOX-S (Apache-2.0, CPU): each camera picks among COCO's 80 kinds, sensitivity,
      minimum time and analyses per second; boxes in the live view; one record with photo per stay
- [x] When to record, per camera and for any model: each time it appears, at an interval (again while it stays),
      or the first time of the day; counted per person or kind, remembered across restarts; unknown faces always
- [ ] Use the RTX 4070: ONNX Runtime with CUDA or DirectML
- [ ] Add RF-DETR (or D-FINE) as a second, more accurate option, exported from the official weights; compare it
      with YOLOX-S on the bakery's real footage
- [ ] Load each model once and share it across cameras; a scheduler sets each camera's frame rate
- [ ] Analyze the cameras' smaller second stream; keep the full one for photos
- [ ] Tracking across frames (count people one by one), zones drawn on the picture, cooldowns
- [ ] Zone rules, on top of tracking: a line crossed by people or vehicles, counted in and out; how many are inside
      an area; how long someone stays; how long an area sits empty; queue times. Anonymous: boxes, no faces. For
      the bakery: customers per hour at the shop door, three people waiting at the till for two minutes, the
      counter empty for two minutes while open, someone lingering at the back door at night
- [ ] Heatmaps: where people walk and stand, per camera, by day and by hour
- [ ] Train on the bakery's own images: people first, then hairnets and pests
- [ ] Rules for lights and alarms

## Phase 3: more models and alerts

- [ ] Phone use: a person and a phone together for some seconds; needs close cameras or high resolution
- [ ] Animals: cats, dogs and birds come with the same detector; rats and insects need a custom model
- [ ] Number plates: plate detection and reading (Mercosul), with a vehicle registry
- [x] Fire and smoke: a published YOLO26n (AGPL-3.0, fine in-house per the user), chosen over two others on
      D-Fire's test photos; only in addition to certified smoke detectors, never instead
- [ ] Fire and smoke tested against the ovens and steam; a custom model if it raises false alarms there
- [x] Falls: YOLOv8s-pose; lying when the torso leans 50 degrees or more, recorded after 10 s on the floor
- [ ] Custom models as needed, such as hairnets
- [ ] Alerts by e-mail, WhatsApp later, to the people chosen in their settings

## Phase 4: face search, door extras, sensors, then the dashboard

- [ ] Face search: keep each record's face embedding (computed today, then thrown away) and fill in older records
      from their saved face crops. Find a face in all records, see when an unknown face keeps coming back, and
      add it to People from the photos already taken
- [ ] Open the door from Today and Live, and a lockdown that keeps every door shut even for recognized faces;
      both go to the audit log
- [ ] Door access hours per person or category, and visitor passes that expire on a date
- [ ] Door held open or forced: an alert, from the board's `contact` input
- [ ] An alert when unknown or spoof faces keep trying a door
- [ ] Tailgating: more people pass a door than were recognized; needs the zone rules
- [ ] Intercom on the door tablet; browsers allow its microphone only over HTTPS
- [ ] Temperature probes on ESP32 boards in each freezer, cold room and proofer: an alert when one is out of
      range, and a log of the readings; needs a `temperature` input kind in the firmware contract
- [ ] A silent panic button under the counter (a `button` input): an alert to the owner and a picture from every
      camera at that moment
- [ ] Dashboard, once the zone rules and the items above are in: entries, customers per hour, queues, heatmaps,
      door events, alerts and temperatures, by day and by weekday

## Smaller items

- [ ] Camera form: separate user and password fields, so passwords with `#` work as typed and stay hidden
- [ ] Rotate a camera's picture by 180° (the Jortan films upside down when it stands on a table)
- [ ] Split two-lens pictures that sit side by side; only top and bottom work now
- [ ] Start the app with Windows

## Operations, privacy and security

- [ ] The bakery's server, with a GPU if the models need it
- [ ] Cameras and ESP32 boards on their own network, with fixed addresses
- [ ] The Jortan leaves telnet and FTP open: never forward router ports to it
- [ ] LGPD: a purpose, a notice and a retention period for each kind of event, especially phone use and plates
