# 言灵工坊论坛推送

监控 `https://flarum.aicue.top/` 的指定标签。`tag_slug` 支持用 `|` 分隔多个中文名称或 Slug，例如 `中转站|注册器` 或 `transit|register-bot`。同一轮发现多个分类的新帖时，插件按分类汇总成一条群消息；每个分类内部按“新帖页面截图 + 该帖 Markdown 标题链接---作者”逐帖配对展示。监控截图缓存在 AstrBot 持久化目录 `data/plugin_data/astrbot_plugin_aicue_forum/screenshots`，供多群推送和失败重试复用，清理时长由 `screenshot_cache_hours` 配置；普通分类、公告、最近和最火榜单每次从持久化目录 `data/plugin_data/astrbot_plugin_aicue_forum/advertisements` 广告图库随机取图，保持一图多帖。同一帖子命中多个标签时按配置顺序归入第一个分类，只推送一次。群推送成功后，使用配置的本人论坛账号回复“已推送至群中”。

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

## 指令

- `/推送本群`：开关当前群推送（管理员）
- `/公告`：发送一张广告图库图片和全部公告的 Markdown 链接
- `/中转站`、`/注册器`、`/破甲词`、`/最新资讯`、`/技术讨论`、`/资源分析`、`/灌水区`：发送该分类最火 10 帖和最近发布 10 帖，两部分各使用一张广告图库图片和多条 `Markdown 标题链接---作者名`
- `/注册机` 是 `/注册器` 的别名，`/资源分享` 是 `/资源分析` 的别名
- `/最近帖子`：先发送一张广告图库图片，再以 `Markdown 标题链接---作者名` 汇总最近三天的新帖
- `/最火帖子`：先发送一张广告图库图片，再以 `Markdown 标题链接---作者名` 汇总所有监控标签的热门帖子
- `/最新帖子`：以图文形式显示官 Q 达到每日限额后尚未自动推送的帖子，每 20 篇合并为一条消息；次日配额恢复后插件自动补发，补发完成即从列表移除
- `/上传图片`：上传广告图（管理员）；支持命令消息直接带图，或先执行命令后在 60 秒内发送图片
- `/删除图片 文件名或序号`：删除指定广告图（管理员）；可用 `/图片统计` 查询文件名和序号
- `/图片统计`：查看广告图库的图片数量、序号和文件名（管理员）
- `/立即检查帖子`：立即执行一次监控（管理员）

插件使用 AstrBot 统一会话与消息链发送，支持 AstrBot 已适配的平台。QQ 官方 API 会在发送前恢复群场景；缺少必要会话缓存时保留任务重试且不会误回帖“已推送至群中”。每日限额只作用于 QQ 官方 API 平台，按帖子计数，不因多个目标群或失败重试重复扣减；其他平台继续正常推送。

## 账号安全

插件通过论坛 `/login` 登录，并向 `/api/posts` 创建回复。用户名和密码保存在 AstrBot 插件配置中；登录 Cookie 与 CSRF Token 只存在于插件运行内存，不写入磁盘。请为论坛使用独立密码，并限制 AstrBot 配置文件的读取权限。

## 其他说明
插件未上传至astrbot商店，需要你自己自己把zip拉进plugin；
AstrBot：https://github.com/AstrBotDevs/AstrBot