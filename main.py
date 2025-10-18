import pymysql
pymysql.install_as_MySQLdb()
from io import BytesIO
from flask import Flask, request, render_template, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
import mysql.connector
import bcrypt
import re
from datetime import timedelta
from flask_session import Session
from flask_sqlalchemy import SQLAlchemy
devices = {}
user_sockets = {}  # <--- thêm dòng này
app = Flask(__name__)
app.secret_key = 'super_secure_key_here'  
app.permanent_session_lifetime = timedelta(minutes=50000)  
socketio = SocketIO(app, cors_allowed_origins="*", logger=True, engineio_logger=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://root:@127.0.0.1:3306/camera_app?charset=utf8mb4'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_TYPE'] = 'sqlalchemy'
app.config['SESSION_SQLALCHEMY'] = SQLAlchemy(app)  
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'your_prefix:'

Session(app)

def get_db_connection():
    return mysql.connector.connect(
        host='localhost',
        user='root',
        password='',
        database='camera_app'
    )

def is_valid_username(username):
    return bool(re.match(r'^[a-zA-Z0-9_.-]{3,20}$', username))

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user WHERE username = %s', (username,))
        if cursor.fetchone():
            return "Username already exists.", 400
        cursor.execute('INSERT INTO user (username, password) VALUES (%s, %s)', (username, hashed_password))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, password FROM user WHERE username = %s', (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and bcrypt.checkpw(password.encode('utf-8'), user[1].encode('utf-8')):
            session.permanent = True
            session['user_id'] = user[0]
            return redirect(url_for('dashboard'))
        
        return "Invalid credentials.", 401
    return render_template('login.html')
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Load danh sách thiết bị chỉ có viewer xem được (camera gửi stream chứ không xem)
    cursor.execute('SELECT device_id, cameras FROM device WHERE user_id = %s AND is_camera = FALSE', (user_id,))
    devices = cursor.fetchall()
    cursor.close()
    conn.close()

    # Luôn luôn xác định là viewer (không phải camera)
    return render_template('dashboard.html', devices=devices, user_id=user_id, is_camera_device=False)
@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

@app.route('/add_device', methods=['GET', 'POST'])
def add_device():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        device_id = request.form['device_id']
        cameras = request.form.getlist('cameras')
        cameras_str = ','.join(cameras)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO device (user_id, device_id, cameras) VALUES (%s, %s, %s)',
                       (session['user_id'], device_id, cameras_str))
        conn.commit()
        cursor.close()
        conn.close()

        return redirect(url_for('dashboard'))

    return render_template('add_device.html')

# Định nghĩa: room = device_id, nhưng thêm target_id để biết ai gửi cho ai

@socketio.on('offer')
def handle_offer(data):
    room = data['room']  # device_id
    target_id = data.get('target_id')  # viewer_id
    if target_id:
        emit('offer', data, room=target_id)  # Chỉ gửi cho viewer cụ thể
    else:
        emit('offer', data, room=room, broadcast=True)  # fallback (nếu cần)

@socketio.on('answer')
def handle_answer(data):
    room = data['room']  # device_id
    target_id = data.get('target_id')  # camera_id
    if target_id:
        emit('answer', data, room=target_id)  # Trả lời camera cụ thể
    else:
        emit('answer', data, room=room, broadcast=True)  # fallback

@socketio.on('candidate')
def handle_candidate(data):
    room = data['room']  # device_id
    target_id = data.get('target_id')  # gửi tới ai
    if target_id:
        emit('candidate', data, room=target_id)  # Chỉ gửi đúng người
    else:
        emit('candidate', data, room=room, broadcast=True)  # fallback

@socketio.on('connect')
def connect():
    uid = str(session.get('user_id', ''))
    print(f"✅ Connected {uid} SID={request.sid}")
    if uid:
        user_sockets[uid] = request.sid
        join_room(uid)

@socketio.on('disconnect')
def disconnect():
    for d, sid in list(devices.items()):
        if sid == request.sid:
            del devices[d]
    for u, sid in list(user_sockets.items()):
        if sid == request.sid:
            del user_sockets[u]
    print(f"❌ Disconnected {request.sid}")

@socketio.on('register_device')
def register_device(data):
    did = str(data['device_id'])
    devices[did] = request.sid
    join_room(did)
    print(f"📡 Device {did} online")

@socketio.on('notify_view_camera')
def notify_view_camera(data):
    did = str(data['device_id'])
    vid = str(data['viewer_id'])
    print(f"🔔 Viewer {vid} yêu cầu xem {did}")
    socketio.emit('register_camera_command', {'device_id': did, 'viewer_id': vid}, to=devices.get(did))

@socketio.on('frame_binary')
def frame_binary(data):
    did = str(data['device_id'])
    sid_sender = request.sid
    tid = str(data['target_id'])
    buf = data['frame']
    print(f"📤 Frame {did} {sid_sender} → {tid}")
    if tid in user_sockets:
        emit('frame_to_viewer', {'frame': buf}, room=user_sockets[tid], binary=True)
    else:
        print(f"⚠️ Viewer {tid} not found")
if __name__ == '__main__':
    socketio.run(app,host='0.0.0.0',certfile="localhost.crt", keyfile="localhost.key")