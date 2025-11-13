# main.py (V8.1 - 버그 수정)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import json
import random
import datetime
import os
from typing import Any

# --- Google Calendar API Imports ---
import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
# [V8.1 버그 수정] 아래 줄 추가
from google_auth_oauthlib.flow import InstalledAppFlow 
from googleapiclient.discovery import build

# --- FastAPI 앱 생성 ---
app = FastAPI()

# --- Google Calendar 설정 ---
CLIENT_SECRET = 'client_secret.json'
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_IDS = {
    '1F': 'q2ipgq5e47l7d9g24ibbq08avo@group.calendar.google.com',
    '3F': 'cmhg0lmmdk66tmd9nc6ug7fob0@group.calendar.google.com'
}

# --- GCalendar 클래스 (V7과 동일) ---
class GCalendar:
    KST = datetime.timezone(datetime.timedelta(hours=9))

    def __init__(self, storage_name):
        self.storage_name = storage_name
        self.credentials = None
        self.service = None

    def build_service(self):
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
                    if os.path.exists(self.storage_name):
                         with open(self.storage_name, 'w') as token:
                            token.write(self.credentials.to_json())
                except Exception as e:
                    print(f"토큰 리프레시 실패: {e}. 로컬 인증이 필요할 수 있습니다.")
                    self.credentials = None
            
            if not self.credentials:
                print("유효한 Google Credential이 없습니다. 로컬 인증 흐름(InstalledAppFlow)을 시작합니다.")
                try:
                    # [V8.1 버그 수정] 이제 'InstalledAppFlow'가 정의됨
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
                'summary': event_name, 'description': description,
                'start': {'dateTime': start, 'timeZone': 'Asia/Seoul'},
                'end': {'dateTime': end, 'timeZone': 'Asia/Seoul'},
            }
            return self.service.events().insert(calendarId=calendar_id, body=body).execute()
        except Exception as e:
            print(f"An error occurred while inserting the event: {e}")
            return None

# GCalendar 인스턴스 생성
calendar_service = GCalendar("Calendar.storage")

# --- 사용자 및 턴 관리 (V8) ---

try:
    with open('names.json', 'r', encoding='utf-8') as f:
        name_map = json.load(f)
    print(f"names.json 로드 성공. {len(name_map)}명.")
except FileNotFoundError:
    print("[에러] names.json 파일을 찾을 수 없습니다!")
    name_map = {"건우": "KW"}

client_connections: dict[str, dict[str, Any]] = {}
turn_order: list[str] = []
current_turn_index: int = 0

# --- 상태 관리 변수 (V7과 동일) ---
week_mode = 1
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

@app.get("/get_names")
async def get_names():
    return {"names": list(name_map.keys())}


# --- WebSocket 헬퍼 함수 (V8) ---
async def broadcast(message: str):
    for user_data in client_connections.values():
        client_ws = user_data.get("ws")
        if client_ws:
            try:
                await client_ws.send_text(message)
            except Exception as e:
                print(f"브로드캐스트 실패: {e}")

async def notify_turn():
    global current_turn_index, turn_order
    if current_turn_index < len(turn_order):
        current_user = turn_order[current_turn_index]
        message = json.dumps({"type": "turn_update", "user": current_user})
    else:
        message = json.dumps({"type": "turn_update", "user": "ROUND_END"})
    print(f"턴 알림 전송: {message}")
    await broadcast(message)

async def broadcast_user_list():
    user_list = []
    for user_name, data in client_connections.items():
        user_list.append({
            "name": user_name,
            "participating": data.get("participating", True)
        })
    await broadcast(json.dumps({"type": "user_list_update", "users": user_list}))

async def broadcast_initial_state():
    # [V8.4 수정 반영] Yellow 슬롯(pending)도 이름 대신 이니셜을 전송
    pending_with_initials = {
        slot_id: name_map.get(user_name, "??") 
        for slot_id, user_name in current_round_selections.items()
    }
    
    await broadcast(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots, # Red ({slot_id: initial})
        "pending": pending_with_initials # Yellow ({slot_id: initial})
    }))

# 날짜 계산 헬퍼 함수 (V7과 동일)
def get_event_datetime(day_str, time_str, current_week_mode):
    day_map = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4, 'Sat': 5, 'Sun': 6}
    time_map = {'AM': (8, 0, 13, 0), 'PM': (13, 0, 19, 0), 'NT': (19, 0, 24, 0)}
    day_num = day_map.get(day_str)
    time_data = time_map.get(time_str)
    if day_num is None or time_data is None:
        raise ValueError(f"Invalid day/time: {day_str}, {time_str}")
    start_hour, start_minute, end_hour, end_minute = time_data
    today = datetime.date.today()
    if current_week_mode == 0:
        days_diff = day_num - today.weekday()
    elif current_week_mode == 1:
        days_diff = 7 - today.weekday() + day_num
    else:
        days_diff = 14 - today.weekday() + day_num
    event_date = today + datetime.timedelta(days=days_diff)
    start_time = datetime.datetime.combine(event_date, datetime.time(start_hour, start_minute, tzinfo=GCalendar.KST))
    if end_hour == 24:
        end_time = datetime.datetime.combine(event_date + datetime.timedelta(days=1), datetime.time(0, 0, tzinfo=GCalendar.KST))
    else:
        end_time = datetime.datetime.combine(event_date, datetime.time(end_hour, end_minute, tzinfo=GCalendar.KST))
    return start_time.isoformat(), end_time.isoformat()


# --- 관리자 API (V7과 동일) ---
@app.post("/reset_session")
async def reset_session():
    global confirmed_reserved_slots, current_round_selections, turn_order, current_turn_index
    print("[Admin] 시스템 상태 초기화...")
    confirmed_reserved_slots = {}
    current_round_selections = {}
    turn_order = []
    current_turn_index = 0
    await broadcast_initial_state()
    await notify_turn()
    return {"status": "success", "message": "시스템이 초기화되었습니다."}

@app.post("/set_week_mode/{mode}")
async def set_week_mode(mode: int):
    global week_mode
    if mode in [0, 1, 2]:
        week_mode = mode
        print(f"[Admin] 주간 모드 변경 -> {mode}")
        await broadcast(json.dumps({"type": "week_mode_update", "mode": week_mode}))
        return {"status": "success", "week_mode": week_mode}
    raise HTTPException(status_code=400, detail="Invalid mode")

class ManualAddRequest(BaseModel):
    name: str
    day: str
    time: str
    floor: str

@app.post("/admin/manual_add")
async def manual_add(item: ManualAddRequest):
    global confirmed_reserved_slots
    slot_id = f"{item.day}-{item.time}-{item.floor}"
    print(f"[Admin] 수동 추가 시도: {slot_id} / {item.name}")
    if slot_id in confirmed_reserved_slots or slot_id in current_round_selections:
        print(" > 에러: 중복된 슬롯")
        raise HTTPException(status_code=400, detail="이미 선택되거나 확정된 슬롯입니다.")
    try:
        if not calendar_service.service or (calendar_service.credentials and not calendar_service.credentials.valid):
            calendar_service.build_service()
        start_iso, end_iso = get_event_datetime(item.day, item.time, week_mode)
        initial = name_map.get(item.name, "??")
        calendar_id = CALENDAR_IDS.get(item.floor)
        if not calendar_id:
            raise ValueError("Invalid floor")
        event = calendar_service.insert_event(
            calendar_id=calendar_id, event_name=initial,
            start=start_iso, end=end_iso, description=slot_id
        )
        if not event:
            raise Exception("Calendar API returned None")
        confirmed_reserved_slots[slot_id] = initial
        print(f" > 성공. 캘린더 추가 완료.")
        await broadcast_initial_state()
        return {"status": "success", "slot_id": slot_id, "initial": initial}
    except Exception as e:
        print(f" > 수동 추가 실패: {e}")
        raise HTTPException(status_code=500, detail=f"수동 추가 실패: {e}")


# --- [V8] 라운드 시작 API (참여자 필터링) ---
@app.post("/start_round")
async def start_round():
    global turn_order, current_turn_index, current_round_selections
    print("새 라운드 시작.")
    current_round_selections = {}
    
    # [V8] "참여(participating: True)"로 설정한 접속자만 셔플
    participants = [
        name for name, data in client_connections.items() 
        if data.get("participating", True)
    ]
    
    if not participants:
        print(" > 에러: 참여자가 없습니다.")
        return {"status": "error", "message": "참여자로 설정된 사용자가 없습니다."}

    turn_order = random.sample(participants, len(participants)) 
    current_turn_index = 0
    print(f"새 라운드 순서 ({len(participants)}명): {turn_order}")

    await broadcast_initial_state()
    await broadcast(json.dumps({"type": "round_started", "order": turn_order}))
    await notify_turn() 
    return {"status": "round started", "turn_order": turn_order}


# --- 캘린더 일괄 추가 API (V7과 동일) ---
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
            parts = slot_id.split('-')
            initial = name_map[user_name]
            calendar_id = CALENDAR_IDS[parts[2]]
            start_iso, end_iso = get_event_datetime(parts[0], parts[1], week_mode)
            event = calendar_service.insert_event(
                calendar_id=calendar_id, event_name=initial,
                start=start_iso, end=end_iso, description=slot_id
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


# --- [V8] WebSocket 핵심 로직 ---
@app.websocket("/ws/{user_name}")
async def websocket_endpoint(websocket: WebSocket, user_name: str):
    
    global current_turn_index

    if user_name not in name_map:
        await websocket.close(code=1008, reason="Invalid user name")
        return

    await websocket.accept()
    
    if user_name in client_connections:
        print(f"'{user_name}' 님은 이미 접속 중입니다. 새 연결을 거부합니다.")
        await websocket.send_text(json.dumps({
            "type": "error", "message": "이미 접속 중인 이름입니다."
        }))
        await websocket.close(code=1003, reason="Duplicate connection")
        return

    # [V8] 접속 성공: "참여" 상태를 기본값 True로 설정
    client_connections[user_name] = {"ws": websocket, "participating": True}
    print(f"클라이언트 '{user_name}' 접속. (총 {len(client_connections)} 명)")
    await broadcast_user_list()
    
    await websocket.send_text(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots,
        "pending": {slot_id: name_map.get(name, "??") for slot_id, name in current_round_selections.items()} # [V8.4]
    }))
    
    current_user = "대기 중..."
    if turn_order:
        current_user = turn_order[current_turn_index] if current_turn_index < len(turn_order) else "ROUND_END"
    await websocket.send_text(json.dumps({"type": "turn_update", "user": current_user}))
    
    await websocket.send_text(json.dumps({"type": "week_mode_update", "mode": week_mode}))

    try:
        while True:
            data = await websocket.receive_text()
            
            try:
                msg_data = json.loads(data)
                msg_type = msg_data.get("type")

                # 1. 관리자 수동 삭제
                if msg_type == "admin_delete" and user_name == '건우':
                    slot_id = msg_data.get("slotId")
                    if slot_id:
                        print(f"[Admin] 슬롯 삭제 시도: {slot_id}")
                        deleted_from_pending = current_round_selections.pop(slot_id, None)
                        deleted_from_confirmed = confirmed_reserved_slots.pop(slot_id, None)
                        if deleted_from_pending or deleted_from_confirmed:
                             print(f" > 삭제 성공.")
                             await broadcast_initial_state()
                        else:
                             print(f" > 삭제 실패: 존재하지 않는 슬롯.")
                    continue

                # 2. [V8] 참여 상태 변경
                elif msg_type == "set_participation":
                    status = msg_data.get("status", True)
                    if user_name in client_connections:
                        client_connections[user_name]["participating"] = status
                        print(f"'{user_name}' 님 참여 상태 변경 -> {status}")
                        await broadcast_user_list()
                    continue

            except json.JSONDecodeError:
                pass # 일반 예약 메시지(String)이므로 계속 진행

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
        pass
    except Exception as e:
        print(f"에러 발생 (user: {user_name}): {e}")
    finally:
        # 접속 종료 시
        if user_name in client_connections:
            del client_connections[user_name]
            print(f"클라이언트 '{user_name}' 접속 해제. (남은 인원 {len(client_connections)} 명)")
            await broadcast_user_list()

# --- 서버 실행 ---
if __name__ == "__main__":
    print("서버 시작... http://127.0.0.1:8000")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)