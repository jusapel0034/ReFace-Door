import argparse
import json
import os
import pickle
import threading
import time

import cv2
from gpiozero import Button, OutputDevice
from picamera2 import Picamera2

try:
    import telebot
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("[WARN] telebot not installed. Telegram disabled.")

try:
    from RPLCD.i2c import CharLCD
    LCD_AVAILABLE = True
except ImportError:
    LCD_AVAILABLE = False
    print("[WARN] RPLCD not installed. LCD disabled.")


CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
MODEL_PATH = "face_model.yml"
LABEL_JSON_PATH = "label_map.json"
LABEL_PKL_PATH = "label_map.pkl"

TELEGRAM_BOT_TOKEN = "PUT_YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "PUT_YOUR_CHAT_ID_HERE"
TELEGRAM_COOLDOWN_SECONDS = 120
UNAUTHORIZED_REQUIRED_COUNT = 3


def parse_args():
    parser = argparse.ArgumentParser(description="Face recognition door lock system")

    parser.add_argument("--relay-pin", type=int, default=17,
                        help="BCM GPIO pin connected to relay IN")

    parser.add_argument("--limit-pin", type=int, default=27,
                        help="BCM GPIO pin connected to limit switch OUT")

    parser.add_argument("--confidence-threshold", type=float, default=50,
                        help="Lower is stricter. Try 40-60.")

    parser.add_argument("--cooldown-seconds", type=float, default=5,
                        help="Delay before another unlock can happen")

    parser.add_argument("--startup-delay", type=float, default=3,
                        help="Seconds to wait before allowing unlock after startup")

    parser.add_argument("--max-open-seconds", type=float, default=30,
                        help="Safety timeout if door is not closed")

    parser.add_argument("--width", type=int, default=640,
                        help="Camera width")

    parser.add_argument("--height", type=int, default=480,
                        help="Camera height")

    parser.add_argument("--dry-run", action="store_true",
                        help="Do not control relay, only print actions")

    parser.add_argument("--no-lcd", action="store_true",
                        help="Disable LCD display")

    parser.add_argument("--lcd-address", default="0x27",
                        help="I2C LCD address, usually 0x27")

    return parser.parse_args()


class TelegramNotifier:
    def __init__(self, token, chat_id, cooldown_seconds=120):
        self.token = token
        self.chat_id = chat_id
        self.cooldown_seconds = cooldown_seconds
        self.last_sent_time = 0
        self.enabled = False
        self.bot = None

        if not TELEGRAM_AVAILABLE:
            print("[TELEGRAM] Disabled: telebot not installed.")
            return

        if not self.token or self.token == "PUT_YOUR_BOT_TOKEN_HERE":
            print("[TELEGRAM] Disabled: bot token not set.")
            return

        if not self.chat_id or self.chat_id == "PUT_YOUR_CHAT_ID_HERE":
            print("[TELEGRAM] Disabled: chat ID not set.")
            return

        try:
            self.bot = telebot.TeleBot(self.token)
            self.enabled = True
            print("[TELEGRAM] Notifier initialized.")
        except Exception as e:
            print(f"[TELEGRAM] Failed to initialize: {e}")

    def send_unauthorized_alert(self, confidence=None):
        if not self.enabled:
            return

        now = time.time()
        if now - self.last_sent_time < self.cooldown_seconds:
            return

        self.last_sent_time = now

        message = "Unauthorized face detected!"
        if confidence is not None:
            message += f"\nConfidence: {confidence:.1f}"
        message += "\nPlease check the door."

        thread = threading.Thread(
            target=self._send_message,
            args=(message,),
            daemon=True
        )
        thread.start()

    def _send_message(self, message):
        try:
            self.bot.send_message(self.chat_id, message)
            print("[TELEGRAM] Unauthorized alert sent.")
        except Exception as e:
            print(f"[TELEGRAM] Failed to send alert: {e}")


class LCDDisplay:
    def __init__(self, enabled=True, address="0x27"):
        self.enabled = enabled and LCD_AVAILABLE
        self.lcd = None
        self.last_line1 = None
        self.last_line2 = None
        self.last_update = 0

        if not self.enabled:
            print("[LCD] Disabled")
            return

        try:
            self.lcd = CharLCD(
                i2c_expander="PCF8574",
                address=int(address, 16),
                port=1,
                cols=16,
                rows=2,
                dotsize=8,
                charmap="A00",
                auto_linebreaks=False,
                backlight_enabled=True
            )

            time.sleep(0.2)
            self.clear()
            self.message("System Ready", "Scan Face", force=True)
            print("[LCD] Initialized")

        except Exception as e:
            self.enabled = False
            self.lcd = None
            print(f"[LCD WARN] LCD disabled: {e}")

    def format_line(self, text):
        text = str(text)[:16]
        return text.ljust(16)

    def message(self, line1="", line2="", force=False):
        if not self.enabled or self.lcd is None:
            return

        line1 = self.format_line(line1)
        line2 = self.format_line(line2)

        if not force and line1 == self.last_line1 and line2 == self.last_line2:
            return

        now = time.time()
        if not force and now - self.last_update < 0.5:
            return

        try:
            self.lcd.cursor_pos = (0, 0)
            self.lcd.write_string(line1)
            self.lcd.cursor_pos = (1, 0)
            self.lcd.write_string(line2)

            self.last_line1 = line1
            self.last_line2 = line2
            self.last_update = now

        except Exception as e:
            print(f"[LCD WARN] Write failed: {e}")

    def clear(self):
        if not self.enabled or self.lcd is None:
            return

        try:
            self.lcd.clear()
            time.sleep(0.05)
            self.last_line1 = None
            self.last_line2 = None
        except Exception as e:
            print(f"[LCD WARN] Clear failed: {e}")

    def close(self):
        if not self.enabled or self.lcd is None:
            return

        try:
            self.lcd.clear()
            time.sleep(0.1)
            self.lcd.backlight_enabled = False
            time.sleep(0.1)
            self.lcd.close()
        except Exception:
            pass


class DoorRelay:
    def __init__(self, pin, dry_run=False):
        self.pin = pin
        self.dry_run = dry_run
        self.is_unlocked = False

        if self.dry_run:
            self.relay = None
            print("[RELAY] Dry-run mode. Relay disabled.")
            return

        self.relay = OutputDevice(
            pin,
            active_high=True,
            initial_value=False
        )
        self.relay.off()

        print(f"[RELAY] GPIO {pin} initialized")
        print("[RELAY] MODE=ACTIVE HIGH")
        print("[RELAY] OFF=LOW, ON=HIGH")

    def unlock(self):
        if self.is_unlocked:
            return

        if self.dry_run:
            print("[DRY RUN] Relay would turn ON")
            self.is_unlocked = True
            return

        self.relay.on()
        self.is_unlocked = True
        print("[DOOR] UNLOCKED - relay ON")

    def lock(self):
        if self.dry_run:
            if self.is_unlocked:
                print("[DRY RUN] Relay would turn OFF")
            self.is_unlocked = False
            return

        self.relay.off()

        if self.is_unlocked:
            print("[DOOR] LOCKED - relay OFF")

        self.is_unlocked = False

    def close(self):
        self.lock()
        if self.relay is not None:
            self.relay.close()


class DoorLimitSwitch:
    def __init__(self, pin):
        self.switch = Button(pin, pull_up=False, bounce_time=0.1)
        print(f"[LIMIT] GPIO {pin} initialized")
        print("[LIMIT] 3-wire module mode")
        print("[LIMIT] Pressed = door closed")

    @property
    def door_closed(self):
        return self.switch.is_pressed

    def close(self):
        self.switch.close()


def load_labels():
    if os.path.exists(LABEL_JSON_PATH):
        with open(LABEL_JSON_PATH, "r", encoding="utf-8") as file:
            raw_labels = json.load(file)
        return {int(label_id): name for label_id, name in raw_labels.items()}

    if os.path.exists(LABEL_PKL_PATH):
        with open(LABEL_PKL_PATH, "rb") as file:
            return pickle.load(file)

    print("[ERROR] No label map found. Run train_model.py first.")
    return None


def load_model():
    if not os.path.exists(MODEL_PATH):
        print("[ERROR] face_model.yml not found. Run train_model.py first.")
        return None, None

    labels = load_labels()
    if labels is None:
        return None, None

    if not hasattr(cv2, "face"):
        print("[ERROR] cv2.face not found.")
        print("[ERROR] Install OpenCV contrib or Raspberry Pi OpenCV with LBPH support.")
        return None, None

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(MODEL_PATH)

    print(f"[INFO] Model loaded. Authorized users: {list(labels.values())}")
    return recognizer, labels


def main():
    args = parse_args()

    telegram = TelegramNotifier(
        token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        cooldown_seconds=TELEGRAM_COOLDOWN_SECONDS
    )

    lcd = LCDDisplay(
        enabled=not args.no_lcd,
        address=args.lcd_address
    )

    relay = DoorRelay(
        pin=args.relay_pin,
        dry_run=args.dry_run
    )

    limit_switch = DoorLimitSwitch(args.limit_pin)

    relay.lock()
    lcd.message("System Ready", "Scan Face", force=True)

    recognizer, label_map = load_model()
    if recognizer is None:
        relay.close()
        limit_switch.close()
        lcd.close()
        return

    detector = cv2.CascadeClassifier(CASCADE_PATH)
    if detector.empty():
        print(f"[ERROR] Could not load Haar cascade: {CASCADE_PATH}")
        relay.close()
        limit_switch.close()
        lcd.close()
        return

    picam2 = Picamera2()
    picam2.configure(
        picam2.create_preview_configuration(
            main={"format": "RGB888", "size": (args.width, args.height)}
        )
    )

    picam2.start()
    time.sleep(2)

    started_at = time.time()
    last_unlock_time = 0
    door_open_since = 0
    waiting_for_door_close = False
    unauthorized_count = 0

    print("[INFO] System ready.")
    print("[INFO] Door stays unlocked until limit switch is pressed.")
    print("[INFO] Press q to quit.")

    try:
        while True:
            now = time.time()
            startup_ready = now - started_at >= args.startup_delay

            if waiting_for_door_close:
                if limit_switch.door_closed:
                    print("[LIMIT] Door closed detected.")
                    relay.lock()
                    lcd.message("Door Locked", "Scan Face", force=True)
                    waiting_for_door_close = False
                    last_unlock_time = now

                elif now - door_open_since >= args.max_open_seconds:
                    print("[SAFETY] Max open time reached. Locking door.")
                    relay.lock()
                    lcd.message("Timeout", "Door Locked", force=True)
                    waiting_for_door_close = False
                    last_unlock_time = now

            frame = picam2.capture_array()
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

            faces = detector.detectMultiScale(
                gray,
                scaleFactor=1.2,
                minNeighbors=5,
                minSize=(100, 100)
            )

            for (x, y, w, h) in faces:
                face_roi = gray[y:y + h, x:x + w]
                face_roi = cv2.resize(face_roi, (200, 200))

                label_id, confidence = recognizer.predict(face_roi)

                is_authorized = confidence <= args.confidence_threshold
                name = label_map.get(label_id, "Unknown") if is_authorized else "Unknown"

                color = (0, 255, 0) if is_authorized else (0, 0, 255)
                text = f"{name} ({confidence:.1f})"

                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                cv2.putText(
                    frame,
                    text,
                    (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    color,
                    2
                )

                if is_authorized:
                    unauthorized_count = 0
                    can_unlock = now - last_unlock_time >= args.cooldown_seconds

                    if not startup_ready:
                        print("[AUTH] Authorized face seen, but startup delay is active.")

                    elif can_unlock and not waiting_for_door_close:
                        print(f"[AUTH] Authorized: {name} confidence={confidence:.1f}")
                        relay.unlock()
                        lcd.message("Welcome", name, force=True)
                        waiting_for_door_close = True
                        door_open_since = now

                else:
                    unauthorized_count += 1
                    print(f"[AUTH] Unknown face confidence={confidence:.1f} count={unauthorized_count}/{UNAUTHORIZED_REQUIRED_COUNT}")

                    if unauthorized_count >= UNAUTHORIZED_REQUIRED_COUNT:
                        telegram.send_unauthorized_alert(confidence)
                        unauthorized_count = 0

            door_status = "UNLOCKED" if relay.is_unlocked else "LOCKED"
            status_color = (0, 255, 0) if relay.is_unlocked else (0, 0, 255)

            cv2.putText(
                frame,
                f"Door: {door_status}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                status_color,
                2
            )

            if waiting_for_door_close:
                cv2.putText(
                    frame,
                    "Waiting for door close",
                    (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255),
                    2
                )
            else:
                cv2.putText(
                    frame,
                    "Scan Face",
                    (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 255),
                    2
                )

            if not startup_ready:
                cv2.putText(
                    frame,
                    "Startup delay active",
                    (10, 90),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255),
                    2
                )

            cv2.imshow("Face Door Lock", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")

    finally:
        picam2.stop()
        cv2.destroyAllWindows()
        relay.close()
        limit_switch.close()
        lcd.close()
        print("[INFO] System stopped.")


if __name__ == "__main__":
    main()