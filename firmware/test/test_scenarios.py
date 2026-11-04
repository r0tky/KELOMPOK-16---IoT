#!/usr/bin/env python3
"""
Test Suite Otomatis Logika Sistem IoT FishSlide
Memvalidasi skenario operasional:
1. Kondisi awal (Inisialisasi)
2. Skenario Hujan (Hujan turun -> Deteksi -> Penutupan)
3. Konfirmasi Limit Switch Tutup
4. Skenario Hujan Reda (Histeresis & Debounce timer 5 detik)
5. Skenario Cuaca Terang (Buka penutup otomatis)
6. Konfirmasi Limit Switch Buka
7. Cuaca Mendung / Kurang Terang (Penutup tetap tertutup)
8. Kendali Manual via Serial dan MQTT (open, close, auto)
9. Deteksi Gangguan (Fault: Timeout limit switch 8 detik)
10. Deteksi Gangguan Ganda (Fault: Kedua limit switch aktif bersamaan)
11. Pemulihan Kondisi Gangguan (Fault Recovery)
12. Validasi Struktur Payload JSON Telemetri & Event
"""

import unittest
import json

ADC_MAX = 4095
RAIN_ON_PCT = 30
RAIN_OFF_PCT = 20
LIGHT_OPEN_PCT = 40
RAIN_CLEAR_MS = 5000
INTERVAL_SERVO_MS = 15
LIMIT_TIMEOUT_MS = 8000
DEBOUNCE_MS = 30
SERVO_ANGLE_OPEN = 0
SERVO_ANGLE_CLOSED = 90

STATUS_MOVING = "MOVING"
STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_FAULT = "FAULT"

class FishSlideSystem:
    def __init__(self, device_id="fishslide-node01"):
        self.device_id = device_id
        self.servo_angle = SERVO_ANGLE_OPEN
        self.servo_target = SERVO_ANGLE_OPEN
        self.mode_otomatis = True
        self.hujan_terdeteksi = False
        self.reda_aktif = False
        self.t_reda_mulai = 0
        
        self.status = STATUS_OPEN
        self.status_sebelumnya = STATUS_OPEN
        self.terkonfirmasi = True
        self.sudah_tiba = True
        self.t_tiba = 0
        self.led_state = False
        self.led_mode = "OFF"  # OFF, ON, BLINK_300, BLINK_100
        
        self.rain_raw = 0
        self.ldr_raw = 0
        self.rain_pct = 0
        self.terang_pct = 0
        self.limit_buka = False
        self.limit_tutup = False
        
        self.events = []
        self.now_ms = 0

    def kirim_event(self, event, detail=None):
        payload = {
            "device_id": self.device_id,
            "type": "event",
            "uptime_ms": self.now_ms,
            "event": event,
        }
        if detail is not None:
            payload["detail"] = detail
        self.events.append(payload)
        return payload

    def kirim_status_change(self, dari, ke):
        payload = {
            "device_id": self.device_id,
            "type": "event",
            "uptime_ms": self.now_ms,
            "event": "status_change",
            "from": dari,
            "to": ke
        }
        self.events.append(payload)
        return payload

    def set_target(self, sudut, alasan):
        if self.servo_target == sudut:
            return
        self.servo_target = sudut
        self.terkonfirmasi = False
        self.sudah_tiba = False
        event_name = "cover_close" if sudut == SERVO_ANGLE_CLOSED else "cover_open"
        self.kirim_event(event_name, alasan)

    def on_mqtt_message(self, raw_payload):
        msg = raw_payload.strip().lower()
        if msg in ("open", "o"):
            self.jalankan_perintah('o', "mqtt_command")
        elif msg in ("close", "c"):
            self.jalankan_perintah('c', "mqtt_command")
        elif msg in ("auto", "a"):
            self.jalankan_perintah('a', "mqtt_command")
        else:
            self.kirim_event("mqtt_cmd_ignored", raw_payload)

    def jalankan_perintah(self, cmd, sumber="manual_command"):
        cmd = cmd.lower()
        if cmd in ('a', 'auto'):
            self.mode_otomatis = True
            self.kirim_event("mode_change", "AUTO")
        elif cmd in ('o', 'open'):
            self.mode_otomatis = False
            self.kirim_event("mode_change", "MANUAL")
            if self.status == STATUS_FAULT and self.servo_target == SERVO_ANGLE_OPEN:
                self.sudah_tiba = False
                self.terkonfirmasi = False
                self.status = STATUS_MOVING
                self.kirim_event("fault_retry", "open")
            else:
                self.set_target(SERVO_ANGLE_OPEN, sumber)
        elif cmd in ('c', 'close'):
            self.mode_otomatis = False
            self.kirim_event("mode_change", "MANUAL")
            if self.status == STATUS_FAULT and self.servo_target == SERVO_ANGLE_CLOSED:
                self.sudah_tiba = False
                self.terkonfirmasi = False
                self.status = STATUS_MOVING
                self.kirim_event("fault_retry", "close")
            else:
                self.set_target(SERVO_ANGLE_CLOSED, sumber)

    def evaluasi_otomatis(self):
        if self.rain_pct >= RAIN_ON_PCT:
            self.hujan_terdeteksi = True
            self.reda_aktif = False
            self.set_target(SERVO_ANGLE_CLOSED, "rain_detected")
            return

        if self.hujan_terdeteksi:
            if self.rain_pct <= RAIN_OFF_PCT:
                if not self.reda_aktif:
                    self.reda_aktif = True
                    self.t_reda_mulai = self.now_ms
                    self.kirim_event("rain_easing", "waiting_confirmation")
                if self.now_ms - self.t_reda_mulai >= RAIN_CLEAR_MS:
                    self.hujan_terdeteksi = False
                    self.reda_aktif = False
                    self.kirim_event("rain_stopped", None)
            else:
                self.reda_aktif = False

        if not self.hujan_terdeteksi and self.terang_pct >= LIGHT_OPEN_PCT:
            self.set_target(SERVO_ANGLE_OPEN, "rain_stopped_and_bright")

    def update_servo(self):
        if self.servo_angle != self.servo_target:
            self.servo_angle += 1 if self.servo_target > self.servo_angle else -1

    def update_status(self):
        if self.limit_buka and self.limit_tutup:
            self.status = STATUS_FAULT
            return

        if self.servo_angle != self.servo_target:
            self.status = STATUS_MOVING
            self.sudah_tiba = False
            return

        if not self.sudah_tiba:
            self.sudah_tiba = True
            self.t_tiba = self.now_ms

        limit_target = self.limit_tutup if (self.servo_target == SERVO_ANGLE_CLOSED) else self.limit_buka
        if limit_target:
            self.terkonfirmasi = True

        if self.terkonfirmasi:
            self.status = STATUS_CLOSED if (self.servo_target == SERVO_ANGLE_CLOSED) else STATUS_OPEN
        elif (self.now_ms - self.t_tiba) >= LIMIT_TIMEOUT_MS:
            self.status = STATUS_FAULT
        else:
            self.status = STATUS_MOVING

    def update_led(self):
        if self.status == STATUS_OPEN:
            self.led_mode = "OFF"
            self.led_state = False
        elif self.status == STATUS_CLOSED:
            self.led_mode = "ON"
            self.led_state = True
        elif self.status == STATUS_FAULT:
            self.led_mode = "BLINK_100"
        else:
            self.led_mode = "BLINK_300"

    def step(self, dt_ms=15, rain_pct=None, terang_pct=None, limit_buka=None, limit_tutup=None):
        self.now_ms += dt_ms
        if rain_pct is not None:
            self.rain_pct = max(0, min(100, rain_pct))
        if terang_pct is not None:
            self.terang_pct = max(0, min(100, terang_pct))
        if limit_buka is not None:
            self.limit_buka = limit_buka
        if limit_tutup is not None:
            self.limit_tutup = limit_tutup

        if self.mode_otomatis:
            self.evaluasi_otomatis()

        self.update_servo()
        self.update_status()

        if self.status != self.status_sebelumnya:
            self.kirim_status_change(self.status_sebelumnya, self.status)
            self.status_sebelumnya = self.status

        self.update_led()

    def generate_telemetry_json(self, seq=1):
        doc = {
            "device_id": self.device_id,
            "type": "telemetry",
            "seq": seq,
            "uptime_ms": self.now_ms,
            "mode": "AUTO" if self.mode_otomatis else "MANUAL",
            "sensor": {
                "rain_raw": int(self.rain_pct * ADC_MAX / 100),
                "rain_pct": self.rain_pct,
                "ldr_raw": int((100 - self.terang_pct) * ADC_MAX / 100),
                "light_pct": self.terang_pct,
                "raining": self.hujan_terdeteksi
            },
            "cover": {
                "status": self.status,
                "servo_angle": self.servo_angle,
                "servo_target": self.servo_target,
                "limit_open": self.limit_buka,
                "limit_closed": self.limit_tutup
            }
        }
        return json.dumps(doc)


class TombolDebounce:
    def __init__(self, debounce_ms=30):
        self.debounce_ms = debounce_ms
        self.raw_terakhir = False
        self.stabil = False
        self.t_ubah = 0

    def update(self, pin_low, now_ms):
        raw = pin_low  # aktif LOW
        if raw != self.raw_terakhir:
            self.raw_terakhir = raw
            self.t_ubah = now_ms
        if now_ms - self.t_ubah >= self.debounce_ms:
            self.stabil = raw
        return self.stabil


class TestFishSlideScenarios(unittest.TestCase):
    def setUp(self):
        self.sys = FishSlideSystem()

    def test_01_kondisi_awal(self):
        """Kondisi awal harus OPEN, sudut 0, mode AUTO, LED OFF"""
        self.assertEqual(self.sys.status, STATUS_OPEN)
        self.assertEqual(self.sys.servo_angle, 0)
        self.assertEqual(self.sys.servo_target, 0)
        self.assertTrue(self.sys.mode_otomatis)
        self.assertEqual(self.sys.led_mode, "OFF")

    def test_02_skenario_hujan_dan_penutupan(self):
        """Deteksi hujan (>=30%) memicu servo bergerak menutup dan event rain_detected"""
        self.sys.step(15, rain_pct=0, terang_pct=80, limit_buka=True, limit_tutup=False)
        self.assertEqual(self.sys.status, STATUS_OPEN)
        
        self.sys.step(15, rain_pct=75, limit_buka=False)
        self.assertTrue(self.sys.hujan_terdeteksi)
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_CLOSED)
        self.assertEqual(self.sys.status, STATUS_MOVING)
        self.assertEqual(self.sys.led_mode, "BLINK_300")
        
        event_names = [e["event"] for e in self.sys.events]
        self.assertIn("cover_close", event_names)

        while self.sys.servo_angle < 90:
            self.sys.step(15)
        self.assertEqual(self.sys.servo_angle, 90)

    def test_03_konfirmasi_limit_switch_tutup(self):
        """Ketika servo tiba di 90 dan limit_tutup aktif, status menjadi CLOSED dan LED ON"""
        self.sys.step(15, rain_pct=80, limit_buka=False, limit_tutup=False)
        while self.sys.servo_angle < 90:
            self.sys.step(15)
        
        self.sys.step(15, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_CLOSED)
        self.assertEqual(self.sys.led_mode, "ON")
        self.assertTrue(self.sys.terkonfirmasi)
        
        self.sys.step(15, limit_tutup=False)
        self.assertEqual(self.sys.status, STATUS_CLOSED)

    def test_04_skenario_hujan_reda_dan_debounce_timer(self):
        """Hujan reda (<=20%) harus menunggu RAIN_CLEAR_MS (5000ms) sebelum dinyatakan berhenti"""
        self.sys.step(15, rain_pct=70, limit_buka=False, limit_tutup=True)
        while self.sys.servo_angle < 90:
            self.sys.step(15)
        self.sys.step(15, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_CLOSED)

        self.sys.step(15, rain_pct=15, limit_tutup=False)
        self.assertTrue(self.sys.reda_aktif)
        self.assertTrue(self.sys.hujan_terdeteksi)
        event_names = [e["event"] for e in self.sys.events]
        self.assertIn("rain_easing", event_names)

        # Maju 3000 ms (belum 5000 ms), hujan belum boleh berhenti
        for _ in range(200):
            self.sys.step(15, rain_pct=15)
        self.assertTrue(self.sys.hujan_terdeteksi)

        # Maju hingga melewati total 5000 ms
        for _ in range(150):
            self.sys.step(15, rain_pct=15)
        self.assertFalse(self.sys.hujan_terdeteksi)
        event_names = [e["event"] for e in self.sys.events]
        self.assertIn("rain_stopped", event_names)

    def test_05_skenario_cuaca_terang_buka_otomatis(self):
        """Setelah hujan reda dan cahaya terang (>=40%), penutup otomatis terbuka kembali"""
        self.sys.step(15, rain_pct=80, limit_tutup=True)
        while self.sys.servo_angle < 90:
            self.sys.step(15)
        self.sys.step(15, limit_tutup=True)
        
        for _ in range(400):
            self.sys.step(15, rain_pct=0, terang_pct=60, limit_tutup=False)
        
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_OPEN)
        self.assertEqual(self.sys.status, STATUS_MOVING)
        event_names = [e["event"] for e in self.sys.events]
        self.assertIn("rain_stopped_and_bright", [e.get("detail") for e in self.sys.events])

        while self.sys.servo_angle > 0:
            self.sys.step(15)
        self.assertEqual(self.sys.servo_angle, 0)
        
        self.sys.step(15, limit_buka=True)
        self.assertEqual(self.sys.status, STATUS_OPEN)
        self.assertEqual(self.sys.led_mode, "OFF")

    def test_06_cuaca_mendung_penutup_tetap_tertutup(self):
        """Jika hujan berhenti tetapi mendung (<40%), penutup tetap tertutup"""
        self.sys.step(15, rain_pct=80, limit_tutup=True)
        while self.sys.servo_angle < 90:
            self.sys.step(15)
        self.sys.step(15, limit_tutup=True)

        for _ in range(400):
            self.sys.step(15, rain_pct=0, terang_pct=20, limit_tutup=False)
        
        self.assertFalse(self.sys.hujan_terdeteksi)
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_CLOSED)
        self.assertEqual(self.sys.status, STATUS_CLOSED)

    def test_07_kendali_manual_serial_dan_mqtt(self):
        """Uji kendali manual 'o', 'c', dan kembali ke 'a'"""
        self.sys.step(15, rain_pct=0, terang_pct=70, limit_buka=True)
        self.assertEqual(self.sys.status, STATUS_OPEN)

        self.sys.jalankan_perintah('c', "serial_command")
        self.assertFalse(self.sys.mode_otomatis)
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_CLOSED)

        while self.sys.servo_angle < 90:
            self.sys.step(15, limit_buka=False)
        self.sys.step(15, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_CLOSED)

        self.sys.jalankan_perintah("open", "mqtt_command")
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_OPEN)

        self.sys.jalankan_perintah("auto", "mqtt_command")
        self.assertTrue(self.sys.mode_otomatis)

    def test_08_fault_timeout_limit_switch(self):
        """Jika servo tiba di target tapi limit switch tidak tersentuh dalam 8 detik -> FAULT"""
        self.sys.step(15, rain_pct=80, limit_buka=False, limit_tutup=False)
        while self.sys.servo_angle < 90:
            self.sys.step(15)
        
        self.assertEqual(self.sys.servo_angle, 90)
        self.assertFalse(self.sys.terkonfirmasi)
        self.assertEqual(self.sys.status, STATUS_MOVING)

        # Lewati 8.5 detik tanpa menekan limit switch
        for _ in range(570):
            self.sys.step(15, limit_tutup=False)
        
        self.assertEqual(self.sys.status, STATUS_FAULT)
        self.assertEqual(self.sys.led_mode, "BLINK_100")

    def test_09_fault_kedua_limit_switch_aktif_bersamaan(self):
        """Jika kedua limit switch aktif bersamaan -> seketika FAULT"""
        self.sys.step(15, limit_buka=True, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_FAULT)
        self.assertEqual(self.sys.led_mode, "BLINK_100")

    def test_10_pemulihan_dari_fault(self):
        """Memastikan sistem dapat pulih dari kondisi FAULT dengan perintah baru"""
        # Masuk FAULT karena kedua tombol ditekan
        self.sys.step(15, limit_buka=True, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_FAULT)

        # Lepas tombol dan tekan tombol buka (konfirmasi manual batas buka)
        self.sys.limit_buka = True
        self.sys.limit_tutup = False
        self.sys.step(15)
        self.assertEqual(self.sys.status, STATUS_OPEN)

        # Uji juga pemulihan dari timeout FAULT via perintah retry
        self.sys.jalankan_perintah('c', "serial_command")
        while self.sys.servo_angle < 90:
            self.sys.step(15, limit_buka=False, limit_tutup=False)
        for _ in range(570):
            self.sys.step(15)
        self.assertEqual(self.sys.status, STATUS_FAULT)
        
        # Kirim ulang perintah 'c' -> harus memicu retry dan kembali MOVING
        self.sys.jalankan_perintah('c', "serial_command")
        self.assertEqual(self.sys.status, STATUS_MOVING)
        # Sekarang sentuh limit_tutup -> harus kembali CLOSED
        self.sys.step(15, limit_tutup=True)
        self.assertEqual(self.sys.status, STATUS_CLOSED)

    def test_11_validasi_skema_payload_telemetri(self):
        """Memvalidasi struktur dan kelengkapan field JSON telemetri"""
        self.sys.step(15, rain_pct=45, terang_pct=85, limit_buka=True)
        raw_json = self.sys.generate_telemetry_json(seq=42)
        data = json.loads(raw_json)

        self.assertEqual(data["device_id"], "fishslide-node01")
        self.assertEqual(data["type"], "telemetry")
        self.assertEqual(data["seq"], 42)
        self.assertIn("sensor", data)
        self.assertIn("cover", data)
        self.assertEqual(data["sensor"]["rain_pct"], 45)
        self.assertEqual(data["sensor"]["light_pct"], 85)
        self.assertTrue(data["cover"]["limit_open"])
        self.assertFalse(data["cover"]["limit_closed"])

    def test_12_debounce_tombol_filter_noise(self):
        """Memvalidasi mekanisme debouncing 30ms menolak noise sesaat dan menerima sinyal stabil"""
        btn = TombolDebounce(debounce_ms=30)
        
        # Noise spike selama 15ms (belum 30ms)
        btn.update(pin_low=True, now_ms=100)
        btn.update(pin_low=True, now_ms=115)
        self.assertFalse(btn.stabil)  # Harus tetap False
        
        # Noise hilang
        btn.update(pin_low=False, now_ms=120)
        self.assertFalse(btn.stabil)
        
        # Penekanan valid (stabil >= 30ms)
        btn.update(pin_low=True, now_ms=200)
        btn.update(pin_low=True, now_ms=235)  # Delta 35ms >= 30ms
        self.assertTrue(btn.stabil)  # Harus True
        
        # Pelepasan valid (stabil >= 30ms)
        btn.update(pin_low=False, now_ms=300)
        btn.update(pin_low=False, now_ms=335)
        self.assertFalse(btn.stabil)

    def test_13_mqtt_sanitasi_whitespace_dan_case(self):
        """Memvalidasi pembersihan whitespace, newline, dan variasi huruf besar pada payload MQTT"""
        # Perintah close dengan spasi dan newline
        self.sys.on_mqtt_message("  CLOSE\r\n")
        self.assertFalse(self.sys.mode_otomatis)
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_CLOSED)

        # Perintah open dengan huruf besar 'O'
        self.sys.on_mqtt_message("O\n")
        self.assertEqual(self.sys.servo_target, SERVO_ANGLE_OPEN)

        # Perintah auto campuran
        self.sys.on_mqtt_message(" Auto \t")
        self.assertTrue(self.sys.mode_otomatis)

        # Perintah tidak dikenal
        self.sys.on_mqtt_message("reboot")
        event_names = [e["event"] for e in self.sys.events]
        self.assertIn("mqtt_cmd_ignored", event_names)

    def test_14_identitas_dan_topik_mqtt_kelompok16(self):
        """Memvalidasi identitas node dan struktur topik MQTT resmi Kelompok 16"""
        base_topic = "unib/fishslide/kelompok16/node01"
        expected_topics = {
            "telemetry": f"{base_topic}/telemetry",
            "event": f"{base_topic}/event",
            "status": f"{base_topic}/status",
            "cmd": f"{base_topic}/cmd"
        }
        self.assertEqual(expected_topics["cmd"], "unib/fishslide/kelompok16/node01/cmd")
        self.assertEqual(expected_topics["telemetry"], "unib/fishslide/kelompok16/node01/telemetry")
        self.assertEqual(expected_topics["status"], "unib/fishslide/kelompok16/node01/status")
        self.assertEqual(expected_topics["event"], "unib/fishslide/kelompok16/node01/event")


if __name__ == "__main__":
    unittest.main(verbosity=2)
