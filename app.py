import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='.', static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB max request
CORS(app)

_lock = threading.Lock()
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
IMAGES_DIR = os.path.join(DATA, 'images')
DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA, 'app.db'))
SESSIONS = {}

# Rate limiting: per-token, 60 requests per minute
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 60
_rate_limits = defaultdict(list)

DOCUMENTS_DIR = os.path.join(DATA, 'documents')

ALLOWED_IMAGE_EXTS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

ALLOWED_DOC_EXTS = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'csv', 'zip', 'rar', 'md', 'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_DOC_SIZE = 2 * 1024 * 1024  # 2 MB

# Role constants
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'

# Valid opinion types
VALID_OP_TYPES = ('事实', '观点', '反驳', '预测', '计划')

# Valid permission sections
VALID_SECTIONS = ('notes', 'opinions', 'archives', 'projects')

# Input length limits
MAX_TITLE_LENGTH = 500
MAX_CONTENT_SIZE = 500 * 1024  # 500 KB


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

        CREATE TABLE IF NOT EXISTS archives (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '',
            content_description TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_tags (
            archive_id TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (archive_id, tag_id),
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_documents (
            id TEXT PRIMARY KEY,
            archive_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            file_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_comments (
            id TEXT PRIMARY KEY,
            archive_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_questions (
            id TEXT PRIMARY KEY,
            archive_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS archive_ratings (
            id TEXT PRIMARY KEY,
            archive_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_permissions (
            username TEXT NOT NULL,
            section TEXT NOT NULL,
            PRIMARY KEY (username, section),
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES users(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_members (
            project_id TEXT NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY (project_id, username),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE (project_id, name),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_notes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            topic TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            priority INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_note_tags (
            note_id TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (note_id, tag_id),
            FOREIGN KEY (note_id) REFERENCES project_notes(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_comments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES project_notes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS project_questions (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            parent_id TEXT,
            content TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            resolved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES project_notes(id) ON DELETE CASCADE
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

    # Migrate: add new opinion fields for expanded types
    for col, col_type, default in [
        ('rebutted_opinion', 'TEXT', "''"),
        ('rebutted_source', 'TEXT', "''"),
        ('fact_source', 'TEXT', "''"),
        ('fact_link', 'TEXT', "''"),
        ('verify_priority', 'INTEGER', '0'),
    ]:
        try:
            db.execute(f'SELECT {col} FROM opinions LIMIT 1')
        except sqlite3.OperationalError:
            db.execute(f'ALTER TABLE opinions ADD COLUMN {col} {col_type} DEFAULT {default}')

    # Fix old data: decouple topic from type name
    db.execute("UPDATE opinions SET topic = '' WHERE topic IN ('观点', '预测')")
    db.execute("UPDATE opinions SET topic = REPLACE(topic, '观点 / ', '') WHERE topic LIKE '观点 / %'")
    db.execute("UPDATE opinions SET topic = REPLACE(topic, '预测 / ', '') WHERE topic LIKE '预测 / %'")

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

    # Test accounts
    for uname, pwd in [('TestUser1', '123456'), ('TestUser2', '123456'), ('TestUser3', '123456')]:
        db.execute(
            'INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)',
            (uname, pwd, ROLE_USER)
        )

    # Seed default permissions
    for section in VALID_SECTIONS:
        db.execute(
            'INSERT OR IGNORE INTO user_permissions (username, section) VALUES (?, ?)',
            ('Lin', section)
        )
    for uname in ('Qingli', 'TestUser1', 'TestUser2', 'TestUser3'):
        for section in ('opinions', 'archives', 'projects'):
            db.execute(
                'INSERT OR IGNORE INTO user_permissions (username, section) VALUES (?, ?)',
                (uname, section)
            )

    db.commit()


def _get_user_role(db, username):
    row = db.execute('SELECT role FROM users WHERE username = ?', (username,)).fetchone()
    return row['role'] if row else ROLE_USER


def _make_token(username):
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = username
    return token


def _check_rate_limit(token):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = _rate_limits[token]
    _rate_limits[token] = [t for t in timestamps if t > window_start]
    if len(_rate_limits[token]) >= RATE_LIMIT_MAX:
        return False
    _rate_limits[token].append(now)
    # Prevent unbounded growth: purge stale tokens periodically
    if len(_rate_limits) > 10000:
        stale = [k for k, v in _rate_limits.items() if not v or v[-1] < window_start]
        for k in stale:
            del _rate_limits[k]
    return True


def _require_auth(view_fn):
    @wraps(view_fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return jsonify({'error': '未登录'}), 401
        token = auth.split(' ', 1)[1].strip()

        if not _check_rate_limit(token):
            return jsonify({'error': '请求过于频繁，请稍后再试'}), 429

        # Try session token first (fast in-memory lookup)
        username = SESSIONS.get(token)
        if not username:
            # Fallback to API key (database lookup)
            db = _get_db()
            row = db.execute(
                'SELECT username FROM api_keys WHERE key = ?', (token,)
            ).fetchone()
            if row:
                username = row['username']
                now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                db.execute('UPDATE api_keys SET last_used_at = ? WHERE key = ?', (now, token))
                db.commit()
            else:
                return jsonify({'error': '登录已失效，请重新登录'}), 401

        g.current_user = username
        return view_fn(*args, **kwargs)
    return wrapper


def _require_section(section):
    """Check if user has permission for a section. Use AFTER @_require_auth."""
    def decorator(view_fn):
        @wraps(view_fn)
        def wrapper(*args, **kwargs):
            db = _get_db()
            permissions = _get_user_permissions(db, g.current_user)
            if section not in permissions:
                return jsonify({'error': f'无权访问 {section} 模块'}), 403
            return view_fn(*args, **kwargs)
        return wrapper
    return decorator


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


# ── 归档辅助 ─────────────────────────────────────────────────────────────────
def _archive_tags(db, archive_id):
    rows = db.execute(
        'SELECT t.name FROM tags t JOIN archive_tags at2 ON at2.tag_id = t.id WHERE at2.archive_id = ?',
        (archive_id,)
    ).fetchall()
    return [r['name'] for r in rows]


def _set_archive_tags(db, archive_id, tag_names):
    db.execute('DELETE FROM archive_tags WHERE archive_id = ?', (archive_id,))
    for name in tag_names:
        row = db.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()
        if row:
            db.execute(
                'INSERT OR IGNORE INTO archive_tags (archive_id, tag_id) VALUES (?, ?)',
                (archive_id, row['id'])
            )


def _archive_to_dict(row, tags):
    return {
        'id': row['id'],
        'title': row['title'],
        'topic': row['topic'],
        'content_description': row['content_description'],
        'author': row['author'],
        'body': row['body'],
        'tags': tags,
        'created_by': row['created_by'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _archive_avg_rating(db, archive_id):
    row = db.execute(
        'SELECT AVG(rating) as avg_rating FROM archive_ratings WHERE archive_id = ?',
        (archive_id,)
    ).fetchone()
    return round(row['avg_rating'], 1) if row and row['avg_rating'] is not None else 0


def _archive_documents(db, archive_id):
    rows = db.execute(
        'SELECT * FROM archive_documents WHERE archive_id = ? ORDER BY created_at ASC',
        (archive_id,)
    ).fetchall()
    return [{'id': r['id'], 'filename': r['filename'], 'original_name': r['original_name'],
             'file_size': r['file_size'], 'url': f'/data/documents/{r["filename"]}',
             'created_at': r['created_at']} for r in rows]


def _archive_comment_to_dict(row):
    return {
        'id': row['id'],
        'archive_id': row['archive_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
    }


def _archive_question_to_dict(row):
    return {
        'id': row['id'],
        'archive_id': row['archive_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'resolved': bool(row['resolved']),
        'created_at': row['created_at'],
    }


def _archive_rating_to_dict(row):
    return {
        'id': row['id'],
        'archive_id': row['archive_id'],
        'rating': row['rating'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
    }


# ── 项目辅助 ─────────────────────────────────────────────────────────────────
def _project_note_tags(db, note_id):
    rows = db.execute(
        'SELECT t.name FROM tags t JOIN project_note_tags pnt ON pnt.tag_id = t.id WHERE pnt.note_id = ?',
        (note_id,)
    ).fetchall()
    return [r['name'] for r in rows]


def _set_project_note_tags(db, note_id, tag_names):
    db.execute('DELETE FROM project_note_tags WHERE note_id = ?', (note_id,))
    for name in tag_names:
        row = db.execute('SELECT id FROM tags WHERE name = ?', (name,)).fetchone()
        if row:
            db.execute(
                'INSERT OR IGNORE INTO project_note_tags (note_id, tag_id) VALUES (?, ?)',
                (note_id, row['id'])
            )


def _project_note_to_dict(row, tags):
    return {
        'id': row['id'],
        'project_id': row['project_id'],
        'title': row['title'],
        'topic': row['topic'],
        'content': row['content'],
        'priority': row['priority'],
        'tags': tags,
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _ensure_project_topic(db, project_id, name):
    db.execute('INSERT OR IGNORE INTO project_topics (project_id, name) VALUES (?, ?)', (project_id, name))


def _check_project_member(db, project_id, username):
    role = _get_user_role(db, username)
    if role == ROLE_ADMIN:
        return True
    row = db.execute(
        'SELECT 1 FROM project_members WHERE project_id = ? AND username = ?',
        (project_id, username)
    ).fetchone()
    return row is not None


def _project_comment_to_dict(row):
    return {
        'id': row['id'],
        'note_id': row['note_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'created_at': row['created_at'],
    }


def _project_question_to_dict(row):
    return {
        'id': row['id'],
        'note_id': row['note_id'],
        'parent_id': row['parent_id'],
        'content': row['content'],
        'created_by': row['created_by'],
        'resolved': bool(row['resolved']),
        'created_at': row['created_at'],
    }


def _get_user_permissions(db, username):
    role = _get_user_role(db, username)
    if role == ROLE_ADMIN:
        return list(VALID_SECTIONS)
    rows = db.execute(
        'SELECT section FROM user_permissions WHERE username = ?', (username,)
    ).fetchall()
    return [r['section'] for r in rows]


# ── 主页 ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


# ── 图片 ─────────────────────────────────────────────────────────────────────
@app.route('/data/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route('/data/documents/<path:filename>')
def serve_document(filename):
    return send_from_directory(DOCUMENTS_DIR, filename)


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
    permissions = _get_user_permissions(db, username)
    return jsonify({
        'status': 'ok',
        'token': token,
        'username': username,
        'role': row['role'],
        'permissions': permissions,
    })


@app.route('/api/auth/me', methods=['GET'])
@_require_auth
def me():
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    permissions = _get_user_permissions(db, g.current_user)
    return jsonify({'status': 'ok', 'username': g.current_user, 'role': role, 'permissions': permissions})


# ── 主题 API ─────────────────────────────────────────────────────────────────
@app.route('/api/topics', methods=['GET'])
@_require_auth
def list_topics():
    section = request.args.get('section', '').strip()
    db = _get_db()
    section_table = {'notes': 'notes', 'opinions': 'opinions', 'archives': 'archives'}
    if section in section_table:
        table = section_table[section]
        rows = db.execute(
            f"SELECT DISTINCT topic AS name FROM {table} WHERE topic != '' ORDER BY topic"
        ).fetchall()
        return jsonify([{'id': 0, 'name': r['name']} for r in rows])
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
@_require_section('notes')
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

    q = request.args.get('q', '').strip()
    if q:
        wheres.append('(n.title LIKE ? OR n.content LIKE ?)')
        params.extend(['%' + q + '%', '%' + q + '%'])

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

    if request.args.get('fields') == 'metadata':
        for item in result:
            item.pop('content', None)

    return jsonify(result)


@app.route('/api/notes', methods=['POST'])
@_require_auth
@_require_section('notes')
def create_note():
    b = request.get_json(force=True) or {}
    title = str(b.get('title', '')).strip()
    topic = str(b.get('topic', '')).strip()
    content = str(b.get('content', ''))
    priority = int(b.get('priority', 0) or 0)
    tag_names = b.get('tags', [])

    if not topic:
        return jsonify({'error': '请选择或创建主题'}), 400
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
    if len(content) > MAX_CONTENT_SIZE:
        return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400

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
@_require_section('notes')
def get_note(note_id):
    db = _get_db()
    row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
    if not row:
        return jsonify({'error': '笔记不存在'}), 404
    tags = _note_tags(db, note_id)
    return jsonify(_note_to_dict(row, tags))


@app.route('/api/notes/<note_id>', methods=['PUT'])
@_require_auth
@_require_section('notes')
def update_note(note_id):
    b = request.get_json(force=True) or {}

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()
        if not row:
            return jsonify({'error': '笔记不存在'}), 404

        title = str(b.get('title', row['title'])).strip()
        topic = str(b.get('topic', row['topic'])).strip()
        content = str(b.get('content', row['content']))
        priority = int(b.get('priority', row['priority']) or 0)
        tag_names = b.get('tags') if 'tags' in b else None

        if not topic:
            return jsonify({'error': '请选择或创建主题'}), 400
        if len(title) > MAX_TITLE_LENGTH:
            return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
        if len(content) > MAX_CONTENT_SIZE:
            return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400

        _ensure_topic(db, topic)
        db.execute(
            'UPDATE notes SET title=?, topic=?, content=?, priority=?, updated_at=? WHERE id=?',
            (title, topic, content, priority, now, note_id)
        )
        if tag_names is not None:
            _ensure_tags(db, tag_names)
            _set_note_tags(db, note_id, tag_names)
        db.commit()
        tags = _note_tags(db, note_id)
        row = db.execute('SELECT * FROM notes WHERE id = ?', (note_id,)).fetchone()

    return jsonify({'status': 'ok', 'note': _note_to_dict(row, tags)})


@app.route('/api/notes/<note_id>', methods=['DELETE'])
@_require_auth
@_require_section('notes')
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
@_require_section('notes')
def list_comments(note_id):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM comments WHERE note_id = ? ORDER BY created_at ASC',
        (note_id,)
    ).fetchall()
    return jsonify([_comment_to_dict(r) for r in rows])


@app.route('/api/notes/<note_id>/comments', methods=['POST'])
@_require_auth
@_require_section('notes')
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
@_require_section('notes')
def list_questions(note_id):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM questions WHERE note_id = ? ORDER BY created_at ASC',
        (note_id,)
    ).fetchall()
    return jsonify([_question_to_dict(r) for r in rows])


@app.route('/api/notes/<note_id>/questions', methods=['POST'])
@_require_auth
@_require_section('notes')
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
@_require_section('notes')
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
@_require_section('notes')
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
@_require_section('notes')
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
    d = {
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
    for col in ('rebutted_opinion', 'rebutted_source', 'fact_source', 'fact_link'):
        try:
            d[col] = row[col]
        except (IndexError, KeyError):
            d[col] = ''
    try:
        d['verify_priority'] = row['verify_priority']
    except (IndexError, KeyError):
        d['verify_priority'] = 0
    return d


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
@_require_section('opinions')
def list_opinions():
    op_type = request.args.get('type', '').strip()
    created_by = request.args.get('created_by', '').strip()
    predictor = request.args.get('predictor', '').strip()
    topic = request.args.get('topic', '').strip()
    sort = request.args.get('sort', 'updated_desc').strip()

    sql = 'SELECT * FROM opinions'
    wheres = []
    params = []

    if topic:
        wheres.append('topic = ?')
        params.append(topic)
    if op_type:
        wheres.append('type = ?')
        params.append(op_type)
    if created_by:
        wheres.append('created_by = ?')
        params.append(created_by)
    if predictor:
        wheres.append('predictor = ?')
        params.append(predictor)

    q = request.args.get('q', '').strip()
    if q:
        wheres.append('(title LIKE ? OR content LIKE ?)')
        params.extend(['%' + q + '%', '%' + q + '%'])

    if wheres:
        sql += ' WHERE ' + ' AND '.join(wheres)

    sort_map = {
        'updated_desc': 'updated_at DESC',
        'created_desc': 'created_at DESC',
        'created_asc': 'created_at ASC',
        'verify_priority_desc': 'verify_priority DESC, updated_at DESC',
        'verify_priority_asc': 'verify_priority ASC, updated_at DESC',
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

    if request.args.get('fields') == 'metadata':
        for item in result:
            item.pop('content', None)

    return jsonify(result)


@app.route('/api/opinions', methods=['POST'])
@_require_auth
@_require_section('opinions')
def create_opinion():
    b = request.get_json(force=True) or {}
    op_type = str(b.get('type', '')).strip()
    topic = str(b.get('topic', '')).strip()
    title = str(b.get('title', '')).strip()
    content = str(b.get('content', ''))
    predictor = str(b.get('predictor', '')).strip()
    time_scale = str(b.get('time_scale', '')).strip()
    rebutted_opinion = str(b.get('rebutted_opinion', '')).strip()
    rebutted_source = str(b.get('rebutted_source', '')).strip()
    fact_source = str(b.get('fact_source', '')).strip()
    fact_link = str(b.get('fact_link', '')).strip()
    verify_priority = int(b.get('verify_priority', 0) or 0)

    if op_type not in VALID_OP_TYPES:
        return jsonify({'error': '类型必须为: ' + ', '.join(VALID_OP_TYPES)}), 400
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
    if len(content) > MAX_CONTENT_SIZE:
        return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400
    if op_type == '反驳' and not content.strip():
        return jsonify({'error': '反驳类型必须填写正文'}), 400
    if op_type == '反驳' and not predictor:
        return jsonify({'error': '反驳类型必须填写反驳人'}), 400
    if op_type == '事实' and not fact_source:
        return jsonify({'error': '事实类型必须填写出处描述'}), 400

    oid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        db.execute(
            '''INSERT INTO opinions (id, type, topic, title, content, predictor, time_scale,
               rebutted_opinion, rebutted_source, fact_source, fact_link, verify_priority,
               created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (oid, op_type, topic, title, content, predictor, time_scale,
             rebutted_opinion, rebutted_source, fact_source, fact_link, verify_priority,
             g.current_user, now, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()

    d = _opinion_to_dict(row)
    d['avg_rating'] = 0
    d['measure_time'] = ''
    return jsonify({'status': 'ok', 'opinion': d})


@app.route('/api/opinions/<oid>', methods=['GET'])
@_require_auth
@_require_section('opinions')
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
@_require_section('opinions')
def update_opinion(oid):
    b = request.get_json(force=True) or {}

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()
        if not row:
            return jsonify({'error': '不存在'}), 404

        title = str(b.get('title', row['title'])).strip()
        topic = str(b.get('topic', row['topic'])).strip()
        content = str(b.get('content', row['content']))
        predictor = str(b.get('predictor', row['predictor'])).strip()
        time_scale = str(b.get('time_scale', row['time_scale'])).strip()
        rebutted_opinion = str(b.get('rebutted_opinion', row['rebutted_opinion'])).strip()
        rebutted_source = str(b.get('rebutted_source', row['rebutted_source'])).strip()
        fact_source = str(b.get('fact_source', row['fact_source'])).strip()
        fact_link = str(b.get('fact_link', row['fact_link'])).strip()
        verify_priority = int(b.get('verify_priority', row['verify_priority']) or 0)

        if len(title) > MAX_TITLE_LENGTH:
            return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
        if len(content) > MAX_CONTENT_SIZE:
            return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400

        op_type = row['type']
        if op_type == '反驳' and not content.strip():
            return jsonify({'error': '反驳类型必须填写正文'}), 400
        if op_type == '反驳' and not predictor:
            return jsonify({'error': '反驳类型必须填写反驳人'}), 400
        if op_type == '事实' and not fact_source:
            return jsonify({'error': '事实类型必须填写出处描述'}), 400

        db.execute(
            '''UPDATE opinions SET title=?, topic=?, content=?, predictor=?, time_scale=?,
               rebutted_opinion=?, rebutted_source=?, fact_source=?, fact_link=?, verify_priority=?,
               updated_at=? WHERE id=?''',
            (title, topic, content, predictor, time_scale,
             rebutted_opinion, rebutted_source, fact_source, fact_link, verify_priority,
             now, oid)
        )
        db.commit()
        row = db.execute('SELECT * FROM opinions WHERE id = ?', (oid,)).fetchone()

    d = _opinion_to_dict(row)
    d['avg_rating'] = _opinion_avg_rating(db, oid)
    d['measure_time'] = _opinion_latest_measure_time(db, oid)
    return jsonify({'status': 'ok', 'opinion': d})


@app.route('/api/opinions/<oid>', methods=['DELETE'])
@_require_auth
@_require_section('opinions')
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
@_require_section('opinions')
def list_opinion_comments(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_comments WHERE opinion_id = ? ORDER BY created_at ASC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_comment_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/comments', methods=['POST'])
@_require_auth
@_require_section('opinions')
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
@_require_section('opinions')
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
@_require_section('opinions')
def list_opinion_questions(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_questions WHERE opinion_id = ? ORDER BY created_at ASC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_question_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/questions', methods=['POST'])
@_require_auth
@_require_section('opinions')
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
@_require_section('opinions')
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
@_require_section('opinions')
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
@_require_section('opinions')
def list_opinion_ratings(oid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM opinion_ratings WHERE opinion_id = ? ORDER BY created_at DESC',
        (oid,)
    ).fetchall()
    return jsonify([_opinion_rating_to_dict(r) for r in rows])


@app.route('/api/opinions/<oid>/ratings', methods=['POST'])
@_require_auth
@_require_section('opinions')
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
@_require_section('opinions')
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
@_require_section('opinions')
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


# ── 权限 API ─────────────────────────────────────────────────────────────────
@app.route('/api/permissions', methods=['GET'])
@_require_auth
def list_permissions():
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role != ROLE_ADMIN:
        return jsonify({'error': '无权限'}), 403
    users = db.execute('SELECT username, role FROM users ORDER BY username').fetchall()
    result = []
    for u in users:
        perms = db.execute(
            'SELECT section FROM user_permissions WHERE username = ?', (u['username'],)
        ).fetchall()
        result.append({
            'username': u['username'],
            'role': u['role'],
            'permissions': [p['section'] for p in perms],
        })
    return jsonify(result)


@app.route('/api/permissions/<username>', methods=['PUT'])
@_require_auth
def update_permissions(username):
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role != ROLE_ADMIN:
        return jsonify({'error': '无权限'}), 403
    b = request.get_json(force=True) or {}
    sections = [s for s in b.get('permissions', []) if s in VALID_SECTIONS]
    with _lock:
        db = _get_db()
        db.execute('DELETE FROM user_permissions WHERE username = ?', (username,))
        for section in sections:
            db.execute(
                'INSERT INTO user_permissions (username, section) VALUES (?, ?)',
                (username, section)
            )
        db.commit()
    return jsonify({'status': 'ok', 'username': username, 'permissions': sections})


# ── API Key 管理 ─────────────────────────────────────────────────────────────
@app.route('/api/auth/api-keys', methods=['GET'])
@_require_auth
def list_api_keys():
    db = _get_db()
    rows = db.execute(
        'SELECT key, label, created_at, last_used_at FROM api_keys WHERE username = ? ORDER BY created_at DESC',
        (g.current_user,)
    ).fetchall()
    return jsonify([{
        'key_prefix': r['key'][:8],
        'label': r['label'],
        'created_at': r['created_at'],
        'last_used_at': r['last_used_at'],
    } for r in rows])


@app.route('/api/auth/api-keys', methods=['POST'])
@_require_auth
def create_api_key():
    b = request.get_json(force=True) or {}
    label = str(b.get('label', '')).strip() or 'default'

    key = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO api_keys (key, username, label, created_at) VALUES (?, ?, ?, ?)',
            (key, g.current_user, label, now)
        )
        db.commit()

    return jsonify({
        'status': 'ok',
        'key': key,
        'label': label,
        'created_at': now,
        'warning': '请保存此密钥，它不会再次显示。',
    })


@app.route('/api/auth/api-keys/<key_prefix>', methods=['DELETE'])
@_require_auth
def delete_api_key(key_prefix):
    with _lock:
        db = _get_db()
        row = db.execute(
            'SELECT key FROM api_keys WHERE username = ? AND key LIKE ?',
            (g.current_user, key_prefix + '%')
        ).fetchone()
        if not row:
            return jsonify({'error': 'API密钥不存在'}), 404
        db.execute('DELETE FROM api_keys WHERE key = ?', (row['key'],))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 归档 API ─────────────────────────────────────────────────────────────────
@app.route('/api/archives', methods=['GET'])
@_require_auth
@_require_section('archives')
def list_archives():
    topic = request.args.get('topic', '').strip()
    tag = request.args.get('tag', '').strip()
    sort = request.args.get('sort', 'updated_desc').strip()

    base = 'SELECT DISTINCT a.* FROM archives a'
    joins = []
    wheres = []
    params = []

    if tag:
        joins.append('JOIN archive_tags at2 ON at2.archive_id = a.id')
        joins.append('JOIN tags t ON t.id = at2.tag_id')
        wheres.append('t.name = ?')
        params.append(tag)
    if topic:
        wheres.append('a.topic = ?')
        params.append(topic)

    q = request.args.get('q', '').strip()
    if q:
        wheres.append('(a.title LIKE ? OR a.content_description LIKE ?)')
        params.extend(['%' + q + '%', '%' + q + '%'])

    sql = base
    if joins:
        sql += ' ' + ' '.join(joins)
    if wheres:
        sql += ' WHERE ' + ' AND '.join(wheres)

    sort_map = {
        'updated_desc': 'a.updated_at DESC',
        'created_desc': 'a.created_at DESC',
        'created_asc': 'a.created_at ASC',
    }
    db_sort = sort_map.get(sort, '')
    if db_sort:
        sql += ' ORDER BY ' + db_sort

    with _lock:
        db = _get_db()
        rows = db.execute(sql, params).fetchall()
        result = []
        for row in rows:
            tags = _archive_tags(db, row['id'])
            d = _archive_to_dict(row, tags)
            d['avg_rating'] = _archive_avg_rating(db, row['id'])
            d['document_count'] = len(_archive_documents(db, row['id']))
            result.append(d)

    if sort == 'rating_desc':
        result.sort(key=lambda x: x['avg_rating'], reverse=True)
    elif sort == 'rating_asc':
        result.sort(key=lambda x: x['avg_rating'])

    if request.args.get('fields') == 'metadata':
        for item in result:
            item.pop('body', None)

    return jsonify(result)


@app.route('/api/archives', methods=['POST'])
@_require_auth
@_require_section('archives')
def create_archive():
    b = request.get_json(force=True) or {}
    title = str(b.get('title', '')).strip()
    topic = str(b.get('topic', '')).strip()
    content_description = str(b.get('content_description', '')).strip()
    author = str(b.get('author', '')).strip()
    body = str(b.get('body', ''))
    tag_names = b.get('tags', [])

    if not content_description:
        return jsonify({'error': '内容描述及链接不能为空'}), 400
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
    if len(body) > MAX_CONTENT_SIZE:
        return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400

    aid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        if topic:
            _ensure_topic(db, topic)
        _ensure_tags(db, tag_names)
        db.execute(
            '''INSERT INTO archives (id, title, topic, content_description, author, body,
               created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (aid, title, topic, content_description, author, body, g.current_user, now, now)
        )
        _set_archive_tags(db, aid, tag_names)
        db.commit()
        tags = _archive_tags(db, aid)
        row = db.execute('SELECT * FROM archives WHERE id = ?', (aid,)).fetchone()

    d = _archive_to_dict(row, tags)
    d['avg_rating'] = 0
    d['documents'] = []
    return jsonify({'status': 'ok', 'archive': d})


@app.route('/api/archives/<aid>', methods=['GET'])
@_require_auth
@_require_section('archives')
def get_archive(aid):
    db = _get_db()
    row = db.execute('SELECT * FROM archives WHERE id = ?', (aid,)).fetchone()
    if not row:
        return jsonify({'error': '归档不存在'}), 404
    tags = _archive_tags(db, aid)
    d = _archive_to_dict(row, tags)
    d['avg_rating'] = _archive_avg_rating(db, aid)
    d['documents'] = _archive_documents(db, aid)
    return jsonify(d)


@app.route('/api/archives/<aid>', methods=['PUT'])
@_require_auth
@_require_section('archives')
def update_archive(aid):
    b = request.get_json(force=True) or {}

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM archives WHERE id = ?', (aid,)).fetchone()
        if not row:
            return jsonify({'error': '归档不存在'}), 404

        title = str(b.get('title', row['title'])).strip()
        topic = str(b.get('topic', row['topic'])).strip()
        content_description = str(b.get('content_description', row['content_description'])).strip()
        author = str(b.get('author', row['author'])).strip()
        body = str(b.get('body', row['body']))
        tag_names = b.get('tags') if 'tags' in b else None

        if not content_description:
            return jsonify({'error': '内容描述及链接不能为空'}), 400
        if len(title) > MAX_TITLE_LENGTH:
            return jsonify({'error': f'标题超过最大长度限制 ({MAX_TITLE_LENGTH} 字符)'}), 400
        if len(body) > MAX_CONTENT_SIZE:
            return jsonify({'error': f'正文超过最大长度限制 ({MAX_CONTENT_SIZE // 1024} KB)'}), 400

        if topic:
            _ensure_topic(db, topic)
        db.execute(
            '''UPDATE archives SET title=?, topic=?, content_description=?, author=?, body=?,
               updated_at=? WHERE id=?''',
            (title, topic, content_description, author, body, now, aid)
        )
        if tag_names is not None:
            _ensure_tags(db, tag_names)
            _set_archive_tags(db, aid, tag_names)
        db.commit()
        tags = _archive_tags(db, aid)
        row = db.execute('SELECT * FROM archives WHERE id = ?', (aid,)).fetchone()

    d = _archive_to_dict(row, tags)
    d['avg_rating'] = _archive_avg_rating(db, aid)
    d['documents'] = _archive_documents(db, aid)
    return jsonify({'status': 'ok', 'archive': d})


@app.route('/api/archives/<aid>', methods=['DELETE'])
@_require_auth
@_require_section('archives')
def delete_archive(aid):
    with _lock:
        db = _get_db()
        docs = db.execute('SELECT filename FROM archive_documents WHERE archive_id = ?', (aid,)).fetchall()
        for doc in docs:
            try:
                os.remove(os.path.join(DOCUMENTS_DIR, doc['filename']))
            except OSError:
                pass
        db.execute('DELETE FROM archive_tags WHERE archive_id = ?', (aid,))
        db.execute('DELETE FROM archive_documents WHERE archive_id = ?', (aid,))
        db.execute('DELETE FROM archive_comments WHERE archive_id = ?', (aid,))
        db.execute('DELETE FROM archive_questions WHERE archive_id = ?', (aid,))
        db.execute('DELETE FROM archive_ratings WHERE archive_id = ?', (aid,))
        db.execute('DELETE FROM archives WHERE id = ?', (aid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 归档文档 API ─────────────────────────────────────────────────────────────
@app.route('/api/archives/<aid>/documents', methods=['POST'])
@_require_auth
@_require_section('archives')
def upload_archive_document(aid):
    if 'document' not in request.files:
        return jsonify({'error': '未找到文件'}), 400
    f = request.files['document']
    if not f.filename:
        return jsonify({'error': '文件名为空'}), 400

    ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
    if ext not in ALLOWED_DOC_EXTS:
        return jsonify({'error': f'不支持的文件格式: {ext}'}), 400

    data = f.read()
    if len(data) > MAX_DOC_SIZE:
        return jsonify({'error': '文件过大（最大 2MB）'}), 400

    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    rand = uuid.uuid4().hex[:6]
    safe_filename = f'{ts}_{rand}.{ext}'
    filepath = os.path.join(DOCUMENTS_DIR, safe_filename)

    with open(filepath, 'wb') as out:
        out.write(data)

    doc_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO archive_documents (id, archive_id, filename, original_name, file_size, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (doc_id, aid, safe_filename, f.filename, len(data), now)
        )
        db.commit()

    return jsonify({
        'status': 'ok',
        'document': {
            'id': doc_id, 'filename': safe_filename, 'original_name': f.filename,
            'file_size': len(data), 'url': f'/data/documents/{safe_filename}', 'created_at': now,
        }
    })


@app.route('/api/archive-documents/<doc_id>', methods=['DELETE'])
@_require_auth
@_require_section('archives')
def delete_archive_document(doc_id):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM archive_documents WHERE id = ?', (doc_id,)).fetchone()
        if not row:
            return jsonify({'error': '文档不存在'}), 404
        try:
            os.remove(os.path.join(DOCUMENTS_DIR, row['filename']))
        except OSError:
            pass
        db.execute('DELETE FROM archive_documents WHERE id = ?', (doc_id,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 归档评论 API ─────────────────────────────────────────────────────────────
@app.route('/api/archives/<aid>/comments', methods=['GET'])
@_require_auth
@_require_section('archives')
def list_archive_comments(aid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM archive_comments WHERE archive_id = ? ORDER BY created_at ASC', (aid,)
    ).fetchall()
    return jsonify([_archive_comment_to_dict(r) for r in rows])


@app.route('/api/archives/<aid>/comments', methods=['POST'])
@_require_auth
@_require_section('archives')
def create_archive_comment(aid):
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
            'INSERT INTO archive_comments (id, archive_id, parent_id, content, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (cid, aid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM archive_comments WHERE id = ?', (cid,)).fetchone()
    return jsonify(_archive_comment_to_dict(row))


@app.route('/api/archive-comments/<cid>', methods=['DELETE'])
@_require_auth
@_require_section('archives')
def delete_archive_comment(cid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM archive_comments WHERE id = ?', (cid,)).fetchone()
        if not row:
            return jsonify({'error': '评论不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'archive_comments', cid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


# ── 归档提问 API ─────────────────────────────────────────────────────────────
@app.route('/api/archives/<aid>/questions', methods=['GET'])
@_require_auth
@_require_section('archives')
def list_archive_questions(aid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM archive_questions WHERE archive_id = ? ORDER BY created_at ASC', (aid,)
    ).fetchall()
    return jsonify([_archive_question_to_dict(r) for r in rows])


@app.route('/api/archives/<aid>/questions', methods=['POST'])
@_require_auth
@_require_section('archives')
def create_archive_question(aid):
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
            'INSERT INTO archive_questions (id, archive_id, parent_id, content, created_by, resolved, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)',
            (qid, aid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM archive_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_archive_question_to_dict(row))


@app.route('/api/archive-questions/<qid>/toggle-resolved', methods=['POST'])
@_require_auth
@_require_section('archives')
def toggle_archive_question_resolved(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM archive_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        new_val = 0 if row['resolved'] else 1
        db.execute('UPDATE archive_questions SET resolved = ? WHERE id = ?', (new_val, qid))
        db.commit()
        row = db.execute('SELECT * FROM archive_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_archive_question_to_dict(row))


@app.route('/api/archive-questions/<qid>', methods=['DELETE'])
@_require_auth
@_require_section('archives')
def delete_archive_question(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM archive_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        deleted_ids = _delete_thread_rows(db, 'archive_questions', qid)
        db.commit()
    return jsonify({'status': 'ok', 'deleted': len(deleted_ids)})


# ── 归档评分 API ─────────────────────────────────────────────────────────────
@app.route('/api/archives/<aid>/ratings', methods=['GET'])
@_require_auth
@_require_section('archives')
def list_archive_ratings(aid):
    db = _get_db()
    rows = db.execute(
        'SELECT * FROM archive_ratings WHERE archive_id = ? ORDER BY created_at DESC', (aid,)
    ).fetchall()
    return jsonify([_archive_rating_to_dict(r) for r in rows])


@app.route('/api/archives/<aid>/ratings', methods=['POST'])
@_require_auth
@_require_section('archives')
def create_archive_rating(aid):
    b = request.get_json(force=True) or {}
    rating = int(b.get('rating', 0))
    if rating < 1 or rating > 5:
        return jsonify({'error': '评分必须在1-5之间'}), 400
    rid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO archive_ratings (id, archive_id, rating, created_by, created_at) VALUES (?, ?, ?, ?, ?)',
            (rid, aid, rating, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM archive_ratings WHERE id = ?', (rid,)).fetchone()
    return jsonify(_archive_rating_to_dict(row))


@app.route('/api/archive-ratings/<rid>', methods=['DELETE'])
@_require_auth
@_require_section('archives')
def delete_archive_rating(rid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT id FROM archive_ratings WHERE id = ?', (rid,)).fetchone()
        if not row:
            return jsonify({'error': '评分记录不存在'}), 404
        db.execute('DELETE FROM archive_ratings WHERE id = ?', (rid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 项目 API ─────────────────────────────────────────────────────────────────

@app.route('/api/projects', methods=['GET'])
@_require_auth
@_require_section('projects')
def list_projects():
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role == ROLE_ADMIN:
        rows = db.execute('SELECT * FROM projects ORDER BY updated_at DESC').fetchall()
    else:
        rows = db.execute(
            'SELECT p.* FROM projects p JOIN project_members pm ON pm.project_id = p.id WHERE pm.username = ? ORDER BY p.updated_at DESC',
            (g.current_user,)
        ).fetchall()
    result = []
    for r in rows:
        members = db.execute('SELECT username FROM project_members WHERE project_id = ?', (r['id'],)).fetchall()
        result.append({
            'id': r['id'], 'name': r['name'], 'created_by': r['created_by'],
            'created_at': r['created_at'], 'updated_at': r['updated_at'],
            'members': [m['username'] for m in members],
        })
    return jsonify(result)


@app.route('/api/projects', methods=['POST'])
@_require_auth
@_require_section('projects')
def create_project():
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role != ROLE_ADMIN:
        return jsonify({'error': '无权限'}), 403
    b = request.get_json(force=True) or {}
    name = str(b.get('name', '')).strip()
    if not name:
        return jsonify({'error': '请输入项目名称'}), 400
    if len(name) > MAX_TITLE_LENGTH:
        return jsonify({'error': '项目名称过长'}), 400
    members = b.get('members', [])
    pid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO projects (id, name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
            (pid, name, g.current_user, now, now)
        )
        for uname in members:
            db.execute('INSERT OR IGNORE INTO project_members (project_id, username) VALUES (?, ?)', (pid, uname))
        db.commit()
    mrows = db.execute('SELECT username FROM project_members WHERE project_id = ?', (pid,)).fetchall()
    return jsonify({
        'status': 'ok',
        'project': {'id': pid, 'name': name, 'created_by': g.current_user,
                     'created_at': now, 'updated_at': now,
                     'members': [m['username'] for m in mrows]},
    })


@app.route('/api/projects/<pid>', methods=['GET'])
@_require_auth
@_require_section('projects')
def get_project(pid):
    db = _get_db()
    row = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not row:
        return jsonify({'error': '项目不存在'}), 404
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    members = db.execute('SELECT username FROM project_members WHERE project_id = ?', (pid,)).fetchall()
    return jsonify({
        'id': row['id'], 'name': row['name'], 'created_by': row['created_by'],
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'members': [m['username'] for m in members],
    })


@app.route('/api/projects/<pid>', methods=['PUT'])
@_require_auth
@_require_section('projects')
def update_project(pid):
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role != ROLE_ADMIN:
        return jsonify({'error': '无权限'}), 403
    row = db.execute('SELECT * FROM projects WHERE id = ?', (pid,)).fetchone()
    if not row:
        return jsonify({'error': '项目不存在'}), 404
    b = request.get_json(force=True) or {}
    name = str(b.get('name', row['name'])).strip()
    if not name:
        return jsonify({'error': '项目名称不能为空'}), 400
    if len(name) > MAX_TITLE_LENGTH:
        return jsonify({'error': '项目名称过长'}), 400
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute('UPDATE projects SET name = ?, updated_at = ? WHERE id = ?', (name, now, pid))
        if 'members' in b:
            db.execute('DELETE FROM project_members WHERE project_id = ?', (pid,))
            for uname in b['members']:
                db.execute('INSERT OR IGNORE INTO project_members (project_id, username) VALUES (?, ?)', (pid, uname))
        db.commit()
    mrows = db.execute('SELECT username FROM project_members WHERE project_id = ?', (pid,)).fetchall()
    return jsonify({
        'status': 'ok',
        'project': {'id': pid, 'name': name, 'created_by': row['created_by'],
                     'created_at': row['created_at'], 'updated_at': now,
                     'members': [m['username'] for m in mrows]},
    })


@app.route('/api/projects/<pid>', methods=['DELETE'])
@_require_auth
@_require_section('projects')
def delete_project(pid):
    db = _get_db()
    role = _get_user_role(db, g.current_user)
    if role != ROLE_ADMIN:
        return jsonify({'error': '无权限'}), 403
    row = db.execute('SELECT id FROM projects WHERE id = ?', (pid,)).fetchone()
    if not row:
        return jsonify({'error': '项目不存在'}), 404
    with _lock:
        db = _get_db()
        db.execute('DELETE FROM project_members WHERE project_id = ?', (pid,))
        db.execute('DELETE FROM project_topics WHERE project_id = ?', (pid,))
        # notes cascade handles comments, questions, tags
        nids = db.execute('SELECT id FROM project_notes WHERE project_id = ?', (pid,)).fetchall()
        for n in nids:
            db.execute('DELETE FROM project_note_tags WHERE note_id = ?', (n['id'],))
            db.execute('DELETE FROM project_comments WHERE note_id = ?', (n['id'],))
            db.execute('DELETE FROM project_questions WHERE note_id = ?', (n['id'],))
        db.execute('DELETE FROM project_notes WHERE project_id = ?', (pid,))
        db.execute('DELETE FROM projects WHERE id = ?', (pid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 项目主题 ─────────────────────────────────────────────────────────────────

@app.route('/api/projects/<pid>/topics', methods=['GET'])
@_require_auth
@_require_section('projects')
def list_project_topics(pid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    source = request.args.get('source', '').strip()
    if source == 'notes':
        rows = db.execute(
            "SELECT DISTINCT topic AS name FROM project_notes WHERE project_id = ? AND topic != '' ORDER BY topic",
            (pid,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT name FROM project_topics WHERE project_id = ? ORDER BY name',
            (pid,)
        ).fetchall()
    return jsonify([{'id': i, 'name': r['name']} for i, r in enumerate(rows)])


# ── 项目笔记 CRUD ───────────────────────────────────────────────────────────

@app.route('/api/projects/<pid>/notes', methods=['GET'])
@_require_auth
@_require_section('projects')
def list_project_notes(pid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    topic = request.args.get('topic', '').strip()
    tag = request.args.get('tag', '').strip()
    sort = request.args.get('sort', 'updated_desc').strip()
    q = request.args.get('q', '').strip()
    fields = request.args.get('fields', '').strip()

    sql = 'SELECT DISTINCT n.* FROM project_notes n'
    params = []
    if tag:
        sql += ' JOIN project_note_tags pnt ON pnt.note_id = n.id JOIN tags t ON t.id = pnt.tag_id'
    sql += ' WHERE n.project_id = ?'
    params.append(pid)
    if topic:
        sql += ' AND n.topic = ?'
        params.append(topic)
    if tag:
        sql += ' AND t.name = ?'
        params.append(tag)
    if q:
        sql += ' AND (n.title LIKE ? OR n.content LIKE ?)'
        like = f'%{q}%'
        params.extend([like, like])

    sort_map = {
        'updated_desc': 'n.updated_at DESC',
        'updated_asc': 'n.updated_at ASC',
        'created_desc': 'n.created_at DESC',
        'created_asc': 'n.created_at ASC',
        'priority_desc': 'n.priority DESC, n.updated_at DESC',
        'priority_asc': 'n.priority ASC, n.updated_at DESC',
    }
    sql += ' ORDER BY ' + sort_map.get(sort, 'n.updated_at DESC')

    rows = db.execute(sql, params).fetchall()
    result = []
    for r in rows:
        tags = _project_note_tags(db, r['id'])
        d = _project_note_to_dict(r, tags)
        if fields == 'metadata':
            d.pop('content', None)
        result.append(d)
    return jsonify(result)


@app.route('/api/projects/<pid>/notes', methods=['POST'])
@_require_auth
@_require_section('projects')
def create_project_note(pid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    row = db.execute('SELECT id FROM projects WHERE id = ?', (pid,)).fetchone()
    if not row:
        return jsonify({'error': '项目不存在'}), 404

    b = request.get_json(force=True) or {}
    topic = str(b.get('topic', '')).strip()
    if not topic:
        return jsonify({'error': '主题不能为空'}), 400
    title = str(b.get('title', '')).strip()
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({'error': '标题过长'}), 400
    content = str(b.get('content', ''))
    if len(content.encode('utf-8')) > MAX_CONTENT_SIZE:
        return jsonify({'error': '内容过大'}), 400
    priority = int(b.get('priority', 0))
    tag_names = b.get('tags', [])

    nid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        _ensure_project_topic(db, pid, topic)
        _ensure_tags(db, tag_names)
        db.execute(
            'INSERT INTO project_notes (id, project_id, title, topic, content, priority, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (nid, pid, title, topic, content, priority, now, now)
        )
        _set_project_note_tags(db, nid, tag_names)
        db.commit()
        nr = db.execute('SELECT * FROM project_notes WHERE id = ?', (nid,)).fetchone()
    tags = _project_note_tags(db, nid)
    return jsonify(_project_note_to_dict(nr, tags))


@app.route('/api/projects/<pid>/notes/<nid>', methods=['GET'])
@_require_auth
@_require_section('projects')
def get_project_note(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    row = db.execute('SELECT * FROM project_notes WHERE id = ? AND project_id = ?', (nid, pid)).fetchone()
    if not row:
        return jsonify({'error': '笔记不存在'}), 404
    tags = _project_note_tags(db, nid)
    return jsonify(_project_note_to_dict(row, tags))


@app.route('/api/projects/<pid>/notes/<nid>', methods=['PUT'])
@_require_auth
@_require_section('projects')
def update_project_note(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    b = request.get_json(force=True) or {}
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM project_notes WHERE id = ? AND project_id = ?', (nid, pid)).fetchone()
        if not row:
            return jsonify({'error': '笔记不存在'}), 404
        title = str(b.get('title', row['title'])).strip()
        topic = str(b.get('topic', row['topic'])).strip()
        if not topic:
            return jsonify({'error': '主题不能为空'}), 400
        if len(title) > MAX_TITLE_LENGTH:
            return jsonify({'error': '标题过长'}), 400
        content = str(b.get('content', row['content']))
        if len(content.encode('utf-8')) > MAX_CONTENT_SIZE:
            return jsonify({'error': '内容过大'}), 400
        priority = int(b.get('priority', row['priority']))
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        _ensure_project_topic(db, pid, topic)
        db.execute(
            'UPDATE project_notes SET title = ?, topic = ?, content = ?, priority = ?, updated_at = ? WHERE id = ?',
            (title, topic, content, priority, now, nid)
        )
        if 'tags' in b:
            _ensure_tags(db, b['tags'])
            _set_project_note_tags(db, nid, b['tags'])
        db.commit()
        nr = db.execute('SELECT * FROM project_notes WHERE id = ?', (nid,)).fetchone()
    tags = _project_note_tags(db, nid)
    return jsonify(_project_note_to_dict(nr, tags))


@app.route('/api/projects/<pid>/notes/<nid>', methods=['DELETE'])
@_require_auth
@_require_section('projects')
def delete_project_note(pid, nid):
    with _lock:
        db = _get_db()
        if not _check_project_member(db, pid, g.current_user):
            return jsonify({'error': '无权访问此项目'}), 403
        row = db.execute('SELECT id FROM project_notes WHERE id = ? AND project_id = ?', (nid, pid)).fetchone()
        if not row:
            return jsonify({'error': '笔记不存在'}), 404
        db.execute('DELETE FROM project_note_tags WHERE note_id = ?', (nid,))
        db.execute('DELETE FROM project_comments WHERE note_id = ?', (nid,))
        db.execute('DELETE FROM project_questions WHERE note_id = ?', (nid,))
        db.execute('DELETE FROM project_notes WHERE id = ?', (nid,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 项目笔记评论 & 问题 ─────────────────────────────────────────────────────

@app.route('/api/projects/<pid>/notes/<nid>/comments', methods=['GET'])
@_require_auth
@_require_section('projects')
def list_project_comments(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    rows = db.execute('SELECT * FROM project_comments WHERE note_id = ? ORDER BY created_at ASC', (nid,)).fetchall()
    return jsonify([_project_comment_to_dict(r) for r in rows])


@app.route('/api/projects/<pid>/notes/<nid>/comments', methods=['POST'])
@_require_auth
@_require_section('projects')
def create_project_comment(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    if not content:
        return jsonify({'error': '评论内容不能为空'}), 400
    parent_id = b.get('parent_id')
    cid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO project_comments (id, note_id, parent_id, content, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (cid, nid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM project_comments WHERE id = ?', (cid,)).fetchone()
    return jsonify(_project_comment_to_dict(row))


@app.route('/api/project-comments/<cid>', methods=['DELETE'])
@_require_auth
@_require_section('projects')
def delete_project_comment(cid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM project_comments WHERE id = ?', (cid,)).fetchone()
        if not row:
            return jsonify({'error': '评论不存在'}), 404
        # Delete replies first
        to_delete = [cid]
        queue = [cid]
        while queue:
            parent = queue.pop(0)
            children = db.execute('SELECT id FROM project_comments WHERE parent_id = ?', (parent,)).fetchall()
            for c in children:
                to_delete.append(c['id'])
                queue.append(c['id'])
        for did in to_delete:
            db.execute('DELETE FROM project_comments WHERE id = ?', (did,))
        db.commit()
    return jsonify({'status': 'ok'})


@app.route('/api/projects/<pid>/notes/<nid>/questions', methods=['GET'])
@_require_auth
@_require_section('projects')
def list_project_questions(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    rows = db.execute('SELECT * FROM project_questions WHERE note_id = ? ORDER BY created_at ASC', (nid,)).fetchall()
    return jsonify([_project_question_to_dict(r) for r in rows])


@app.route('/api/projects/<pid>/notes/<nid>/questions', methods=['POST'])
@_require_auth
@_require_section('projects')
def create_project_question(pid, nid):
    db = _get_db()
    if not _check_project_member(db, pid, g.current_user):
        return jsonify({'error': '无权访问此项目'}), 403
    b = request.get_json(force=True) or {}
    content = str(b.get('content', '')).strip()
    if not content:
        return jsonify({'error': '问题内容不能为空'}), 400
    parent_id = b.get('parent_id')
    qid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        db = _get_db()
        db.execute(
            'INSERT INTO project_questions (id, note_id, parent_id, content, created_by, resolved, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)',
            (qid, nid, parent_id, content, g.current_user, now)
        )
        db.commit()
        row = db.execute('SELECT * FROM project_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_project_question_to_dict(row))


@app.route('/api/project-questions/<qid>/toggle-resolved', methods=['POST'])
@_require_auth
@_require_section('projects')
def toggle_project_question_resolved(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM project_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        new_val = 0 if row['resolved'] else 1
        db.execute('UPDATE project_questions SET resolved = ? WHERE id = ?', (new_val, qid))
        db.commit()
        row = db.execute('SELECT * FROM project_questions WHERE id = ?', (qid,)).fetchone()
    return jsonify(_project_question_to_dict(row))


@app.route('/api/project-questions/<qid>', methods=['DELETE'])
@_require_auth
@_require_section('projects')
def delete_project_question(qid):
    with _lock:
        db = _get_db()
        row = db.execute('SELECT * FROM project_questions WHERE id = ?', (qid,)).fetchone()
        if not row:
            return jsonify({'error': '问题不存在'}), 404
        to_delete = [qid]
        queue = [qid]
        while queue:
            parent = queue.pop(0)
            children = db.execute('SELECT id FROM project_questions WHERE parent_id = ?', (parent,)).fetchall()
            for c in children:
                to_delete.append(c['id'])
                queue.append(c['id'])
        for did in to_delete:
            db.execute('DELETE FROM project_questions WHERE id = ?', (did,))
        db.commit()
    return jsonify({'status': 'ok'})


# ── 启动 ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    with app.app_context():
        _init_db()
    app.run(debug=True, port=3006)
else:
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(DOCUMENTS_DIR, exist_ok=True)
    with app.app_context():
        _init_db()
