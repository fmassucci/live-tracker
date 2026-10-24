# padaria-cameras

Face recognition at the staff entrance, with a web UI in Portuguese and English.

**MVP stage:** the door is not controlled yet. Decisions are only recorded ("shadow mode") until a liveness check
and the ESP32 relay exist. Without liveness, a photo of an employee held up to the camera would be accepted.

## How it fits together

```
IP camera ──RTSP──► recognition service ──── local port (video, status) ────► web UI ◄── browsers
                    python -m portaria run                                     python -m portaria web
                         │                                                          │
                         └──────────────── data/portaria.db (SQLite) ───────────────┘
                                           data/photos, data/snapshots
```

- **Recognition service** (`run`): reads every enabled camera from the database. Door cameras recognize people,
  record each decision with a snapshot and, later, open the door. It keeps working when the web UI is stopped.
- **Web UI** (`web`): Today (who came in), People, Records, Cameras, Live view, Settings, plus the full-screen
  door screen for a tablet at the entrance. Changes to people or cameras reach the service within a second.
- **People** is the base registry every module builds on: the door today, others later.

## Setup

Python 3.11 or newer.

```
py -3.13 -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m portaria download-models
copy config.example.toml config.toml
```

| Model | Role | License |
|---|---|---|
| YuNet 2023mar | face detection and 5 landmarks | MIT |
| AuraFace v1 | recognition, ResNet-100 (default) | Apache-2.0 |
| SFace 2021dec | recognition, lightweight (for comparison) | Apache-2.0 |

Only AuraFace's recognition file is downloaded. The other files in its repository are byte-for-byte InsightFace
models, which are licensed for non-commercial research only.

## Run

In two terminals:

```
python -m portaria run
python -m portaria web
```

Open http://127.0.0.1:8000 and sign in with user `admin`, password `admin`. That account is created on the
first start; every page reminds you to change its password (Settings, Accounts) until you do. More accounts
can be added in Settings, or with `python -m portaria create-user <name>`.

To use it from other computers and the door tablet, set `[web] host = "0.0.0.0"` and open the server's
address. The door screen's address (with its secret key) is in Settings.

### First steps in the UI

1. **Cameras:** add the door camera with its video address. *Test connection* checks it before saving, and
   *Find cameras on this network* lists ONVIF cameras and their addresses. On Jortan cameras the address is
   often `rtsp://user:pass@IP:554/onvif1`, and `onvif2` for the second lens.
2. **People:** add each person with 3 to 5 face photos. Each photo is checked as soon as it is chosen. Photos
   taken by the door camera itself (*Take photos with a camera*) match best. Photos require confirming that the
   person was told how their face is used (LGPD).
3. **Today:** entries appear on the time card as the camera recognizes people.
4. **Settings:** name and logo shown in the app, door screen address, accounts.

## Command line

| Command | Does |
|---|---|
| `run [--source URL]` | recognition service; `--source` uses only that camera, for tests |
| `web` | web UI |
| `create-user NAME` | adds a web UI account |
| `discover --user admin` | finds ONVIF cameras and lists their RTSP addresses |
| `probe --source URL` | opens a stream, prints resolution, codec and frame rate, saves one frame |
| `import` | one-time import of the first setup's files: `data/enroll/<name>/` photos, events, `[camera] source` |
| `evaluate` | compares the models on photo folders in `data/enroll/` and suggests `accept_threshold` |
| `download-models` | downloads the model files, verifying their checksums |

## Languages

English is the source language: the English text in templates and code is the key, and
`src/portaria/i18n/translation_cache.json` maps it to Portuguese (`{"pt": {"Today": "Hoje"}}`). A text without
a translation falls back to English. Data (names, numbers, file names) is never translated.

`tests/test_i18n.py` fails when any text in the templates or code has no Portuguese translation, so new
screens can't ship half translated. To add a text, write it in English with `t("...")` and add the Portuguese
to the catalog.

## Data and privacy

```
data/portaria.db         people, photos, face data, cameras, records, accounts, audit log
data/photos/<id>/        face photos
data/snapshots/<day>/    one picture per decision (and a face crop), deleted after [storage] retention_days
data/branding/logo.png   the logo set in Settings
```

`data/` holds biometric data and `config.toml` may hold camera passwords; both are git-ignored. Never commit them
or copy them to shared drives, and keep the server's disk encrypted (BitLocker on Windows, LUKS on Linux).
Deleting a person deletes their photos and face data; past records keep their name until they expire.

## Tests

```
python -m pytest
```

## Not built yet

- Liveness check (passive check on the regular camera first; depth or infrared later)
- ESP32 door relay
