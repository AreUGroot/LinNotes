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
            password TEXT NOT NULL
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
    ''')

    db.execute(
        'INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)',
        ('Lin', '951204')
    )
    db.commit()


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
        'SELECT username, password FROM users WHERE username = ?',
        (username,)
    ).fetchone()
    if not row or row['password'] != password:
        return jsonify({'error': '用户名或密码错误'}), 401

    token = _make_token(username)
    return jsonify({'status': 'ok', 'token': token, 'username': username})


@app.route('/api/auth/me', methods=['GET'])
@_require_auth
def me():
    return jsonify({'status': 'ok', 'username': g.current_user})


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
