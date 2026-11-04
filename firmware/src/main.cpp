#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ESP32Servo.h>
#include <ArduinoJson.h>
#include <esp_random.h>

// ===================== Pemetaan GPIO =====================
const uint8_t PIN_RAIN        = 34;  // Potensiometer (simulasi sensor hujan), ADC1
const uint8_t PIN_LDR         = 35;  // Photoresistor, ADC1
const uint8_t PIN_SERVO       = 18;  // Servo SG90
const uint8_t PIN_LIMIT_OPEN  = 25;  // Pushbutton hijau: limit terbuka
const uint8_t PIN_LIMIT_CLOSE = 26;  // Pushbutton merah: limit tertutup
const uint8_t PIN_LED         = 27;  // LED indikator status

// ===================== Identitas perangkat =====================
const char* DEVICE_ID = "fishslide-node01";

// ===================== Konfigurasi jaringan =====================
const char* WIFI_SSID    = "Wokwi-GUEST";   // Jaringan simulasi Wokwi
const char* WIFI_PASS    = "";
const int   WIFI_CHANNEL = 6;

const char*    MQTT_HOST = "broker.hivemq.com";  // Broker publik (hanya untuk uji)
const uint16_t MQTT_PORT = 1883;
// Topik resmi Kelompok 16
const char* TOPIC_PREFIX = "unib/fishslide/kelompok16/node01";

// Penerima uji HTTP; nanti diganti ke backend-api (Tahap 8)
const char* HTTP_URL = "http://httpbin.org/post";

const unsigned long WIFI_RETRY_MS = 15000;
const unsigned long MQTT_RETRY_MS = 5000;
const unsigned long HTTP_INTERVAL_MS = 10000;

// ===================== Konfigurasi sensor =====================
const int ADC_MAX = 4095;
// Ubah ke false jika saat uji ternyata terang membuat nilai ADC NAIK
const bool LDR_TERANG_NILAI_RENDAH = true;

// ===================== Ambang logika otomatis =====================
const int RAIN_ON_PCT     = 30;
const int RAIN_OFF_PCT    = 20;
const int LIGHT_OPEN_PCT  = 40;
const unsigned long RAIN_CLEAR_MS = 5000;  // Simulasi; alat nyata: beberapa menit

// ===================== Konfigurasi waktu dan servo =====================
const unsigned long INTERVAL_SENSOR_MS    = 500;
const unsigned long INTERVAL_TELEMETRI_MS = 1000;
const unsigned long INTERVAL_SERVO_MS     = 15;
const unsigned long DEBOUNCE_MS           = 30;
const unsigned long LIMIT_TIMEOUT_MS      = 8000;
const int SERVO_ANGLE_OPEN   = 0;
const int SERVO_ANGLE_CLOSED = 90;

// ===================== Status sistem =====================
enum StatusPenutup { ST_BERGERAK, ST_TERBUKA, ST_TERTUTUP, ST_FAULT };

const char* namaStatus(StatusPenutup s) {
  switch (s) {
    case ST_BERGERAK: return "MOVING";
    case ST_TERBUKA:  return "OPEN";
    case ST_TERTUTUP: return "CLOSED";
    default:          return "FAULT";
  }
}

// Tombol dengan debounce (aktif LOW karena INPUT_PULLUP)
struct Tombol {
  uint8_t pin;
  bool rawTerakhir;
  bool stabil;            // true = sedang ditekan
  unsigned long tUbah;
};

Tombol limitBuka  = { PIN_LIMIT_OPEN,  false, false, 0 };
Tombol limitTutup = { PIN_LIMIT_CLOSE, false, false, 0 };

void updateTombol(Tombol& t, unsigned long now) {
  bool raw = (digitalRead(t.pin) == LOW);
  if (raw != t.rawTerakhir) {
    t.rawTerakhir = raw;
    t.tUbah = now;
  }
  if (now - t.tUbah >= DEBOUNCE_MS) {
    t.stabil = raw;
  }
}

// ===================== Variabel =====================
Servo penutup;
int servoAngle  = SERVO_ANGLE_OPEN;
int servoTarget = SERVO_ANGLE_OPEN;
unsigned long tSensorTerakhir    = 0;
unsigned long tTelemetriTerakhir = 0;
unsigned long tServoTerakhir     = 0;
unsigned long tLedTerakhir       = 0;

bool modeOtomatis    = true;
bool hujanTerdeteksi = false;
bool redaAktif       = false;
unsigned long tRedaMulai = 0;

StatusPenutup status = ST_TERBUKA;
StatusPenutup statusSebelumnya = ST_TERBUKA;
bool terkonfirmasi = true;
bool sudahTiba = true;
unsigned long tTiba = 0;
bool ledState = false;

int rainRaw = 0, ldrRaw = 0;
int rainPct = 0, terangPct = 0;
unsigned long nomorPaket = 0;

// Jaringan
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
bool wifiTerhubung = false;
bool mqttTerhubung = false;
unsigned long tWifiCoba = 0;
unsigned long tMqttCoba = 0;
unsigned long tHttpTerakhir = 0;
char clientId[32];
char topicTelemetry[64], topicEvent[64], topicStatus[64], topicCmd[64];
char jsonTelemetri[512] = "";   // Telemetri terakhir, dipakai juga oleh HTTP POST

// ===================== Keluaran JSON =====================
// Event: tampil di Serial dan dipublikasikan ke MQTT (jika terhubung)
void keluarkanEvent(JsonDocument& doc) {
  char buf[256];
  serializeJson(doc, buf, sizeof(buf));
  Serial.println(buf);
  if (mqtt.connected()) mqtt.publish(topicEvent, buf);
}

// Field "detail" boleh nullptr
void kirimEvent(const char* event, const char* detail) {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["type"] = "event";
  doc["uptime_ms"] = millis();
  doc["event"] = event;
  if (detail) doc["detail"] = detail;
  keluarkanEvent(doc);
}

void kirimStatusChange(StatusPenutup dari, StatusPenutup ke, unsigned long now) {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["type"] = "event";
  doc["uptime_ms"] = now;
  doc["event"] = "status_change";
  doc["from"] = namaStatus(dari);
  doc["to"] = namaStatus(ke);
  keluarkanEvent(doc);
}

void kirimTelemetri() {
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["type"] = "telemetry";
  doc["seq"] = ++nomorPaket;
  doc["uptime_ms"] = millis();
  doc["mode"] = modeOtomatis ? "AUTO" : "MANUAL";

  JsonObject sensor = doc["sensor"].to<JsonObject>();
  sensor["rain_raw"]  = rainRaw;
  sensor["rain_pct"]  = rainPct;
  sensor["ldr_raw"]   = ldrRaw;
  sensor["light_pct"] = terangPct;
  sensor["raining"]   = hujanTerdeteksi;

  JsonObject cover = doc["cover"].to<JsonObject>();
  cover["status"]       = namaStatus(status);
  cover["servo_angle"]  = servoAngle;
  cover["servo_target"] = servoTarget;
  cover["limit_open"]   = limitBuka.stabil;
  cover["limit_closed"] = limitTutup.stabil;

  serializeJson(doc, jsonTelemetri, sizeof(jsonTelemetri));
  Serial.println(jsonTelemetri);
  if (mqtt.connected()) mqtt.publish(topicTelemetry, jsonTelemetri);
}

// ===================== Servo =====================
void setTarget(int sudut, const char* alasan) {
  if (servoTarget == sudut) return;
  servoTarget = sudut;
  terkonfirmasi = false;
  sudahTiba = false;
  kirimEvent(sudut == SERVO_ANGLE_CLOSED ? "cover_close" : "cover_open", alasan);
}

void updateServo() {
  if (servoAngle == servoTarget) return;
  servoAngle += (servoTarget > servoAngle) ? 1 : -1;
  penutup.write(servoAngle);
}

// ===================== Perintah (Serial dan MQTT) =====================
void jalankanPerintah(char cmd, const char* sumber) {
  if (cmd == 'a') {
    modeOtomatis = true;
    kirimEvent("mode_change", "AUTO");
  } else if (cmd == 'o') {
    modeOtomatis = false;
    kirimEvent("mode_change", "MANUAL");
    if (status == ST_FAULT && servoTarget == SERVO_ANGLE_OPEN) {
      sudahTiba = false;
      terkonfirmasi = false;
      status = ST_BERGERAK;
      kirimEvent("fault_retry", "open");
    } else {
      setTarget(SERVO_ANGLE_OPEN, sumber);
    }
  } else if (cmd == 'c') {
    modeOtomatis = false;
    kirimEvent("mode_change", "MANUAL");
    if (status == ST_FAULT && servoTarget == SERVO_ANGLE_CLOSED) {
      sudahTiba = false;
      terkonfirmasi = false;
      status = ST_BERGERAK;
      kirimEvent("fault_retry", "close");
    } else {
      setTarget(SERVO_ANGLE_CLOSED, sumber);
    }
  }
}

void tampilkanMenuSerial() {
  Serial.println(F("\n--- [Menu Perintah FishSlide] ---"));
  Serial.println(F("  o / O : Buka penutup (Mode Manual)"));
  Serial.println(F("  c / C : Tutup penutup (Mode Manual)"));
  Serial.println(F("  a / A : Kembalikan ke Mode Otomatis"));
  Serial.println(F("  ? / h : Tampilkan bantuan ini"));
  Serial.println(F("---------------------------------\n"));
}

void bacaPerintahSerial() {
  while (Serial.available() > 0) {
    char cmd = tolower(Serial.read());
    if (cmd == '\r' || cmd == '\n' || cmd == ' ') continue;
    if (cmd == '?' || cmd == 'h') {
      tampilkanMenuSerial();
    } else {
      jalankanPerintah(cmd, "serial_command");
    }
  }
}

// Dipanggil PubSubClient saat ada pesan di topik cmd
void onMqttMessage(char* topic, byte* payload, unsigned int len) {
  char buf[24];
  if (len >= sizeof(buf)) len = sizeof(buf) - 1;
  memcpy(buf, payload, len);
  buf[len] = '\0';

  // Hapus karakter whitespace/newline di akhir pesan
  while (len > 0 && (buf[len - 1] == '\r' || buf[len - 1] == '\n' || buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
    buf[--len] = '\0';
  }

  // Lewati whitespace di awal pesan
  char* msg = buf;
  while (*msg == ' ' || *msg == '\t' || *msg == '\r' || *msg == '\n') {
    msg++;
  }

  // Konversi pesan ke huruf kecil
  for (char* p = msg; *p; p++) {
    *p = tolower((unsigned char)*p);
  }

  if (strcmp(msg, "open") == 0 || strcmp(msg, "o") == 0)            jalankanPerintah('o', "mqtt_command");
  else if (strcmp(msg, "close") == 0 || strcmp(msg, "c") == 0)      jalankanPerintah('c', "mqtt_command");
  else if (strcmp(msg, "auto") == 0 || strcmp(msg, "a") == 0)       jalankanPerintah('a', "mqtt_command");
  else kirimEvent("mqtt_cmd_ignored", msg);
}

// ===================== Logika otomatis =====================
void evaluasiOtomatis(unsigned long now) {
  if (rainPct >= RAIN_ON_PCT) {
    hujanTerdeteksi = true;
    redaAktif = false;
    setTarget(SERVO_ANGLE_CLOSED, "rain_detected");
    return;
  }

  if (hujanTerdeteksi) {
    if (rainPct <= RAIN_OFF_PCT) {
      if (!redaAktif) {
        redaAktif = true;
        tRedaMulai = now;
        kirimEvent("rain_easing", "waiting_confirmation");
      }
      if (now - tRedaMulai >= RAIN_CLEAR_MS) {
        hujanTerdeteksi = false;
        redaAktif = false;
        kirimEvent("rain_stopped", nullptr);
      }
    } else {
      redaAktif = false;
    }
  }

  if (!hujanTerdeteksi && terangPct >= LIGHT_OPEN_PCT) {
    setTarget(SERVO_ANGLE_OPEN, "rain_stopped_and_bright");
  }
}

// ===================== Status berdasarkan limit switch =====================
void updateStatus(unsigned long now) {
  if (limitBuka.stabil && limitTutup.stabil) {
    status = ST_FAULT;
    return;
  }

  if (servoAngle != servoTarget) {
    status = ST_BERGERAK;
    sudahTiba = false;
    return;
  }

  if (!sudahTiba) {
    sudahTiba = true;
    tTiba = now;
  }

  bool limitTarget = (servoTarget == SERVO_ANGLE_CLOSED) ? limitTutup.stabil
                                                         : limitBuka.stabil;
  if (limitTarget) terkonfirmasi = true;

  if (terkonfirmasi) {
    status = (servoTarget == SERVO_ANGLE_CLOSED) ? ST_TERTUTUP : ST_TERBUKA;
  } else if (now - tTiba >= LIMIT_TIMEOUT_MS) {
    status = ST_FAULT;
  } else {
    status = ST_BERGERAK;
  }
}

// ===================== LED non-blocking =====================
void updateLed(unsigned long now) {
  switch (status) {
    case ST_TERBUKA:
      digitalWrite(PIN_LED, LOW);
      ledState = false;
      break;
    case ST_TERTUTUP:
      digitalWrite(PIN_LED, HIGH);
      ledState = true;
      break;
    default: {
      unsigned long periode = (status == ST_FAULT) ? 100 : 300;
      if (now - tLedTerakhir >= periode) {
        tLedTerakhir = now;
        ledState = !ledState;
        digitalWrite(PIN_LED, ledState);
      }
    }
  }
}

// ===================== Pembacaan sensor =====================
void prosesSensor(unsigned long now) {
  rainRaw = analogRead(PIN_RAIN);
  ldrRaw  = analogRead(PIN_LDR);
  rainPct = constrain(map(rainRaw, 0, ADC_MAX, 0, 100), 0, 100);
  int ldrPct = constrain(map(ldrRaw, 0, ADC_MAX, 0, 100), 0, 100);
  terangPct = constrain(LDR_TERANG_NILAI_RENDAH ? (100 - ldrPct) : ldrPct, 0, 100);

  if (modeOtomatis) evaluasiOtomatis(now);
}

// ===================== Wi-Fi =====================
void updateWifi(unsigned long now) {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiTerhubung) {
      wifiTerhubung = true;
      kirimEvent("wifi_connected", WiFi.localIP().toString().c_str());
    }
    return;
  }

  if (wifiTerhubung) {
    wifiTerhubung = false;
    kirimEvent("wifi_lost", nullptr);
  }
  // Coba sambung ulang secara berkala
  if (now - tWifiCoba >= WIFI_RETRY_MS) {
    tWifiCoba = now;
    WiFi.disconnect();
    if (strcmp(WIFI_SSID, "Wokwi-GUEST") == 0) {
      WiFi.begin(WIFI_SSID, WIFI_PASS, WIFI_CHANNEL);
    } else {
      WiFi.begin(WIFI_SSID, WIFI_PASS);
    }
  }
}

// ===================== MQTT =====================
void updateMqtt(unsigned long now) {
  if (WiFi.status() != WL_CONNECTED) return;

  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }

  if (mqttTerhubung) {
    mqttTerhubung = false;
    kirimEvent("mqtt_lost", nullptr);
  }
  if (now - tMqttCoba < MQTT_RETRY_MS) return;
  tMqttCoba = now;

  // Last Will: broker menerbitkan "offline" jika perangkat putus tiba-tiba
  bool ok = mqtt.connect(clientId, nullptr, nullptr,
                         topicStatus, 1, true, "offline");
  if (ok) {
    mqttTerhubung = true;
    mqtt.publish(topicStatus, "online", true);   // retained
    mqtt.subscribe(topicCmd);
    kirimEvent("mqtt_connected", MQTT_HOST);
  } else {
    char d[16];
    snprintf(d, sizeof(d), "rc=%d", mqtt.state());
    kirimEvent("mqtt_failed", d);
  }
}

// ===================== HTTP POST =====================
// Catatan: pemanggilan HTTPClient bersifat synchronous/blocking
void kirimHttp() {
  if (WiFi.status() != WL_CONNECTED || jsonTelemetri[0] == '\0') return;

  // Hindari blocking HTTP saat servo sedang aktif bergerak agar pergerakan tetap mulus
  if (status == ST_BERGERAK) return;

  HTTPClient http;
  http.setConnectTimeout(1500);
  http.setTimeout(1500);
  http.begin(HTTP_URL);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t*)jsonTelemetri, strlen(jsonTelemetri));
  http.end();

  char d[24];
  snprintf(d, sizeof(d), "code=%d", code);
  kirimEvent("http_post", d);
}

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);

  pinMode(PIN_LIMIT_OPEN,  INPUT_PULLUP);
  pinMode(PIN_LIMIT_CLOSE, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  ESP32PWM::allocateTimer(0);
  penutup.setPeriodHertz(50);
  penutup.attach(PIN_SERVO, 500, 2400);
  penutup.write(servoAngle);

  // Susun nama topik dan Client ID unik
  snprintf(topicTelemetry, sizeof(topicTelemetry), "%s/telemetry", TOPIC_PREFIX);
  snprintf(topicEvent,     sizeof(topicEvent),     "%s/event",     TOPIC_PREFIX);
  snprintf(topicStatus,    sizeof(topicStatus),    "%s/status",    TOPIC_PREFIX);
  snprintf(topicCmd,       sizeof(topicCmd),       "%s/cmd",       TOPIC_PREFIX);
  snprintf(clientId, sizeof(clientId), "fishslide-%08lx", (unsigned long)esp_random());

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  mqtt.setBufferSize(512);
  mqtt.setSocketTimeout(3);

  WiFi.mode(WIFI_STA);
  if (strcmp(WIFI_SSID, "Wokwi-GUEST") == 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASS, WIFI_CHANNEL);
  } else {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  tWifiCoba = millis();

  kirimEvent("boot", "FishSlide Kelompok 16: MQTT dan HTTP");
}

void loop() {
  unsigned long now = millis();

  updateTombol(limitBuka, now);
  updateTombol(limitTutup, now);

  if (now - tSensorTerakhir >= INTERVAL_SENSOR_MS) {
    tSensorTerakhir = now;
    prosesSensor(now);
  }

  if (now - tServoTerakhir >= INTERVAL_SERVO_MS) {
    tServoTerakhir = now;
    updateServo();
  }

  updateStatus(now);
  updateLed(now);

  if (status != statusSebelumnya) {
    kirimStatusChange(statusSebelumnya, status, now);
    statusSebelumnya = status;
  }

  updateWifi(now);
  updateMqtt(now);

  if (now - tTelemetriTerakhir >= INTERVAL_TELEMETRI_MS) {
    tTelemetriTerakhir = now;
    kirimTelemetri();
  }

  if (now - tHttpTerakhir >= HTTP_INTERVAL_MS) {
    tHttpTerakhir = now;
    kirimHttp();
  }

  bacaPerintahSerial();
}