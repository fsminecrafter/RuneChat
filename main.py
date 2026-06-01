"""
RuneChat — Flask + SocketIO Backend
====================================
Features:
  - Session-based auth (register / login / logout)
  - SQLite via SQLAlchemy  (users, contacts, messages)
  - Real-time messaging via SocketIO rooms
  - File uploads (images, docs) with type/size validation
  - Message deletion (broadcast to both sides)
  - Account settings (name, emoji/avatar, status, description, email)
  - CSRF-safe JSON API + secure HTTP headers
  - New-chat / add-contact flow
"""

import os, uuid, secrets, mimetypes
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, session,
    jsonify, send_from_directory, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_bcrypt import Bcrypt

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY            = os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'runechat.db')}",
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    MAX_CONTENT_LENGTH    = 16 * 1024 * 1024,   # 16 MB upload cap
    SESSION_COOKIE_HTTPONLY = True,
    SESSION_COOKIE_SAMESITE = "Lax",
)

db       = SQLAlchemy(app)
bcrypt   = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False)

# ──────────────────────────────────────────────
# Allowed upload extensions
# ──────────────────────────────────────────────
ALLOWED_EXTENSIONS = {
    "png","jpg","jpeg","gif","webp",
    "pdf","txt","md","csv",
    "mp3","wav","ogg",
    "zip","tar","gz",
}

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def secure_filename_custom(filename: str) -> str:
    """Return a UUID-based name that keeps the original extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"{uuid.uuid4().hex}.{ext}"

# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────
class User(db.Model):
    __tablename__ = "users"
    id          = db.Column(db.Integer, primary_key=True)
    username    = db.Column(db.String(40),  unique=True, nullable=False)
    email       = db.Column(db.String(120), unique=True, nullable=False)
    password    = db.Column(db.String(128), nullable=False)
    display_name= db.Column(db.String(60),  nullable=False)
    avatar      = db.Column(db.String(10),  default="🧑")   # emoji or filename
    status      = db.Column(db.String(30),  default="Online")
    description = db.Column(db.String(200), default="")
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_public(self):
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "avatar": self.avatar,
            "status": self.status,
            "description": self.description,
        }

    def to_private(self):
        d = self.to_public()
        d["email"] = self.email
        return d


class Contact(db.Model):
    """Directional relationship: owner_id has contact_id in their list."""
    __tablename__ = "contacts"
    id         = db.Column(db.Integer, primary_key=True)
    owner_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    added_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("owner_id", "contact_id"),)


class Message(db.Model):
    __tablename__ = "messages"
    id         = db.Column(db.Integer, primary_key=True)
    room       = db.Column(db.String(80), nullable=False, index=True)
    sender_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    text       = db.Column(db.Text, default="")
    file_url   = db.Column(db.String(300), nullable=True)   # relative URL
    file_name  = db.Column(db.String(200), nullable=True)
    file_type  = db.Column(db.String(80),  nullable=True)
    deleted    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self, me_id=None):
        return {
            "id":        self.id,
            "room":      self.room,
            "sender_id": self.sender_id,
            "text":      "" if self.deleted else self.text,
            "file_url":  None if self.deleted else self.file_url,
            "file_name": None if self.deleted else self.file_name,
            "file_type": None if self.deleted else self.file_type,
            "deleted":   self.deleted,
            "time":      self.created_at.strftime("%H:%M"),
            "me":        (self.sender_id == me_id) if me_id else False,
        }

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def room_id(a: int, b: int) -> str:
    """Deterministic room name for two user IDs."""
    return f"dm_{min(a,b)}_{max(a,b)}"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def current_user():
    return User.query.get(session["user_id"]) if "user_id" in session else None

# ──────────────────────────────────────────────
# Security headers
# ──────────────────────────────────────────────
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"]        = "DENY"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    return resp

# ──────────────────────────────────────────────
# Routes — Auth
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip().lower()
    email    = (data.get("email")    or "").strip().lower()
    password =  data.get("password") or ""
    name     = (data.get("display_name") or username).strip()

    if not username or not email or not password:
        return jsonify({"error": "All fields required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be ≥ 6 characters"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username taken"}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already registered"}), 409

    hashed = bcrypt.generate_password_hash(password).decode()
    user = User(username=username, email=email, password=hashed, display_name=name)
    db.session.add(user)
    db.session.commit()
    session["user_id"] = user.id
    return jsonify({"user": user.to_private()}), 201

@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.get_json(force=True)
    username = (data.get("username") or "").strip().lower()
    password =  data.get("password") or ""
    user     = User.query.filter_by(username=username).first()
    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = user.id
    return jsonify({"user": user.to_private()})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({"user": u.to_private()})

# ──────────────────────────────────────────────
# Routes — Account settings
# ──────────────────────────────────────────────
@app.route("/api/account", methods=["PATCH"])
@login_required
def update_account():
    u    = current_user()
    data = request.get_json(force=True)

    if "display_name"  in data: u.display_name  = str(data["display_name"])[:60]
    if "avatar"        in data: u.avatar        = str(data["avatar"])[:10]
    if "status"        in data: u.status        = str(data["status"])[:30]
    if "description"   in data: u.description   = str(data["description"])[:200]
    if "email"         in data:
        new_email = str(data["email"]).strip().lower()
        existing  = User.query.filter_by(email=new_email).first()
        if existing and existing.id != u.id:
            return jsonify({"error": "Email already in use"}), 409
        u.email = new_email

    if "new_password" in data and data["new_password"]:
        if len(data["new_password"]) < 6:
            return jsonify({"error": "Password must be ≥ 6 characters"}), 400
        u.password = bcrypt.generate_password_hash(data["new_password"]).decode()

    db.session.commit()
    return jsonify({"user": u.to_private()})

@app.route("/api/account/avatar", methods=["POST"])
@login_required
def upload_avatar():
    u = current_user()
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not allowed_file(f.filename):
        return jsonify({"error": "File type not allowed"}), 400
    name = secure_filename_custom(f.filename)
    f.save(os.path.join(UPLOAD_DIR, name))
    u.avatar = f"/static/uploads/{name}"
    db.session.commit()
    return jsonify({"avatar": u.avatar})

# ──────────────────────────────────────────────
# Routes — Contacts
# ──────────────────────────────────────────────
@app.route("/api/contacts")
@login_required
def list_contacts():
    uid   = session["user_id"]
    links = Contact.query.filter_by(owner_id=uid).all()
    ids   = [l.contact_id for l in links]
    users = User.query.filter(User.id.in_(ids)).all() if ids else []

    result = []
    for u in users:
        rid = room_id(uid, u.id)
        last_msg = (Message.query
                    .filter_by(room=rid, deleted=False)
                    .order_by(Message.id.desc())
                    .first())
        unread = (Message.query
                  .filter_by(room=rid, deleted=False)
                  .filter(Message.sender_id != uid)
                  .count())   # simplified — no read tracking yet
        result.append({
            **u.to_public(),
            "room":     rid,
            "last_msg": last_msg.to_dict() if last_msg else None,
            "unread":   unread,
        })
    return jsonify(result)

@app.route("/api/contacts/add", methods=["POST"])
@login_required
def add_contact():
    uid  = session["user_id"]
    data = request.get_json(force=True)
    q    = (data.get("username") or "").strip().lower()
    if not q:
        return jsonify({"error": "Username required"}), 400
    target = User.query.filter_by(username=q).first()
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target.id == uid:
        return jsonify({"error": "Cannot add yourself"}), 400
    existing = Contact.query.filter_by(owner_id=uid, contact_id=target.id).first()
    if existing:
        return jsonify({"error": "Already in contacts"}), 409
    db.session.add(Contact(owner_id=uid, contact_id=target.id))
    db.session.add(Contact(owner_id=target.id, contact_id=uid))
    db.session.commit()
    rid = room_id(uid, target.id)
    return jsonify({**target.to_public(), "room": rid, "last_msg": None, "unread": 0}), 201

@app.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
@login_required
def remove_contact(contact_id):
    uid = session["user_id"]
    Contact.query.filter_by(owner_id=uid, contact_id=contact_id).delete()
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/users/search")
@login_required
def search_users():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    users = User.query.filter(
        (User.username.ilike(f"%{q}%")) | (User.display_name.ilike(f"%{q}%"))
    ).limit(10).all()
    return jsonify([u.to_public() for u in users])

# ──────────────────────────────────────────────
# Routes — Messages
# ──────────────────────────────────────────────
@app.route("/api/messages/<room>")
@login_required
def get_messages(room):
    uid = session["user_id"]
    # Verify user belongs to this room
    parts = room.split("_")   # dm_A_B
    if len(parts) != 3 or parts[0] != "dm":
        abort(400)
    a, b = int(parts[1]), int(parts[2])
    if uid not in (a, b):
        abort(403)
    msgs = (Message.query
            .filter_by(room=room)
            .order_by(Message.id.asc())
            .limit(200)
            .all())
    return jsonify([m.to_dict(me_id=uid) for m in msgs])

@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
@login_required
def delete_message(msg_id):
    uid = session["user_id"]
    msg = Message.query.get_or_404(msg_id)
    if msg.sender_id != uid:
        abort(403)
    msg.deleted = True
    db.session.commit()
    # Broadcast deletion to room
    socketio.emit("message_deleted", {"id": msg.id}, to=msg.room)
    return jsonify({"ok": True})

@app.route("/api/upload", methods=["POST"])
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not allowed_file(f.filename):
        return jsonify({"error": "File type not allowed"}), 400
    name = secure_filename_custom(f.filename)
    f.save(os.path.join(UPLOAD_DIR, name))
    mime = mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
    return jsonify({
        "file_url":  f"/static/uploads/{name}",
        "file_name": f.filename,
        "file_type": mime,
    })

@app.route("/static/uploads/<path:filename>")
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# ──────────────────────────────────────────────
# SocketIO events
# ──────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid:
        return False   # reject unauthenticated socket connections

@socketio.on("join")
def on_join(data):
    uid  = session.get("user_id")
    room = data.get("room", "")
    parts = room.split("_")
    if len(parts) != 3 or parts[0] != "dm":
        return
    a, b = int(parts[1]), int(parts[2])
    if uid not in (a, b):
        return
    join_room(room)

@socketio.on("leave")
def on_leave(data):
    leave_room(data.get("room", ""))

@socketio.on("send_message")
def on_message(data):
    uid  = session.get("user_id")
    if not uid:
        return
    room      = data.get("room", "")
    text      = (data.get("text") or "").strip()[:4000]
    file_url  = data.get("file_url")
    file_name = data.get("file_name")
    file_type = data.get("file_type")

    if not text and not file_url:
        return

    parts = room.split("_")
    if len(parts) != 3 or parts[0] != "dm":
        return
    a, b = int(parts[1]), int(parts[2])
    if uid not in (a, b):
        return

    msg = Message(
        room=room, sender_id=uid,
        text=text, file_url=file_url,
        file_name=file_name, file_type=file_type,
    )
    db.session.add(msg)
    db.session.commit()
    emit("new_message", msg.to_dict(me_id=uid), to=room)

@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    if uid:
        emit("typing", {"user_id": uid}, to=data.get("room",""), include_self=False)

@socketio.on("stop_typing")
def on_stop_typing(data):
    uid = session.get("user_id")
    if uid:
        emit("stop_typing", {"user_id": uid}, to=data.get("room",""), include_self=False)

# ──────────────────────────────────────────────
# Init DB and run
# ──────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=8333)