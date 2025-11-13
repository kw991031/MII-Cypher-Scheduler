# main.py (V7)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import json
import random
import datetime
import os

# --- Google Calendar API Imports ---
import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- FastAPI 앱 생성 ---
app = FastAPI()

# --- Google Calendar 설정 ---
CLIENT_SECRET = 'client_secret.json'
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_IDS = {
    '1F': 'q2ipgq5e47l7d9g24ibbq08avo@group.calendar.google.com',  # 1층
    '3F': 'cmhg0lmmdk66tmd9nc6ug7fob0@group.calendar.google.com'   # 3층
}

# --- GCalendar 클래스 ---
class GCalendar:
    KST = datetime.timezone(datetime.timedelta(hours=9))

    def __init__(self, storage_name):
        self.storage_name = storage_name
        self.credentials = None
        self.service = None

    def build_service(self):
        # (V6/V5에서 가져온 '서버 환경용' 인증 코드)
        storage_content = os.environ.get('CALENDAR_STORAGE_JSON')
        
        if storage_content:
            print("환경 변수에서 Google Credential 로드 시도...")
            try:
                creds_info = json.loads(storage_content)
                self.credentials = Credentials.from_authorized_user_info(creds_info, SCOPES)
            except Exception as e:
                print(f"환경 변수 로드 실패: {e}")
                self.credentials = None
        else:
            # (로컬 테스트용: 파일 시스템 사용)
            print("환경 변수 없음. 로컬 파일(Calendar.storage)로 인증 시도...")
            if os.path.exists(self.storage_name):
                self.credentials = Credentials.from_authorized_user_file(self.storage_name, SCOPES)
            else:
                self.credentials = None

        if not self.credentials or not self.credentials.valid:
            if self.credentials and self.credentials.expired and self.credentials.refresh_token:
                print("토큰 만료. 리프레시 시도...")
                try:
                    self.credentials.refresh(Request())
                    # (로컬 테스트용) 파일이 존재하면 갱신된 토큰을 저장
                    if os.path.exists(self.storage_name):
                         with open(self.storage_name, 'w') as token:
                            token.write(self.credentials.to_json())
                except Exception as e:
                    print(f"토큰 리프레시 실패: {e}. 로컬 인증이 필요할 수 있습니다.")
                    self.credentials = None # 리프레시 실패 시 자격 증명 초기화
            
            # (로컬 테스트용) 환경 변수도 없고, 파일도 없거나 리프레시 실패 시
            if not self.credentials:
                print("유효한 Google Credential이 없습니다. 로컬 인증 흐름(InstalledAppFlow)을 시작합니다.")
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
                    self.credentials = flow.run_local_server(port=0)
                    with open(self.storage_name, 'w') as token:
                        token.write(self.credentials.to_json())
                    print("로컬 인증 성공. Calendar.storage 파일 생성됨.")
                except Exception as e:
                     print(f"로컬 인증 흐름 실패: {e}")
                     raise Exception("Google Auth Error.")

        print("Google Credential 로드 성공.")
        self.service = build('calendar', 'v3', credentials=self.credentials)


    def insert_event(self, calendar_id, event_name, start, end, description):
        try:
            body = {
                'summary': event_name,
                'description': description,
                'start': {'dateTime': start, 'timeZone': 'Asia/Seoul'},
                'end': {'dateTime': end, 'timeZone': 'Asia/Seoul'},
            }
            return self.service.events().insert(calendarId=calendar_id, body=body).execute()
        except Exception as e:
            print(f"An error occurred while inserting the event: {e}")
            return None

# GCalendar 인스턴스 생성
calendar_service = GCalendar("Calendar.storage")

# --- 사용자 및 턴 관리 ---
name_map = {
    '건우': 'KW', '구민': 'GM', '채율': 'CY', '윤아': 'YA', '채원': 'CW',
    '동훈': 'DH', '성현': 'KSH', '연규': 'YG', '상길': 'PSG', '준호': 'YJH',
    '윤한': 'YH', '의창': 'UC', '웨이슈엔': 'WH', '영우': 'YW', '진호': 'JH',
    '성곤': 'SG', '밧조릭': 'BB', '동연': 'DY'
}
client_connections: dict[str, WebSocket] = {}
turn_order: list[str] = []
current_turn_index: int = 0

# --- 상태 관리 변수 ---
week_mode = 1 # 0:이번주, 1:다음주, 2:다다음주 (Request 4)
confirmed_reserved_slots: dict[str, str] = {}
current_round_selections: dict[str, str] = {}


# --- HTML 서빙 ---
@app.get("/")
async def get_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        html = "<html><body><h1>index.html 파일을 찾을 수 없습니다.</h1></body></html>"
    return HTMLResponse(html)

# --- WebSocket 헬퍼 함수 ---
async def broadcast(message: str):
    """모든 연결된 클라이언트에게 JSON 메시지 전송"""
    for client in client_connections.values():
        try:
            await client.send_text(message)
        except Exception as e:
            print(f"브로드캐스트 실패: {e}")

async def notify_turn():
    """현재 턴인 사용자에게 알림"""
    global current_turn_index, turn_order
    
    if current_turn_index < len(turn_order):
        current_user = turn_order[current_turn_index]
        message = json.dumps({"type": "turn_update", "user": current_user})
    else:
        message = json.dumps({"type": "turn_update", "user": "ROUND_END"})
    
    print(f"턴 알림 전송: {message}")
    await broadcast(message)

# [신규] 접속자 목록 브로드캐스트 (Request 2)
async def broadcast_user_list():
    user_list = list(client_connections.keys())
    await broadcast(json.dumps({"type": "user_list_update", "users": user_list}))

# [신규] 현재 보드 상태 브로드캐스트 (초기화, 삭제 시 사용)
async def broadcast_initial_state():
    await broadcast(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots,
        "pending": current_round_selections
    }))

# 날짜 계산 헬퍼 함수
def get_event_datetime(day_str, time_str, current_week_mode):
    """요일, 시간, 주간모드를 기반으로 start/end ISO 시간을 계산 (Request 4)"""
    day_map = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4, 'Sat': 5, 'Sun': 6}
    time_map = {'AM': (8, 0, 13, 0), 'PM': (13, 0, 19, 0), 'NT': (19, 0, 24, 0)}
    
    day_num = day_map.get(day_str)
    time_data = time_map.get(time_str)
    
    if day_num is None or time_data is None:
        raise ValueError(f"Invalid day/time: {day_str}, {time_str}")

    start_hour, start_minute, end_hour, end_minute = time_data
    today = datetime.date.today()
    
    # [수정] week_mode(0, 1, 2)에 따라 날짜 계산
    if current_week_mode == 0: # 이번주
        days_diff = day_num - today.weekday()
    elif current_week_mode == 1: # 다음주
        days_diff = 7 - today.weekday() + day_num
    else: # current_week_mode == 2 (다다음주)
        days_diff = 14 - today.weekday() + day_num
        
    event_date = today + datetime.timedelta(days=days_diff)
    
    start_time = datetime.datetime.combine(event_date, datetime.time(start_hour, start_minute, tzinfo=GCalendar.KST))
    if end_hour == 24:
        end_time = datetime.datetime.combine(event_date + datetime.timedelta(days=1), datetime.time(0, 0, tzinfo=GCalendar.KST))
    else:
        end_time = datetime.datetime.combine(event_date, datetime.time(end_hour, end_minute, tzinfo=GCalendar.KST))
        
    return start_time.isoformat(), end_time.isoformat()


# --- 관리자 API ---

# [신규] 시스템 초기화 API (Request 1)
@app.post("/reset_session")
async def reset_session():
    global confirmed_reserved_slots, current_round_selections, turn_order, current_turn_index
    
    print("[Admin] 시스템 상태 초기화...")
    confirmed_reserved_slots = {}
    current_round_selections = {}
    turn_order = []
    current_turn_index = 0
    
    await broadcast_initial_state() # 비워진 상태 전파
    await notify_turn() # "ROUND_END" (또는 "대기 중") 전파
    
    return {"status": "success", "message": "시스템이 초기화되었습니다."}

# [신규] 주간 모드 변경 API (Request 4)
@app.post("/set_week_mode/{mode}")
async def set_week_mode(mode: int):
    global week_mode
    if mode in [0, 1, 2]:
        week_mode = mode
        print(f"[Admin] 주간 모드 변경 -> {mode}")
        await broadcast(json.dumps({"type": "week_mode_update", "mode": week_mode}))
        return {"status": "success", "week_mode": week_mode}
    raise HTTPException(status_code=400, detail="Invalid mode")

# [신규] 수동 추가 요청 Body 모델 (Request 3)
class ManualAddRequest(BaseModel):
    name: str
    day: str # "Mon"
    time: str # "AM"
    floor: str # "1F"

# [신규] 수동 추가 API (Request 3)
@app.post("/admin/manual_add")
async def manual_add(item: ManualAddRequest):
    global confirmed_reserved_slots
    
    slot_id = f"{item.day}-{item.time}-{item.floor}"
    print(f"[Admin] 수동 추가 시도: {slot_id} / {item.name}")
    
    # 1. 중복 확인
    if slot_id in confirmed_reserved_slots or slot_id in current_round_selections:
        print(" > 에러: 중복된 슬롯")
        raise HTTPException(status_code=400, detail="이미 선택되거나 확정된 슬롯입니다.")
        
    try:
        # 2. 캘린더 인증 및 시간 계산 (현재 week_mode 사용)
        if not calendar_service.service or (calendar_service.credentials and not calendar_service.credentials.valid):
            calendar_service.build_service()
        
        start_iso, end_iso = get_event_datetime(item.day, item.time, week_mode)
        initial = name_map.get(item.name, "??")
        calendar_id = CALENDAR_IDS.get(item.floor)

        if not calendar_id:
            raise ValueError("Invalid floor")
            
        # 3. 캘린더에 삽입
        event = calendar_service.insert_event(
            calendar_id=calendar_id,
            event_name=initial,
            start=start_iso,
            end=end_iso,
            description=slot_id
        )
        if not event:
            raise Exception("Calendar API returned None")
            
        # 4. 성공 시, 시스템(Red)에 즉시 반영
        confirmed_reserved_slots[slot_id] = initial
        print(f" > 성공. 캘린더 추가 완료.")
        
        # 5. 모든 클라이언트에 보드 상태 갱신
        await broadcast_initial_state()
        
        return {"status": "success", "slot_id": slot_id, "initial": initial}

    except Exception as e:
        print(f" > 수동 추가 실패: {e}")
        raise HTTPException(status_code=500, detail=f"수동 추가 실패: {e}")


# --- 라운드 시작 API (V6 기준) ---
@app.post("/start_round")
async def start_round():
    global turn_order, current_turn_index, current_round_selections
    
    print("새 라운드 시작.")

    # 1. 이번 라운드 버퍼(Yellow) 초기화 (Red는 유지)
    current_round_selections = {}
    
    # 2. 접속자 기준 셔플
    current_connected_users = list(client_connections.keys())
    if not current_connected_users:
        return {"status": "error", "message": "접속한 사용자가 없습니다."}

    turn_order = random.sample(current_connected_users, len(current_connected_users)) 
    current_turn_index = 0
    print(f"새 라운드 순서: {turn_order}")

    # 3. "초기 상태" 전파 (유지된 Red + 비워진 Yellow 전송)
    await broadcast_initial_state()
    
    # 4. 순서 공지
    await broadcast(json.dumps({"type": "round_started", "order": turn_order}))
    
    # 5. 첫 턴 공지
    await notify_turn() 
    
    return {"status": "round started", "turn_order": turn_order}


# --- 캘린더 일괄 추가 API (V6 수정) ---
@app.post("/commit_calendar")
async def commit_calendar():
    global confirmed_reserved_slots, current_round_selections, week_mode
    
    if not current_round_selections:
        return {"status": "error", "message": "새로 추가할 예약이 없습니다."}

    print(f"캘린더 일괄 추가 시작 (모드: {week_mode}). 대상: {current_round_selections}")
    
    try:
        if not calendar_service.service or (calendar_service.credentials and not calendar_service.credentials.valid):
            calendar_service.build_service()
            
        success_data = {}
        
        for slot_id, user_name in current_round_selections.items():
            parts = slot_id.split('-') # [Mon, AM, 1F]
            initial = name_map[user_name]
            calendar_id = CALENDAR_IDS[parts[2]]
            
            # [수정] get_event_datetime 헬퍼 함수 사용 (week_mode 반영)
            start_iso, end_iso = get_event_datetime(parts[0], parts[1], week_mode)

            event = calendar_service.insert_event(
                calendar_id=calendar_id,
                event_name=initial,
                start=start_iso,
                end=end_iso,
                description=slot_id
            )
            if event:
                print(f"  > {slot_id} ({user_name}) 추가 성공.")
                success_data[slot_id] = initial
            else:
                print(f"  > {slot_id} ({user_name}) 추가 실패.")

        confirmed_reserved_slots.update(success_data)
        current_round_selections = {} 

        await broadcast(json.dumps({
            "type": "calendar_committed",
            "committed_data": success_data
        }))
        
        return {"status": "success", "committed_count": len(success_data)}

    except Exception as e:
        print(f"[캘린더 일괄 추가 에러] {e}")
        raise HTTPException(status_code=500, detail=f"캘린더 일괄 추가 실패: {e}")


# --- WebSocket 핵심 로직 ---
@app.websocket("/ws/{user_name}")
async def websocket_endpoint(websocket: WebSocket, user_name: str):
    
    global current_turn_index

    if user_name not in name_map:
        await websocket.close(code=1008, reason="Invalid user name")
        return

    await websocket.accept()
    
    # 중복 접속 확인
    if user_name in client_connections:
        print(f"'{user_name}' 님은 이미 접속 중입니다. 새 연결을 거부합니다.")
        await websocket.send_text(json.dumps({
            "type": "error", 
            "message": "이미 접속 중인 이름입니다."
        }))
        await websocket.close(code=1003, reason="Duplicate connection")
        return

    # 접속 성공
    client_connections[user_name] = websocket
    print(f"클라이언트 '{user_name}' 접속. (총 {len(client_connections)} 명)")
    await broadcast_user_list() # [신규] 접속자 목록 갱신 (Request 2)
    
    # 접속 시 "현재 상태" 전송
    await websocket.send_text(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots,
        "pending": current_round_selections
    }))
    
    # 현재 턴 상태 전송
    current_user = "대기 중..."
    if turn_order:
        current_user = turn_order[current_turn_index] if current_turn_index < len(turn_order) else "ROUND_END"
    await websocket.send_text(json.dumps({"type": "turn_update", "user": current_user}))
    
    # 현재 주간 모드 전송
    await websocket.send_text(json.dumps({"type": "week_mode_update", "mode": week_mode}))

    try:
        while True:
            data = await websocket.receive_text()
            
            # [신규] 관리자 수동 삭제 (Request 3)
            try:
                msg_data = json.loads(data)
                if msg_data.get("type") == "admin_delete" and user_name == '건우':
                    slot_id = msg_data.get("slotId")
                    if slot_id:
                        print(f"[Admin] 슬롯 삭제 시도: {slot_id}")
                        # Yellow, Red 양쪽에서 모두 삭제 시도
                        deleted_from_pending = current_round_selections.pop(slot_id, None)
                        deleted_from_confirmed = confirmed_reserved_slots.pop(slot_id, None)
                        
                        if deleted_from_pending or deleted_from_confirmed:
                             print(f" > 삭제 성공.")
                             await broadcast_initial_state() # 갱신된 보드 상태 전파
                        else:
                             print(f" > 삭제 실패: 존재하지 않는 슬롯.")
                    continue # 턴 처리 로직 스킵
            except json.JSONDecodeError:
                # 일반 텍스트 메시지(예약)이므로 계속 진행
                pass

            # --- 일반 예약 로직 (data = "Mon-AM-1F") ---
            
            # 2. 턴 검증
            if not turn_order:
                await websocket.send_text(json.dumps({"type": "error", "message": "라운드가 아직 시작되지 않았습니다."}))
                continue
            if current_turn_index >= len(turn_order):
                await websocket.send_text(json.dumps({"type": "error", "message": "모든 턴이 종료되었습니다."}))
                continue
            current_user = turn_order[current_turn_index]
            if user_name != current_user:
                await websocket.send_text(json.dumps({"type": "error", "message": f"현재 {current_user} 님의 턴입니다."}))
                continue
            
            # 3. 슬롯 중복 검증
            if data in confirmed_reserved_slots or data in current_round_selections:
                await websocket.send_text(json.dumps({"type": "error", "message": "이미 선택된 슬롯입니다."}))
                continue

            # 4. 버퍼(Yellow)에 추가
            print(f"'{user_name}' 님이 '{data}' 슬롯 선택 (버퍼에 추가)")
            current_round_selections[data] = user_name

            # 5. "슬롯 선택됨(Yellow)" 알림
            await broadcast(json.dumps({
                "type": "slot_update", 
                "slotId": data,
                "user": user_name,
                "initial": name_map.get(user_name, '??')
            }))
            
            # 6. 다음 턴으로 넘김
            current_turn_index += 1
            await notify_turn()
            
    except WebSocketDisconnect:
        pass # 예외 처리는 finally에서
    except Exception as e:
        print(f"에러 발생 (user: {user_name}): {e}")
    finally:
        # 접속 종료 시 (정상/비정상)
        if user_name in client_connections:
            del client_connections[user_name]
            print(f"클라이언트 '{user_name}' 접속 해제. (남은 인원 {len(client_connections)} 명)")
            await broadcast_user_list() # [신규] 접속자 목록 갱신 (Request 2)

# --- 서버 실행 ---
if __name__ == "__main__":
    print("서버 시작... http://127.0.0.1:8000")
    # 로컬 테스트 시 Google 인증을 위해 host="127.0.0.1" 사용
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)