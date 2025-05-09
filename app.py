import os
import sqlite3
import uuid
import json
from flask import Flask, g, request, jsonify, render_template, flash, redirect, url_for, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-key")

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
        if 'file' not in request.files or 'custom_filename' not in request.form:
            flash('필수 항목이 누락되었습니다.')
            return redirect(request.url)
        file = request.files['file']
        custom_filename = request.form['custom_filename'].strip()
        if file.filename == '' or not allowed_file(file.filename):
            flash('유효하지 않은 파일입니다.')
            return redirect(request.url)
        ext = file.filename.rsplit('.', 1)[1].lower()
        # 고유번호 계산
        max_id = 0
        for fname in os.listdir(app.config['UPLOAD_FOLDER']):
            if allowed_file(fname):
                meta = parse_filename(fname)
                if meta and meta['unique_id'].isdigit():
                    max_id = max(max_id, int(meta['unique_id']))
        new_filename = f"{custom_filename}{max_id + 1}.{ext}"
        file_type = request.form.get('file_type', '')
        dest_folder = os.path.join(app.config['UPLOAD_FOLDER'], file_type)
        os.makedirs(dest_folder, exist_ok=True)
        file.save(os.path.join(dest_folder, new_filename))
        flash(f"업로드 성공: {new_filename}")
        return redirect(url_for('admin_upload'))
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
    result = []
    for root, _, files in os.walk(app.config['UPLOAD_FOLDER']):
        rel_folder = os.path.relpath(root, app.config['UPLOAD_FOLDER'])
        for fname in files:
            if not allowed_file(fname): continue
            meta = parse_filename(fname)
            if not meta: continue
            meta['sub_category'] = rel_folder if rel_folder != '.' else ''
            result.append(meta)
    return jsonify(result)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
