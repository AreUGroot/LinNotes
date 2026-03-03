# Lin Notes 项目指引

## 项目概述
Lin Notes 是一个基于 Flask + SQLite 的笔记管理系统，运行在 `https://notes.linguistat.com`。

## API 使用

### 认证
所有 API 请求需要 Bearer Token 认证。登录获取 Token：
```bash
curl -s -X POST https://notes.linguistat.com/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"Lin","password":"951204"}'
```

### 常用操作

**创建笔记：**
```bash
curl -s -X POST https://notes.linguistat.com/api/notes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"标题","topic":"主题名","content":"Markdown正文","priority":0,"tags":["标签"]}'
```
必填字段：`topic`。可用主题：数学、计算机科学、哲学、经济学、物理（可通过 GET /api/topics 查询）。

**创建观点：**
```bash
curl -s -X POST https://notes.linguistat.com/api/opinions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"观点","topic":"主题名","title":"标题","content":"正文"}'
```
必填字段：`type`（可选值：事实、观点、反驳、预测、计划）。

**创建归档：**
```bash
curl -s -X POST https://notes.linguistat.com/api/archives \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"标题","topic":"主题名","content_description":"描述（必填）","author":"作者","body":"正文","tags":["标签"]}'
```

### 查询
- 笔记列表：`GET /api/notes?fields=metadata`
- 观点列表：`GET /api/opinions?fields=metadata`
- 归档列表：`GET /api/archives?fields=metadata`
- 主题列表：`GET /api/topics`（支持 `?section=notes|opinions|archives` 按模块筛选）
- 标签列表：`GET /api/tags`

所有列表接口支持 `q` 参数进行模糊搜索，如 `GET /api/notes?q=关键词`。

### 中文输出
格式化 JSON 时使用以下命令确保中文正常显示：
```bash
| python3 -c "import sys,json;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=2))"
```

## 完整 API 文档
详见 [API.md](API.md)
