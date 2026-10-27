# Rules: models in sequence, then actions

Proposed on 2026-10-25, not started; the decisions at the end are still open. A rule can chain AI models (a face recognized, then the liveness
check; a phone found, then who holds it; a fall, then who fell) and end in actions on the ESP32 boards (open a door,
switch a light or a siren). It takes over the roadmap's Phase 1 item "Rules: when this happens, do that on this
output", and gives the models planned for Phases 3 and 4 (phone use, number plates, hairnets) one common way in.

## How other systems do it

Researched on 2026-10-25, from current documentation (Axis and UniFi only from search excerpts). They split the work in two, and so does this plan:
models in sequence find out what is in the picture, and rules act on what was found.

**Models in sequence:**
- **NVIDIA DeepStream (9.2):**
  - A primary model runs on whole frames. Secondary models run only on the objects it found, chosen by the parent
    model, its classes (`operate-on-gie-id`, `operate-on-class-ids`) and a minimum size.
  - With a tracker in between, a secondary classifier keeps its result per track. It runs again only for a new
    track or when the box grows by 20% or more.
- **Intel DL Streamer (2026.2):** the same idea in GStreamer elements. `gvaclassify object-class=vehicle` classifies
  only the vehicles found before it, on their crops. With `gvatrack` in between, `reclassify-interval` reuses the
  last result for the same track.
- **Roboflow Workflows:**
  - A JSON graph of blocks, with a visual editor: a model, then Dynamic Crop (one crop per detection, which keeps
    where it came from), then a model on each crop.
  - Continue If stops a branch unless a condition holds.
  - Sinks act on the result, such as a webhook with a cooldown (5 s by default).
  - It is the most general of the four, and some of its blocks are still marked as subject to change.
- **Frigate (0.18):**
  - Face recognition is an "enrichment" of a tracked person: it needs a person first, then a face of at least
    `min_area`.
  - It tries many frames while the person is in view, and decides the name from all of them together, weighted by
    face size. One high score alone names nobody.
  - The name goes on the person's track, as its `sub_label`.

**Rules that act on events:**
- **Milestone XProtect (2026 R1):**
  - A rule starts on an event or at a time, can add conditions such as a time profile, and runs several actions,
    also on other devices.
  - An action that starts something has a stop action, on an event or after a time. Start recording requires one,
    since otherwise "recording would potentially continue indefinitely".
  - Time profiles are named periods that many rules share. Milestone advises many simple rules over a few complex
    ones.
  - Its rule wizard offers only the options that apply, and won't save a rule with a part missing. Analytics events
    have a Test Event button.
- **Genetec Security Center (5.13):**
  - An event-to-action is When (the event), From (its source), Action, Effective (always or a schedule) and
    Enabled. Save stays off until the required parts are filled in.
  - Threat levels switch a whole area into a mode, with actions on entering and leaving it, until someone resets it.
  - Its newer Automation feature is recommended over event-to-actions.
- **Nx Witness (Gen 6, 2026):** a new rules engine, and Rule Lists of cameras, doors or identifiers. Change a list
  once, and every rule that uses it follows.
- **Axis cameras:** rules on the camera itself, with an event, conditions that must all be true, a schedule, and
  actions such as an output that stays on "while the rule is active".
- **UniFi Protect's Alarm Manager:** an alarm is a trigger, a scope (cameras included or left out), a schedule and
  actions (notifications, webhooks, devices).

**Not covered on 2026-10-25:**
- Home Assistant and Node-RED, Blue Iris, Agent DVR, Hikvision and Dahua.
- UniFi's door unlocks.
- How doors are opened safely from software: locks that stay shut without power, door held and forced alarms,
  two-factor entry. This research comes before Step 2.

**What this plan takes from them:**
- Follow-up models on crops of what the main model found, filtered by kind and size (DeepStream, DL Streamer,
  Frigate).
- A tracker in between, and each answer kept with its track (DeepStream, DL Streamer, Frigate's `sub_label`).
- A name from several looks, never from one (Frigate).
- Rules as sentences (when, only if, then), stored as data that the screens edit (all of them).
- Named schedules and lists that many rules share (Milestone, Genetec, Nx).
- An end for every output that switches on: a pulse, or on while the event lasts (Milestone, Axis).
- A guided editor that offers only what applies, won't save a rule with a part missing, and has a test button
  (Milestone, Genetec).
- Many small rules, each with its own switch (Milestone).
- Two things missing from every page read, which this plan adds: a test mode for each rule, and a guard
  against rules that start each other in a loop.

## The idea

Two halves, kept apart as in the systems above:

1. **What the cameras see.** Each camera runs its main model on its whole picture, as today. A tracker follows each
   person and thing from one look to the next, and each one carries what is known about it so far: who it is,
   whether the face is real, what it holds, whether it is lying down. **Follow-up models** find these out. They run
   on the part of the picture that matters (a face, a person's head, a vehicle), only for the people and things a
   rule asks about, and what they find stays with that person or thing, so they don't run again at every look.
2. **What happens then.** A rule says *when* (an event), what to *find out*, *only if* (conditions) and *then*
   (actions). The rules decide which follow-up models each camera runs: a camera runs only what its rules ask.

```
picture ─▶ main model ─▶ tracker ─▶ people and things, each with what is known about it
                                       ▲                    │ events
                  follow-up models ────┘                    ▼
                  (on a crop, kept with the person)     rules: when ▶ find out ▶ only if ▶ then
board inputs, schedules, screen buttons ──────────────────▶            │
                                         record · door and outputs · notify · door screen · MQTT
```

So "a sequence of models stipulated by a rule" becomes a question the rule asks, such as "who holds this phone?".
The camera answers it with the models that can: the person whose hand is on the phone, their face, its match in
People. Another rule asking the same question reuses the answer.

Nothing in the engine knows a particular model or device. Models declare what they need and give, rules name events
and facts, and outputs are keys on a board. A new model, fact or device is a new entry, not new rule code.

## Words

- **Main model:** what a camera runs on its whole picture, chosen in its settings: faces, people and objects, fire
  and smoke, or falls. Still one per camera, as decided on 2026-10-24; rules add follow-up models, not main ones.
- **Track:** one person or thing followed across looks, with a number, while it stays in the picture.
- **Fact:** something known about a track, with how sure and which model said it: `person` (who), `live`,
  `holder`, `lying`, `plate`, `hairnet`.
- **Follow-up model:** a model that turns a part of the picture into a fact. Each model declares what it needs and
  what it gives (below), so a new model plugs in without changes to the rules engine. It runs on the crop of what
  the main model found, only for the kinds and sizes it can use, and its answer stays with the track: it runs again
  only for a new track, after a while, or when the track's box grows by a fifth (the person came closer), as
  DeepStream does.
- **Event:** something that happened: a track appears, stays or leaves; a face is recognized, unknown or a spoof; a
  fall; a button pressed on a board or on a screen; a board or camera offline; a schedule starting. A screen's
  button, such as opening the door from Today, is an event like the others, so it goes through the same rules,
  checks and logs.
- **Rule:** when / find out / only if / then, with a mode (off, test or live), a cooldown and a schedule. Many
  small rules, each with its own switch, rather than a few large ones.
- **Schedules and lists:** named once and shared by rules, such as "Opening hours" or "Managers". Change one, and
  every rule that uses it follows.
- **Action:** keep a record, switch an output (pulse, on, off), notify someone, show something on the door screen,
  publish on MQTT.

## What each model needs and gives

| Model | Needs | Gives |
|---|---|---|
| YuNet | a picture, or a person's head | faces |
| AuraFace or SFace | a face | `person`: who, and how sure |
| MiniFASNet | a face | `live`: how sure the face is real |
| YOLOX-S or D-FINE-M | a picture | people and 79 kinds of things |
| YOLOv8s-pose | a picture, or a person | people with their 17 body points; `lying` |
| YOLO26n fire | a picture | fire, smoke |
| Plates (planned) | a vehicle | the plate, then `plate`: its text |
| Hairnet (custom, planned) | a person's head | `hairnet` |

Steps that aren't models give facts too: `holder` (which person holds a thing: its box sits in theirs, or a wrist is
next to it), `zone`, `count` and how long something has stayed.

With this table the rule editor fills in *find out* by itself. A condition on who holds the phone needs `holder`,
which needs people (and better, their body points); then a face in the holder's head; then `person`. The rule shows
the chain, and people can change it.

## A rule, stored as JSON

Rules live in the database as JSON, checked against a schema when saved. The editor writes it; nobody has to.

```json
{
  "name": "Phone at the counter",
  "when": {"event": "appears", "kind": "cell_phone", "cameras": [4], "for_s": 10},
  "find_out": ["holder.person"],
  "only_if": {"all": [{"fact": "holder.person.category", "in": ["employee"]}, {"schedule": "Opening hours"}]},
  "then": [{"do": "record"}, {"do": "notify", "to": "managers"}],
  "mode": "test",
  "cooldown": {"seconds": 3600, "per": "person"}
}
```

```json
{
  "name": "Staff entrance",
  "when": {"event": "recognized", "cameras": [3]},
  "only_if": {"all": [{"access_to": "esp32-a1b2c3/relay1"}]},
  "then": [{"do": "pulse", "output": "esp32-a1b2c3/relay1", "ms": 3000}, {"do": "door_screen", "show": "welcome"}],
  "mode": "test",
  "cooldown": {"seconds": 5, "per": "person"}
}
```

*Find out* lists facts a rule wants even without a condition on them: a fall rule finds out who fell to name them in
the record and the message.

Answers can come a moment after the event: a face may show only a second later. A rule waits a few seconds at most
for what it needs to find out (its own setting, 0 for a door), then goes on with what it knows. A condition on
something still unknown fails, and a message names "someone". Frigate's docs make the same point from the other
side: a name that comes late misses the notification.

## The three examples, worked through

### A face is recognized, then the liveness check

The entrance camera's main model is faces: YuNet finds the face and AuraFace recognizes it. Liveness runs after
that, and only on faces that match someone, at each look until the visit is decided (today it runs on every face).
The visit is the face's track: recognized once most of its matched looks are live (at least 3), a spoof once most
aren't, and that verdict holds until the face leaves. This replaces today's votes, which let a photo through after
flagging it, and flag a person already recognized (found on 2026-10-25).

This one chain belongs to face recognition itself rather than to a rule: "recognized", in a record, on Today or in a
rule, then always means a live face, and no rule can forget it. A rule that opens a lock needs the liveness model
downloaded.

### A cell phone appears, then who holds it

The counter camera's main model is people and objects (person and cell phone). The holder is the person whose
box contains the phone; with body points (the pose model, run on those people's crops only while a phone is in the
picture), the person with a wrist next to it, which tells people apart when they stand close together. Then who it
is: YuNet on the holder's head, cut from the full-resolution picture, then AuraFace on several looks. As in
Frigate, the name comes from all of them together, weighted by the size of the face, and never from one high score.

Someone looking down at a phone often shows the top of their head, not their face. So on cameras whose rules ask
who someone is, the people are recognized whenever their face shows, before anything happens, and the answer stays
with their track. When the phone appears, the name is usually known already.

### A fall, then who fell

The kitchen camera's main model is falls (people with their body points). A fallen person's face is usually hidden
or sideways, so who they are comes from before the fall, the same way: recognized while they walked and stood, kept
with their track. At the fall, the event carries that name; without one, one more try on the lying person; then
"someone". The tracker must keep the track through the fall, when the box suddenly turns from tall to wide.

## Actions and safety

- **Outputs over MQTT,** as in `firmware/README.md`: `pulse` with its time, confirmed by `ack`; the board times
  every output itself, and off is the safe state.
- **Every output that switches on has an end:** a pulse, or on while the event lasts (a light while someone is
  there), up to the output's longest time. When the rule fires again meanwhile, the time starts over. Off can be
  sent twice without harm.
- **Every rule starts in test mode:** its actions are only logged, as the door's shadow mode does today. Making it
  live is for administrators, and goes to the audit log.
- **Locks:**
  - A rule that opens one needs liveness downloaded, the board online and the person's access to that output.
  - A site mode overrides every rule: open, closed or lockdown. Rules can set it, and only a person sets it back,
    as with Genetec's threat levels.
  - A master switch ("outputs in test mode only", today's `[door] mode`) also overrides every rule.
- **No floods:** a cooldown per rule and per person or thing, "once per visit", and at most so many messages an hour.
- **No loops:** an event made by a rule can start other rules but never the same one, and never opens a lock.
  Chains stop after 3.
- **Traces:** every time a rule's *when* matches, it writes what it found out, each condition passed or failed, and
  each action with its result (confirmed, timed out). "Why didn't it open?" is answered from these.
- **Fire and smoke** only in addition to certified smoke detectors, never instead.

## Data

- `rules`: name, mode (off, test or live), the JSON, who changed it and when.
- `schedules`: named weekly hours, such as "Opening hours" and "Night", in the server's local time like every
  other time in the app.
- `lists`: named lists of people, cameras or outputs, such as "Managers".
- The site mode, in `settings`.
- `devices`, `outputs`, `inputs`: the boards and Tuya modules, from the roadmap's Phase 1 and devices.md.
- `events`: today's table for any kind of event: `kind` as `face.recognized`, `object.detected`, `fall`,
  `phone.in_hand`, `input.button`; a `facts` JSON (holder, plate, liveness score); the track; the rule that made it.
- `rule_runs`: the traces. `action_log`: every action and its result, for the audit.
- A `rules_version` setting the recognition service watches, as it watches cameras and models today.

The rules engine runs inside the recognition service, beside the cameras, so a door doesn't depend on the web UI.

## Where it goes in the code

- `rules/` (new): the schema (Pydantic, which FastAPI already brings), the engine, the actions, and the planner
  that works out each camera's follow-ups from its rules.
- `service.py`: hosts the engine; the door logic in `on_decision` becomes the first rule.
- `CameraWorker`: main model, tracker, follow-ups, then events, in place of one analyzer's result.
- `pipeline.py` and `decision.py`: liveness after a match, and the visit's verdict.
- `objects.py`: `Presence` per track instead of per kind.
- `models.py`: what each model needs and gives, beside its files.
- `door.py`: replaced by outputs, through the device drivers of devices.md (MQTT boards, Tuya modules).

## Screens

New screens go to React (react-migration.md).
- **Rules:** each rule with on or off, test or live, when it last fired and how often today. A new rule starts from
  a template: open a door for recognized people, phone in hand, a fall, fire and smoke, someone at the back door at
  night.
- **The editor:** the four parts as forms, not a node graph.
  - It offers only the choices that apply, fills in *find out* by itself, and won't save a rule with a part missing.
  - It warns when a rule needs a model that isn't downloaded, or a camera whose faces come out too small.
  - **Try it** runs the rule on a recent event from its camera, without acting, and shows which checks passed.
- **A rule's page:** its recent firings with photos and traces.
- **A camera's page:** the rules that use it, and the follow-up models it runs because of them.

## Decisions to make

1. **Builder:** forms with templates (recommended), or a node graph like Node-RED. Forms suit the bakery's staff, and
   the JSON can take a graph later.
2. **Main models:** one per camera plus follow-ups (recommended), or several per camera, which needs the GPU and a
   scheduler first.
3. **Where rules run:** inside Live Tracker (recommended: the door's speed, the audit, nothing else to install), or in
   Home Assistant or Node-RED, fed over MQTT. Publishing events on MQTT is cheap and keeps that open.
4. **Who someone is:** recognized all along on cameras whose rules ask (recommended; costs processing), or only at
   the moment of the event (cheaper, but misses most falls and phone use).
5. **Rules across cameras,** such as "recognized at the door, and not at the time clock within 10 minutes": later
   (recommended), or now.
6. **Liveness:** part of face recognition, after a match (recommended), or a step each rule chooses.

## Steps

### Step 1: events and rules on today's models

- [ ] The visit's liveness verdict in decision.py (most looks live, kept until the face leaves); it can land first
- [ ] Events inside the recognition service: today's decisions and sightings become events
- [ ] Tables `rules`, `schedules`, `lists`, `rule_runs`, `action_log`; the JSON schema; reload on `rules_version`
- [ ] Actions: record, door screen; outputs in test mode only
- [ ] Today's door becomes the first rule, in test mode; `[door] mode` becomes the master switch
- [ ] Rules page, editor with templates, traces

### Step 2: devices

- [ ] Research first: how doors are opened safely from software (see "Not covered")
- [ ] The roadmap's Phase 1 MQTT items; outputs as actions, inputs and offline boards as events
- [ ] The site mode, and opening a door from a screen, through the rules

### Step 3: tracks and follow-up models

- [ ] A tracker on every camera, and tracks that keep their facts
- [ ] Each model loaded once and shared, with a queue that serves doors first; crops from the full-resolution picture
- [ ] The model table (needs and gives), and each camera's follow-ups worked out from its rules
- [ ] Liveness only after a match, on the face's track

### Step 4: who, and what they hold

- [ ] Who someone is, from a face in a person's head, kept with the track
- [ ] Holders: by boxes first, then by body points
- [ ] Templates: phone in hand, who fell

### Step 5: what comes next

- [ ] Zones (with the React zone editor), counts and how long things stay; a person is in a zone when the middle
      of the bottom of their box is: their feet
- [ ] Notifications by e-mail
- [ ] Events published on MQTT, for other systems

### Later

- A node graph for advanced rules; several main models per camera; rules across cameras; recognizing a person by
  their body and clothes across cameras; actions in steps, such as opening a door and then waiting for its contact
  to close.

## Risks

- **Processing:** follow-up models multiply the work. The GPU and shared models come before Step 4 on busy cameras.
- **Small faces:** recognizing needs faces of 80 px or more; counter and kitchen cameras may need to be closer, or
  the full-resolution picture.
- **Tracking through a fall** can lose the track, and with it the name.
- **Wrong names in records:** each fact keeps how sure it was, and records say "probably" below a threshold. Phone
  use needs its LGPD purpose, notice and retention first.
- **Testing:** unit tests feed events to the engine; whole rules are tried on recorded clips with
  `live-tracker run --source`.
