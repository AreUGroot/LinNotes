import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

_lock = threading.Lock()
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
IMAGES_DIR = os.path.join(DATA, 'images')
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA, 'app.db'))
SESSIONS = {}

ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

# Role constants
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'


def _get_db():
    if 'db' not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _init_db():
    db = _get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            topic TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            priority INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS note_tags (
            note_id TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (note_id, tag_id),
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS opinions (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            topic TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            predictor TEXT NOT NULL DEFAULT '',
            time_scale TEXT DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS opinion_comments (
            id TEXT PRIMARY KEY,
            opinion_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (opinion_id) REFERENCES opinions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS opinion_questions (
            id TEXT PRIMARY KEY,
            opinion_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (opinion_id) REFERENCES opinions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS opinion_ratings (
            id TEXT PRIMARY KEY,
            opinion_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            measure_time TEXT DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (opinion_id) REFERENCES opinions(id) ON DELETE CASCADE
        );
    ''')

    # Ensure role column exists (for existing databases)
    try:
        db.execute('SELECT role FROM users LIMIT 1')
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    # Migrate opinions.source -> opinions.predictor
    try:
        db.execute('SELECT predictor FROM opinions LIMIT 1')
    except sqlite3.OperationalError:
        try:
            db.execute('ALTER TABLE opinions RENAME COLUMN source TO predictor')
        except sqlite3.OperationalError:
            pass

    # Ensure created_by column exists for opinion comments/questions
    try:
        db.execute('SELECT created_by FROM opinion_comments LIMIT 1')
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE opinion_comments ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
    try:
        db.execute('SELECT created_by FROM opinion_questions LIMIT 1')
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE opinion_questions ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")

    db.execute(
        'INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
        ('Lin', '951204', ROLE_ADMIN)
    )
    # Update Lin's role if already exists
    db.execute('UPDATE users SET role = ? WHERE username = ?', (ROLE_ADMIN, 'Lin'))

    db.execute(
        'INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
        ('Qingli', '888888', ROLE_USER)
    )
    db.commit()


def _get_user_role(db, username):
    row = db.execute('SELECT role FROM users WHERE username = ?', (username,)).fetchone()
    return row['role'] if row else ROLE_USER


def _make_token(username):
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = username
    return token


def _require_auth(view_fn):
    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'error': '未登录'}), 401
        token = auth.split(' ', 1)[1].strip()
        username = SESSIONS.get(token)
        if not username:
            return jsonify({'error': '登录已失效，请重新登录'}), 401
        g.current_user = username
        return view_fn(*args, **kwargs)
    return wrapper


def _note_tags(db, note_id):
    rows = db.execute(
        'SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id = t.id WHERE nt.note_id = ?',
        (note_id,)
    ).fetchall()
    return [r['name'] for r in rows]


def _note_to_dict(row, tags):
    return {
        'id': row['id'],
        'title': row['title'],
        'topic': row['topic'],
        'content': row['content'],
        'priority': row['priority'],
        'tags': tags,
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _ensure_topic(db, name):
    db.execute('INSERT OR IGNORE INTO topics (name) VALUES (?)', (name,))


def _ensure_tags(db, tag_names):
    for name in tag_names:
        db.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (name,))


def _set_note_tags(db, note_id, tag_names):
    db.execute('DELETE FROM note_tags WHERE note_id = ?', (note_id,))
    for name in tag_names:
        row = db.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()
        if row:
            db.execute(
                'INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)',
                (note_id, row['id'])
            )


# ── 主页 ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


# ── 图片 ─────────────────────────────────────────────────────────────────────
@app.route('/data/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


# ── 鉴权 API ─────────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    b = request.get_json(force=True) or {}
    username = str(b.get('username', '')).strip()
    password = str(b.get('password', ''))
    if not username or not password:
        return jsonify({'error': '请输入用户名和密码'}), 400

    db = _get_db()
    row = db.execute(
        'SELECT username, password, role FROM users WHERE username = ?',
        (username,)
    ).fetchone()
    if not row or row['password'] != password:
        return jsonify({'error': '用户名或密码错误'}), 401

    token = _make_token(username)
    return jsonify({
        'status': 'ok',
        'token': token,
        'username': username,
        'role': row['role'],
    })


@app.route('/api/auth/me', methods=['GET'])
@_require_auth
def me():
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    return jsonify({'status': 'ok', 'username': g.current_user, 'role': role})


# ── 主题 API ─────────────────────────────────────────────────────────────────
@app.route('/api/topics', methods=['GET'])
@_require_auth
def list_topics():
    db = _get_db()
    rows = db.execute('SELECT id, name FROM topics ORDER BY name').fetchall()
    return jsonify([{'id': r['id'], 'name': r['name']} for r in rows])


@app.route('/api/topics', methods=['POST'])
@_require_auth
def create_topic():
    b = request.get_json(force=True) or {}
    name = str(b.get('name', '')).strip()
    if not name:
        return jsonify({'error': '主题名称不能为空'}), 400
    db = _get_db()
    db.execute('INSERT OR IGNORE INTO topics (name) VALUES (?)', (name,))
    db.commit()
    row = db.execute('SELECT id, name FROM topics WHERE name = ?', (name,)).fetchone()
    return jsonify({'id': row['id'], 'name': row['name']})


# ── 标签 API ─────────────────────────────────────────────────────────────────
@app.route('/api/tags', methods=['GET'])
@_require_auth
def list_tags():
    db = _get_db()
    rows = db.execute('SELECT id, name FROM tags ORDER BY name').fetchall()
    return jsonify([{'id': r['id'], 'name': r['name']} for r in rows])


@app.route('/api/tags', methods=['POST'])
@_require_auth
def create_tag():
    b = request.get_json(force=True) or {}
    name = str(b.get('name', '')).strip()
    if not name:
        return jsonify({'error': '标签名称不能为空'}), 400
    db = _get_db()
    db.execute('INSERT OR IGNORE INTO tags (name) VALUES (?)', (name,))
    db.commit()
    row = db.execute('SELECT id, name FROM tags WHERE name = ?', (name,)).fetchone()
    return jsonify({'id': row['id'], 'name': row['name']})


# ── 笔记 API ─────────────────────────────────────────────────────────────────
@app.route('/api/notes', methods=['GET'])
@_require_auth
def list_notes():
    topic = request.args.get('topic', '').strip()
    tag = request.args.get('tag', '').strip()
    sort = request.args.get('sort', 'updated_desc').strip()

    base = 'SELECT DISTINCT n.id, n.title, n.topic, n.content, n.priority, n.created_at, n.updated_at FROM notes n'
    joins = []
    wheres = []
    params = []

    if tag:
        joins.append('JOIN note_tags nt ON nt.note_id = n.id')
        joins.append('JOIN tags t ON t.id = nt.tag_id')
        wheres.append('t.name = ?')
        params.append(tag)

    if topic:
        wheres.append('n.topic = ?')
        params.append(topic)

    sql = base
    if joins:
        sql += ' ' + ' '.join(joins)
    if wheres:
        sql += ' WHERE ' + ' AND '.join(wheres)

    sort_map = {
        'updated_desc': 'n.updated_at DESC',
        'updated_asc': 'n.updated_at ASC',
        'created_desc': 'n.created_at DESC',
        'created_asc': 'n.created_at ASC',
        'priority_desc': 'n.priority DESC, n.updated_at DESC',
        'priority_asc': 'n.priority ASC, n.updated_at DESC',
    }
    sql += ' ORDER BY ' + sort_map.get(sort, 'n.updated_at DESC')

    with _lock:
        db = _get_db()
        rows = db.execute(sql, params).fetchall()
        result = []
        for row in rows:
            tags = _note_tags(db, row['id'])
            result.append(_note_to_dict(row, tags))

    return jsonify(result)


@app.route('/api/notes', methods=['POST'])
@_require_auth
def create_note():
    b = request.get_json(force=True) or {}
    title = str(b.get('title', '')).strip()
    topic = str(b.get('topic', '')).strip()
    content = str(b.get('content', ''))
    priority = int(b.get('priority', 0) or 0)
    tag_names = b.get('tags', [])

    if not topic:
        return jsonify({'error': '请选择或创建主题'}), 400

    note_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        _ensure_topic(db, topic)
        _ensure_tags(db, tag_names)
        db.execute(
            'INSERT INTO notes (id, title, topic, content, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (note_id, title, topic, content, priority, now, now)
        )
        _set_note_tags(db, note_id, tag_names)
        db.commit()
        tags = _note_tags(db, note_id)
        row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()

    return jsonify({'status': 'ok', 'note': _note_to_dict(row, tags)})


@app.route('/api/notes/<note_id>', methods=['GET'])
@_require_auth
def get_note(note_id):
    db = _get_db()
    row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
    if not row:
        return jsonify({'error': '笔记不存在'}), 404
    tags = _note_tags(db, note_id)
    return jsonify(_note_to_dict(row, tags))


@app.route('/api/notes/<note_id>', methods=['PUT'])
@_require_auth
def update_note(note_id):
    b = request.get_json(force=True) or {}
    title = str(b.get('title', '')).strip()
    topic = str(b.get('topic', '')).strip()
    content = str(b.get('content', ''))
    priority = int(b.get('priority', 0) or 0)
    tag_names = b.get('tags', [])

    if not topic:
        return jsonify({'error': '请选择或创建主题'}), 400

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
        if not row:
            return jsonify({'error': '笔记不存在'}), 404

        _ensure_topic(db, topic)
        _ensure_tags(db, tag_names)
        db.execute(
            'UPDATE notes SET title=?, topic=?, content=?, priority=?, updated_at=? WHERE id=?',
            (title, topic, content, priority, now, note_id)
        )
        _set_note_tags(db, note_id, tag_names)
        db.commit()
        tags = _note_tags(db, note_id)
        row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()

    return jsonify({'status': 'ok', 'note': _note_to_dict(row, tags)})


@app.route('/api/notes/<note_id>', methods=['DELETE'])
@_require_auth
def delete_note(note_id):
    with _lock:
        db = _get_db()
        db.execute('DELETE FROM note_tags WHERE note_id = ?', (note_id,))
        db.execute('DELETE FROM notes WHERE id = ?', (note_id,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 评论 API ─────────────────────────────────────────────────────────────────
def _comment_to_dict(row):
    return {
        'id': row['id'],
        'note_id': row['note_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_at': row['created_at'],
    }


@app.route('/api/notes/<note_id>/comments', methods=['GET'])
@_require_auth
def list_comments(note_id):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM comments WHERE note_id = ? ORDER BY created_at ASC',
        (note_id,)
    ).fetchall()
    return jsonify([_comment_to_dict(r) for r in rows])


@app.route('/api/notes/<note_id>/comments', methods=['POST'])
@_require_auth
def create_comment(note_id):
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    parent_id = b.get('parent_id') or None
    if not content:
        return jsonify({'error': '评论内容不能为空'}), 400

    cid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO comments (id, note_id, parent_id, content, created_at) VALUES (?, ?, ?, ?, ?)',
            (cid, note_id, parent_id, content, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM comments WHERE id = ?', (cid,)).fetchone()
    return jsonify(_comment_to_dict(row))


# ── 问题 API ─────────────────────────────────────────────────────────────────
def _question_to_dict(row):
    return {
        'id': row['id'],
        'note_id': row['note_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'resolved': bool(row['resolved']),
        'created_at': row['created_at'],
    }


@app.route('/api/notes/<note_id>/questions', methods=['GET'])
@_require_auth
def list_questions(note_id):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM questions WHERE note_id = ? ORDER BY created_at ASC',
        (note_id,)
    ).fetchall()
    return jsonify([_question_to_dict(r) for r in rows])


@app.route('/api/notes/<note_id>/questions', methods=['POST'])
@_require_auth
def create_question(note_id):
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    parent_id = b.get('parent_id') or None
    if not content:
        return jsonify({'error': '问题内容不能为空'}), 400

    qid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO questions (id, note_id, parent_id, content, resolved, created_at) VALUES (?, ?, ?, ?, 0, ?)',
            (qid, note_id, parent_id, content, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_question_to_dict(row))


def _delete_thread_rows(db, table, root_id):
    to_delete = [root_id]
    idx = 0
    while idx < len(to_delete):
        current_id = to_delete[idx]
        rows = db.execute(
            f'SELECT id FROM {table} WHERE parent_id = ?',
            (current_id,)
        ).fetchall()
        to_delete.extend([r['id'] for r in rows])
        idx += 1
    db.executemany(
        f'DELETE FROM {table} WHERE id = ?',
        [(rid,) for rid in to_delete]
    )
    return to_delete


@app.route('/api/comments/<cid>', methods=['DELETE'])
@_require_auth
def delete_comment(cid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM comments WHERE id = ?', (cid,)).fetchone()
        if not row:
            return jsonify({'error': '评论不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'comments', cid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


@app.route('/api/questions/<qid>/toggle-resolved', methods=['POST'])
@_require_auth
def toggle_resolved(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        new_val = 0 if row['resolved'] else 1
        db.execute('UPDATE questions SET resolved = ? WHERE id = ?', (new_val, qid))
        db.commit()
        row = db.execute('SELECT * FROM questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_question_to_dict(row))


@app.route('/api/questions/<qid>', methods=['DELETE'])
@_require_auth
def delete_question(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'questions', qid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


# ── 观点与预测 API ─────────────────────────────────────────────────────────────
def _opinion_to_dict(row):
    return {
        'id': row['id'],
        'type': row['type'],
        'topic': row['topic'],
        'title': row['title'],
        'content': row['content'],
        'predictor': row['predictor'],
        'time_scale': row['time_scale'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _opinion_avg_rating(db, opinion_id):
    row = db.execute(
        'SELECT AVG(rating) as avg_rating FROM opinion_ratings WHERE opinion_id = ?',
        (opinion_id,)
    ).fetchone()
    return round(row['avg_rating'], 1) if row and row['avg_rating'] is not None else 0


def _opinion_latest_measure_time(db, opinion_id):
    row = db.execute(
        "SELECT measure_time FROM opinion_ratings WHERE opinion_id = ? AND measure_time != '' ORDER BY created_at DESC LIMIT 1",
        (opinion_id,)
    ).fetchone()
    return row['measure_time'] if row else ''


@app.route('/api/opinions', methods=['GET'])
@_require_auth
def list_opinions():
    op_type = request.args.get('type', '').strip()
    created_by = request.args.get('created_by', '').strip()
    predictor = request.args.get('predictor', '').strip()
    sort = request.args.get('sort', 'updated_desc').strip()

    sql = 'SELECT * FROM opinions'
    wheres = []
    params = []

    if op_type:
        wheres.append('type = ?')
        params.append(op_type)
    if created_by:
        wheres.append('created_by = ?')
        params.append(created_by)
    if predictor:
        wheres.append('predictor = ?')
        params.append(predictor)

    if wheres:
        sql += ' WHERE ' + ' AND '.join(wheres)

    sort_map = {
        'updated_desc': 'updated_at DESC',
        'created_desc': 'created_at DESC',
        'created_asc': 'created_at ASC',
    }
    # For rating/measure_time sort, we sort in Python after fetching
    db_sort = sort_map.get(sort, '')
    if db_sort:
        sql += ' ORDER BY ' + db_sort

    with _lock:
        db = _get_db()
        rows = db.execute(sql, params).fetchall()
        result = []
        for row in rows:
            d = _opinion_to_dict(row)
            d['avg_rating'] = _opinion_avg_rating(db, row['id'])
            d['measure_time'] = _opinion_latest_measure_time(db, row['id'])
            result.append(d)

    if sort == 'rating_desc':
        result.sort(key=lambda x: x['avg_rating'], reverse=True)
    elif sort == 'rating_asc':
        result.sort(key=lambda x: x['avg_rating'])
    elif sort == 'measure_time_desc':
        result.sort(key=lambda x: x['measure_time'] or '', reverse=True)
    elif sort == 'measure_time_asc':
        result.sort(key=lambda x: x['measure_time'] or '')

    return jsonify(result)


@app.route('/api/opinions', methods=['POST'])
@_require_auth
def create_opinion():
    b = request.get_json(force=True) or {}
    op_type = str(b.get('type', '')).strip()
    topic = str(b.get('topic', '')).strip()
    title = str(b.get('title', '')).strip()
    content = str(b.get('content', ''))
    predictor = str(b.get('predictor', '')).strip()
    time_scale = str(b.get('time_scale', '')).strip()

    if op_type not in ('观点', '预测'):
        return jsonify({'error': '类型必须为"观点"或"预测"'}), 400
    if not topic:
        return jsonify({'error': '请选择或创建主题'}), 400

    oid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO opinions (id, type, topic, title, content, predictor, time_scale, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (oid, op_type, topic, title, content, predictor, time_scale, g.current_user, now, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()

    d = _opinion_to_dict(row)
    d['avg_rating'] = 0
    d['measure_time'] = ''
    return jsonify({'status': 'ok', 'opinion': d})


@app.route('/api/opinions/<oid>', methods=['GET'])
@_require_auth
def get_opinion(oid):
    db = _get_db()
    row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()
    if not row:
        return jsonify({'error': '不存在'}), 404
    d = _opinion_to_dict(row)
    d['avg_rating'] = _opinion_avg_rating(db, oid)
    d['measure_time'] = _opinion_latest_measure_time(db, oid)
    return jsonify(d)


@app.route('/api/opinions/<oid>', methods=['PUT'])
@_require_auth
def update_opinion(oid):
    b = request.get_json(force=True) or {}
    title = str(b.get('title', '')).strip()
    topic = str(b.get('topic', '')).strip()
    content = str(b.get('content', ''))
    predictor = str(b.get('predictor', '')).strip()
    time_scale = str(b.get('time_scale', '')).strip()

    if not topic:
        return jsonify({'error': '请选择或创建主题'}), 400

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()
        if not row:
            return jsonify({'error': '不存在'}), 404

        db.execute(
            'UPDATE opinions SET title=?, topic=?, content=?, predictor=?, time_scale=?, updated_at=? WHERE id=?',
            (title, topic, content, predictor, time_scale, now, oid)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()

    d = _opinion_to_dict(row)
    d['avg_rating'] = _opinion_avg_rating(db, oid)
    d['measure_time'] = _opinion_latest_measure_time(db, oid)
    return jsonify({'status': 'ok', 'opinion': d})


@app.route('/api/opinions/<oid>', methods=['DELETE'])
@_require_auth
def delete_opinion(oid):
    with _lock:
        db = _get_db()
        db.execute('DELETE FROM opinions WHERE id = ?', (oid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 观点评论 API ──────────────────────────────────────────────────────────────
def _opinion_comment_to_dict(row):
    return {
        'id': row['id'],
        'opinion_id': row['opinion_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
    }


@app.route('/api/opinions/<oid>/comments', methods=['GET'])
@_require_auth
def list_opinion_comments(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_comments WHERE opinion_id = ? ORDER BY created_at ASC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_comment_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/comments', methods=['POST'])
@_require_auth
def create_opinion_comment(oid):
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    parent_id = b.get('parent_id') or None
    if not content:
        return jsonify({'error': '评论内容不能为空'}), 400

    cid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO opinion_comments (id, opinion_id, parent_id, content, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (cid, oid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinion_comments WHERE id = ?', (cid,)).fetchone()
    return jsonify(_opinion_comment_to_dict(row))


@app.route('/api/opinion-comments/<cid>', methods=['DELETE'])
@_require_auth
def delete_opinion_comment(cid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM opinion_comments WHERE id = ?', (cid,)).fetchone()
        if not row:
            return jsonify({'error': '评论不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'opinion_comments', cid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


# ── 观点提问 API ──────────────────────────────────────────────────────────────
def _opinion_question_to_dict(row):
    return {
        'id': row['id'],
        'opinion_id': row['opinion_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'resolved': bool(row['resolved']),
        'created_at': row['created_at'],
    }


@app.route('/api/opinions/<oid>/questions', methods=['GET'])
@_require_auth
def list_opinion_questions(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_questions WHERE opinion_id = ? ORDER BY created_at ASC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_question_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/questions', methods=['POST'])
@_require_auth
def create_opinion_question(oid):
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    parent_id = b.get('parent_id') or None
    if not content:
        return jsonify({'error': '问题内容不能为空'}), 400

    qid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO opinion_questions (id, opinion_id, parent_id, content, created_by, resolved, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)',
            (qid, oid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinion_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_opinion_question_to_dict(row))


@app.route('/api/opinion-questions/<qid>/toggle-resolved', methods=['POST'])
@_require_auth
def toggle_opinion_question_resolved(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM opinion_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        new_val = 0 if row['resolved'] else 1
        db.execute('UPDATE opinion_questions SET resolved = ? WHERE id = ?', (new_val, qid))
        db.commit()
        row = db.execute('SELECT * FROM opinion_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_opinion_question_to_dict(row))


@app.route('/api/opinion-questions/<qid>', methods=['DELETE'])
@_require_auth
def delete_opinion_question(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM opinion_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'opinion_questions', qid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


# ── 观点评分 API ──────────────────────────────────────────────────────────────
def _opinion_rating_to_dict(row):
    return {
        'id': row['id'],
        'opinion_id': row['opinion_id'],
        'rating': row['rating'],
        'measure_time': row['measure_time'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
    }


@app.route('/api/opinions/<oid>/ratings', methods=['GET'])
@_require_auth
def list_opinion_ratings(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_ratings WHERE opinion_id = ? ORDER BY created_at DESC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_rating_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/ratings', methods=['POST'])
@_require_auth
def create_opinion_rating(oid):
    b = request.get_json(force=True) or {}
    rating = int(b.get('rating', 0))
    measure_time = str(b.get('measure_time', '')).strip()

    if rating < 1 or rating > 5:
        return jsonify({'error': '评分必须在1-5之间'}), 400

    rid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO opinion_ratings (id, opinion_id, rating, measure_time, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (rid, oid, rating, measure_time, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinion_ratings WHERE id = ?', (rid,)).fetchone()
    return jsonify(_opinion_rating_to_dict(row))


@app.route('/api/opinion-ratings/<rid>', methods=['DELETE'])
@_require_auth
def delete_opinion_rating(rid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM opinion_ratings WHERE id = ?', (rid,)).fetchone()
        if not row:
            return jsonify({'error': '评分记录不存在'}), 404
        db.execute('DELETE FROM opinion_ratings WHERE id = ?', (rid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 观点筛选选项 API ──────────────────────────────────────────────────────────
@app.route('/api/opinions/filter-options', methods=['GET'])
@_require_auth
def opinion_filter_options():
    db = _get_db()
    creators = db.execute("SELECT DISTINCT created_by FROM opinions ORDER BY created_by").fetchall()
    predictors = db.execute("SELECT DISTINCT predictor FROM opinions WHERE predictor != '' ORDER BY predictor").fetchall()
    return jsonify({
        'creators': [r['created_by'] for r in creators],
        'predictors': [r['predictor'] for r in predictors],
    })


# ── 图片上传 API ─────────────────────────────────────────────────────────────
@app.route('/api/upload', methods=['POST'])
@_require_auth
def upload_image():
    if 'image' not in request.files:
        return jsonify({'error': '未找到图片文件'}), 400

    f = request.files['image']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else 'png'
    if ext not in ALLOWED_IMAGE_EXTS:
        return jsonify({'error': f'不支持的图片格式: {ext}'}), 400

    data = f.read()
    if len(data) > MAX_IMAGE_SIZE:
        return jsonify({'error': '图片文件过大（最大 10MB）'}), 400

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    rand = uuid.uuid4().hex[:6]
    filename = f'{ts}_{rand}.{ext}'
    filepath = os.path.join(IMAGES_DIR, filename)

    with open(filepath, 'wb') as out:
        out.write(data)

    return jsonify({'status': 'ok', 'url': f'/data/images/{filename}'})


# ── 启动 ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    with app.app_context():
        _init_db()
    app.run(debug=True, port=3006)
else:
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    with app.app_context():
        _init_db()
