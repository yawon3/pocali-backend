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

@app.route('/api/upload', methods=['POST'])
def api_upload():
    # 1) 파일 & file_type 검증
    file = request.files.get('file')
    file_type = request.form.get('file_type', '').strip()
    if not file or not file_type:
        return jsonify({"error":"file or file_type missing"}), 400

    # 2) 원본 이름으로 public_id 설정
    name, _ = os.path.splitext(file.filename)
    public_id = f"{file_type}/{name}"

    try:
        res = cloudinary.uploader.upload(
            file,
            public_id=public_id,
            overwrite=True,
            resource_type='image'
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # 3) JSON으로 성공 리턴
    return jsonify({
        "public_id": res["public_id"],
        "format":    res["format"],
        "url":       res["secure_url"]
    }), 200

# ────────────────────────────────────────────
# 메인 페이지
# ────────────────────────────────────────────

@app.route('/')
def index():
    # 클라우디너리에서 한 번에 최대 500개 가져오기
    resp = cloudinary.api.resources(type='upload', max_results=500)

    images = []
    for r in resp.get('resources', []):
        # public_id 예: "album/IVE_AN_3rdFC_SCOUTJP_351545"
        file_type, name = r['public_id'].split('/', 1)
        ext = r.get('format', 'jpg')
        filename = f"{name}.{ext}"

        # parse_filename 로 group, member, category, title, version, unique_id 뽑기
        meta = parse_filename(filename) or {}
        meta.update({
            'file_type': file_type,
            'filename':  filename,
            'url':       r['secure_url']
        })
        images.append(meta)

    # unique_id 를 수치로 정렬 (내림차순)
    images.sort(key=lambda x: int(x.get('unique_id', 0)), reverse=True)

    # 원래 index.html에 넘겨주던 다른 컨텍스트(name, 설정 등)가 있으면 함께 넘겨주세요
    return render_template('index.html', images=images)

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
    resp = cloudinary.api.resources(type='upload', max_results=500)
    images = []

    # 1차 페이지 리소스
    for r in resp.get("resources", []):
        file_type, name = r["public_id"].split("/", 1)
        ext      = r.get("format", "jpg")
        filename = f"{name}.{ext}"

        # ★ 여기: 파일명 파싱 결과를 meta에 담고…
        meta = parse_filename(filename) or {}
        meta.update({
            "file_type": file_type,
            "filename":  filename,
            "url":       r["secure_url"]
        })

        images.append(meta)

    # 다음 페이지가 있으면 모두 합칩니다
    while resp.get("next_cursor"):
        resp = cloudinary.api.resources(
            type='upload',
            max_results=500,
            next_cursor=resp["next_cursor"]
        )
        for r in resp.get("resources", []):
            file_type, name = r["public_id"].split("/", 1)
            ext      = r.get("format", "jpg")
            filename = f"{name}.{ext}"

            meta = parse_filename(filename) or {}
            meta.update({
                "file_type": file_type,
                "filename":  filename,
                "url":       r["secure_url"]
            })
            images.append(meta)

    # unique_id 기준 내림차순 정렬
    images.sort(key=lambda x: int(x.get("unique_id", 0)), reverse=True)

    # images 배열만 내려주는 게 front에서 더 쓰기 편하다면…
    return jsonify(images)
    # 만약 { images: [...] } 형태를 유지하려면,
    # return jsonify({ "images": images })



if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
