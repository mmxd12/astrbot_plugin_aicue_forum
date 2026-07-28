# 言灵工坊论坛推送

监控 `https://flarum.aicue.top/` 的“中转站”（`transit`）标签。新帖推送包含标题、作者、时间、正文摘要、帖子图片和原帖链接；群推送成功后，使用配置的本人论坛账号回复“已推送至群中”。

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

3. 在 AstrBot 管理面板重载插件，或重启 AstrBot。
## 配置

1. 在插件配置中填写 `forum_username` 和 `forum_password`。密码仅用于登录论坛，请勿发送给他人。
2. 重载插件。插件启动时登录 `/login`，Cookie 与 CSRF Token 仅保存在运行内存中；会话失效后自动恢复。分群推送或回帖失败会在后续检查中定向重试。
3. 在目标群执行 `/推送本群` 开启推送；再次执行可关闭。
4. 首次启动会将现有帖子作为基线，不补推旧帖，防止刷屏。

## 指令

- `/推送本群`：开关当前群推送（管理员）
- `/最近帖子`：以 Markdown 标题链接汇总最近三天新发布的帖子
- `/最火帖子`：以 Markdown 标题链接列出“中转站”回复数最高的 20 篇帖子
- `/最新帖子`：以图文形式显示官 Q 达到每日限额后尚未自动推送的帖子，每 20 篇合并为一条消息；次日配额恢复后插件自动补发，补发完成即从列表移除
- `/立即检查帖子`：立即执行一次监控（管理员）

插件使用 AstrBot 统一会话与消息链发送，支持 AstrBot 已适配的平台。每日限额只作用于 QQ 官方 API 平台，按帖子计数，不因多个目标群或失败重试重复扣减；其他平台继续正常推送。

## 账号安全

插件通过论坛 `/login` 登录，并向 `/api/posts` 创建回复。用户名和密码保存在 AstrBot 插件配置中；登录 Cookie 与 CSRF Token 只存在于插件运行内存，不写入磁盘。请为论坛使用独立密码，并限制 AstrBot 配置文件的读取权限。

## 其他说明
插件未上传至astrbot商店，需要你自己自己把zip拉进plugin；
AstrBot：https://github.com/AstrBotDevs/AstrBot

