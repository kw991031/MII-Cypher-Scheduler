# main.py

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
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
SCOPES = ['https.www.googleapis.com/auth/calendar']
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
        if os.path.exists(self.storage_name):
            self.credentials = Credentials.from_authorized_user_file(self.storage_name, SCOPES)
        if not self.credentials or not self.credentials.valid:
            if self.credentials and self.credentials.expired and self.credentials.refresh_token:
                self.credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
                self.credentials = flow.run_local_server(port=0)
            with open(self.storage_name, 'w') as token:
                token.write(self.credentials.to_json())
        self.service = build('calendar', 'v3', credentials=self.credentials)

    def get_events(self, calendar_id, start, end):
        # (V6에서는 사용되지 않음)
        try:
            events_result = self.service.events().list(
                calendarId=calendar_id, timeMin=start, timeMax=end,
                timeZone="Asia/Seoul", singleEvents=True, orderBy='startTime').execute()
            return events_result.get('items', [])
        except Exception as e:
            print(f"An error occurred while fetching events: {e}")
            return []

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
week_mode = 1 # 1: 다음주 (고정)
# "확정된" 슬롯 (Red) - {슬롯ID: 이니셜} (세션 누적)
confirmed_reserved_slots: dict[str, str] = {}
# "이번 라운드에 선택된" 슬롯 버퍼 (Yellow) - {슬롯ID: 이름}
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
    for user_name, client in client_connections.items():
        try:
            await client.send_text(message)
        except Exception as e:
            print(f"{user_name} 에게 브로드캐스트 실패: {e}")

async def notify_turn():
    global current_turn_index, turn_order
    if current_turn_index < len(turn_order):
        current_user = turn_order[current_turn_index]
        message = json.dumps({"type": "turn_update", "user": current_user})
    else:
        message = json.dumps({"type": "turn_update", "user": "ROUND_END"})
    print(f"턴 알림 전송: {message}")
    await broadcast(message)

# 날짜 계산 헬퍼 함수
def get_week_range(week_offset=1):
    today = datetime.date.today()
    if week_offset == 0:
        start_of_week = today - datetime.timedelta(days=today.weekday())
    else:
        days_until_next_week = 7 - today.weekday()
        start_of_week = today + datetime.timedelta(days=days_until_next_week)
    end_of_week = start_of_week + datetime.timedelta(days=7)
    start_iso = datetime.datetime.combine(start_of_week, datetime.time(0, 0), tzinfo=GCalendar.KST).isoformat()
    end_iso = datetime.datetime.combine(end_of_week, datetime.time(0, 0), tzinfo=GCalendar.KST).isoformat()
    return start_iso, end_iso


# --- [V6] 라운드 시작 API (캘린더 읽기 X) ---
@app.post("/start_round")
async def start_round():
    global turn_order, current_turn_index, confirmed_reserved_slots, current_round_selections
    
    # 1. (캘린더 읽기 로직 없음)
    print("새 라운드 시작. 기존 확정 슬롯(Red)을 유지합니다.")

    # 2. 이번 라운드 버퍼(Yellow) 초기화
    current_round_selections = {}
    
    # 3. 접속자 기준 셔플
    current_connected_users = list(client_connections.keys())
    if not current_connected_users:
        return {"status": "error", "message": "접속한 사용자가 없습니다."}

    turn_order = random.sample(current_connected_users, len(current_connected_users)) 
    current_turn_index = 0
    print(f"새 라운드 순서: {turn_order}")

    # 4. "초기 상태" 전파 (유지된 Red + 비워진 Yellow 전송)
    await broadcast(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots, 
        "pending": current_round_selections
    }))
    
    # 5. 순서 공지
    await broadcast(json.dumps({"type": "round_started", "order": turn_order}))
    
    # 6. 첫 턴 공지
    await notify_turn() 
    
    return {"status": "round started", "turn_order": turn_order}


# --- 캘린더 일괄 추가 API (V5와 동일) ---
@app.post("/commit_calendar")
async def commit_calendar():
    global confirmed_reserved_slots, current_round_selections
    
    if not current_round_selections:
        return {"status": "error", "message": "새로 추가할 예약이 없습니다."}

    print(f"캘린더 일괄 추가 시작. 대상: {current_round_selections}")
    
    try:
        if not calendar_service.service or (calendar_service.credentials and not calendar_service.credentials.valid):
            print("Google Calendar 서비스 인증 시도...")
            calendar_service.build_service()
            
        success_data = {}
        
        for slot_id, user_name in current_round_selections.items():
            # (파싱 로직 시작)
            parts = slot_id.split('-')
            day_str_map = {'Mon': '월', 'Tue': '화', 'Wed': '수', 'Thu': '목', 'Fri': '금', 'Sat': '토', 'Sun': '일'}
            time_str_map = {'AM': '오전', 'PM': '오후', 'NT': '밤'}
            day_str = day_str_map.get(parts[0])
            time_str = time_str_map.get(parts[1])
            floor_str = parts[2]
            day_map = {'월': 0, '화': 1, '수': 2, '목': 3, '금': 4, '토': 5, '일': 6}
            time_map = {'오전': (8, 0, 13, 0), '오후': (13, 0, 19, 0), '밤': (19, 0, 24, 0)}
            event_name = name_map[user_name]
            start_hour, start_minute, end_hour, end_minute = time_map[time_str]
            today = datetime.date.today()
            if week_mode == 0:
                days_diff = day_map[day_str] - today.weekday()
                event_date = today + datetime.timedelta(days=days_diff)
            else:
                days_until_next_week = 7 - today.weekday()
                event_date = today + datetime.timedelta(days=days_until_next_week + day_map[day_str])
            start_time = datetime.datetime.combine(event_date, datetime.time(start_hour, start_minute, tzinfo=GCalendar.KST))
            if end_hour == 24:
                end_time = datetime.datetime.combine(event_date + datetime.timedelta(days=1), datetime.time(0, 0, tzinfo=GCalendar.KST))
            else:
                end_time = datetime.datetime.combine(event_date, datetime.time(end_hour, end_minute, tzinfo=GCalendar.KST))
            # (파싱 로직 끝)

            event = calendar_service.insert_event(
                calendar_id=CALENDAR_IDS[floor_str],
                event_name=event_name,
                start=start_time.isoformat(),
                end=end_time.isoformat(),
                description=slot_id
            )
            if event:
                print(f"  > {slot_id} ({user_name}) 추가 성공.")
                success_data[slot_id] = event_name
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
        return {"status": "error", "message": str(e)}


# --- [수정] WebSocket 핵심 로직 (중복 접속 방지) ---
@app.websocket("/ws/{user_name}")
async def websocket_endpoint(websocket: WebSocket, user_name: str):
    
    global current_turn_index

    if user_name not in name_map:
        await websocket.close(code=1008, reason="Invalid user name")
        return

    # [수정] 연결을 먼저 수락
    await websocket.accept()
    
    # [신규] 중복 접속 확인 (Request 2)
    if user_name in client_connections:
        print(f"'{user_name}' 님은 이미 접속 중입니다. 새 연결을 거부합니다.")
        # 클라이언트에게 에러 메시지 전송
        await websocket.send_text(json.dumps({
            "type": "error", 
            "message": "이미 접속 중인 이름입니다. 다른 이름으로 시도하세요."
        }))
        # 연결 강제 종료
        await websocket.close(code=1003, reason="Duplicate connection")
        return # 함수 종료

    # 중복이 아니면 정식으로 추가
    client_connections[user_name] = websocket
    print(f"클라이언트 '{user_name}' 접속. (총 {len(client_connections)} 명)")
    
    # 접속 시 "현재 상태" 전송
    await websocket.send_text(json.dumps({
        "type": "initial_state",
        "reserved": confirmed_reserved_slots,
        "pending": current_round_selections
    }))
    
    # 현재 턴 상태도 전송
    current_user = "대기 중..."
    if turn_order:
        current_user = turn_order[current_turn_index] if current_turn_index < len(turn_order) else "ROUND_END"
    await websocket.send_text(json.dumps({"type": "turn_update", "user": current_user}))


    try:
        while True:
            # 1. 클라이언트로부터 메시지 (슬롯 ID) 수신
            data = await websocket.receive_text() # data = "Mon-AM-1F"
            
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
            
            # 3. 슬롯 중복 검증 (Red, Yellow 슬롯인지 확인)
            if data in confirmed_reserved_slots or data in current_round_selections:
                await websocket.send_text(json.dumps({"type": "error", "message": "이미 선택된 슬롯입니다."}))
                continue

            # 4. 예약 처리: "버퍼(Yellow)"에 추가
            print(f"'{user_name}' 님이 '{data}' 슬롯 선택 (버퍼에 추가)")
            current_round_selections[data] = user_name # 버퍼에 추가

            # 5. 모든 클라이언트에게 "슬롯이 선택됨(Yellow)" 알림
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
        # 연결이 끊어지면 (정상 종료든, 에러든) 목록에서 제거
        if user_name in client_connections:
            del client_connections[user_name]
        print(f"클라이언트 '{user_name}' 접속 해제. (남은 인원 {len(client_connections)} 명)")
    except Exception as e:
        print(f"에러 발생 (user: {user_name}): {e}")
        if user_name in client_connections:
            del client_connections[user_name]

# --- 서버 실행 ---
if __name__ == "__main__":
    print("서버 시작... http://127.0.0.1:8000")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)