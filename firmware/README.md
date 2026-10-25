# Devices: ESP32 boards over MQTT

Relays (door locks, lights, sirens) and inputs (buttons, door contacts) are wired to ESP32 boards. Boards and
the app talk through an MQTT broker, Mosquitto, which runs on the server. Each board keeps one connection
open to the broker, and so does the app. Messages travel both ways over these connections within
milliseconds, and nobody polls.

```
ESP32 board ──connection──▶ Mosquitto on the server ◀──connection── Portaria
```

- **The board needs the server's IP**, set in its firmware, plus the broker login. The app never needs a
  board's IP: the board connects outward, so a new address after a restart breaks nothing.
- **The app finds boards by itself.** A board announces what it has when it connects, the app lists it
  as a new device, and you name it there.
- `portaria_device/portaria_device.ino` is a working example: two relays and a button. Change the
  settings at its top and the lists of outputs and inputs to match your wiring.

## Server: install the broker

1. Install Mosquitto from mosquitto.org (Windows installer). It runs as the Windows service "Mosquitto Broker".
2. Create two logins, one for the app and one shared by the boards, in the Mosquitto folder:
   ```
   mosquitto_passwd -c passwd portaria
   mosquitto_passwd passwd device
   ```
3. Write `mosquitto.conf` in the same folder:
   ```
   listener 1883
   allow_anonymous false
   password_file C:\Program Files\mosquitto\passwd
   acl_file C:\Program Files\mosquitto\acl
   ```
   And the `acl` file, so each board can only use its own topics:
   ```
   user portaria
   topic readwrite portaria/#

   pattern readwrite portaria/%c/#
   ```
   `%c` is the board's client ID, which the example firmware sets to its device ID.
4. Restart the service (`net stop mosquitto` then `net start mosquitto`, as administrator), and allow TCP
   port 1883 through the Windows firewall on the private network.

## Topics and messages

Every board has a device ID: `esp32-` plus the last six hex digits of its MAC, such as `esp32-a1b2c3`.
All of its topics start with `portaria/<device>/`.

| Topic | From | Kept by the broker | Message |
|---|---|---|---|
| `status` | board | yes | `online` when it connects. If it drops off the network, the broker sends `offline` for it about 20 s later (its "last will") |
| `info` | board | yes | what the board has, sent when it connects (below) |
| `out/<key>/set` | app | no | a command for one output (below) |
| `out/<key>/state` | board | yes | `on` or `off`, after every change, including the automatic switch-off |
| `ack` | board | no | `{"id": "c-41", "ok": true}`, or `{"id": "c-41", "ok": false, "error": "no such output"}` |
| `in/<key>` | board | contacts only | `{"value": "pressed"}` for a button, `{"value": "open"}` or `{"value": "closed"}` for a contact |
| `health` | board | no | every minute: `{"uptime_s": 3600, "rssi": -61, "ip": "192.168.0.50"}` |

`info`, sent when the board connects:

```json
{
  "name": "Porta funcionarios",
  "firmware": "1.0.0",
  "ip": "192.168.0.50",
  "mac": "A4:CF:12:A1:B2:C3",
  "outputs": [{"key": "relay1", "kind": "lock", "max_ms": 10000}, {"key": "relay2", "kind": "light", "max_ms": 3600000}],
  "inputs": [{"key": "button1", "kind": "button"}]
}
```

Output kinds are `lock`, `light`, `siren` and `relay`; input kinds are `button` and `contact`. The kind only tells the
app what to show; the board treats every output the same way.

### Commands

The app sends a command to `out/<key>/set` with QoS 1, so the broker keeps sending it until the board
confirms it has it:

```json
{"id": "c-41", "action": "pulse", "ms": 3000}
```

- `pulse` or `on`: switch on, then off after `ms`. Without `ms`, or with more than the output's
  `max_ms`, the board uses `max_ms`.
- `off`: switch off now.
- `id` is a text the app makes unique. QoS 1 can deliver a command twice; the board remembers its last
  eight IDs and runs each command only once.

The board answers on `ack` and reports the new state on `out/<key>/state`.

## Rules every firmware must keep

1. **The board times every output itself.** Each output switches off after at most `max_ms`, counted on
   the board, even when the network is down or the server never sends `off`. The example uses ESP
   timers, which keep running while the board reconnects.
2. **Everything starts off.** At power-up, before connecting, all outputs are off.
3. **Off is the rest state.** Wire each relay so that off means door locked, light off and siren quiet.
   Avoid pins 0, 2, 5, 12 and 15, which can flicker while the ESP32 boots. The example uses 25, 26 and 27.
4. **No late commands.** The board connects with a clean session, so the broker drops commands sent
   while it was offline instead of delivering them when it comes back.
5. **Debounce inputs.** The example waits for 50 ms of stable signal.

## Testing without the app

With Mosquitto installed, watch everything, in PowerShell from the Mosquitto folder:

```
.\mosquitto_sub -h 127.0.0.1 -u portaria -P <password> -t "portaria/#" -v
```

Pulse relay 1 of a board for 2 seconds, from a second window:

```
.\mosquitto_pub -h 127.0.0.1 -u portaria -P <password> -q 1 -t "portaria/esp32-a1b2c3/out/relay1/set" -m '{"id":"t1","action":"pulse","ms":2000}'
```

The first window shows `state` going `on`, then `off` 2 seconds later, and the `ack`. The board's serial
monitor (115200 baud) prints its device ID when it starts.
