# Lin Notes API 文档

## 概述

Lin Notes 提供 RESTful API，允许已注册用户通过编程方式访问和管理笔记、观点库和归档数据。所有 API 端点返回 JSON 格式响应。

---

## 认证

### 方式一：登录获取会话 Token（适用于浏览器/短期使用）

```bash
curl -X POST https://your-server/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "你的用户名", "password": "你的密码"}'
```

**响应：**
```json
{
  "status": "ok",
  "token": "会话token",
  "username": "你的用户名",
  "role": "user",
  "permissions": ["opinions", "archives"]
}
```

> 注意：会话 Token 保存在服务器内存中，服务器重启后失效。

### 方式二：API Key（推荐，适用于编程/长期使用）

API Key 持久化存储在数据库中，服务器重启后仍然有效。

**创建 API Key（需先用会话 Token 认证）：**

```bash
curl -X POST https://your-server/api/auth/api-keys \
  -H "Authorization: Bearer 你的会话token" \
  -H "Content-Type: application/json" \
  -d '{"label": "我的脚本"}'
```

**响应：**
```json
{
  "status": "ok",
  "key": "完整的API密钥（仅此一次显示）",
  "label": "我的脚本",
  "created_at": "2024-01-01 00:00:00",
  "warning": "请保存此密钥，它不会再次显示。"
}
```

> **重要：请立即保存返回的 `key`，它仅在创建时显示一次。**

**使用 API Key 访问接口：**

```bash
curl https://your-server/api/opinions?fields=metadata \
  -H "Authorization: Bearer 你的API密钥"
```

### 查看当前用户信息

```bash
curl https://your-server/api/auth/me \
  -H "Authorization: Bearer 你的token或API密钥"
```

**响应：**
```json
{
  "status": "ok",
  "username": "你的用户名",
  "role": "user",
  "permissions": ["opinions", "archives"]
}
```

### 管理 API Key

**列出已有 Key：**
```bash
curl https://your-server/api/auth/api-keys \
  -H "Authorization: Bearer 你的token"
```

**删除 Key（用前8位字符作为标识）：**
```bash
curl -X DELETE https://your-server/api/auth/api-keys/前8位字符 \
  -H "Authorization: Bearer 你的token"
```

---

## 权限

系统包含三个模块，每个用户拥有不同的访问权限：

| 模块 | 说明 |
|------|------|
| `notes` | 笔记 |
| `opinions` | 观点库 |
| `archives` | 归档 |

- **admin** 角色自动拥有所有模块的权限
- **user** 角色仅能访问管理员分配的模块
- 访问无权限的模块会返回 `403` 错误

可通过 `GET /api/auth/me` 查看当前用户的 `permissions` 字段确认可访问的模块。

---

## 获取列表（元数据模式 vs 完整模式）

### 元数据模式（不含正文）

在列表请求中添加 `?fields=metadata` 参数，返回结果将不包含正文内容（`content` 或 `body` 字段），适合获取条目概览。

```bash
# 获取笔记列表（不含正文）
curl "https://your-server/api/notes?fields=metadata" \
  -H "Authorization: Bearer 你的API密钥"

# 获取观点列表（不含正文）
curl "https://your-server/api/opinions?fields=metadata" \
  -H "Authorization: Bearer 你的API密钥"

# 获取归档列表（不含正文）
curl "https://your-server/api/archives?fields=metadata" \
  -H "Authorization: Bearer 你的API密钥"
```

### 完整模式（包含正文）

不带 `?fields=metadata` 参数，或通过单条获取接口，会返回完整内容。

```bash
# 获取笔记列表（含正文）
curl "https://your-server/api/notes" \
  -H "Authorization: Bearer 你的API密钥"

# 获取单条笔记（始终含完整内容）
curl "https://your-server/api/notes/笔记ID" \
  -H "Authorization: Bearer 你的API密钥"
```

---

## 端点详情

### 笔记 (Notes) — 需要 `notes` 权限

#### 获取笔记列表

```
GET /api/notes
GET /api/notes?fields=metadata
```

**查询参数：**

| 参数 | 说明 |
|------|------|
| `topic` | 按主题筛选 |
| `tag` | 按标签筛选 |
| `q` | 模糊搜索（搜索标题和正文） |
| `sort` | 排序方式：`updated_desc`（默认）、`created_desc`、`created_asc`、`priority_desc`、`priority_asc` |
| `fields` | 设为 `metadata` 则不返回 `content` 字段 |

**响应字段（元数据模式）：**
```json
[
  {
    "id": "abc123def456",
    "title": "笔记标题",
    "topic": "数学",
    "priority": 3,
    "tags": ["线性代数", "矩阵"],
    "created_at": "2024-01-01 00:00:00",
    "updated_at": "2024-01-02 00:00:00"
  }
]
```

**完整模式额外包含：** `content`（Markdown 正文，图片以 Markdown 语法嵌入）

#### 获取单条笔记

```
GET /api/notes/<id>
```

始终返回完整内容。

#### 创建笔记

```
POST /api/notes
```

**请求体：**
```json
{
  "title": "笔记标题",
  "topic": "主题名（必填）",
  "content": "Markdown 正文",
  "priority": 0,
  "tags": ["标签1", "标签2"]
}
```

**必填字段：** `topic`

#### 更新笔记

```
PUT /api/notes/<id>
```

请求体同创建。

#### 删除笔记

```
DELETE /api/notes/<id>
```

---

### 观点库 (Opinions) — 需要 `opinions` 权限

#### 获取观点列表

```
GET /api/opinions
GET /api/opinions?fields=metadata
```

**查询参数：**

| 参数 | 说明 |
|------|------|
| `type` | 按类型筛选：`事实`、`观点`、`反驳`、`预测`、`计划` |
| `topic` | 按主题筛选 |
| `created_by` | 按创建者筛选 |
| `predictor` | 按预测者筛选 |
| `q` | 模糊搜索（搜索标题和正文） |
| `sort` | 排序方式：`updated_desc`（默认）、`created_desc`、`created_asc`、`verify_priority_desc`、`verify_priority_asc`、`rating_desc`、`rating_asc`、`measure_time_desc`、`measure_time_asc` |
| `fields` | 设为 `metadata` 则不返回 `content` 字段 |

**响应字段（元数据模式）：**
```json
[
  {
    "id": "abc123def456",
    "type": "观点",
    "topic": "经济",
    "title": "观点标题",
    "predictor": "",
    "time_scale": "",
    "rebutted_opinion": "",
    "rebutted_source": "",
    "fact_source": "",
    "fact_link": "",
    "verify_priority": 0,
    "created_by": "Lin",
    "created_at": "2024-01-01 00:00:00",
    "updated_at": "2024-01-02 00:00:00",
    "avg_rating": 3.5,
    "measure_time": "2024-06"
  }
]
```

**完整模式额外包含：** `content`（Markdown 正文）

#### 创建观点

```
POST /api/opinions
```

**请求体：**
```json
{
  "type": "观点（必填，可选值：事实/观点/反驳/预测/计划）",
  "topic": "主题名",
  "title": "标题",
  "content": "Markdown 正文",
  "predictor": "预测者（预测类型填写）",
  "time_scale": "时间尺度",
  "rebutted_opinion": "被反驳的观点（反驳类型填写）",
  "rebutted_source": "反驳来源",
  "fact_source": "出处描述（事实类型必填）",
  "fact_link": "出处链接",
  "verify_priority": 0
}
```

**必填字段：** `type`。不同类型有额外必填项：
- `反驳` 类型：`content`（正文）、`predictor`（反驳人）必填
- `事实` 类型：`fact_source`（出处描述）必填

#### 获取/更新/删除观点

```
GET    /api/opinions/<id>
PUT    /api/opinions/<id>
DELETE /api/opinions/<id>
```

#### 观点评分

```
GET  /api/opinions/<id>/ratings        # 查看评分列表
POST /api/opinions/<id>/ratings        # 添加评分
DELETE /api/opinion-ratings/<rating_id> # 删除评分
```

**添加评分请求体：**
```json
{
  "rating": 4,
  "measure_time": "2024-06"
}
```

`rating` 必须在 1-5 之间。

#### 观点评论与提问

```
GET  /api/opinions/<id>/comments          # 查看评论
POST /api/opinions/<id>/comments          # 添加评论
DELETE /api/opinion-comments/<comment_id> # 删除评论

GET  /api/opinions/<id>/questions                     # 查看提问
POST /api/opinions/<id>/questions                     # 添加提问
POST /api/opinion-questions/<question_id>/toggle-resolved # 切换已解决状态
DELETE /api/opinion-questions/<question_id>            # 删除提问
```

**评论/提问请求体：**
```json
{
  "content": "评论或提问内容（必填）",
  "parent_id": "父级ID（可选，用于回复嵌套）"
}
```

---

### 归档 (Archives) — 需要 `archives` 权限

#### 获取归档列表

```
GET /api/archives
GET /api/archives?fields=metadata
```

**查询参数：**

| 参数 | 说明 |
|------|------|
| `topic` | 按主题筛选 |
| `tag` | 按标签筛选 |
| `q` | 模糊搜索（搜索标题和内容描述） |
| `sort` | 排序方式：`updated_desc`（默认）、`created_desc`、`created_asc`、`rating_desc`、`rating_asc` |
| `fields` | 设为 `metadata` 则不返回 `body` 字段 |

**响应字段（元数据模式）：**
```json
[
  {
    "id": "abc123def456",
    "title": "归档标题",
    "topic": "技术",
    "content_description": "内容描述及链接",
    "author": "作者",
    "tags": ["Python", "Flask"],
    "created_by": "Lin",
    "created_at": "2024-01-01 00:00:00",
    "updated_at": "2024-01-02 00:00:00",
    "avg_rating": 4.0,
    "document_count": 2
  }
]
```

**完整模式额外包含：** `body`（Markdown 正文）

#### 获取单条归档

```
GET /api/archives/<id>
```

返回完整内容，包括 `body` 和 `documents` 列表：
```json
{
  "id": "...",
  "body": "Markdown 正文",
  "documents": [
    {
      "id": "doc_id",
      "filename": "20240101120000_abc123.pdf",
      "original_name": "论文.pdf",
      "file_size": 102400,
      "url": "/data/documents/20240101120000_abc123.pdf",
      "created_at": "2024-01-01 00:00:00"
    }
  ]
}
```

#### 创建归档

```
POST /api/archives
```

**请求体：**
```json
{
  "title": "归档标题",
  "topic": "主题名",
  "content_description": "内容描述及链接（必填）",
  "author": "作者",
  "body": "Markdown 正文",
  "tags": ["标签1", "标签2"]
}
```

**必填字段：** `content_description`

#### 更新/删除归档

```
PUT    /api/archives/<id>
DELETE /api/archives/<id>
```

#### 归档文档上传

```
POST /api/archives/<id>/documents
```

使用 `multipart/form-data` 上传：
```bash
curl -X POST https://your-server/api/archives/归档ID/documents \
  -H "Authorization: Bearer 你的API密钥" \
  -F "document=@/path/to/file.pdf"
```

**限制：** 单文件最大 2MB。支持格式：pdf, doc, docx, xls, xlsx, ppt, pptx, txt, csv, zip, rar, md, png, jpg, jpeg, gif, webp

#### 归档评分、评论、提问

与观点库的评分/评论/提问接口格式相同，路径前缀替换为 `/api/archives/`：

```
GET/POST  /api/archives/<id>/ratings
DELETE    /api/archive-ratings/<rating_id>
GET/POST  /api/archives/<id>/comments
DELETE    /api/archive-comments/<comment_id>
GET/POST  /api/archives/<id>/questions
POST      /api/archive-questions/<question_id>/toggle-resolved
DELETE    /api/archive-questions/<question_id>
```

---

### 共享端点

以下端点所有已认证用户均可访问，不受模块权限限制：

```
GET  /api/topics              # 获取所有主题（用于表单选择器）
GET  /api/topics?section=notes     # 获取笔记模块的主题
GET  /api/topics?section=opinions  # 获取观点库模块的主题
GET  /api/topics?section=archives  # 获取归档模块的主题
POST /api/topics              # 创建主题（请求体：{"name": "主题名"}）
GET  /api/tags                # 获取所有标签
POST /api/tags                # 创建标签（请求体：{"name": "标签名"}）
```

> `section` 参数可选。指定时返回该模块中实际使用的主题；不指定时返回全局主题表（用于表单层级选择器）。各模块的主题互相独立。

### 图片上传

```
POST /api/upload
```

使用 `multipart/form-data`：
```bash
curl -X POST https://your-server/api/upload \
  -H "Authorization: Bearer 你的API密钥" \
  -F "image=@/path/to/photo.png"
```

**限制：** 最大 10MB。支持格式：png, jpg, jpeg, gif, webp

**响应：**
```json
{
  "status": "ok",
  "url": "/data/images/20240101120000_abc123.png"
}
```

在笔记正文中引用图片：`![描述](返回的url)`

---

## 频率限制

- 每个 Token/API Key 每分钟最多 **60** 次请求
- 超限时返回 `429` 状态码：`{"error": "请求过于频繁，请稍后再试"}`

## 输入限制

- 标题最大长度：500 字符
- 正文/body 最大长度：500 KB
- 单次请求最大：16 MB

---

## 错误码

| 状态码 | 说明 |
|--------|------|
| `400` | 请求参数错误（缺少必填字段、格式不正确等） |
| `401` | 未认证（缺少 Token 或 Token 无效/过期） |
| `403` | 无权限（用户无法访问该模块） |
| `404` | 资源不存在 |
| `413` | 请求体过大（超过 16 MB） |
| `429` | 请求过于频繁 |

所有错误响应格式：
```json
{"error": "错误描述信息"}
```

---

## 部署指南

### 本地开发

```bash
pip install flask flask-cors
python app.py
# 服务运行在 http://localhost:3006
```

### 生产环境部署

#### 1. 安装依赖

```bash
pip install flask flask-cors gunicorn
```

#### 2. 使用 Gunicorn 启动

```bash
gunicorn -w 4 -b 0.0.0.0:3006 app:app
```

- `-w 4`：4 个工作进程（根据 CPU 核数调整）
- `-b 0.0.0.0:3006`：监听所有地址的 3006 端口

#### 3. 配置 Nginx 反向代理

```nginx
server {
    listen 80;
    server_name notes.example.com;

    client_max_body_size 16M;

    location / {
        proxy_pass http://127.0.0.1:3006;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### 4. 启用 HTTPS（强烈推荐）

```bash
# 使用 certbot 获取免费 SSL 证书
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d notes.example.com
```

#### 5. 使用 systemd 管理服务

创建 `/etc/systemd/system/lin-notes.service`：

```ini
[Unit]
Description=Lin Notes
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/lin_notes
ExecStart=/path/to/gunicorn -w 4 -b 127.0.0.1:3006 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable lin-notes
sudo systemctl start lin-notes
```

### 启用 API 访问步骤

1. 完成上述部署，确保服务运行正常
2. 通过网页端或命令行登录获取会话 Token
3. 用会话 Token 调用 `POST /api/auth/api-keys` 创建 API Key
4. 保存返回的 Key
5. 后续所有 API 调用使用 `Authorization: Bearer 你的API密钥`

### 安全建议

- **务必启用 HTTPS**，防止 Token/API Key 在传输中泄露
- 生产环境应限制 CORS 来源（在 `app.py` 中修改 `CORS(app)` 为 `CORS(app, origins=["https://notes.example.com"])`）
- 定期轮换 API Key
- 使用防火墙仅开放 80/443 端口
- 密码目前为明文存储，建议在生产环境启用前进行密码哈希升级

---

## 终端中文显示

`python3 -m json.tool` 默认会将中文转义为 `\uXXXX`。建议在终端中先定义以下快捷函数，后续所有命令用 `pj` 替代 `python3 -m json.tool` 即可正常显示中文：

```bash
pj() { python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=2))"; }
```

使用示例：

```bash
curl -s "https://your-server/api/notes?fields=metadata" \
  -H "Authorization: Bearer $API_KEY" | pj
```

---

## 快速开始示例

### Python 示例

```python
import requests

BASE_URL = "https://your-server"
API_KEY = "你的API密钥"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# 获取观点库列表（仅元数据）
resp = requests.get(f"{BASE_URL}/api/opinions?fields=metadata", headers=HEADERS)
opinions = resp.json()
print(f"共 {len(opinions)} 条观点")

# 获取单条观点完整内容
if opinions:
    detail = requests.get(f"{BASE_URL}/api/opinions/{opinions[0]['id']}", headers=HEADERS)
    print(detail.json()['content'])

# 创建新观点
new_opinion = requests.post(f"{BASE_URL}/api/opinions", headers=HEADERS, json={
    "type": "观点",
    "topic": "技术",
    "title": "AI 将改变软件开发",
    "content": "## 正文\n\n详细内容...",
})
print(new_opinion.json())
```

### curl 示例

```bash
API_KEY="你的API密钥"

# 查看权限
curl -s "https://your-server/api/auth/me" \
  -H "Authorization: Bearer $API_KEY" | pj

# 获取归档列表（元数据）
curl -s "https://your-server/api/archives?fields=metadata" \
  -H "Authorization: Bearer $API_KEY" | pj

# 创建归档
curl -X POST "https://your-server/api/archives" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "测试归档",
    "topic": "技术",
    "content_description": "这是一个测试归档的描述",
    "author": "作者名",
    "body": "## 正文\n\nMarkdown内容..."
  }' | pj
```
