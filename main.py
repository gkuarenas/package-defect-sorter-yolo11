import cv2
import serial
import time
import requests
from ultralytics import YOLO
import numpy as np

# SERIAL SETUP
try:
    arduino = serial.Serial('COM10', 115200)
    time.sleep(2)
except serial.SerialException as e:
    print(f"[ERROR] Could not open serial port: {e}")
    exit()

# YOLOv11n MODEL
model = YOLO("yolov11_gyom2.pt")

# STREAM URL
url = "http://192.168.43.76:81/stream"

try:
    stream = requests.get(url, stream=True, timeout=10)
except requests.exceptions.RequestException as e:
    print(f"[ERROR] Cannot connect to ESP32-CAM stream: {e}")
    exit()

bytes_data = b''
prev_state = "clear"
last_sent_time = time.time()
cooldown = 1.0  # seconds
hold_duration = 5.0  # seconds to hold the 'defect' state
defect_hold_until = 0  # future timestamp to keep holding defect state

# Size of the detection region
center_width, center_height = 270, 140

def is_defective(classes_detected):
    return any(cls in classes_detected for cls in ['hole', 'open', 'torn'])

def is_in_center(x1, y1, x2, y2, frame_w, frame_h):
    center_x = frame_w // 2
    center_y = frame_h // 2
    box_cx = (x1 + x2) / 2
    box_cy = (y1 + y2) / 2
    return (center_x - center_width // 2 <= box_cx <= center_x + center_width // 2) and \
           (center_y - center_height // 2 <= box_cy <= center_y + center_height // 2)

for chunk in stream.iter_content(chunk_size=1024):
    bytes_data += chunk
    a = bytes_data.find(b'\xff\xd8')
    b = bytes_data.find(b'\xff\xd9')

    if a == -1 or b == -1:
        continue

    jpg = bytes_data[a:b+2]
    bytes_data = bytes_data[b+2:]

    img_array = np.frombuffer(jpg, dtype=np.uint8)
    if img_array.size == 0:
        continue

    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        continue

    frame_h, frame_w = img.shape[:2]
    valid_classes = []

    # Run YOLO inference
    results = model(img)[0]

    # Draw boxes that are inside center area
    for box, cls in zip(results.boxes.xyxy, results.boxes.cls):
        x1, y1, x2, y2 = map(int, box.tolist())
        label = model.names[int(cls)]

        if is_in_center(x1, y1, x2, y2, frame_w, frame_h):
            valid_classes.append(label)
            color = (0, 255, 0) if label in ['sealed', 'box'] else (0, 0, 255)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    # Draw center zone for reference
    center_x = frame_w // 2
    center_y = frame_h // 2
    cv2.rectangle(
        img,
        (center_x - center_width // 2, center_y - center_height // 2),
        (center_x + center_width // 2, center_y + center_height // 2),
        (255, 255, 0), 2
    )

    # Servo logic with cooldown + 5s hold
    current_time = time.time()
    box_detected = 'box' in valid_classes

    if box_detected:
        if is_defective(valid_classes) and prev_state != "defect":
            try:
                arduino.flush()
                arduino.write(b"defect\n")
                prev_state = "defect"
                defect_hold_until = current_time + hold_duration
                last_sent_time = current_time
                print("[INFO] Sent: defect")
            except serial.SerialException as e:
                print(f"[ERROR] Serial write failed: {e}")

        elif not is_defective(valid_classes) and prev_state != "clear":
            try:
                arduino.flush()
                arduino.write(b"clear\n")
                prev_state = "clear"
                last_sent_time = current_time
                print("[INFO] Sent: clear")
            except serial.SerialException as e:
                print(f"[ERROR] Serial write failed: {e}")

    elif not box_detected:
        if prev_state == "defect" and current_time < defect_hold_until:
            # Still holding the defect state — do nothing
            pass
        elif prev_state == "defect" and current_time >= defect_hold_until:
            try:
                arduino.flush()
                arduino.write(b"clear\n")
                prev_state = "clear"
                last_sent_time = current_time
                print("[INFO] Reset after hold: Sent clear")
            except serial.SerialException as e:
                print(f"[ERROR] Serial write failed: {e}")

    # Display
    cv2.imshow("ESP32-CAM Stream", img)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cv2.destroyAllWindows()
arduino.close()
