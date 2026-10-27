# Live Tracker

Face recognition at the staff entrance, with a web UI in Portuguese and English.

**MVP stage:** the door is not controlled yet. Decisions are only recorded ("shadow mode"): devices (ESP32 boards and
Tuya Wi-Fi modules) can be added and tested on the Devices page, but no camera drives them yet. Download the liveness check (Settings, AI models): without it, a photo of an employee held up to the
camera would be accepted.

## How it fits together

```
IP camera ──RTSP──► recognition service ──── local port (video, status) ────► web UI ◄── browsers
                    live-tracker run                                          live-tracker web
                         │                                                          │
                         └────────────── data/live-tracker.db (SQLite) ─────────────┘
                                         data/photos, data/snapshots
```

- **Recognition service** (`run`): reads every enabled camera from the database. Door cameras recognize people,
  record each decision with a snapshot and, later, open the door. It also keeps the connections to the devices
  and carries out their commands. It keeps working when the web UI is stopped.
- **Web UI** (`web`): Today (who came in), People, Records, Cameras, Devices, Live view, Settings, plus the
  full-screen door screen for a tablet at the entrance. Changes to people, cameras or devices reach the service
  within a second.
- **People** is the base registry every module builds on: the door today, others later.

## Install

On Windows:

1. Install Python 3.13, 64-bit: `winget install Python.Python.3.13`, or the Python install manager from
   https://www.python.org/downloads/ and then `py install 3.13`. Python 3.11 to 3.14 work too: those are the
   versions onnxruntime is built for.
2. Put this folder where it will stay, outside OneDrive, such as `C:\live-tracker`: `data\` will hold face photos,
   which must not be copied to the cloud. With git:
   `git clone https://github.com/fmassucci/live-tracker.git C:\live-tracker`.
3. Double-click `install.bat`. It creates the Python environment in `.venv` with Live Tracker and its libraries,
   and `config.toml` from `config.example.toml`. It downloads the face models and puts **Live Tracker** on the
   desktop (a shortcut, with the app's icon, that runs `start.bat` from this folder). It needs the internet and
   takes a few minutes the first time. Run it again after an update, with Live Tracker closed, or after moving the
   folder: it keeps `config.toml`, the models and `data\`.

By hand, or on Linux, with the test tools:

```
py -3.13 -m venv .venv --prompt live-tracker
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -e ".[dev]"
live-tracker download-models
copy config.example.toml config.toml
```

| Model | Role | License |
|---|---|---|
| YuNet 2023mar | face detection and 5 landmarks | MIT |
| AuraFace v1 | recognition, ResNet-100 (default) | Apache-2.0 |
| SFace 2021dec | recognition, lightweight (for comparison) | Apache-2.0 |

Only AuraFace's recognition file is downloaded. The other files in its repository are byte-for-byte InsightFace
models, which are licensed for non-commercial research only.

The other models are downloaded in Settings, AI models, where each card says whether it's downloaded. Each camera
then chooses one in its settings; liveness has no camera of its own, since face recognition uses it by itself.

| Model | Role | License |
|---|---|---|
| MiniFASNetV2 + V1SE (Silent-Face) | liveness: tells a real face from a photo or a screen | Apache-2.0 |
| YOLOX-S | people and objects, COCO's 80 kinds | Apache-2.0 |
| D-FINE-M | people and objects, more accurate and slower (chosen in each camera's settings) | Apache-2.0 |
| YOLO26n, trained on fire and smoke | fire and smoke | AGPL-3.0 (Ultralytics) |
| YOLOv8s-pose | falls: someone who stays lying on the floor | AGPL-3.0 (Ultralytics) |

The Ultralytics models are AGPL-3.0, which is fine for this in-house system; selling or hosting it for others
would need Ultralytics' enterprise license. Fire and smoke only adds to certified smoke detectors, never replaces
them: ovens, steam and flour dust can look like fire or smoke, so try it with the ovens on. On 606 of D-Fire's
test photos, at *Balanced* it found 79% of the fires and 76% of the smoke, and at *Finds more* 86% and 83%; both
raised a false alarm on 2% of the photos without either.
Falls counts a person as lying when the torso leans 50° or more from upright; on 189 labelled photos it found
82% of the falls and took nobody upright for one. `[liveness] threshold` in `config.toml` sets how sure the
liveness check must be (real faces at the Jortan score about 0.98).

People and objects runs on YOLOX-S or D-FINE-M, chosen under *Model* in each camera's settings. On 250 COCO
photos, at *Balanced* and with as many false boxes as YOLOX-S, D-FINE-M found 68% of the people (YOLOX-S: 58%)
and 57% of all the things (41%), at about 0.3 s per analysis on the processor instead of 0.12, so 2 analyses per
second suit it. Its sensitivity levels have their own thresholds (`dfine.py`), since its scores run higher.

## Run

Double-click **Live Tracker** on the desktop, or `start.bat` in this folder (`live-tracker start`). A window
shows the recognition service and the web UI starting, both in the background without windows of their own.
Once they answer, the default browser opens the system and the window closes by itself. If one of them doesn't
start, the window stays open with its last messages. Whatever already runs is left as it is, so double-clicking
again only opens the browser. Their messages go to `data\logs\run.log` and `data\logs\web.log`; the previous
start's are kept beside them as `run.previous.log` and `web.previous.log`.

To stop both, use **Stop the system** in Settings (System). Only administrators see it, and only in a browser
on the computer that runs the system, since that's the only place to start it again.

By hand, in two terminals:

```
live-tracker run
live-tracker web
```

Sign in at http://127.0.0.1:8000 with user `admin`, password `admin`. That account is created on the
first start; every page reminds you to change its password (Settings, Accounts) until you do. More accounts
can be added in Settings, or with `live-tracker create-user <name>`. Only administrators add accounts,
edit the others (user name, administrator or not, a new password) or remove them, and see Settings, Database.
`admin` is one, and so is every account made before administrators existed. At least one administrator always
remains. A user name can't be one another account has, even in other upper and lower case; a renamed person
signs in again with the new name.

Phones, tablets and other computers on the same network open it at the address shown in Settings (System)
and printed by `web` when it starts, such as http://192.168.0.10:8000. The first time, Windows Firewall asks
whether Python may use the network: allow it on private or public networks, whichever Windows has set for the
Wi-Fi. Anyone on the network can reach the sign-in page, so change the `admin` password first. To allow only
this computer, set `[web] host = "127.0.0.1"`. The door screen's address (with its secret key) is there too.

### First steps in the UI

1. **Cameras:** add each camera with its video address. Every camera recognizes people and records; set
   *Attached device* to *Door* on the camera at the door, which opens it and counts the entries on Today.
   *Test connection* checks the address before saving, and
   *Find cameras on this network* lists ONVIF cameras and their addresses. Jortan cameras use the Yoosee
   app: turn on *NVR connection* in the camera's settings there and create a password. The address is then
   `rtsp://admin:PASSWORD@IP:554/onvif1` (write a `#` in the password as `%23`), and the network search may
   not list it. A dual-lens Jortan sends both lenses in one picture, one above the other: *Split into two
   cameras*, on the camera's edit page, makes one camera for each lens.
2. **People:** add each person with 3 to 5 face photos. Each photo is checked as soon as it is chosen. Photos
   taken by the door camera itself (*Take photos with a camera*) match best. Photos require confirming that the
   person was told how their face is used (LGPD).
3. **Today:** entries appear on the time card as the camera recognizes people.
4. **Devices** (administrators): the boards and modules that open doors and switch lights and sirens. *Test* on an
   output switches it on for a second; the device switches it off by itself, and every test goes to the audit log.
   - **Tuya Wi-Fi modules** (NovaDigital and other brands): pair the module in the Smart Life app, then on
     https://iot.tuya.com create a cloud project (data center *Western America* for accounts made in Brazil), and
     under its *Devices*, *Link App Account*, scan the QR code with Smart Life. *Add Tuya devices* takes the
     project's Access ID and Secret and imports the modules with their local keys, one output per channel. The
     system then talks to each module on the local network (tinytuya), without the internet. It finds a module by
     the module's broadcasts: the first time, Windows Firewall may ask whether Python may use the network; allow
     it. A module on another network than this computer's needs its address in its settings. Importing again after a module is paired
     again gives it its new key. A module accepts one local connection, so while the system runs, Smart Life
     reaches it through Tuya's cloud.
   - **ESP32 boards** with the Live Tracker firmware connect through an MQTT broker (`firmware/README.md`):
     set `[mqtt]` in `config.toml`, and boards appear on the page by themselves, to be named.
5. **Settings:** name and logo shown in the app and accounts. *System* has the addresses for this computer,
   phones and tablets, and the door screen's address. *Database*, for administrators, has where the images
   are stored, and downloads or deletes records with their pictures.

## Command line

`live-tracker <command>` in the activated environment, or `.venv\Scripts\python.exe -m live_tracker <command>`.

| Command | Does |
|---|---|
| `run [--source URL]` | recognition service; `--source` uses only that camera, for tests; stops if one already runs |
| `web [--open]` | web UI; `--open` opens it in the browser once it answers, or only opens it if it already runs |
| `start` | what start.bat runs: both of the above in the background, without windows, then opens the browser |
| `create-user NAME [--admin]` | adds a web UI account; `--admin` makes it an administrator |
| `discover --user admin` | finds ONVIF cameras and lists their RTSP addresses |
| `probe --source URL` | opens a stream, prints resolution, codec and frame rate, saves one frame |
| `import` | one-time import of the first setup's files: `data/enroll/<name>/` photos, events, `[camera] source` |
| `evaluate` | compares the models on photo folders in `data/enroll/` and suggests `accept_threshold` |
| `download-models` | downloads the model files, verifying their checksums |

## Languages

English is the source language: the English text in templates and code is the key, and
`src/live_tracker/i18n/translation_cache.json` maps it to Portuguese (`{"pt": {"Today": "Hoje"}}`). A text without
a translation falls back to English. Data (names, numbers, file names) is never translated.

`tests/test_i18n.py` fails when any text in the templates or code has no Portuguese translation, so new
screens can't ship half translated. To add a text, write it in English with `t("...")` and add the Portuguese
to the catalog.

## Data and privacy

```
data/live-tracker.db     people, photos, face data, cameras, devices (with Tuya keys), records, accounts, audit log
data/photos/<id>/        face photos
data/snapshots/<day>/    one picture per decision (and a face crop), deleted after [storage] retention_days
data/branding/logo.png   the logo set in Settings
data/logs/               the messages of the recognition service and the web UI started by start.bat
```

Settings, Database (administrators only):

- *Open folder* opens this folder in the file manager. It only shows on the computer that runs the system,
  since the folder opens on that computer's screen.
- *Download .zip* sends the records of the days chosen (or all of them) as a spreadsheet, `Registros_<first
  day>_<last day>.csv` (semicolons in Portuguese, for Excel), with their pictures in `snapshots/<day>/`.
- *Delete records and pictures* deletes the same records, after confirming. Both go to the audit log.

`data/` holds biometric data and `config.toml` may hold camera passwords; both are git-ignored. Never commit them
or copy them to shared drives, and keep the server's disk encrypted (BitLocker on Windows, LUKS on Linux).
Deleting a person deletes their photos and face data; past records keep their name until they expire.

## Tests

```
python -m pytest
```

## Not built yet

- A camera opening a door through a device output (the Devices page and the outputs' tests work)
- Alerts by e-mail or WhatsApp, for fire, smoke and falls among others
- Liveness with depth or infrared, when a camera has it (the current check uses the regular picture)
