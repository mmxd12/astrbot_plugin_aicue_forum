# 言灵工坊论坛推送

监控 `https://flarum.aicue.top/` 的指定标签。`tag_slug` 支持用 `|` 分隔多个中文名称或 Slug，例如 `中转站|注册器` 或 `transit|register-bot`。同一轮发现多个分类的新帖时，插件按分类汇总成一条群消息；每个分类内部按“新帖页面截图 + 该帖 Markdown 标题链接---作者”逐帖配对展示。同一帖在同一轮只截一次图，多个群共用同一张公网图片，避免重复启浏览器、重复占用上行带宽；普通分类、公告、最近和最火榜单统一使用配置项 `ad_image_url` 指定的头图（留空则用插件自带的 `assets/forum_header.png`）。同一帖子命中多个标签时按配置顺序归入第一个分类，只推送一次。群推送成功后，使用配置的本人论坛账号回复“已推送至群中”。

## 安装

```text
https://github.com/mmxd12/astrbot_plugin_aicue_forum
```

1. 将本仓库克隆或下载到插件目录：
```text
AstrBot/data/plugins/astrbot_plugin_aicue_forum/
```

桌面端常见路径示例：

```text
core/data/plugins/astrbot_plugin_aicue_forum/
```

2. 确保目录结构如下：

```text
astrbot_plugin_aicue_forum/
├── main.py
├── shotd.py          # 可选，只在独立截图服务器上使用
├── _conf_schema.json
├── requirements.txt
├── metadata.yaml
├── README.md
├── LICENSE
└── __init__.py
```

> 注意：不要多套一层同名目录。

## 配置

1. 在插件配置中填写 `forum_username` 和 `forum_password`。密码仅用于登录论坛，请勿发送给他人。
2. 重载插件。插件启动时登录 `/login`，Cookie 与 CSRF Token 仅保存在运行内存中；会话失效后自动恢复。分群推送或回帖失败会在后续检查中定向重试。
3. 在目标群执行 `/推送本群` 开启推送；再次执行可关闭。插件始终保存真实群 ID（不受群内独立会话影响）；QQ 官方 API 的群场景会持久化，Docker/AstrBot 重启后仍可主动推送。
4. 首次启动会将现有帖子作为基线，不补推旧帖，防止刷屏。

## 截图与图床

推送的帖子配图是浏览器实拍的网页截图，需要 Chromium 内核。截好的图必须能被 QQ 服务器公网访问，所以要有一个对外的文件服务（推荐 Caddy 或 Nginx）。

### 方式一：本机截图（默认）

AstrBot 所在机器自己截图、自己发图，`shot_service_url` 和 `shot_service_token` 留空即可。

1. 安装浏览器内核（在 AstrBot 容器内执行）：

```bash
pip install playwright
python -m playwright install --with-deps chromium
```

2. 配置 `image_host_dir` 为落盘目录（如 `/AstrBot/data/imgs`），`image_host_base` 为该目录的公网地址（如 `http://example.com:8080`，**末尾不带斜杠、开头不要多字符**）。
3. 图片保留天数由 `image_keep_days` 控制，过期自动清理。

这种方式最简单，代价是每张图都由本机上行发给 QQ：一帖推 N 个群，QQ 就会来拉 N 次。出网带宽紧张时可把 `screenshot_quality` 降到 50 左右。

### 方式二：远端截图服务（省带宽）

把浏览器和图床都放到另一台机器上，本机只收一段 JSON，图片一个字节都不经过 AstrBot。适合 AstrBot 出网带宽很小的情况。

远端机器需要：**公网可入站的 IP**、Python 3.9+、Chromium、一个文件服务。AI 沙盒之类只能出网不能入站的环境不适用。

1. 从本仓库下载 `shotd.py` 放到远端机器，例如 `/opt/shotd/shotd.py`：

```bash
pip install aiohttp playwright
python -m playwright install --with-deps chromium
```

2. 用文件服务把图片目录发出去（Caddy 示例）：

```caddyfile
img.example.com {
    root * /var/www/images
    file_server
}
```

3. 启动服务：

```bash
SHOT_TOKEN=一串长随机字符 SHOT_IMAGE_DIR=/var/www/images SHOT_IMAGE_BASE=https://img.example.com SHOT_ALLOW_HOSTS=www.aicue.top,flarum.aicue.top python shotd.py
```

4. 只放行 AstrBot 的 IP，**不要把端口对公网敞开**（服务走明文 HTTP，Token 在链路上可见）：

```bash
ufw allow from <AstrBot机器IP> to any port 8899
```

5. 插件配置填 `shot_service_url`（如 `http://远端IP:8899`）和 `shot_service_token`（与 `SHOT_TOKEN` 一致），保存重载。

可用环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SHOT_TOKEN` | 无，必填 | 调用口令，与插件 `shot_service_token` 一致 |
| `SHOT_IMAGE_DIR` | `/var/www/images` | 图片落盘目录，需与文件服务一致 |
| `SHOT_IMAGE_BASE` | `http://127.0.0.1:8080` | 图片对外地址，末尾不带斜杠 |
| `SHOT_ALLOW_HOSTS` | `www.aicue.top,flarum.aicue.top` | 允许截图的域名白名单，逗号分隔 |
| `SHOT_PORT` | `8899` | 监听端口 |
| `SHOT_QUALITY` | `70` | JPEG 质量 |
| `SHOT_MAX_HEIGHT` | `1400` | 截图最大高度 |
| `SHOT_KEEP_DAYS` | `3` | 图片保留天数 |
| `SHOT_WAIT_SELECTOR` | `.Post-body` | 等待渲染完成的选择器 |
| `SHOT_RESTART_AFTER` | `200` | 截够张数后重启浏览器，防内存增长 |

远端模式下，插件的 `screenshot_quality`、`screenshot_max_height`、`image_host_dir`、`image_host_base`、`image_keep_days` 都不参与，一切由远端环境变量决定。`GET /health` 可查看服务状态。

两种方式的优先级：填了 `shot_service_url` 就走远端，否则走本机截图。

截图失败不会阻塞推送，插件会降级为纯文字（标题 + 链接 + 作者）先发出去。同一帖对同一个群连续失败超过 `push_retry_max` 轮后放弃该目标并记录日志。

## 指令

**看帖（人人可用）**

- `/最近帖子`：最近三天的新帖，头图 + `Markdown 标题链接 --作者`
- `/最火帖子`：所有监控标签内按回复数排序的热门帖
- `/公告`：头图 + 全部公告的 Markdown 链接
- `/中转站`、`/注册器`、`/破甲词`、`/最新资讯`、`/技术讨论`、`/资源分享`、`/灌水区`：该分类最火 10 帖 + 最近 10 帖
- 别名：`/注册机` = `/注册器`，`/资源分析` = `/资源分享`

**推送管理（仅管理员）**

- `/推送本群 [备注]`：开关当前群推送，可顺手起个备注名
- `/推送备注 <序号> <名字>`：给已开启的群补备注
- `/推送状态`：额度用量、各群平台与会话状态、积压帖数、监控是否在跑
- `/立即检查帖子`：不等定时，马上抓一轮新帖
- `/推送帮助`（别名 `/论坛帮助`）：指令一览与当前关键配置

上面的头图取自配置项 `ad_image_url`；留空则使用插件自带的 `assets/forum_header.png`，该文件缺失时只发文字。

插件使用 AstrBot 统一会话与消息链发送，支持 AstrBot 已适配的平台。QQ 官方 API 会在发送前恢复群场景；缺少必要会话缓存时保留任务重试且不会误回帖“已推送至群中”。每日限额只作用于 QQ 官方 API 平台，按帖子计数，不因多个目标群或失败重试重复扣减；其他平台继续正常推送。

## 账号安全

插件通过论坛 `/login` 登录，并向 `/api/posts` 创建回复。用户名和密码保存在 AstrBot 插件配置中；登录 Cookie 与 CSRF Token 只存在于插件运行内存，不写入磁盘。请为论坛使用独立密码，并限制 AstrBot 配置文件的读取权限。

## 其他说明

插件未上传至 AstrBot 商店，需要自己把 zip 拉进 plugins 目录。

`shotd.py` 只在使用方式二时才需要，它跑在**另一台机器**上，不要放进 AstrBot 插件目录执行。

AstrBot：https://github.com/AstrBotDevs/AstrBot