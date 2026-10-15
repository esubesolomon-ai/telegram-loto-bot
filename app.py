import os, json, hmac, hashlib, secrets, sqlite3, random
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
import requests

APP_SECRET = os.environ.get('APP_SECRET', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
DB_PATH = os.environ.get('DB_PATH', 'lottery.db')
DEMO_MODE = os.environ.get('DEMO_MODE', 'true').lower() != 'false'
app = Flask(__name__)
app.secret_key = APP_SECRET or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','true').lower() == 'true', PERMANENT_SESSION_LIFETIME=timedelta(minutes=30), MAX_CONTENT_LENGTH=1024*1024)

PRICES = {'daily': 100, 'weekly': 50, 'monthly': 50}
LABELS = {'daily': 'Daily', 'weekly': 'Weekly', 'monthly': 'Monthly'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS players (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, first_name TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draws (id INTEGER PRIMARY KEY, draw_type TEXT NOT NULL, title TEXT NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', closes_at TEXT NOT NULL, winning_number TEXT, seed_commitment TEXT, seed_reveal TEXT, created_at TEXT NOT NULL, closed_at TEXT, drawn_at TEXT);
CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, ticket_code TEXT UNIQUE NOT NULL, draw_id INTEGER NOT NULL REFERENCES draws(id), player_id INTEGER NOT NULL REFERENCES players(id), number TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'demo', created_at TEXT NOT NULL, UNIQUE(draw_id, number));
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, entry_type TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'ETB', status TEXT NOT NULL, reference TEXT UNIQUE NOT NULL, player_id INTEGER, ticket_id INTEGER, notes TEXT, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, object_type TEXT, object_id TEXT, details TEXT, created_at TEXT NOT NULL);
'''

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    con=db(); con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('terms_version','1.0'),('draw_policy','If no ticket matches the winning number, the prize remains unclaimed pending the published rules; no reroll.'),('demo_mode',?)", ('true' if DEMO_MODE else 'false',))
    if ADMIN_PASSWORD:
        con.execute('INSERT OR IGNORE INTO admins(username,password_hash,role) VALUES(?,?,?)', (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), 'owner'))
    con.commit(); con.close()

def audit(actor, action, object_type=None, object_id=None, details=None):
    con=db(); con.execute('INSERT INTO audit_log(actor,action,object_type,object_id,details,created_at) VALUES(?,?,?,?,?,?)', (actor,action,object_type,str(object_id) if object_id is not None else None, json.dumps(details or {}, ensure_ascii=False),now_iso())); con.commit(); con.close()

def tg(method, payload):
    if not BOT_TOKEN: return None
    try:
        r=requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}', json=payload, timeout=8)
        r.raise_for_status(); return r.json()
    except requests.RequestException:
        app.logger.exception('Telegram API call failed')
        return None

def send_message(chat_id, text, reply_markup=None):
    data={'chat_id':chat_id,'text':text,'disable_web_page_preview':True}
    if reply_markup: data['reply_markup']=reply_markup
    return tg('sendMessage',data)

def main_keyboard():
    return {'keyboard':[[{'text':'🎟 Buy demo ticket'},{'text':'📋 My tickets'}],[{'text':'🎲 Available draws'},{'text':'📜 Rules'}]],'resize_keyboard':True}

def active_draws():
    con=db(); rows=con.execute("SELECT * FROM draws WHERE status='open' AND closes_at>? ORDER BY closes_at",(now_iso(),)).fetchall(); con.close(); return rows

def draw_text(draw):
    deadline=draw['closes_at'].replace('T',' ')[:16]+' UTC'
    return f"{draw['title']}\nTicket price: {draw['price']} ETB (DEMO only)\nCloses: {deadline}\nStatus: {draw['status'].upper()}\nDraw ID: {draw['id']}"

def player_upsert(user):
    con=db(); con.execute('INSERT INTO players(telegram_id,username,first_name,created_at) VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name', (user['id'],user.get('username'),user.get('first_name','Player'),now_iso())); con.commit(); row=con.execute('SELECT id FROM players WHERE telegram_id=?',(user['id'],)).fetchone(); con.close(); return row['id']

def rules_text():
    return ('ETHIO LOTTERY — DEMO RULES\n\nThis bot is a demonstration. No real payments are accepted and no cash prizes are paid.\nDaily ticket: 100 ETB (display only). Weekly: 50 ETB. Monthly: 50 ETB.\nEach ticket has a six-digit number from 000000 to 999999. Numbers are unique within each draw.\nThe winning number is selected using a cryptographically secure random generator after sales close. The operator must not reroll just because no ticket matches.\nA ticket is valid only when its receipt is recorded by the bot. A ticket applies to one draw only.\nReal-money play requires legal authorization, approved payments, published prize/refund rules, age and eligibility controls, and reliable storage.')

def handle_update(update):
    msg=update.get('message')
    if not msg: return
    user=msg.get('from',{}); chat_id=msg.get('chat',{}).get('id'); text=(msg.get('text') or '').strip()
    if not chat_id or not user.get('id'): return
    pid=player_upsert(user)
    if text.startswith('/start'):
        send_message(chat_id,'Welcome to Ethio Lottery Demo. No money is collected and no cash prizes are paid in this test version. Choose an option below.',main_keyboard()); return
    if text in ['/rules','📜 Rules']:
        send_message(chat_id,rules_text(),main_keyboard()); return
    if text in ['/draws','🎲 Available draws']:
        rows=active_draws()
        send_message(chat_id, '\n\n'.join(draw_text(r) for r in rows) if rows else 'There are no open draws right now. Please check again later.',main_keyboard()); return
    if text in ['/mytickets','📋 My tickets']:
        con=db(); rows=con.execute('SELECT t.ticket_code,t.number,t.status,t.created_at,d.title,d.status AS draw_status,d.winning_number FROM tickets t JOIN draws d ON d.id=t.draw_id WHERE t.player_id=? ORDER BY t.id DESC LIMIT 20',(pid,)).fetchall(); con.close()
        if not rows: send_message(chat_id,'You do not have any tickets yet.',main_keyboard()); return
        lines=[]
        for r in rows:
            outcome=''
            if r['winning_number']: outcome=' | WINNER' if r['number']==r['winning_number'] else ' | not winner'
            lines.append(f"{r['ticket_code']} — {r['title']} — #{r['number']} — {r['status']}{outcome}")
        send_message(chat_id,'YOUR LAST TICKETS\n'+'\n'.join(lines),main_keyboard()); return
    if text in ['/buy','🎟 Buy demo ticket']:
        rows=active_draws()
        if not rows: send_message(chat_id,'No open draws are available. Check back later.',main_keyboard()); return
        buttons=[]
        for d in rows: buttons.append([{'text':f"{d['title']} — {d['price']} ETB (demo)",'callback_data':f"buy:{d['id']}"}])
        tg('sendMessage',{'chat_id':chat_id,'text':'Choose a draw. This creates a demo ticket only; do not send money.','reply_markup':{'inline_keyboard':buttons}}); return
    if text.startswith('/admin'):
        send_message(chat_id,'Admin controls are available only in the secure web dashboard.'); return
    send_message(chat_id,'Please choose an option from the menu or use /rules, /draws, /mytickets, /buy.',main_keyboard())

def handle_callback(update):
    cb=update.get('callback_query',{}); data=cb.get('data',''); user=cb.get('from',{}); message=cb.get('message',{}); chat_id=message.get('chat',{}).get('id')
    tg('answerCallbackQuery',{'callback_query_id':cb.get('id')})
    if not data.startswith('buy:') or not user.get('id') or not chat_id: return
    try: draw_id=int(data.split(':',1)[1])
    except ValueError: return
    pid=player_upsert(user); con=db(); draw=con.execute("SELECT * FROM draws WHERE id=? AND status='open' AND closes_at>?",(draw_id,now_iso())).fetchone()
    if not draw: con.close(); send_message(chat_id,'This draw is closed or unavailable.',main_keyboard()); return
    used=set(r['number'] for r in con.execute('SELECT number FROM tickets WHERE draw_id=?',(draw_id,)).fetchall())
    if len(used)>=1_000_000: con.close(); send_message(chat_id,'This draw has reached its ticket-number capacity.',main_keyboard()); return
    # Securely sample from unused numbers; avoid a long loop near capacity.
    available_count=1_000_000-len(used)
    if available_count <= 10000:
        candidate=secrets.randbelow(1_000_000)
        while f'{candidate:06d}' in used: candidate=secrets.randbelow(1_000_000)
    else:
        candidate=secrets.randbelow(1_000_000)
        while f'{candidate:06d}' in used: candidate=secrets.randbelow(1_000_000)
    number=f'{candidate:06d}'; code='ETL-'+secrets.token_hex(4).upper()
    try:
        cur=con.execute('INSERT INTO tickets(ticket_code,draw_id,player_id,number,status,created_at) VALUES(?,?,?,?,?,?)',(code,draw_id,pid,number,'DEMO',now_iso()))
        ticket_id=cur.lastrowid
        con.execute('INSERT INTO ledger(entry_type,amount,currency,status,reference,player_id,ticket_id,notes,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)',('demo_ticket',draw['price'],'ETB','DEMO_ONLY','DEMO-'+code,pid,ticket_id,'Not a real payment; no money collected.',now_iso(),'system'))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback(); con.close(); send_message(chat_id,'A conflict occurred while assigning the number. Please try again.',main_keyboard()); return
    con.close()
    send_message(chat_id,f"DEMO TICKET RECEIPT\nTicket ID: {code}\nDraw: {draw['title']}\nNumber: {number}\nDisplayed price: {draw['price']} ETB\nPayment: NONE — DEMO ONLY\nCreated: {now_iso()}\nKeep this receipt. Use /mytickets to check it.",main_keyboard())

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    app.logger.info("Telegram update received; keys=%s", list(update.keys()))
    if not WEBHOOK_SECRET or not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),WEBHOOK_SECRET): abort(403)
    update=request.get_json(silent=True) or {}
    if 'callback_query' in update: handle_callback(update)
    else: handle_update(update)
    return {'ok':True}

@app.route('/health')
def health(): return {'ok':True,'mode':'DEMO' if DEMO_MODE else 'CONFIGURED','service':'Ethio Lottery'},200

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args,**kwargs):
        if not session.get('admin_id'): return redirect(url_for('login'))
        return fn(*args,**kwargs)
    return wrapper

def csrf_token():
    if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(24)
    return session['csrf']
@app.context_processor
def inject_helpers(): return {'csrf_token':csrf_token,'demo_mode':DEMO_MODE}
def check_csrf():
    if not session.get('csrf') or not hmac.compare_digest(session.get('csrf',''),request.form.get('csrf','')): abort(400,'Invalid form token')

@app.route('/admin/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        check_csrf(); username=request.form.get('username','')[:100]; password=request.form.get('password','')
        con=db(); row=con.execute('SELECT * FROM admins WHERE username=? AND active=1',(username,)).fetchone(); con.close()
        if row and check_password_hash(row['password_hash'],password):
            session.clear(); session['admin_id']=row['id']; session['username']=row['username']; session['role']=row['role']; session.permanent=True
            audit(username,'admin_login','admin',row['id']); return redirect(url_for('dashboard'))
        audit(username or 'unknown','admin_login_failed','admin',None); flash('Login failed. Check credentials.','error')
    return render_template('login.html')
@app.route('/admin/logout',methods=['POST'])
@admin_required
def logout(): check_csrf(); audit(session.get('username','admin'),'admin_logout','admin',session.get('admin_id')); session.clear(); return redirect(url_for('login'))

@app.route('/admin')
@admin_required
def dashboard():
    con=db()
    counts={ 'players':con.execute('SELECT COUNT(*) FROM players').fetchone()[0], 'tickets':con.execute('SELECT COUNT(*) FROM tickets').fetchone()[0], 'draws':con.execute('SELECT COUNT(*) FROM draws').fetchone()[0], 'open_draws':con.execute("SELECT COUNT(*) FROM draws WHERE status='open'").fetchone()[0] }
    ledger=con.execute("SELECT entry_type,status,COUNT(*) AS n,COALESCE(SUM(amount),0) AS amount FROM ledger GROUP BY entry_type,status ORDER BY entry_type").fetchall()
    draws=con.execute('SELECT d.*, (SELECT COUNT(*) FROM tickets t WHERE t.draw_id=d.id) AS ticket_count FROM draws d ORDER BY d.id DESC LIMIT 30').fetchall()
    audit_rows=con.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 15').fetchall(); con.close()
    return render_template('dashboard.html',counts=counts,ledger=ledger,draws=draws,audit_rows=audit_rows)

@app.route('/admin/draws/create',methods=['POST'])
@admin_required
def create_draw():
    check_csrf(); kind=request.form.get('draw_type','daily'); title=request.form.get('title','').strip()[:100]; price=PRICES.get(kind)
    try: close=datetime.fromisoformat(request.form.get('closes_at','')).replace(tzinfo=timezone.utc)
    except (ValueError,TypeError): flash('Enter a valid closing date/time.','error'); return redirect(url_for('dashboard'))
    if kind not in PRICES or not title or not price or close<=datetime.now(timezone.utc): flash('Choose a draw type, title, and future closing time.','error'); return redirect(url_for('dashboard'))
    con=db(); cur=con.execute('INSERT INTO draws(draw_type,title,price,status,closes_at,created_at) VALUES(?,?,? ,?,?,?)',(kind,title,price,'open',close.isoformat(timespec='seconds'),now_iso())); draw_id=cur.lastrowid; con.commit(); con.close()
    audit(session.get('username','admin'),'draw_created','draw',draw_id,{'type':kind,'title':title,'price':price,'closes_at':close.isoformat()}); flash('Demo draw created.','success'); return redirect(url_for('dashboard'))

@app.route('/admin/draw/<int:draw_id>/close',methods=['POST'])
@admin_required
def close_draw(draw_id):
    check_csrf(); con=db(); row=con.execute('SELECT * FROM draws WHERE id=?',(draw_id,)).fetchone()
    if not row or row['status']!='open': con.close(); flash('Draw not found or not open.','error'); return redirect(url_for('dashboard'))
    con.execute("UPDATE draws SET status='closed',closed_at=? WHERE id=?",(now_iso(),draw_id)); con.commit(); con.close(); audit(session.get('username','admin'),'draw_closed','draw',draw_id); flash('Draw closed. You may now run the demo draw.','success'); return redirect(url_for('dashboard'))

@app.route('/admin/draw/<int:draw_id>/run',methods=['POST'])
@admin_required
def run_draw(draw_id):
    check_csrf(); con=db(); row=con.execute("SELECT * FROM draws WHERE id=? AND status='closed'",(draw_id,)).fetchone()
    if not row: con.close(); flash('Close the draw before selecting a winning number.','error'); return redirect(url_for('dashboard'))
    # Draw result is generated once; never reroll because no ticket matches.
    seed=secrets.token_bytes(32); commitment=hashlib.sha256(seed).hexdigest()
    # Derive an unbiased six-digit result from the committed seed using rejection sampling.
    modulus=1_000_000; limit=(1 << 256) - ((1 << 256) % modulus); counter=0
    while True:
        digest=hashlib.sha256(seed + counter.to_bytes(4, 'big')).digest(); value=int.from_bytes(digest, 'big')
        if value < limit: break
        counter += 1
    number=f'{value % modulus:06d}'
    con.execute("UPDATE draws SET status='drawn',winning_number=?,seed_commitment=?,seed_reveal=?,drawn_at=? WHERE id=? AND status='closed'",(number,commitment,seed.hex(),now_iso(),draw_id)); con.commit(); count=con.execute('SELECT COUNT(*) FROM tickets WHERE draw_id=? AND number=?',(draw_id,number)).fetchone()[0]; con.close()
    audit(session.get('username','admin'),'demo_draw_run','draw',draw_id,{'winning_number':number,'seed_commitment':commitment,'matched_tickets':count}); flash(f'DEMO result: {number}. Matching tickets: {count}. No real prize payout is enabled.','success'); return redirect(url_for('dashboard'))

@app.route('/admin/settings',methods=['GET','POST'])
@admin_required
def settings():
    con=db()
    if request.method=='POST':
        check_csrf(); policy=request.form.get('draw_policy','').strip()[:1000]
        if policy:
            con.execute("INSERT INTO settings(key,value) VALUES('draw_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(policy,)); con.commit(); audit(session.get('username','admin'),'settings_updated','settings','draw_policy',{'value':policy}); flash('Rules setting saved. Review terms before any public launch.','success')
    rows=con.execute('SELECT key,value FROM settings ORDER BY key').fetchall(); con.close(); return render_template('settings.html',settings=rows)

@app.route('/admin/ledger')
@admin_required
def ledger_page():
    con=db(); rows=con.execute('SELECT * FROM ledger ORDER BY id DESC LIMIT 200').fetchall(); con.close(); return render_template('ledger.html',rows=rows)

@app.route('/admin/tickets')
@admin_required
def tickets_page():
    con=db(); rows=con.execute('SELECT t.*,p.telegram_id,p.username,d.title FROM tickets t JOIN players p ON p.id=t.player_id JOIN draws d ON d.id=t.draw_id ORDER BY t.id DESC LIMIT 300').fetchall(); con.close(); return render_template('tickets.html',rows=rows)

@app.route('/admin/players')
@admin_required
def players_page():
    con=db(); rows=con.execute('SELECT p.*,COUNT(t.id) AS tickets_count FROM players p LEFT JOIN tickets t ON t.player_id=p.id GROUP BY p.id ORDER BY p.id DESC LIMIT 300').fetchall(); con.close(); return render_template('players.html',rows=rows)

@app.errorhandler(400)
def bad_request(e): return render_template('error.html',message=getattr(e,'description','Bad request')),400
@app.errorhandler(403)
def forbidden(e): return render_template('error.html',message='Forbidden'),403

with app.app_context(): init_db()

if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT','10000')))
