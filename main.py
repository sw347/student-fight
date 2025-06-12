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
from fastapi.staticfiles import StaticFiles

# Firebase Admin SDK 관련 임포트
import firebase_admin
from firebase_admin import credentials
from firebase_admin import messaging # FCM 메시징 서비스

app = FastAPI()

app.mount("/saving", StaticFiles(directory="saving"), name="saving")

UPLOAD_DIR = "uploads"
SAVING_DIR = "saving"
MAX_FILES = 5

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SAVING_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static") # <-- 추가

service_account_key_path = "serviceAccountKey.json"

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
    print(f"[!] Arduino 연결 성고")
    arduino = serial.Serial('COM3', 9600)
    time.sleep(2)  # Arduino가 준비될 때까지 대기
except Exception as e:
    print(f"[!] Arduino 연결 실패: {e}")
    arduino = None  # 연결 실패 시 None으로 설정

model = YOLO(r'D:\student-fight\runs\best.pt')  # 사전 학습된 YOLOv8 모델

# 이전에 등록된 모든 디바이스 토큰을 저장할 리스트 (실제 앱에서는 데이터베이스 사용을 강력 권장)
# 간단한 예시를 위해 파일로 관리합니다.
def get_all_device_tokens():
    tokens = []
    if os.path.exists("tokens.txt"):
        with open("tokens.txt", "r") as f:
            for line in f:
                token = line.strip()
                if token:
                    tokens.append(token)
    return list(set(tokens)) # 중복 제거하여 반환

# FCM 알림을 전송하는 함수
async def send_fcm_notification(title: str, body: str, image_url: str = None):
    tokens = get_all_device_tokens()
    if not tokens:
        print("[!] 등록된 디바이스 토큰이 없습니다. FCM 알림을 보낼 수 없습니다.")
        return

    # FCM 메시지 구성
    # MulticastMessage는 여러 디바이스에 동시에 알림을 보낼 때 사용합니다.
    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
            # image=image_url, # 알림 본문에 이미지 URL 포함 (Android 10+에서 지원)
        ),
        data={ # 앱에서 처리할 수 있는 사용자 정의 데이터 (핵심)
            "click_action": "FLUTTER_NOTIFICATION_CLICK", # Flutter FCM 플러그인에서 사용 (필요시)
            "image_url": image_url if image_url else "", # 이미지 URL을 데이터로 전송
            "event_type": "violence_detected", # 앱에서 특정 동작을 수행할 때 사용
            "timestamp": datetime.now().isoformat() # 알림 발생 시간 추가
        },
        tokens=tokens, # 등록된 모든 디바이스 토큰 리스트
    )

    try:
        # 알림 전송
        response = await messaging.send_multicast(message)
        print(f"[+] FCM 알림 전송 결과: {response.success_count} 성공, {response.failure_count} 실패")

        # 실패한 경우 상세 정보 출력 (디버깅용)
        if response.failure_count > 0:
            for resp in response.responses:
                if not resp.success:
                    print(f"    [!] Failed to send to token: {resp.exception}")
    except Exception as e:
        print(f"[!] FCM 알림 전송 실패: {e}")


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
            
            # fight 감지된 경우, saving 폴더에 따로 저장
            save_path_fight = os.path.join(SAVING_DIR, save_filename)
            img_with_boxes_pil.save(save_path_fight)
            print(f"✅ fight 이미지 저장 완료: {save_path_fight}")

              # === Arduino로 신호 보내기 ===
    if arduino is not None:
        server_ip = "10.56.148.13" # <--- 실제 서버 IP 주소로 변경

        try:
            if fight_detected:
                print("⚠️ 싸움 감지! Arduino에 '1' 전송")
                arduino.write(b'1')
            else:
                arduino.write(b'0')
        except Exception as e:
            print(f"[!] Arduino 전송 실패: {e}")

    if fight_detected:
        # 알림에 포함할 이미지 URL (클라이언트가 이 URL을 통해 이미지를 로드)
        server_ip = "10.56.148.13" # <--- 실제 서버 IP 주소로 변경
        fcm_image_url = f"http://{server_ip}:8000/saving/{save_filename}"
        
        await send_fcm_notification(
            title="학교 폭력 감지 알림!",
            body="교내에서 싸움 활동이 감지되었습니다. 앱에서 즉시 확인하세요.",
            image_url=fcm_image_url # 알림 데이터로 이미지 URL 전달
        )

    return JSONResponse({
        "detections": detections,
        "object_info": object_info,
        "saved_file": save_filename, # 저장된 파일의 이름만 반환 (URL은 클라이언트가 조합)
        "created_at": datetime.now().isoformat(), # 이미지 저장 시간 추가
        "fight_detected": fight_detected # 싸움 감지 여부도 응답에 포함
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
    files_in_uploads = [f for f in os.listdir(SAVING_DIR) if os.path.isfile(os.path.join(SAVING_DIR, f))]
    
    if files_in_uploads:
        latest_file = max(files_in_uploads, key=lambda f: os.path.getmtime(os.path.join(SAVING_DIR, f)))
        
        created_at_timestamp = os.path.getmtime(os.path.join(SAVING_DIR, latest_file))
        created_at_datetime = datetime.fromtimestamp(created_at_timestamp)

        return JSONResponse({
            "image_url": f"http://10.56.148.13:8000/saving/{latest_file}",
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


if __name__ == "__main__":  
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
