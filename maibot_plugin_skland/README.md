# MaiBot Plugin - 森空岛签到

基于 [astrbot_plugin_skland](https://github.com/Azincc/astrbot_plugin_skland) 的协议实现，适配 MaiBot 插件 SDK 的森空岛自动签到插件。

支持：

- 明日方舟签到
- 终末地签到
- 定时自动签到 + 私聊结果推送
- 群内绑定用户状态展示

## 安装

1. 将本目录复制到 MaiBot 的 `plugins/` 下，例如：

```bash
cp -r maibot_plugin_skland /path/to/MaiBot/plugins/maibot_plugin_skland
```

2. 依赖会在插件加载时按 `_manifest.json` 自动安装：

- `httpx>=0.25.0`
- `pycryptodome>=3.19.0`

3. 重启 MaiBot，或在 WebUI 中加载插件。

## 命令

| 命令 | 场景 | 说明 |
|------|------|------|
| `/skdhelp` | 全部 | 查看帮助 |
| `/skdlogin <token>` | 仅私聊 | 登录并立即签到 |
| `/skdlogout` | 仅私聊 | 登出并删除 token |
| `/skd` | 私聊 | 查看/执行自己的签到 |
| `/skd` | 群聊 | 展示本群绑定用户签到状态 |
| `/skdusers` | 全部 | 查看绑定人数与名额 |

## 获取 Token

1. 登录 [鹰角网络通行证](https://user.hypergryph.com/)
2. 打开：https://web-api.hypergryph.com/account/info/hg  
   或登录森空岛后打开：https://web-api.skland.com/account/info/hg
3. 复制返回 JSON 中 `content` 字段的值
4. **私聊**机器人发送：

```text
/skdlogin <content>
```

> 不要在群聊发送 token。

## 配置

WebUI 可改，或编辑插件目录下的 `config.toml`：

```toml
[plugin]
config_version = "1.0.0"
enabled = true

[sign]
auto_sign_enabled = true
auto_sign_hour = 9
auto_sign_minute = 0
auto_sign_delay = 10
max_users = 10
show_player_name = true
notify_on_auto_sign = true
```

## 原理简述

```text
用户 token
  -> 生成设备指纹 dId
  -> OAuth grant 换 code
  -> generate_cred_by_code 换 cred/token
  -> 签名请求查询绑定角色
  -> 明日方舟 / 终末地 attendance 签到
```

用户绑定数据保存在：

```text
data/plugins/com.azincc.skland/users.json
data/plugins/com.azincc.skland/groups.json
```

## 许可

MIT（协议实现来源于原 AstrBot 插件与社区 Rust 自动签到实现）
