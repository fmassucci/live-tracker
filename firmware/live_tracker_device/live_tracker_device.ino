// Live Tracker device: an ESP32 whose relays and inputs are driven over MQTT.
//
// The messages are described in ../README.md. Needs two libraries from the Arduino Library Manager:
// PubSubClient (Nick O'Leary) and ArduinoJson 7 (Benoit Blanchon).
//
// Safety: every output switches itself off after at most max_ms, timed on the board, even while
// the network is down or the board is busy reconnecting. Commands are never queued: a command sent
// while the board is offline is dropped, so a door never opens late.

#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include "esp_timer.h"

// ---------- Settings for this board ----------
const char* WIFI_SSID = "your-wifi";
const char* WIFI_PASSWORD = "your-wifi-password";
const char* MQTT_HOST = "192.168.0.19";  // the server running the broker (Mosquitto)
const uint16_t MQTT_PORT = 1883;
const char* MQTT_USER = "device";
const char* MQTT_PASSWORD = "device-password";
const char* DEVICE_NAME = "Porta funcionarios";  // shown in the app until you rename it there
const char* FIRMWARE = "1.0.0";

// What is wired to the board. kind tells the app what it is: "lock", "light", "siren" or "relay".
// Off is the rest state: wire each relay so that off means door locked, light off, siren quiet.
struct Output {
  const char* key;
  const char* kind;
  uint8_t pin;
  bool activeHigh;  // true when a HIGH pin switches the relay on
  uint32_t maxMs;   // the longest it may stay on; the board switches it off by itself
  esp_timer_handle_t timer = nullptr;
  volatile bool on = false;
  volatile bool changed = false;  // switched off by its timer, not reported yet
};

Output outputs[] = {
  {"relay1", "lock", 26, true, 10000},     // door lock: opens for at most 10 s
  {"relay2", "light", 25, true, 3600000},  // light: on for at most 1 h
};

// Inputs are wired to GND, with the internal pull-up. kind: "button" sends "pressed";
// "contact" (a door sensor, for instance) sends "open" and "closed".
struct Input {
  const char* key;
  const char* kind;
  uint8_t pin;
  bool level = HIGH;    // last stable level
  bool reading = HIGH;  // last raw reading
  uint32_t changedAt = 0;
};

Input inputs[] = {
  {"button1", "button", 27},  // exit button
};

const uint32_t RETRY_MS = 5000;
const uint32_t HEALTH_MS = 60000;
const uint32_t DEBOUNCE_MS = 50;

WiFiClient net;
PubSubClient mqtt(net);
String deviceId;  // esp32-<last 6 hex digits of the MAC>, unique and stable
String prefix;    // live-tracker/<deviceId>/
uint32_t lastTry = 0;
uint32_t lastHealth = 0;
String doneIds[8];  // the last commands run: MQTT may deliver one twice, and a pulse must run once
uint8_t doneNext = 0;

void publish(const String& topic, const String& payload, bool retained = false) {
  mqtt.publish((prefix + topic).c_str(), payload.c_str(), retained);
}

void publishJson(const String& topic, JsonDocument& doc, bool retained = false) {
  String text;
  serializeJson(doc, text);
  publish(topic, text, retained);
}

void writePin(Output& o, bool on) {
  digitalWrite(o.pin, on == o.activeHigh ? HIGH : LOW);
}

void publishState(Output& o) {
  publish(String("out/") + o.key + "/state", o.on ? "on" : "off", true);
}

// Runs in the ESP timer task, so the output goes off on time whatever the main loop is doing.
void switchOffWhenDue(void* arg) {
  Output& o = *static_cast<Output*>(arg);
  writePin(o, false);
  o.on = false;
  o.changed = true;
}

void setOutput(Output& o, bool on, uint32_t ms) {
  esp_timer_stop(o.timer);  // harmless when it isn't running
  writePin(o, on);
  o.on = on;
  if (on) {
    esp_timer_start_once(o.timer, static_cast<uint64_t>(ms) * 1000);
  }
  publishState(o);
}

void publishInput(Input& i) {
  JsonDocument doc;
  bool contact = strcmp(i.kind, "contact") == 0;
  if (contact) {
    doc["value"] = i.level == LOW ? "closed" : "open";
  } else {
    doc["value"] = "pressed";
  }
  publishJson(String("in/") + i.key, doc, contact);  // the broker keeps a contact's last state
}

void publishInfo() {
  JsonDocument info;
  info["name"] = DEVICE_NAME;
  info["firmware"] = FIRMWARE;
  info["ip"] = WiFi.localIP().toString();
  info["mac"] = WiFi.macAddress();
  JsonArray outs = info["outputs"].to<JsonArray>();
  for (auto& o : outputs) {
    JsonObject item = outs.add<JsonObject>();
    item["key"] = o.key;
    item["kind"] = o.kind;
    item["max_ms"] = o.maxMs;
  }
  JsonArray ins = info["inputs"].to<JsonArray>();
  for (auto& i : inputs) {
    JsonObject item = ins.add<JsonObject>();
    item["key"] = i.key;
    item["kind"] = i.kind;
  }
  publishJson("info", info, true);
}

void reply(const String& id, bool ok, const char* error = nullptr) {
  JsonDocument ack;
  ack["id"] = id;
  ack["ok"] = ok;
  if (error) {
    ack["error"] = error;
  }
  publishJson("ack", ack);
}

bool alreadyDone(const String& id) {
  if (id.isEmpty()) {
    return false;
  }
  for (auto& done : doneIds) {
    if (done == id) {
      return true;
    }
  }
  doneIds[doneNext] = id;
  doneNext = (doneNext + 1) % (sizeof(doneIds) / sizeof(doneIds[0]));
  return false;
}

// A command arrives on live-tracker/<deviceId>/out/<key>/set, as {"id": "...", "action": "pulse", "ms": 3000}.
void onMessage(char* topic, byte* payload, unsigned int length) {
  String t(topic);
  String head = prefix + "out/";
  if (!t.startsWith(head) || !t.endsWith("/set")) {
    return;
  }
  String key = t.substring(head.length(), t.length() - 4);

  JsonDocument command;
  if (deserializeJson(command, payload, length)) {
    reply("", false, "not JSON");
    return;
  }
  String id = command["id"] | "";
  Output* output = nullptr;
  for (auto& o : outputs) {
    if (key == o.key) {
      output = &o;
    }
  }
  if (!output) {
    reply(id, false, "no such output");
    return;
  }
  String action = command["action"] | "";
  if (action != "pulse" && action != "on" && action != "off") {
    reply(id, false, "unknown action");
    return;
  }
  if (alreadyDone(id)) {  // a second delivery of a command already run
    reply(id, true);
    return;
  }
  uint32_t ms = command["ms"] | output->maxMs;
  if (ms == 0 || ms > output->maxMs) {
    ms = output->maxMs;
  }
  setOutput(*output, action != "off", ms);
  reply(id, true);
}

void connectMqtt() {
  if (mqtt.connected() || (lastTry && millis() - lastTry < RETRY_MS)) {
    return;
  }
  lastTry = millis();
  String status = prefix + "status";
  // If the board disappears, the broker announces "offline" for it about 20 s later.
  if (!mqtt.connect(deviceId.c_str(), MQTT_USER, MQTT_PASSWORD, status.c_str(), 1, true, "offline")) {
    Serial.printf("MQTT: no connection (state %d), retrying\n", mqtt.state());
    return;
  }
  Serial.println("MQTT: connected");
  mqtt.publish(status.c_str(), "online", true);
  publishInfo();
  for (auto& o : outputs) {
    publishState(o);
  }
  for (auto& i : inputs) {
    if (strcmp(i.kind, "contact") == 0) {
      publishInput(i);
    }
  }
  mqtt.subscribe((prefix + "out/+/set").c_str(), 1);
}

void readInputs() {
  for (auto& i : inputs) {
    bool now = digitalRead(i.pin);
    if (now != i.reading) {
      i.reading = now;
      i.changedAt = millis();
    }
    if (now == i.level || millis() - i.changedAt < DEBOUNCE_MS) {
      continue;
    }
    i.level = now;
    bool contact = strcmp(i.kind, "contact") == 0;
    if (contact || now == LOW) {  // a button reports the press, a contact every change
      publishInput(i);
    }
  }
}

void reportSwitchedOff() {
  for (auto& o : outputs) {
    if (o.changed) {
      o.changed = false;
      publishState(o);
    }
  }
}

void sendHealth() {
  if (millis() - lastHealth < HEALTH_MS) {
    return;
  }
  lastHealth = millis();
  JsonDocument doc;
  doc["uptime_s"] = millis() / 1000;
  doc["rssi"] = WiFi.RSSI();
  doc["ip"] = WiFi.localIP().toString();
  publishJson("health", doc);
}

void setup() {
  Serial.begin(115200);
  for (auto& o : outputs) {
    pinMode(o.pin, OUTPUT);
    writePin(o, false);  // everything off from the start
    esp_timer_create_args_t args = {};
    args.callback = switchOffWhenDue;
    args.arg = &o;
    args.name = o.key;
    esp_timer_create(&args, &o.timer);
  }
  for (auto& i : inputs) {
    pinMode(i.pin, INPUT_PULLUP);
    i.level = i.reading = digitalRead(i.pin);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char id[16];
  snprintf(id, sizeof(id), "esp32-%02x%02x%02x", mac[3], mac[4], mac[5]);
  deviceId = id;
  prefix = String("live-tracker/") + deviceId + "/";
  Serial.printf("device %s\n", id);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
  mqtt.setBufferSize(1024);  // the info message is longer than the default 256 bytes
  mqtt.setKeepAlive(15);
  mqtt.setSocketTimeout(2);  // keeps reconnecting short, so inputs keep being read
}

void loop() {
  readInputs();
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  connectMqtt();
  if (mqtt.connected()) {
    mqtt.loop();
    reportSwitchedOff();
    sendHealth();
  }
}
