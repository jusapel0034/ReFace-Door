from __future__ import annotations

import argparse
import pickle

import cv2
import numpy as np

from face_lock_common import BASE_DIR, DATA_DIR, LABELS_PATH, MODEL_PATH, ensure_dirs, create_recognizer, save_labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the LBPH face recognition model.")
    parser.add_argument("--min-images", type=int, default=20, help="Minimum images required per person")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dirs()

    faces = []
    ids = []
    labels: dict[int, str] = {}

    people = sorted(path for path in DATA_DIR.iterdir() if path.is_dir())
    if not people:
        raise RuntimeError(f"No registered faces found in {DATA_DIR}")

    for label_id, person_dir in enumerate(people):
        image_paths = sorted(person_dir.glob("*.jpg"))
        if len(image_paths) < args.min_images:
            print(
                f"Skipping {person_dir.name}: only {len(image_paths)} images "
                f"(need at least {args.min_images})."
            )
            continue

        labels[label_id] = person_dir.name
        for image_path in image_paths:
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                print(f"Skipping unreadable image: {image_path}")
                continue
            image = cv2.resize(image, (200, 200))
            faces.append(image)
            ids.append(label_id)

    if not faces:
        raise RuntimeError("No usable training images found.")

    recognizer = create_recognizer()
    recognizer.train(faces, np.array(ids))
    recognizer.write(str(MODEL_PATH))
    save_labels(labels)

    with (BASE_DIR / "label_map.pkl").open("wb") as file:
        pickle.dump(labels, file)

    print(f"Trained model with {len(faces)} images for {len(labels)} people.")
    print(f"Model saved to {MODEL_PATH}")
    print(f"Labels saved to {LABELS_PATH}")


if __name__ == "__main__":
    main()
