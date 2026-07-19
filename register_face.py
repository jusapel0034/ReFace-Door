from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

import cv2

from face_lock_common import CameraStream, DATA_DIR, ensure_dirs, load_face_detector


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
    if not cleaned:
        raise ValueError("Name must include at least one letter or number.")
    return cleaned


def next_image_index(person_dir: Path) -> int:
    indexes = []
    for image_path in person_dir.glob("*.jpg"):
        try:
            indexes.append(int(image_path.stem))
        except ValueError:
            continue
    return max(indexes, default=0) + 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register face images for one person.")
    parser.add_argument("--name", required=True, help="Person name, e.g. Alice")
    parser.add_argument("--samples", type=int, default=100, help="Number of face samples to capture")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--width", type=int, default=640, help="Camera frame width")
    parser.add_argument("--height", type=int, default=480, help="Camera frame height")
    parser.add_argument("--opencv-camera", action="store_true", help="Use OpenCV VideoCapture instead of Picamera2")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    person_name = safe_name(args.name)

    ensure_dirs()
    person_dir = DATA_DIR / person_name
    person_dir.mkdir(parents=True, exist_ok=True)

    detector = load_face_detector()
    camera = CameraStream(args.camera, args.width, args.height, use_picamera=not args.opencv_camera)
    camera.start()

    saved_count = 0
    image_index = next_image_index(person_dir)
    last_saved_at = 0.0

    print(f"Registering {person_name}. Press q to stop early.")

    try:
        while saved_count < args.samples:
            ok, frame, gray_conversion = camera.read()
            if not ok:
                print("Could not read frame from camera.")
                break

            gray = cv2.cvtColor(frame, gray_conversion)
            faces = detector.detectMultiScale(
                gray,
                scaleFactor=1.2,
                minNeighbors=5,
                minSize=(80, 80),
            )

            for x, y, w, h in faces:
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

                now = time.time()
                if now - last_saved_at < 0.15:
                    continue

                face_img = gray[y : y + h, x : x + w]
                face_img = cv2.resize(face_img, (200, 200))
                image_path = person_dir / f"{image_index}.jpg"
                cv2.imwrite(str(image_path), face_img)

                saved_count += 1
                image_index += 1
                last_saved_at = now
                print(f"Saved {saved_count}/{args.samples}: {image_path}")

                if saved_count >= args.samples:
                    break

            cv2.putText(
                frame,
                f"{person_name}: {saved_count}/{args.samples}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
            )
            cv2.imshow("Register Face", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        camera.stop()
        cv2.destroyAllWindows()

    print(f"Done. Captured {saved_count} new samples for {person_name}.")


if __name__ == "__main__":
    main()
