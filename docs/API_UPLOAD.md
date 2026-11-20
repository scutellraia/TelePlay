# TelePlay 上传中心 · API 上传规则

> 服务地址：`https://teleplay.de5.net`
> 适用接口：`/up/api/*`
> 文档对应版本：2026-11-17

---

## 1. 如何认证

API 有两种认证方式，**上传类接口两种都接受**。

### 方式一：API 令牌（推荐给服务器/脚本）

在 `https://teleplay.de5.net/up` 页面底部「API 上传令牌」卡片生成。

- 令牌格式：`tp_` + 32 位十六进制
- 请求头：`X-API-Key: tp_xxxxxxxx`
- **明文只在生成时显示一次**，服务器只保存 SHA-256 哈希
- 权限：**只能上传文件和列文件夹**，不能读媒体库、改设置、改密码
- 可随时在页面删除，删除后立即失效（返回 401）

```bash
K=tp_你的令牌
curl -H "X-API-Key: $K" https://teleplay.de5.net/up/api/folders
```

### 方式二：密码登录换会话（临时脚本）

```bash
B=https://teleplay.de5.net
TOKEN=$(curl -s -X POST $B/up/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的网页访问密码"}' \
  | sed -E 's/.*"token":"([^"]+)".*/\1/')

curl -H "Authorization: Bearer $TOKEN" $B/up/api/folders
```

- 会话有效期 **180 天**（refresh token 同为 180 天）
- 密码连续错误 5 次会锁定 60 秒
- 会话权限等同网页登录：上传 + 读状态 + 改设置

> 自动化场景优先用令牌：令牌里不含密码，泄漏只影响上传。

---

## 2. 接口清单

| 方法 | 路径 | 认证 | 用途 |
|---|---|---|---|
| GET | `/up/api/folders` | 令牌 / 会话 | 列出文件夹（取 ID） |
| POST | `/up/api/upload` | 令牌 / 会话 | 上传文件（multipart） |
| PUT | `/up/api/upload/raw` | 令牌 / 会话 | 上传文件（流式，推荐大文件） |
| GET | `/up/api/processed` | 仅会话 | 查看最近自动上传记录 |
| GET | `/up/api/status` | 仅会话 | aria2 与磁盘状态 |
| GET | `/up/api/server` | 仅会话 | 宿主机指标 |
| POST | `/up/api/torrent` | 仅会话 | 添加 BT/直链下载任务 |
| GET | `/up/api/tasks` | 仅会话 | 下载任务列表 |
| GET | `/up/api/settings` | 仅会话 | 读兜底对账频率 |
| POST | `/up/api/settings` | 仅会话 | 改兜底对账频率 |
| GET | `/up/api/keys` | 仅会话 | 列出 API 令牌 |
| POST | `/up/api/keys` | 仅会话 | 创建 API 令牌 |
| DELETE | `/up/api/keys/{id}` | 仅会话 | 删除 API 令牌 |

---

## 3. 上传接口参数

### 3.1 `PUT /up/api/upload/raw`（推荐）

流式接收原始请求体，没有 multipart 开销，适合大文件。

| 参数 | 位置 | 必填 | 说明 |
|---|---|---|---|
| `name` | query | **是** | 文件名（含扩展名），决定媒体类型识别 |
| `folder` | query | 否 | 目标文件夹 **ID 或名称**；留空 = 根目录 |
| `create_folder` | query | 否 | `1` = 文件夹不存在时自动创建；默认 `0` |
| `relpath` | query | 否 | 相对路径（如 `A/B/C.mp4`）：在目标文件夹下按层级建子文件夹，文件放进最深层 |
| `wait` | query | 否 | `0`（默认）= 服务器收完文件立即返回；`1` = 阻塞到 Telegram 传完 |
| `X-API-Key` | header | 二选一 | 令牌认证 |
| `Authorization` | header | 二选一 | `Bearer <会话令牌>` |

> **默认行为（异步）**：文件写入服务器临时目录后**立刻返回** `queued: true`，Telegram 上传在后台串行队列里继续。调用方不必等大文件传完。

```bash
curl -X PUT "https://teleplay.de5.net/up/api/upload/raw?name=movie.mkv&folder=2" \
  -H "X-API-Key: $K" \
  -H "Content-Type: application/octet-stream" \
  --upload-file /data/movie.mkv
```

**保留文件夹结构**（把本地 `A/B/C.mp4` 传到 `Anime`，结果是 `Anime/A/B/C.mp4`）：

```bash
curl -X PUT "$B/up/api/upload/raw?name=C.mp4&folder=Anime&relpath=A/B/C.mp4" \
  -H "X-API-Key: $K" \
  --data-binary @C.mp4
```

- `relpath` 的最后一段视为文件名，它前面的所有段会按层级创建/复用子文件夹
- 最多 32 层；每段不能含 `\ / : * ? " < > |`，也不能出现 `..`
- 同名子文件夹会复用，不会重复创建
- 不传 `relpath` 时行为不变（文件直接放进 `folder`）

### 3.2 `POST /up/api/upload`（multipart）

| 参数 | 位置 | 必填 | 说明 |
|---|---|---|---|
| `file` | form | **是** | 文件字段 |
| `folder` | query | 否 | 同上 |
| `create_folder` | query | 否 | 同上 |
| `relpath` | query | 否 | 同上（保留文件夹结构） |
| `wait` | query | 否 | 同上，默认 `0` |

```bash
curl -X POST "https://teleplay.de5.net/up/api/upload?folder=2" \
  -H "X-API-Key: $K" \
  -F "file=@/data/movie.mkv"
```

> 注意：`folder` 是 **query 参数**，不是表单字段。

### 3.3 `GET /up/api/folders`

```bash
curl -H "X-API-Key: $K" https://teleplay.de5.net/up/api/folders
```

返回：

```json
{"folders":[{"id":1,"name":"大明王朝","parent_id":null},
            {"id":2,"name":"凡人修仙传","parent_id":null},
            {"id":3,"name":"PDF","parent_id":null},
            {"id":6,"name":"GAME","parent_id":null}]}
```

---

## 4. 返回结构

### 4.1 默认：接收即返回（异步排队）

服务器收完文件后**立即**返回（实测 30 MB 文件约 180 ms）：

```json
{
  "ok": true,
  "queued": true,
  "bytes": 30000000,
  "name": "movie.mkv",
  "folder_id": 2,
  "queue_key": "file:/tmp/tp-uploads/movie.mkv"
}
```

- `queued: true` 表示**已收妥并进入上传队列**，此时文件还没到 Telegram
- 后续可在 `/up` 页面的「上传队列」里看进度
- 失败时文件会保留，出现在「失败待处理」里，可手动重试或删除

### 4.2 `wait=1`：阻塞到 Telegram 传完

与旧行为一致，响应里带完整入库信息：

```json
{
  "ok": true,
  "bytes": 20971520,
  "parts": 1,
  "file": {
    "id": 37,
    "file_id": "BQACAgUAAyEGAAMBBR3MYAADKGr7...",
    "file_unique_id": "AgADsSQAAswh2Vc",
    "channel_message_id": 40,
    "file_name": "foldertest.txt",
    "file_size": 41,
    "mime_type": "text/plain",
    "file_type": "document",
    "duration": null,
    "width": null,
    "height": null,
    "thumbnail_file_id": null
  },
  "files": [ "…同上…" ]
}
```

- `id`：主站数据库的文件 ID，可直接用于 `/api/files/{id}` 等接口
- `parts`：分段数量（未分段的普通文件为 `1`）
- `files`：全部分段的记录数组

### 错误

| 状态码 | 含义 | 处置 |
|---|---|---|
| `401` | 无令牌 / 令牌已删除 / 会话过期 | 重新生成令牌或重新登录 |
| `400` | 缺少 `name`、空请求体、URL 含非法字符 | 检查参数 |
| `404` | `folder` 指定的文件夹不存在 | 补 `create_folder=1` 或改用 ID |
| `413` | 超过大小上限（仅在设置了 `UP_MAX_UPLOAD_BYTES` 时出现） | 见第 5 节 |
| `500` | Telegram 上传失败（网络、频道权限等） | 查看后端日志 |

错误体统一为：

```json
{"detail": "folder 'nosuch' not found; pass create_folder=1 to create it"}
```

---

## 5. 限制与行为

| 项目 | 规则 |
|---|---|
| 接口接收上限 | **默认不限制**（`UP_MAX_UPLOAD_BYTES=0`；设为正数才会在超过时返回 413） |
| Telegram 单文件上限 | **2 GB** — 机器人上传硬限制 |
| **视频超过 2 GB** | **自动分段上传**，见 5.1 |
| **非视频超过 2 GB** | 直接失败（无法分段），返回 500 并附原因 |
| 落地位置 | 先写入容器内 `/tmp/tp-uploads`，上传成功后**自动删除** |
| 磁盘要求 | 上传期间需要一份完整文件的临时空间；能传多大实际取决于服务器剩余空间 |
| 入库时机 | Telegram 上传成功后写入数据库，之后才会出现在主站 |
| 重复上传 | **按内容哈希去重**：哈希已在库中直接返回 `duplicate: true`（可加 `X-Content-SHA256` 头在上传前预检） |
| 并发 | 无显式限制，但 Telegram 客户端串行上传更稳 |
| **断点续传** | **支持**：传一半断开后可从断点继续，见 5.2 |
| 流量路径 | 源站 → Cloudflare 隧道 → 服务器，大文件受跨境链路影响 |

### 5.1 大文件自动分段（视频 > 2 GB）

Telegram 机器人单文件上限是 2 GB，因此**视频**超过该阈值时会自动处理：

```
下载/上传 → 检测到视频 > 2GB
          → ffmpeg 流拷贝分段（不重编码，无损）
          → 每段 < 1.9 GB 且可独立播放
          → 逐段上传 Telegram 并各自入库
          → 全部成功后才删除原文件与分段临时文件
```

| 项目 | 说明 |
|---|---|
| 分段方式 | `ffmpeg -c copy -f segment`，按关键帧切，**不重编码** |
| 每段大小 | 默认目标 1.9 GB（`UP_SEGMENT_LIMIT_BYTES`） |
| 失败重试 | 若某段仍超限，自动缩小分段时长重切（0.8 → 0.6 → 0.45 → 0.32 系数） |
| 命名 | `原名.part001.mkv`、`原名.part002.mkv` … |
| 入库 | **每段是独立的一条记录**，可在主站单独播放 |
| 清理 | 全部上传成功后删除原视频与所有分段 |
| 部分失败 | 已成功的段保留入库，原文件**不会**删除，留待排查 |
| 探测失败 | 无法用 ffprobe 读出时长时**拒绝盲切**，返回错误 |

响应会带上分段信息：

```json
{
  "ok": true,
  "parts": 3,
  "file": { "id": 51, "file_name": "movie.part001.mkv", "...": "..." },
  "files": [ { "id": 51 }, { "id": 52 }, { "id": 53 } ]
}
```

- `parts`：实际分段数量（未分段的普通文件为 `1`）
- `file`：第一段（保持向后兼容）
- `files`：全部分段的记录数组

可调参数（`.env`）：

```bash
UP_TG_LIMIT_BYTES=2097152000     # 触发分段的阈值，默认 2000MB
UP_SEGMENT_LIMIT_BYTES=1992294400 # 分段目标大小，默认 1900MB
```

---

### 5.2 断点续传（大文件推荐）

弱网下大文件常在中途断开。开启续传后，重试只从断点继续，不会从头再来。

**1) 客户端准备一个稳定的会话键**：同一文件每次重试都用同一个键，推荐 `"p" + sha256[:40]`；只允许 `A-Za-z0-9_-`，长度 ≥ 8。

**2) 查询服务器已收到多少字节**

```bash
curl -s "$B/up/api/upload/resume?key=p<key>" -H "X-API-Key: $KEY"
# {"ok": true, "key": "p...", "received": 8388608, "exists": true}
```

**3) 从该偏移继续上传**（`offset` 必须等于 `received`，`size` 是文件总大小）

```bash
# 首次：offset=0
curl -X PUT "$B/up/api/upload/raw?name=file.mp4&key=p<key>&offset=0&size=20971520" \
     -H "X-API-Key: $KEY" -H "X-Content-SHA256: $SHA" --data-binary @first8mb.bin

# 断线后：offset=服务器返回的 received
curl -X PUT "$B/up/api/upload/raw?name=file.mp4&key=p<key>&offset=8388608&size=20971520" \
     -H "X-API-Key: $KEY" -H "X-Content-SHA256: $SHA" --data-binary @remaining.bin
```

返回：

| 情况 | 返回 |
|---|---|
| 还没收全 | `{"ok":true,"partial":true,"received":N,"size":M}` → 继续下一段 |
| 已收全 | `{"ok":true,"queued":true,...}` 或 `{"duplicate":true,...}` |
| 偏移对不上 | `409` + `{"detail":{"received":N}}` → 用 N 重新续 |

服务端把未完成的部分放在 `/tmp/tp-uploads/.resume/<key>.part`，收满后自动转入正常流程（哈希去重 → 入队 → 视频分段 → 上传 Telegram）；超过 24 小时未续传的残片会被清理。

## 6. 完整示例

### 6.1 上传到根目录

```bash
B=https://teleplay.de5.net
K=tp_你的令牌

curl -X PUT "$B/up/api/upload/raw?name=a.mkv" \
  -H "X-API-Key: $K" \
  --upload-file /data/a.mkv
```

### 6.2 上传到指定文件夹（按 ID）

```bash
curl -X PUT "$B/up/api/upload/raw?name=a.mkv&folder=2" \
  -H "X-API-Key: $K" --upload-file /data/a.mkv
```

### 6.3 文件夹不存在时自动创建

```bash
curl -X PUT "$B/up/api/upload/raw?name=a.mkv&folder=新剧集&create_folder=1" \
  -H "X-API-Key: $K" --upload-file /data/a.mkv
```

### 6.4 批量上传整个目录到同一文件夹

```bash
#!/bin/bash
B=https://teleplay.de5.net
K=tp_你的令牌
DIR=/data/videos
FOLDER=2

for f in "$DIR"/*.mp4; do
  [ -f "$f" ] || continue
  n=$(basename "$f")
  code=$(curl -s -o /tmp/resp.json -w '%{http_code}' -X PUT \
    "$B/up/api/upload/raw?name=$n&folder=$FOLDER" \
    -H "X-API-Key: $K" --upload-file "$f")
  if [ "$code" = "200" ]; then
    id=$(python3 -c 'import json;print(json.load(open("/tmp/resp.json"))["file"]["id"])' 2>/dev/null)
    echo "OK   $n -> file id $id"
  else
    echo "FAIL $n ($code) $(head -c 120 /tmp/resp.json)"
  fi
done
```

### 6.5 上传后校验

```bash
# 上传返回里已有 id，可直接查
curl -H "Authorization: Bearer $TOKEN" \
  "$B/api/files/37"
```

---

## 7. 注意事项

1. **中文文件夹名用 ID**。curl 把中文直接拼进 URL 时可能被服务端判为非法请求（返回 400）；先 `GET /up/api/folders` 拿 ID 最稳。
2. **文件名要做 URL 编码**。含空格、`&`、`#`、中文时，用 `--data-urlencode` 不行（query 参数），建议用 `curl --url-query` 或先编码：
   ```bash
   enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "我的电影.mkv")
   curl -X PUT "$B/up/api/upload/raw?name=$enc" ...
   ```
3. **扩展名决定媒体类型**。`.mp4/.mkv` → 视频（有播放按钮），`.mp3` → 音频，图片 → 图片，其他 → 文档。
4. **视频超 2GB 会自动分段**（每段独立入库、可单独播放），非视频超 2GB 仍会失败；**上传总大小已不再有 4 GB 限制**（默认不限制，仅受服务器磁盘余量约束）。
5. **令牌丢失无法找回**（只存哈希），删掉重建即可。
6. **不要在公网暴露令牌**，建议一机一令牌，泄漏只删一个。

---

## 8. 添加下载任务（`POST /up/api/torrent`）

只接受会话认证（`Authorization: Bearer`），不接受 API 令牌。

**请求体**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 可以是：完整 magnet、**40 位 info hash**、http(s) 直链，或**带中文的整段文本** |
| `folder` | 否 | 下载完成后存入的文件夹（名称或 ID），留空 = 设置里的默认 |
| `create_folder` | 否 | `1` = 文件夹不存在就创建 |

**自动识别**：粘贴的文本里包含中文、标题、多行内容都可以，服务端会：

1. 提取文本中**所有** `magnet:?xt=...`（一次可建多个任务）
2. 若没有 magnet，再找 40 位十六进制哈希并转成 magnet
3. 若还没有，再找 `http(s)://` 直链
4. 磁力链接后的中文标点、括号等会自动截断，不会污染链接

```bash
# 裸哈希
curl -X POST https://teleplay.de5.net/up/api/torrent \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"f5702b3f7026bb27052dd1ba2ccfa6a8d874301a","folder":"2"}'

# 带中文的整段文本，含多个链接
curl -X POST https://teleplay.de5.net/up/api/torrent \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"合集 magnet:?xt=urn:btih:aaa...3333 还有 magnet:?xt=urn:btih:111...0000"}'
```

**返回**：

```json
{"ok": true, "count": 2, "gid": "第一个任务的 gid", "gids": ["...", "..."], "folder_id": 2}
```

---

## 9. 与网页上传的关系

三条上传路径最终都走同一个模块 `backend/app/tg_upload.py` 的
`upload_path_to_telegram(path, folder_id, delete_after=True)`：

```
网页拖拽上传   ┐
API multipart  ├──→ tg_upload.upload_path_to_telegram() ──→ Telegram 存储群
API raw 流式   ┘                                        ──→ 数据库入库
                                                        ──→ 删除本地文件
```

行为完全一致：上传成功即入库并删除本地文件；失败则保留错误信息并在响应中返回。