import os
import sqlite3
import uuid
import json
import os
import cloudinary
import cloudinary.uploader
import cloudinary.api
from flask import Flask, g, request, jsonify, render_template, flash, redirect, url_for, session
from flask_cors import CORS  # ← import 추가

cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET"),
    secure     = True
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-key")
CORS(app)  # ← Flask 앱 전체에 CORS 허용

# ────────────────────────────────────────────
# Config
# ────────────────────────────────────────────
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None"
)

DATABASE = os.path.join(os.getcwd(), "user_data.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "default_admin_pass")

# ────────────────────────────────────────────
# DB helpers
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
# Static uploads
# ────────────────────────────────────────────
UPLOAD_FOLDER = os.path.join(os.getcwd(), "static", "images")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ────────────────────────────────────────────
# Friend API
# ────────────────────────────────────────────

@app.route("/api/friends", methods=["POST"])
def add_friend():
    data = request.get_json(silent=True) or {}
    me = data.get("me"); friend = data.get("friend")
    if not (me and friend):
        return jsonify({"error": "uuid missing"}), 400
    cur = get_db().cursor()
    cur.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (me, friend))
    cur.execute("INSERT OR IGNORE INTO friends VALUES (?,?)", (friend, me))
    get_db().commit()
    return jsonify({"ok": True})

@app.route("/admin/delete", methods=["POST"])    
def delete_image():
    data = request.get_json()
    file_type = data.get("file_type")
    filename = data.get("filename")

    if not (file_type and filename):
        return jsonify({"error": "필수 정보 누락"}), 400

        public_id = f"{file_type}/{filename}"
        resp = cloudinary.uploader.destroy(public_id)
        # resp.get("result") == "ok" 이면 삭제 성공
        if resp.get("result") == "ok":
            return jsonify({"ok": True, "message": f"{filename} 삭제 완료"})
        return jsonify({"error": "삭제 실패"}), 400


@app.route("/api/friends/<user_id>", methods=["GET"])
def list_friends(user_id):
    cur = get_db().cursor()
    cur.execute("SELECT friend_id FROM friends WHERE user_id = ?", (user_id,))
    return jsonify([row[0] for row in cur.fetchall()])

@app.route("/api/friend/<friend_id>/collection", methods=["GET"])
def friend_collection(friend_id):
    cur = get_db().cursor()
    cur.execute("SELECT data FROM user_data WHERE user_id = ?", (friend_id,))
    row = cur.fetchone()
    return jsonify({"data": json.loads(row[0]) if row else {}})

# ────────────────────────────────────────────
# User data API (단일 엔드포인트 GET/POST)
# ────────────────────────────────────────────

@app.route("/api/user/<uid>", methods=["GET", "POST"])
def user_data(uid):
    cur = get_db().cursor()
    if request.method == "GET":
        cur.execute("SELECT data FROM user_data WHERE user_id = ?", (uid,))
        row = cur.fetchone()
        return jsonify({"user_id": uid, "data": row[0] if row else "{}"})
    # POST – update
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
    cur = get_db().cursor()
    cur.execute("INSERT INTO user_data (user_id, data) VALUES (?, '{}')", (new_uuid,))
    get_db().commit()
    resp = jsonify({"user_id": new_uuid})
    resp.set_cookie("myUUID", new_uuid, max_age=31536000, httponly=True, secure=True, samesite="None")
    return resp

# ────────────────────────────────────────────
# 관리자 로그인 / 이미지 업로드 (기존 로직 그대로)
# ────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            flash('관리자 로그인 성공!')
            return redirect(url_for('admin_upload'))
        flash('잘못된 비밀번호입니다.')
    return render_template('admin_login.html')

@app.route('/admin/upload', methods=['GET', 'POST'])
def admin_upload():
    if request.method == 'POST':
        # 필수 파라미터 체크
        if 'file' not in request.files or 'file_type' not in request.form:
            flash('필수 항목이 누락되었습니다.')
            return redirect(request.url)

        file = request.files['file']
        file_type = request.form['file_type'].strip()

        # 확장자 검증
        if file.filename == '' or not allowed_file(file.filename):
            flash('유효하지 않은 파일입니다.')
            return redirect(request.url)

        # Cloudinary에 업로드
        try:
            res = cloudinary.uploader.upload(
                file,
                folder=file_type
            )
        except Exception as e:
            flash(f'업로드 중 오류 발생: {e}')
            return redirect(request.url)

        flash(f"업로드 성공: {res['public_id']}")
        return redirect(url_for('admin_upload'))

    # GET 요청 시 관리자 업로드 폼 렌더링
    return render_template('admin_upload.html')


# ────────────────────────────────────────────
# 메인 페이지
# ────────────────────────────────────────────

@app.route('/')
def index():
    image_data = []
    for root, _, files in os.walk(app.config['UPLOAD_FOLDER']):
        rel_folder = os.path.relpath(root, app.config['UPLOAD_FOLDER'])
        for filename in files:
            if not allowed_file(filename):
                continue
            meta = parse_filename(filename)
            if not meta:
                continue
            file_type = rel_folder if rel_folder != '.' else ''
            meta['type'] = file_type
            meta['url'] = url_for('static', filename=f"images/{file_type}/{filename}" if file_type else f"images/{filename}")
            try:
                meta['unique_numeric'] = int(meta['unique_id'])
            except ValueError:
                meta['unique_numeric'] = 0
            image_data.append(meta)
    image_data.sort(key=lambda x: x['unique_numeric'], reverse=True)
    return render_template('index.html', images=image_data)

# ────────────────────────────────────────────
# Helper – 파일명 파싱
# ────────────────────────────────────────────

def parse_filename(filename: str):
    base = os.path.splitext(filename)[0]
    tokens = base.split('_')
    if len(tokens) < 4:
        return None
    mapping = {
        'AN': '유진', 'WON': '원영', 'GA': '가을', 'REI': '레이',
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


@app.route("/api/images")
def get_images():
        # Cloudinary에서 업로드된 리소스 리스트 가져오기
        data = cloudinary.api.resources(type='upload')
        images = []
        for item in data.get("resources", []):
            file_type, filename = item["public_id"].split("/", 1)
            images.append({
                "group":   item["public_id"],       # 필요한 메타만 추출
                "file_type": file_type,
                "filename":  filename,
                "url":       item["secure_url"]
            })
        return jsonify({"images": images})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
