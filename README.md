# 明日方舟活动提醒（AstrBot 版）

移植自 AmiyaBot **arknights-activity-remind-2.0**，并在 AstrBot（>= 4.0.0）上运行。
纯后台轮询 + 主动推送，不依赖 LLM。

## 功能

- 自动获取明日方舟官方游戏数据，构建提醒列表：
  - 活动 / 新主题曲：开始、结束、奖励兑换截止
  - 活动节点（活动主题下各阶段：开始 / 开放）
  - 危机合约：赛季开始、结束
  - 卡池：限定、联动、双 UP 等限时卡池开始、结束（不含常驻 / 中坚 / 联合行动等）
  - 保全派驻周期：开始、结束
- **实时提醒**：事件到点立即推送（凌晨 4 点的事件顺延到 10 点）
- **定时提醒**：按配置提前 N 天（0~10）在指定时刻推送，支持三种提醒方式：
  - 普通
  - `@所有人`（需要机器人具备 @全员 权限）
  - 连发三遍
- 群内可自主订阅：开启 / 关闭活动提醒（管理员）

## 安装

1. 在 AstrBot 网页端「插件管理」中上传本插件 zip 安装，或解压到 `data/plugins/` 后重启。
2. 安装后给机器人发送 `活动提醒状态` 确认数据已就绪。
3. 在目标群发送 `开启活动提醒`（需管理员）订阅推送。

## 指令

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `开启活动提醒` | 本群订阅活动提醒 | 管理员（群聊） |
| `关闭活动提醒` | 本群取消订阅 | 管理员（群聊） |
| `活动列表 [类型]` | 查看未来提醒列表，可过滤类型（活动/危机合约/新主题曲/集成战略/生息演算/保全派驻周期/卡池） | 所有人 |
| `刷新活动数据` | 强制刷新游戏数据并重建列表 | 管理员 |
| `活动提醒状态` | 查看插件与数据状态 | 所有人 |

## 配置说明

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `ar_enabled` | 总开关 | true |
| `ar_poll_interval` | 后台检查间隔（秒），建议 60 | 60 |
| `ar_send_time` | 定时提醒时间表（forward 提前天数 / time HH:MM / remind_type 普通、@所有人、连发三遍） | 每天 9:00、19:00，提前 2/1/0 天 |
| `ar_realtime_remind` | 到点实时提醒 | true |
| `ar_send_async` | 同时向所有群推送（true），否则排队（false） | false |
| `ar_send_interval` | 排队推送的群间间隔（秒） | 0.2 |
| `ar_send_gacha_pool` | 是否提醒限时卡池 | true |
| `ar_send_tower_season` | 是否提醒保全派驻周期 | true |
| `ar_data_source` | 数据来源：auto / shared / download | auto |
| `ar_gamedata_base_url` | 自行下载时的主数据源 | GitHub raw（Kengxxiao/ArknightsGameData） |
| `ar_gamedata_mirrors` | 备用数据源（ghproxy / jsdelivr 等） | 见配置 |
| `ar_data_refresh_interval` | 自动刷新数据间隔（秒） | 86400（每天） |
| `ar_request_timeout` | 数据下载超时（秒） | 90 |
| `ar_platform_id` | 推送平台适配器 ID，留空自动探测 | 空 |
| `ar_permission_enabled` / `ar_admin_ids` / `ar_admin_role` | 管理指令权限 | 群主 owner / 管理员 admin |

## 数据来源

插件仅需要 4 张 JSON 表：`activity_table`、`crisis_v2_table`、`gacha_table`、`climb_tower_table`。

- **auto（默认）**：优先复用其它插件已下载的共享 gamedata（如 `siwu-arknights-query`
  存放于 `{astrbot_data}/resource/gamedata/`），无需重复下载；
  若没有则按 `ar_gamedata_base_url` / `ar_gamedata_mirrors` 自行下载所需 JSON 到
  `{astrbot_data}/arknights_activity_remind/excel/`。
- **shared**：仅使用共享 gamedata（需同机安装提供 gamedata 的插件）。
- **download**：始终自行下载。

## 存储位置

- 数据缓存：`{astrbot_data}/arknights_activity_remind/excel/`
- 订阅群列表：`{astrbot_data}/arknights_activity_remind/groups.json`

## 常见问题

- **活动列表为空 / 数据未就绪**：检查服务器是否能访问配置的数据源；使用 `刷新活动数据`
  手动触发；已有 `arknights-query` 时切换 `ar_data_source=shared`。
- **`@所有人` 未生效**：需要机器人账号在群内具备 @全体成员 权限，且平台适配器支持。
- **数据不更新**：`ar_data_refresh_interval` 为 86400（每天刷新一次），也可用 `刷新活动数据` 强制更新。
