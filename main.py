"""
RuneChat — Flask + SocketIO Backend  (v2)
==========================================
New in v2:
  - Google OAuth 2.0 login
  - Friend / contact requests (send → accept / decline)
  - Group chats (name, emoji/logo, members, admin)
  - Extended emoji + GIF support (Tenor API)
"""

import os, uuid, secrets, mimetypes, json
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, session,
    jsonify, send_from_directory, abort, redirect, url_for
)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_bcrypt import Bcrypt
from authlib.integrations.flask_client import OAuth   # pip install authlib

# ──────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY              = os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'runechat.db')}",
    SQLALCHEMY_TRACK_MODIFICATIONS = False,
    MAX_CONTENT_LENGTH      = 16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY = True,
    SESSION_COOKIE_SAMESITE = "Lax",
    # Google OAuth — set these via env vars in production
    GOOGLE_CLIENT_ID        = os.environ.get("GOOGLE_CLIENT_ID", ""),
    GOOGLE_CLIENT_SECRET    = os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    # Tenor GIF API key — set via env var
    TENOR_API_KEY           = os.environ.get("TENOR_API_KEY", ""),
)

db       = SQLAlchemy(app)
bcrypt   = Bcrypt(app)
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=False)
oauth    = OAuth(app)

google = oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ──────────────────────────────────────────────
# Upload helpers
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
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"{uuid.uuid4().hex}.{ext}"

# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────
class User(db.Model):
    __tablename__ = "users"
    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(40),  unique=True, nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password     = db.Column(db.String(128), nullable=True)   # nullable for OAuth users
    display_name = db.Column(db.String(60),  nullable=False)
    avatar       = db.Column(db.String(10),  default="🧑")
    status       = db.Column(db.String(30),  default="Online")
    description  = db.Column(db.String(200), default="")
    google_id    = db.Column(db.String(128), unique=True, nullable=True)
    created_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

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
    """Confirmed bidirectional friendship stored as two rows."""
    __tablename__ = "contacts"
    id         = db.Column(db.Integer, primary_key=True)
    owner_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    added_at   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("owner_id", "contact_id"),)


class FriendRequest(db.Model):
    """Pending friend / chat request."""
    __tablename__ = "friend_requests"
    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status      = db.Column(db.String(10), default="pending")  # pending | accepted | declined
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("sender_id", "receiver_id"),)

    def to_dict(self):
        sender = User.query.get(self.sender_id)
        return {
            "id":         self.id,
            "sender_id":  self.sender_id,
            "receiver_id":self.receiver_id,
            "status":     self.status,
            "sender":     sender.to_public() if sender else None,
            "created_at": self.created_at.isoformat(),
        }


# Group <-> Member many-to-many association
group_members = db.Table(
    "group_members",
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id"), primary_key=True),
    db.Column("user_id",  db.Integer, db.ForeignKey("users.id"),  primary_key=True),
    db.Column("is_admin", db.Boolean, default=False),
    db.Column("joined_at",db.DateTime, default=lambda: datetime.now(timezone.utc)),
)


class Group(db.Model):
    __tablename__ = "groups"
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), nullable=False)
    logo        = db.Column(db.String(200), default="👥")   # emoji or /static/uploads/...
    description = db.Column(db.String(200), default="")
    room        = db.Column(db.String(80), unique=True, nullable=False)
    created_by  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    members     = db.relationship("User", secondary=group_members, backref="groups")

    def to_dict(self, uid=None):
        return {
            "id":          self.id,
            "name":        self.name,
            "logo":        self.logo,
            "description": self.description,
            "room":        self.room,
            "created_by":  self.created_by,
            "member_count":len(self.members),
            "members":     [m.to_public() for m in self.members],
            "is_admin":    (uid == self.created_by) if uid else False,
        }


class GroupInvite(db.Model):
    """Pending group add request."""
    __tablename__ = "group_invites"
    id         = db.Column(db.Integer, primary_key=True)
    group_id   = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    inviter_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    invitee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status     = db.Column(db.String(10), default="pending")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("group_id", "invitee_id"),)

    def to_dict(self):
        group   = Group.query.get(self.group_id)
        inviter = User.query.get(self.inviter_id)
        return {
            "id":         self.id,
            "group":      group.to_dict() if group else None,
            "inviter":    inviter.to_public() if inviter else None,
            "invitee_id": self.invitee_id,
            "status":     self.status,
        }


class Message(db.Model):
    __tablename__ = "messages"
    id         = db.Column(db.Integer, primary_key=True)
    room       = db.Column(db.String(80), nullable=False, index=True)
    sender_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    text       = db.Column(db.Text, default="")
    file_url   = db.Column(db.String(300), nullable=True)
    file_name  = db.Column(db.String(200), nullable=True)
    file_type  = db.Column(db.String(80),  nullable=True)
    gif_url    = db.Column(db.String(500), nullable=True)   # Tenor GIF URL
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
            "gif_url":   None if self.deleted else self.gif_url,
            "deleted":   self.deleted,
            "time":      self.created_at.strftime("%H:%M"),
            "me":        (self.sender_id == me_id) if me_id else False,
        }

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def room_id(a: int, b: int) -> str:
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
# Routes — Auth (username/password)
# ──────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html",
        tenor_key=app.config["TENOR_API_KEY"],
        google_enabled=bool(app.config["GOOGLE_CLIENT_ID"]),
    )

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
    if not user or not user.password or not bcrypt.check_password_hash(user.password, password):
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
# Routes — Google OAuth
# ──────────────────────────────────────────────
@app.route("/auth/google")
def google_login():
    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def google_callback():
    try:
        token = google.authorize_access_token()
        userinfo = token.get("userinfo") or google.userinfo()
        google_id  = userinfo["sub"]
        email      = userinfo.get("email", "")
        name       = userinfo.get("name", email.split("@")[0])
        picture    = userinfo.get("picture", "")

        user = User.query.filter_by(google_id=google_id).first()
        if not user:
            # Try to match on email
            user = User.query.filter_by(email=email).first()
            if user:
                user.google_id = google_id
            else:
                # Create new user
                base_uname = email.split("@")[0].lower().replace(".", "_")[:30]
                uname = base_uname
                ctr = 1
                while User.query.filter_by(username=uname).first():
                    uname = f"{base_uname}{ctr}"; ctr += 1
                user = User(
                    username=uname, email=email,
                    display_name=name, google_id=google_id,
                    avatar="🧑",
                )
                db.session.add(user)
            db.session.commit()

        session["user_id"] = user.id
        return redirect("/?authed=1")
    except Exception as e:
        return redirect(f"/?auth_error={str(e)[:80]}")

# ──────────────────────────────────────────────
# Routes — Account settings
# ──────────────────────────────────────────────
@app.route("/api/account", methods=["PATCH"])
@login_required
def update_account():
    u    = current_user()
    data = request.get_json(force=True)
    if "display_name"  in data: u.display_name  = str(data["display_name"])[:60]
    if "avatar"        in data: u.avatar        = str(data["avatar"])[:200]
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
# Routes — Friend Requests
# ──────────────────────────────────────────────
@app.route("/api/requests/send", methods=["POST"])
@login_required
def send_friend_request():
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
    # Already contacts?
    if Contact.query.filter_by(owner_id=uid, contact_id=target.id).first():
        return jsonify({"error": "Already in contacts"}), 409
    # Already sent / pending?
    existing = FriendRequest.query.filter_by(sender_id=uid, receiver_id=target.id, status="pending").first()
    if existing:
        return jsonify({"error": "Request already sent"}), 409
    # They already sent us one — auto-accept
    reverse = FriendRequest.query.filter_by(sender_id=target.id, receiver_id=uid, status="pending").first()
    if reverse:
        reverse.status = "accepted"
        db.session.add(Contact(owner_id=uid,      contact_id=target.id))
        db.session.add(Contact(owner_id=target.id, contact_id=uid))
        db.session.commit()
        rid = room_id(uid, target.id)
        # Notify both via socket
        socketio.emit("request_accepted", {"contact": {**target.to_public(), "room": rid, "last_msg": None, "unread": 0}}, to=f"user_{uid}")
        socketio.emit("request_accepted", {"contact": {**User.query.get(uid).to_public(), "room": rid, "last_msg": None, "unread": 0}}, to=f"user_{target.id}")
        return jsonify({"status": "accepted", "contact": {**target.to_public(), "room": rid}})
    fr = FriendRequest(sender_id=uid, receiver_id=target.id)
    db.session.add(fr)
    db.session.commit()
    # Notify receiver
    socketio.emit("friend_request", fr.to_dict(), to=f"user_{target.id}")
    return jsonify({"status": "sent", "request": fr.to_dict()}), 201

@app.route("/api/requests/pending")
@login_required
def pending_requests():
    uid = session["user_id"]
    reqs = FriendRequest.query.filter_by(receiver_id=uid, status="pending").all()
    return jsonify([r.to_dict() for r in reqs])

@app.route("/api/requests/<int:req_id>/accept", methods=["POST"])
@login_required
def accept_request(req_id):
    uid = session["user_id"]
    fr  = FriendRequest.query.get_or_404(req_id)
    if fr.receiver_id != uid:
        abort(403)
    if fr.status != "pending":
        return jsonify({"error": "Request already handled"}), 400
    fr.status = "accepted"
    db.session.add(Contact(owner_id=uid,        contact_id=fr.sender_id))
    db.session.add(Contact(owner_id=fr.sender_id, contact_id=uid))
    db.session.commit()
    rid     = room_id(uid, fr.sender_id)
    sender  = User.query.get(fr.sender_id)
    me_user = User.query.get(uid)
    socketio.emit("request_accepted", {"contact": {**sender.to_public(),  "room": rid, "last_msg": None, "unread": 0}}, to=f"user_{uid}")
    socketio.emit("request_accepted", {"contact": {**me_user.to_public(), "room": rid, "last_msg": None, "unread": 0}}, to=f"user_{fr.sender_id}")
    return jsonify({"ok": True, "contact": {**sender.to_public(), "room": rid, "last_msg": None, "unread": 0}})

@app.route("/api/requests/<int:req_id>/decline", methods=["POST"])
@login_required
def decline_request(req_id):
    uid = session["user_id"]
    fr  = FriendRequest.query.get_or_404(req_id)
    if fr.receiver_id != uid:
        abort(403)
    fr.status = "declined"
    db.session.commit()
    return jsonify({"ok": True})

# ──────────────────────────────────────────────
# Routes — Contacts (legacy add still works but now goes via requests)
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
        last_msg = (Message.query.filter_by(room=rid, deleted=False).order_by(Message.id.desc()).first())
        unread   = (Message.query.filter_by(room=rid, deleted=False).filter(Message.sender_id != uid).count())
        result.append({**u.to_public(), "room": rid, "last_msg": last_msg.to_dict() if last_msg else None, "unread": unread})
    return jsonify(result)

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
# Routes — Groups
# ──────────────────────────────────────────────
@app.route("/api/groups", methods=["GET"])
@login_required
def list_groups():
    uid = session["user_id"]
    u   = User.query.get(uid)
    return jsonify([g.to_dict(uid) for g in u.groups])

@app.route("/api/groups", methods=["POST"])
@login_required
def create_group():
    uid  = session["user_id"]
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()[:80]
    if not name:
        return jsonify({"error": "Group name required"}), 400
    logo = (data.get("logo") or "👥")[:200]
    desc = (data.get("description") or "")[:200]
    room = f"grp_{uuid.uuid4().hex[:16]}"
    creator = User.query.get(uid)
    g = Group(name=name, logo=logo, description=desc, room=room, created_by=uid)
    g.members.append(creator)
    db.session.add(g)
    db.session.commit()
    # Mark creator as admin via raw insert
    db.engine.execute(
        "UPDATE group_members SET is_admin=1 WHERE group_id=? AND user_id=?",
        g.id, uid
    ) if hasattr(db.engine, 'execute') else None
    return jsonify(g.to_dict(uid)), 201

@app.route("/api/groups/<int:gid>", methods=["PATCH"])
@login_required
def update_group(gid):
    uid = session["user_id"]
    g   = Group.query.get_or_404(gid)
    if g.created_by != uid:
        abort(403)
    data = request.get_json(force=True)
    if "name"        in data: g.name        = str(data["name"])[:80]
    if "logo"        in data: g.logo        = str(data["logo"])[:200]
    if "description" in data: g.description = str(data["description"])[:200]
    db.session.commit()
    return jsonify(g.to_dict(uid))

@app.route("/api/groups/<int:gid>/logo", methods=["POST"])
@login_required
def upload_group_logo(gid):
    uid = session["user_id"]
    g   = Group.query.get_or_404(gid)
    if g.created_by != uid:
        abort(403)
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not allowed_file(f.filename):
        return jsonify({"error": "File type not allowed"}), 400
    name = secure_filename_custom(f.filename)
    f.save(os.path.join(UPLOAD_DIR, name))
    g.logo = f"/static/uploads/{name}"
    db.session.commit()
    return jsonify({"logo": g.logo})

@app.route("/api/groups/<int:gid>/invite", methods=["POST"])
@login_required
def invite_to_group(gid):
    uid  = session["user_id"]
    g    = Group.query.get_or_404(gid)
    if uid not in [m.id for m in g.members]:
        abort(403)
    data = request.get_json(force=True)
    q    = (data.get("username") or "").strip().lower()
    target = User.query.filter_by(username=q).first()
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target in g.members:
        return jsonify({"error": "Already a member"}), 409
    existing = GroupInvite.query.filter_by(group_id=gid, invitee_id=target.id, status="pending").first()
    if existing:
        return jsonify({"error": "Already invited"}), 409
    inv = GroupInvite(group_id=gid, inviter_id=uid, invitee_id=target.id)
    db.session.add(inv)
    db.session.commit()
    socketio.emit("group_invite", inv.to_dict(), to=f"user_{target.id}")
    return jsonify({"ok": True, "invite": inv.to_dict()}), 201

@app.route("/api/groups/invites/pending")
@login_required
def pending_group_invites():
    uid = session["user_id"]
    invs = GroupInvite.query.filter_by(invitee_id=uid, status="pending").all()
    return jsonify([i.to_dict() for i in invs])

@app.route("/api/groups/invites/<int:inv_id>/accept", methods=["POST"])
@login_required
def accept_group_invite(inv_id):
    uid = session["user_id"]
    inv = GroupInvite.query.get_or_404(inv_id)
    if inv.invitee_id != uid:
        abort(403)
    inv.status = "accepted"
    g  = Group.query.get(inv.group_id)
    me = User.query.get(uid)
    if me not in g.members:
        g.members.append(me)
    db.session.commit()
    socketio.emit("group_member_joined", {"group": g.to_dict(uid), "user": me.to_public()}, to=g.room)
    return jsonify({"ok": True, "group": g.to_dict(uid)})

@app.route("/api/groups/invites/<int:inv_id>/decline", methods=["POST"])
@login_required
def decline_group_invite(inv_id):
    uid = session["user_id"]
    inv = GroupInvite.query.get_or_404(inv_id)
    if inv.invitee_id != uid:
        abort(403)
    inv.status = "declined"
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>/leave", methods=["POST"])
@login_required
def leave_group(gid):
    uid = session["user_id"]
    g   = Group.query.get_or_404(gid)
    me  = User.query.get(uid)
    if me in g.members:
        g.members.remove(me)
        db.session.commit()
    return jsonify({"ok": True})

# ──────────────────────────────────────────────
# Routes — Messages
# ──────────────────────────────────────────────
def _verify_room_access(uid, room):
    """Return True if uid is allowed in this room."""
    if room.startswith("dm_"):
        parts = room.split("_")
        if len(parts) != 3: return False
        a, b = int(parts[1]), int(parts[2])
        return uid in (a, b)
    if room.startswith("grp_"):
        g = Group.query.filter_by(room=room).first()
        if not g: return False
        return any(m.id == uid for m in g.members)
    return False

@app.route("/api/messages/<room>")
@login_required
def get_messages(room):
    uid = session["user_id"]
    if not _verify_room_access(uid, room):
        abort(403)
    msgs = (Message.query.filter_by(room=room).order_by(Message.id.asc()).limit(200).all())
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
    return jsonify({"file_url": f"/static/uploads/{name}", "file_name": f.filename, "file_type": mime})

@app.route("/static/uploads/<path:filename>")
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)

# Tenor GIF proxy (avoids exposing API key to client)
@app.route("/api/gifs/search")
@login_required
def search_gifs():
    import urllib.request, urllib.parse
    q    = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 20)), 50)
    key  = app.config["TENOR_API_KEY"]
    if not key:
        return jsonify({"results": [], "error": "No Tenor key configured"})
    url = f"https://tenor.googleapis.com/v2/search?q={urllib.parse.quote(q)}&key={key}&limit={limit}&media_filter=gif,tinygif"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})

@app.route("/api/gifs/featured")
@login_required
def featured_gifs():
    import urllib.request
    key   = app.config["TENOR_API_KEY"]
    limit = min(int(request.args.get("limit", 20)), 50)
    if not key:
        return jsonify({"results": [], "error": "No Tenor key configured"})
    url = f"https://tenor.googleapis.com/v2/featured?key={key}&limit={limit}&media_filter=gif,tinygif"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})

# ──────────────────────────────────────────────
# SocketIO events
# ──────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid:
        return False
    join_room(f"user_{uid}")   # personal room for notifications

@socketio.on("join")
def on_join(data):
    uid  = session.get("user_id")
    room = data.get("room", "")
    if not _verify_room_access(uid, room):
        return
    join_room(room)

@socketio.on("leave")
def on_leave(data):
    leave_room(data.get("room", ""))

@socketio.on("send_message")
def on_message(data):
    uid = session.get("user_id")
    if not uid: return
    room      = data.get("room", "")
    text      = (data.get("text") or "").strip()[:4000]
    file_url  = data.get("file_url")
    file_name = data.get("file_name")
    file_type = data.get("file_type")
    gif_url   = data.get("gif_url")

    if not text and not file_url and not gif_url: return
    if not _verify_room_access(uid, room): return

    msg = Message(room=room, sender_id=uid, text=text,
                  file_url=file_url, file_name=file_name,
                  file_type=file_type, gif_url=gif_url)
    db.session.add(msg)
    db.session.commit()
    emit("new_message", msg.to_dict(me_id=uid), to=room)

@socketio.on("typing")
def on_typing(data):
    uid = session.get("user_id")
    if uid:
        u = User.query.get(uid)
        emit("typing", {"user_id": uid, "display_name": u.display_name if u else ""}, to=data.get("room",""), include_self=False)

@socketio.on("stop_typing")
def on_stop_typing(data):
    uid = session.get("user_id")
    if uid:
        emit("stop_typing", {"user_id": uid}, to=data.get("room",""), include_self=False)

# ──────────────────────────────────────────────
# Init DB
# ──────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=8333)