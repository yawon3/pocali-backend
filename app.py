import os
import sqlite3
import uuid
import json
import os
from flask import Flask, g, request, jsonify, render_template, redirect, url_for, session, abort
from functools import wraps
from werkzeug.security import check_password_hash
from flask_cors import CORS




# Firebase 관련 import
import firebase_admin
from firebase_admin import credentials, db as firebase_db, storage

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-key")
CORS(app)  # Flask 앱 전체에 CORS 허용

# ────────────────────────────────────────────
# Config
# ────────────────────────────────────────────
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax"
    
)

DATABASE = os.path.join(os.getcwd(), "user_data.db")

# Firebase 초기화 (환경변수로 처리)
def init_firebase():
    if not firebase_admin._apps:  # 중복 초기화 방지
        try:
            cred = credentials.Certificate({
                "type": "service_account",
                "project_id": "pocali",
                "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
                "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
                "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
                "client_id": os.getenv("FIREBASE_CLIENT_ID"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token"
            })
            
            firebase_admin.initialize_app(cred, {
                'databaseURL': "https://pocali-default-rtdb.asia-southeast1.firebasedatabase.app/",
                'storageBucket': "pocali.firebasestorage.app"  # Storage 버킷 추가
            })
            print("Firebase 초기화 성공")
            return storage.bucket()  # 버킷 객체 반환
        except Exception as e:
            print(f"Firebase 초기화 실패: {e}")
            return None

# Firebase 초기화 실행 - 전역 변수로 버킷 저장
bucket = init_firebase()

# ────────────────────────────────────────────
# DB helpers (SQLite 백업용으로 유지)
# ────────────────────────────────────────────

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    return db

def init_db():
    with app.app_context():
        cur = get_db().cursor()
        # 1) 유저별 보유 정보
        cur.execute(
            """CREATE TABLE IF NOT EXISTS user_data (
                   user_id TEXT PRIMARY KEY,
                   data    TEXT
               )"""
        )
        # 2) 친구 관계 (대칭 저장)
        cur.execute(
            """CREATE TABLE IF NOT EXISTS friends (
                   user_id   TEXT,
                   friend_id TEXT,
                   PRIMARY KEY (user_id, friend_id)
               )"""
        )
        # 3) 사용자 잠금 상태
        cur.execute(
            """CREATE TABLE IF NOT EXISTS user_locks (
                   user_id TEXT PRIMARY KEY,
                   locked  BOOLEAN DEFAULT FALSE,
                   locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        get_db().commit()

@app.before_request
def _ensure_db():
    if not getattr(app, "_db_init", False):
        init_db()
        app._db_init = True

@app.teardown_appcontext
def close_connection(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ────────────────────────────────────────────
# UUID 잠금 기능 추가
# ────────────────────────────────────────────

def is_user_locked(user_id):
    """사용자 잠금 상태 확인"""
    try:
        # Firebase에서 먼저 확인
        ref = firebase_db.reference(f'user_locks/{user_id}')
        lock_status = ref.get()
        if lock_status is not None:
            return bool(lock_status)
        
        # Firebase에 없으면 SQLite에서 확인
        cur = get_db().cursor()
        cur.execute("SELECT locked FROM user_locks WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False
    except Exception as e:
        print(f"잠금 상태 확인 오류: {e}")
        return False

def set_user_lock(user_id, locked):
    """사용자 잠금 상태 설정"""
    try:
        # Firebase에 저장
        ref = firebase_db.reference(f'user_locks/{user_id}')
        ref.set(locked)
        
        # SQLite에도 백업
        cur = get_db().cursor()
        cur.execute(
            "INSERT INTO user_locks (user_id, locked) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET locked = excluded.locked, locked_at = CURRENT_TIMESTAMP",
            (user_id, locked)
        )
        get_db().commit()
        return True
    except Exception as e:
        print(f"잠금 상태 설정 오류: {e}")
        return False

@app.route("/api/user/<uid>/toggle-lock", methods=["POST"])
def toggle_user_lock(uid):
    """사용자 잠금 상태 토글"""
    try:
        data = request.get_json() or {}
        current_user = data.get('current_user')
        
        # 본인만 잠금 설정 가능
        if current_user != uid:
            return jsonify({"error": "본인만 잠금 설정이 가능합니다"}), 403
        
        # 현재 잠금 상태 확인
        current_lock = is_user_locked(uid)
        new_lock = not current_lock
        
        # 잠금 상태 변경
        if set_user_lock(uid, new_lock):
            return jsonify({
                "success": True,
                "locked": new_lock,
                "message": "잠금 해제됨" if not new_lock else "잠금 설정됨"
            })
        else:
            return jsonify({"error": "잠금 상태 변경 실패"}), 500
            
    except Exception as e:
        print(f"잠금 토글 오류: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/user/<uid>/lock-status", methods=["GET"])
def get_lock_status(uid):
    """사용자 잠금 상태 조회"""
    try:
        locked = is_user_locked(uid)
        return jsonify({
            "locked": locked,
            "user_id": uid
        })
    except Exception as e:
        print(f"잠금 상태 조회 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ────────────────────────────────────────────
# Friend API (Firebase로 변경)
# ────────────────────────────────────────────

@app.route("/api/friends", methods=["POST"])
def add_friend():
    data = request.get_json(silent=True) or {}
    me = data.get("me")
    friend = data.get("friend")
    
    if not (me and friend):
        return jsonify({"error": "uuid missing"}), 400
    
    try:
        # Firebase에 친구 관계 저장
        ref = firebase_db.reference('friends')
        ref.child(me).child(friend).set(True)
        ref.child(friend).child(me).set(True)
        
        # SQLite에도 백업 (기존 로직 유지)
        cur = get_db().cursor()
        cur.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (me, friend))
        cur.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (friend, me))
        get_db().commit()
        
        return jsonify({"ok": True})
    except Exception as e:
        print(f"친구 추가 오류: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/friends/<user_id>", methods=["GET"])
def list_friends(user_id):
    try:
        # Firebase에서 친구 목록 가져오기
        ref = firebase_db.reference(f'friends/{user_id}')
        friends_data = ref.get() or {}
        friend_list = list(friends_data.keys())
        return jsonify(friend_list)
    except Exception as e:
        # Firebase 실패시 SQLite 백업 사용
        print(f"Firebase 친구목록 오류, SQLite 사용: {e}")
        cur = get_db().cursor()
        cur.execute("SELECT friend_id FROM friends WHERE user_id = ?", (user_id,))
        return jsonify([row[0] for row in cur.fetchall()])

@app.route("/api/friend/<friend_id>/collection", methods=["GET"])
def friend_collection(friend_id):
    try:
        # Firebase에서 친구 컬렉션 가져오기
        ref = firebase_db.reference(f'users/{friend_id}')
        data = ref.get() or {}
        return jsonify({"data": data})
    except Exception as e:
        # Firebase 실패시 SQLite 백업 사용
        print(f"Firebase 컬렉션 오류, SQLite 사용: {e}")
        cur = get_db().cursor()
        cur.execute("SELECT data FROM user_data WHERE user_id = ?", (friend_id,))
        row = cur.fetchone()
        return jsonify({"data": json.loads(row[0]) if row else {}})

# ────────────────────────────────────────────
# User data API (Firebase로 변경 + 잠금 기능 추가)
# ────────────────────────────────────────────

@app.route("/api/user/<uid>", methods=["GET", "POST"])
def user_data(uid):
    try:
        ref = firebase_db.reference(f'users/{uid}')
        
        if request.method == "GET":
            # 요청자 정보 확인 (쿠키에서)
            current_user = request.cookies.get('myUUID')
            
            # 본인이 아닌 경우 잠금 상태 확인
            if current_user != uid:
                if is_user_locked(uid):
                    return jsonify({
                        "error": "이 사용자는 데이터를 잠갔습니다",
                        "locked": True
                    }), 403
            
            # Firebase에서 데이터 가져오기
            data = ref.get() or {}
            return jsonify({"user_id": uid, "data": json.dumps(data)})
        
        # POST - update (본인만 가능하므로 잠금 체크 불필요)
        new_data = request.json.get("data")
        
        # Firebase에 저장
        ref.set(new_data)
        
        # SQLite에도 백업
        cur = get_db().cursor()
        cur.execute(
            "INSERT INTO user_data (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (uid, json.dumps(new_data)),
        )
        get_db().commit()
        
        return jsonify({"ok": True})
        
    except Exception as e:
        print(f"Firebase 사용자 데이터 오류: {e}")
        # Firebase 실패시 SQLite만 사용
        cur = get_db().cursor()
        if request.method == "GET":
            # 본인이 아닌 경우 잠금 상태 확인
            current_user = request.cookies.get('myUUID')
            if current_user != uid and is_user_locked(uid):
                return jsonify({
                    "error": "이 사용자는 데이터를 잠갔습니다",
                    "locked": True
                }), 403
            
            cur.execute("SELECT data FROM user_data WHERE user_id = ?", (uid,))
            row = cur.fetchone()
            return jsonify({"user_id": uid, "data": row[0] if row else "{}"})
        else:
            new_data = request.json.get("data")
            cur.execute(
                "INSERT INTO user_data (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
                (uid, json.dumps(new_data)),
            )
            get_db().commit()
            return jsonify({"ok": True})

@app.route("/api/register", methods=["POST"])
def register():
    new_uuid = str(uuid.uuid4())
    
    try:
        # Firebase에 새 사용자 등록
        ref = firebase_db.reference(f'users/{new_uuid}')
        ref.set({})
        
        # 기본 잠금 상태 설정 (잠금 해제 상태)
        set_user_lock(new_uuid, False)
        
        # SQLite에도 백업
        cur = get_db().cursor()
        cur.execute("INSERT INTO user_data (user_id, data) VALUES (?, '{}')", (new_uuid,))
        get_db().commit()
        
    except Exception as e:
        print(f"Firebase 사용자 등록 오류, SQLite만 사용: {e}")
        # Firebase 실패시 SQLite만 사용
        cur = get_db().cursor()
        cur.execute("INSERT INTO user_data (user_id, data) VALUES (?, '{}')", (new_uuid,))
        get_db().commit()
    
    resp = jsonify({"user_id": new_uuid})
    resp.set_cookie("myUUID", new_uuid, max_age=31536000, httponly=True, secure=True, samesite="None")
    return resp

# ────────────────────────────────────────────
# 이미지 조회 API (Firebase Storage 사용)
# ────────────────────────────────────────────

@app.route("/api/images")
def get_images():
    try:
        blobs = bucket.list_blobs()
        images = []
        
        for blob in blobs:
            # 이미지 파일인지 확인
            if not any(blob.name.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                continue
                
            try:
                # images/ 폴더 제거하고 실제 경로 추출
                _, full_path = blob.name.split('/', 1)
                
                # 실제 file_type과 filename 분리
                if '/' in full_path:
                    file_type, filename = full_path.split('/', 1)
                else:
                    file_type = "unknown"
                    filename = full_path
            except ValueError:
                file_type = "unknown"
                filename = blob.name
                
            # 파일명 파싱 결과를 meta에 담음
            meta = parse_filename(filename) or {}
            meta.update({
                "file_type": file_type,  # 이제 "event"가 됨
                "filename": full_path,   # "event/IVE_AN_ARENA_351631.jpg"
                "url": blob.public_url
            })
            
            images.append(meta)
        
        # unique_id 기준 내림차순 정렬
        images.sort(key=lambda x: int(x.get("unique_id", 0) or 0), reverse=True)
        
        return jsonify(images)
    except Exception as e:
        print(f"Firebase 이미지 조회 오류: {e}")
        return jsonify({"error": str(e)}), 500

# ────────────────────────────────────────────
# 메인 페이지
# ────────────────────────────────────────────

@app.route('/')
def index():
    try:
        blobs = bucket.list_blobs()
        images = []
        
        for blob in blobs:
            # 이미지 파일인지 확인
            if not any(blob.name.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif']):
                continue
                
            try:
                # images/ 폴더 제거하고 실제 경로 추출
                _, full_path = blob.name.split('/', 1)
                
                # 실제 file_type과 filename 분리
                if '/' in full_path:
                    file_type, filename = full_path.split('/', 1)
                else:
                    file_type = "unknown"
                    filename = full_path
            except ValueError:
                file_type = "unknown"
                filename = blob.name
                
            # 파일명 파싱 결과를 meta에 담음
            meta = parse_filename(filename) or {}
            meta.update({
                "file_type": file_type,  # 이제 "event"가 됨
                "filename": full_path,   # "event/IVE_AN_ARENA_351631.jpg"
                "url": blob.public_url
            })
            
            images.append(meta)
        
        # unique_id 기준 내림차순 정렬
        images.sort(key=lambda x: int(x.get("unique_id", 0) or 0), reverse=True)
        
        return render_template('index.html', images=images)
    except Exception as e:
        print(f"Firebase 이미지 조회 오류: {e}")
        return render_template('index.html', images=[])

        # ────────────────────────────────────────────
# djemals (관리자) 로그인/보호
# ────────────────────────────────────────────
from functools import wraps
from werkzeug.security import check_password_hash
from flask import abort

def djemals_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_djemals"):
            abort(401)
        return fn(*args, **kwargs)
    return wrapper

@app.get("/djemals/login")
def djemals_login_page():
    return render_template("djemals_login.html")

@app.post("/djemals/login")
def djemals_login():
    pw = request.form.get("password", "")
    pw_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    if (not pw_hash) or (not check_password_hash(pw_hash, pw)):
        return "Unauthorized", 401

    session["is_djemals"] = True
    return redirect("/djemals")

@app.post("/djemals/logout")
def djemals_logout():
    session.clear()
    return redirect("/djemals/login")

@app.get("/djemals")
@djemals_required
def djemals_home():
    return render_template("djemals.html")

@app.get("/djemals/ping")
def djemals_ping():
    return "pong"

# ────────────────────────────────────────────
# Helper – 파일명 파싱
# ────────────────────────────────────────────

def parse_filename(filename: str):
    base = os.path.splitext(filename)[0]
    tokens = base.split('_')
    if len(tokens) < 4:
        return None
    mapping = {
        '_AN': '유진', 'WON': '원영', 'GA': '가을', 'REI': '레이',
        'LIZ': '리즈', 'LEE': '이서', 'II': "I've IVE", 'LD': 'LOVE DIVE'
    }
    unique_id = tokens[-1]
    mapped = []
    for t in tokens[:-1]:
        for k, v in mapping.items():
            t = t.replace(k, v)
        mapped.append(t)
    group, member, category, *middle = mapped
    title = middle[0] if middle else ''
    version = middle[1] if len(middle) > 1 else ''
    return {
        'group': group, 'member': member, 'category': category,
        'title': title, 'version': version, 'unique_id': unique_id
    }

# ────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)