from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import psycopg2
import psycopg2.extras
import uuid
import os
import time
import json
import urllib.request
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.urandom(24) 

# --- CONFIGURATION ---
DB_HOST = os.environ.get("DB_HOST", "db")
DB_NAME = os.environ.get("POSTGRES_DB", "sentinel_data")
DB_USER = os.environ.get("POSTGRES_USER", "sentinel_admin")
DB_PASS = os.environ.get("POSTGRES_PASSWORD", "secure_password_123")

# Discord Webhook URL 
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1543228121614917702/OhTaAvNl8rXkk84bgGkkzh0vkNCxcfP4LKBDhc5BTJ1mUhicbv51kDhCsb0PV5IzfC6E"

def send_alert(message):
    """Sends a real-time alert to Discord with a disguised User-Agent to bypass bot filters."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        data = json.dumps({"content": message}).encode('utf-8')
        # We must include a custom User-Agent, otherwise Discord blocks Python-urllib!
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, headers=headers, method='POST')
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Failed to send webhook: {e}")

def get_db_connection():
    attempts = 10
    while attempts > 0:
        try:
            conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS)
            return conn
        except psycopg2.OperationalError:
            attempts -= 1
            print(f"Database warming up... retrying in 3 seconds ({attempts} attempts left).")
            time.sleep(3)
    raise Exception("Fatal: Could not connect to the database.")

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            account_tier VARCHAR(50) DEFAULT 'free',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    cur.execute('''
        ALTER TABLE users ADD COLUMN IF NOT EXISTS prefix VARCHAR(20);
        ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255);
        ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(50);
        ALTER TABLE users ADD COLUMN IF NOT EXISTS place_of_stay VARCHAR(255);
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            device_id VARCHAR(50) PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            device_name VARCHAR(255) NOT NULL,
            api_key VARCHAR(255) UNIQUE NOT NULL,
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # FEATURE: Add the Panic Mode column to the database safely
    cur.execute('''ALTER TABLE devices ADD COLUMN IF NOT EXISTS lost_mode BOOLEAN DEFAULT FALSE;''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS telemetry (
            id SERIAL PRIMARY KEY,
            device_id VARCHAR(50) REFERENCES devices(device_id) ON DELETE CASCADE,
            ip_address VARCHAR(50),
            latitude NUMERIC(10, 7),
            longitude NUMERIC(10, 7),
            battery_level INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    
    conn.commit()
    cur.close()
    conn.close()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", ('admin@barakaberenge.com',))
    if not cur.fetchone():
        hashed_pw = generate_password_hash('admin123')
        cur.execute("""
            INSERT INTO users (email, password_hash, account_tier, full_name, prefix, phone_number, place_of_stay)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, ('admin@barakaberenge.com', hashed_pw, 'premium', 'Baraka Berenge', 'Eng.', '+254700000000', 'Nairobi, Kenya'))
        conn.commit()
    cur.close()
    conn.close()
    print("Database tables initialized successfully!")

init_db()

# --- WEB UI ROUTES ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute('''
        SELECT d.device_id, d.device_name, d.lost_mode, t.latitude, t.longitude, t.battery_level, t.timestamp
        FROM devices d
        LEFT JOIN LATERAL (
            SELECT latitude, longitude, battery_level, timestamp
            FROM telemetry
            WHERE device_id = d.device_id
            ORDER BY timestamp DESC
            LIMIT 1
        ) t ON true
        WHERE d.user_id = %s
    ''', (session['user_id'],))
    
    devices = cur.fetchall()
    devices_list = [dict(row) for row in devices]
    
    for dev in devices_list:
        cur.execute("""
            SELECT latitude, longitude, battery_level, timestamp
            FROM telemetry
            WHERE device_id = %s
            ORDER BY timestamp DESC
            LIMIT 50
        """, (dev['device_id'],))
        
        history_rows = cur.fetchall()
        formatted_history = []
        for h in history_rows:
            formatted_history.append({
                'latitude': float(h['latitude']) if h['latitude'] is not None else None,
                'longitude': float(h['longitude']) if h['longitude'] is not None else None,
                'battery_level': h['battery_level'],
                'timestamp': h['timestamp'].strftime('%Y-%m-%d %H:%M:%S') if h['timestamp'] else 'Never'
            })
        
        dev['history'] = formatted_history
        
        if dev['timestamp']:
            dev['timestamp'] = dev['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        else:
            dev['timestamp'] = 'Never'
            
        if dev['latitude'] is not None:
            dev['latitude'] = float(dev['latitude'])
        if dev['longitude'] is not None:
            dev['longitude'] = float(dev['longitude'])
            
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', email=session['email'], devices=devices_list, devices_json=json.dumps(devices_list))

@app.route('/device/<device_id>/toggle_panic', methods=['POST'])
def toggle_panic(device_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    conn = get_db_connection()
    cur = conn.cursor()
    # Flips the boolean from TRUE to FALSE or vice versa so you can turn it off!
    cur.execute("UPDATE devices SET lost_mode = NOT lost_mode WHERE device_id = %s AND user_id = %s", (device_id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    
    send_alert(f"🚨 **PANIC MODE TOGGLED:** Status changed for device `{device_id}`.")
    return redirect(url_for('dashboard'))

# --- REGISTRATION ROUTE ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        prefix = request.form.get('prefix', 'Mr.')
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone_number = request.form.get('phone_number', '').strip()
        place_of_stay = request.form.get('place_of_stay', '').strip()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:
            error = "Passwords do not match."
        else:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                error = "An account with this email address is already registered."
            else:
                hashed_pw = generate_password_hash(password)
                cur.execute("""
                    INSERT INTO users (prefix, full_name, email, phone_number, place_of_stay, password_hash)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (prefix, full_name, email, phone_number, place_of_stay, hashed_pw))
                user_id = cur.fetchone()[0]
                conn.commit()
                
                send_alert(f"🎉 **NEW USER:** {full_name} ({email}) just registered on Sentinel!")
                
                session['user_id'] = user_id
                session['email'] = email
                session['full_name'] = full_name
                cur.close()
                conn.close()
                return redirect(url_for('dashboard'))
            cur.close()
            conn.close()
    return render_template('register.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['email'] = user['email']
            if user['email'].lower() == 'admin@barakaberenge.com':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('dashboard'))
        else:
            error = 'Invalid email or password.'
    return render_template('login.html', error=error)

@app.route('/enroll', methods=['GET', 'POST'])
def enroll():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        device_name = request.form['device_name']
        device_id = 'DEV-' + str(uuid.uuid4())[:8].upper()
        api_key = 'sk_sentinel_' + uuid.uuid4().hex[:16]
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO devices (device_id, user_id, device_name, api_key) VALUES (%s, %s, %s, %s)",
                    (device_id, session['user_id'], device_name, api_key))
        conn.commit()
        cur.close()
        conn.close()
        
        send_alert(f"💻 **NEW DEVICE:** A new hardware unit `{device_name}` was enrolled.")
        return render_template('install.html', api_key=api_key, device_name=device_name)
    return render_template('enroll.html')

# ---ADMIN ROUTES ---
@app.route('/admin')
def admin_dashboard():
    if 'user_id' not in session or session.get('email', '').lower() != 'admin@barakaberenge.com':
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT id, prefix, full_name, email, phone_number, place_of_stay, account_tier, created_at FROM users ORDER BY created_at DESC")
    users = cur.fetchall()
    cur.execute('''
        SELECT d.device_id, d.device_name, d.api_key, u.email as owner_email, 
               t.battery_level, t.timestamp, t.ip_address
        FROM devices d
        JOIN users u ON d.user_id = u.id
        LEFT JOIN LATERAL (
            SELECT battery_level, timestamp, ip_address FROM telemetry WHERE device_id = d.device_id ORDER BY timestamp DESC LIMIT 1
        ) t ON true ORDER BY d.created_at DESC
    ''')
    all_devices = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('admin.html', users=users, devices=all_devices)

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if 'user_id' not in session or session.get('email', '').lower() != 'admin@barakaberenge.com':
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_device/<device_id>', methods=['POST'])
def admin_delete_device(device_id):
    if 'user_id' not in session or session.get('email', '').lower() != 'admin@barakaberenge.com':
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM devices WHERE device_id = %s", (device_id,))
    conn.commit()
    cur.close()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/user/<int:user_id>')
def admin_user_detail(user_id):
    if 'user_id' not in session or session.get('email', '').lower() != 'admin@barakaberenge.com':
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT id, prefix, full_name, email, phone_number, place_of_stay, account_tier, created_at FROM users WHERE id = %s", (user_id,))
    client = cur.fetchone()
    if not client:
        cur.close()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    cur.execute('''
        SELECT d.device_id, d.device_name, d.api_key, d.created_at, t.battery_level, t.timestamp, t.ip_address, t.latitude, t.longitude
        FROM devices d LEFT JOIN LATERAL (
            SELECT battery_level, timestamp, ip_address, latitude, longitude FROM telemetry WHERE device_id = d.device_id ORDER BY timestamp DESC LIMIT 1
        ) t ON true WHERE d.user_id = %s ORDER BY d.created_at DESC
    ''', (user_id,))
    client_devices = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('admin_user.html', client=client, devices=client_devices)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- API ENDPOINT FOR LAPTOP TELEMETRY ---
@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return jsonify({"error": "Unauthorized."}), 401
        
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    cur.execute("SELECT device_id, device_name, lost_mode FROM devices WHERE api_key = %s", (api_key,))
    device = cur.fetchone()
    if not device:
        cur.close()
        conn.close()
        return jsonify({"error": "Invalid API Key."}), 403
        
    device_id = device['device_id']
    device_name = device['device_name']
    lost_mode = device['lost_mode']
    battery = data.get('battery_level')

    # Anti-Spam Webhook Logic: Only alert if it JUST dropped below 15%
    if battery is not None and int(battery) <= 15:
        cur.execute("SELECT battery_level FROM telemetry WHERE device_id = %s ORDER BY timestamp DESC LIMIT 1", (device_id,))
        last_tel = cur.fetchone()
        if not last_tel or (last_tel['battery_level'] is not None and last_tel['battery_level'] > 15):
            send_alert(f"⚠️ **LOW BATTERY:** `{device_name}` has dropped to {battery}%!")

    cur.execute('''
        INSERT INTO telemetry (device_id, ip_address, latitude, longitude, battery_level)
        VALUES (%s, %s, %s, %s, %s)
    ''', (device_id, data.get('ip_address', request.remote_addr), data.get('latitude'), data.get('longitude'), int(battery) if battery is not None else None))
    
    conn.commit()
    cur.close()
    conn.close()
    
    # If Panic Mode is true, we tell the laptop to ping every 60 seconds instead of 300 (5 mins)
    return jsonify({
        "status": "success", 
        "message": "Telemetry logged.",
        "interval_seconds": 60 if lost_mode else 300 
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)