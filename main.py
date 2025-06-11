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

app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static") # <-- 추가

try:
    cred = credentials.Certificate(service_account_key_path)
    firebase_admin.initialize_app(cred)
    print("[+] Firebase Admin SDK 초기화 성공")
except Exception as e:
    print(f"[!] Firebase Admin SDK 초기화 실패: {e}")
    # 초기화 실패 시 앱 종료 또는 FCM 기능 비활성화 고려
    # raise e # 앱 시작을 막고 싶다면 주석 해제

# === Arduino 연결 설정 ===
try:
    arduino = serial.Serial('COM9', 9600)
    time.sleep(2)  # Arduino가 준비될 때까지 대기
except Exception as e:
    print(f"[!] Arduino 연결 실패: {e}")
    arduino = None  # 연결 실패 시 None으로 설정

model = YOLO(r'C:\Users\swlee\workspace\student-fight\runs\best.pt')  # 사전 학습된 YOLOv8 모델

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

        if cls == 1 and conf >= 0.45:  # 'fight' 클래스가 1번이라면
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
        "saved_file": save_filename, # 저장된 파일의 이름만 반환 (URL은 클라이언트가 조합)
        "created_at": datetime.now().isoformat() # 이미지 저장 시간 추가
    })
    
@app.post("/register_token")
def register_token(token: str = Form(...)):
    # 토큰 유효성 검사 (선택 사항)
    if not token or not isinstance(token, str) or len(token) < 10: # 기본적인 길이 체크
        raise HTTPException(status_code=400, detail="Invalid token provided.")

    tokens = get_all_device_tokens()
    if token not in tokens: # 중복 방지
        with open("tokens.txt", "a") as f:
            f.write(token + "\n")
        print(f"[+] 새 디바이스 토큰 등록: {token}")
        return {"status": "success", "message": "Token registered"}
    else:
        print(f"[-] 토큰 이미 등록됨: {token}")
        return {"status": "info", "message": "Token already registered"}

@app.get("/latest-image")
def get_latest_image():
    files_in_uploads = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))]
    
    if files_in_uploads:
        latest_file = max(files_in_uploads, key=lambda f: os.path.getmtime(os.path.join(UPLOAD_DIR, f)))
        
        created_at_timestamp = os.path.getmtime(os.path.join(UPLOAD_DIR, latest_file))
        created_at_datetime = datetime.fromtimestamp(created_at_timestamp)

        return JSONResponse({
            "image_url": f"http://10.56.148.13:8000/static/{latest_file}",
            "created_at": created_at_datetime.isoformat()
        })
    return JSONResponse({"image_url": None, "created_at": None})


@app.get("/all-detected-images") # 모든 감지된 이미지 목록을 반환하는 새로운 엔드포인트
async def get_all_detected_images():
    files = sorted(os.listdir(UPLOAD_DIR), key=lambda x: os.path.getmtime(os.path.join(UPLOAD_DIR, x)), reverse=True)
    
    image_list = []
    server_ip = "10.56.148.13" # 서버 IP 사용

    for file_name in files:
        file_path = os.path.join(UPLOAD_DIR, file_name)
        if os.path.isfile(file_path):
            created_at_timestamp = os.path.getmtime(file_path)
            created_at_datetime = datetime.fromtimestamp(created_at_timestamp)
            image_list.append({
                "image_url": f"http://{server_ip}:8000/static/{file_name}",
                "created_at": created_at_datetime.isoformat()
            })
            
    return JSONResponse(image_list)

# ... (기존 코드 하단 생략) ...

if __name__ == "__main__":  
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
