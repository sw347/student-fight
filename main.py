from fastapi import FastAPI, File, UploadFile, Form
from fastapi.responses import JSONResponse
import uvicorn
from ultralytics import YOLO
from PIL import Image
import io
import cv2
import os
from datetime import datetime
import serial
import time


app = FastAPI()

UPLOAD_DIR = "uploads"
MAX_FILES = 5
os.makedirs(UPLOAD_DIR, exist_ok=True)

# === Arduino 연결 설정 ===
try:
    arduino = serial.Serial('COM4', 9600)
    time.sleep(2)  # Arduino가 준비될 때까지 대기
except Exception as e:
    print(f"[!] Arduino 연결 실패: {e}")
    arduino = None  # 연결 실패 시 None으로 설정

model = YOLO('runs/detect/train6/weights/best.pt')  # 사전 학습된 YOLOv8 모델

def clean_old_files():
    files = [os.path.join(UPLOAD_DIR, f) for f in os.listdir(UPLOAD_DIR)]
    # 파일들이 없으면 종료
    if len(files) <= MAX_FILES:
        return
    
    # 오래된 파일부터 삭제
    files.sort(key=lambda x: os.path.getmtime(x))
    while len(files) > MAX_FILES:
        os.remove(files[0])
        files.pop(0)

@app.post("/upload/")
async def upload_image(file: UploadFile = File(...), object_info: str = Form(...)):
    # 1) 업로드된 이미지 파일을 메모리에서 바로 열기
    contents = await file.read()
    image = Image.open(io.BytesIO(contents)).convert("RGB")

    # 2) YOLO detect 수행
    results = model(image)

     # 3) detect된 이미지에 박스 그리기 (OpenCV ndarray)
    img_with_boxes = results[0].plot()

    # 4) OpenCV -> PIL 변환
    img_with_boxes = cv2.cvtColor(img_with_boxes, cv2.COLOR_BGR2RGB)
    img_with_boxes_pil = Image.fromarray(img_with_boxes)

    # 5) 저장할 파일명: 타임스탬프 + 원본 파일명
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    save_filename = f"{timestamp}_{file.filename}"
    file_location = os.path.join(UPLOAD_DIR, save_filename)

    # 6) detect된 이미지 저장
    img_with_boxes_pil.save(file_location)

    clean_old_files()  # 10개 넘으면 오래된 파일 삭제

    detections = []
    fight_detected = False  # 싸움 감지 여부


    for box in results[0].boxes:
        cls = int(box.cls[0])
        conf = float(box.conf[0])
        detections.append({"class_id": cls, "confidence": round(conf, 3)})

        if cls == 1:  # 'fight' 클래스가 1번이라면
            fight_detected = True

              # === Arduino로 신호 보내기 ===
    if arduino is not None:
        try:
            if fight_detected:
                print("⚠️ 싸움 감지! Arduino에 '1' 전송")
                arduino.write(b'1')
            else:
                arduino.write(b'0')
        except Exception as e:
            print(f"[!] Arduino 전송 실패: {e}")

    return JSONResponse({
        "detections": detections,
        "object_info": object_info,
        "saved_file": file_location
    })

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
