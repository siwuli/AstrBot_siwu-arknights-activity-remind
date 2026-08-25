"""明日方舟活动提醒插件（arknights_activity_remind）。

移植自 AmiyaBot arknights-activity-remind-2.0：
- 后台每分钟检查：活动/主题曲/危机合约/活动节点/卡池/保全派驻周期的开始、结束、
  节点开放、奖励兑换截止时间；
- 实时提醒：事件到点立即推送（凌晨 4 点的事件顺延到 10 点）；
- 定时提醒：按 ar_send_time 配置提前 N 天在指定时刻推送（支持普通/@所有人/连发三遍）；
- 群聊通过「开启活动提醒 / 关闭活动提醒」自主订阅推送，管理员权限。

数据来源：优先复用其它插件（如 arknights-query）已下载的共享 gamedata，
否则按配置自行下载所需 JSON 表。免 LLM，纯后台轮询 + 主动推送。
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from astrbot.api import star
from astrbot.api.all import AstrBotConfig, AstrMessageEvent, MessageChain
from astrbot.api.event import filter
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .gamedata import CACHE_JSON_DIR, SHARED_JSON_DIR, RemindData

logger = logging.getLogger("astrbot")

DATA_DIR = os.path.join(get_astrbot_data_path(), "arknights_activity_remind")
GROUPS_FILE = os.path.join(DATA_DIR, "groups.json")

DEFAULT_POLL_INTERVAL = 60
MIN_POLL_INTERVAL = 30
DEFAULT_REFRESH_INTERVAL = 86400
MAX_LIST_ITEMS = 50


def _norm_list(value) -> list[str]:
    """把配置项归一化为字符串列表（兼容 list 与换行/逗号分隔的字符串）。"""
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).replace(",", "\n").replace("，", "\n").split("\n")
    return [str(x).strip() for x in items if str(x).strip()]


def _norm_send_time(value) -> list[dict]:
    """把 ar_send_time 配置归一化为 [{forward, time, remind_type}]。"""
    out = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        try:
            forward = max(0, min(10, int(item.get("forward", 0) or 0)))
        except (TypeError, ValueError):
            continue
        time_str = str(item.get("time") or "").strip()
        if not time_str:
            continue
        remind_type = str(item.get("remind_type") or item.get("remindType") or "普通").strip()
        if remind_type not in ("普通", "@所有人", "连发三遍"):
            remind_type = "普通"
        out.append({"forward": forward, "time": time_str, "remind_type": remind_type})
    return out


class ArknightsActivityRemindPlugin(star.Star):
    """明日方舟活动提醒插件。"""

    def __init__(self, context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config = config or {}
        self._data = RemindData()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._data_lock = asyncio.Lock()
        self._groups: dict[str, list[str]] = {}  # platform_id -> [group_id]
        self._platform_ids: list[str] = []
        self._last_refresh: datetime | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        self._load_groups()
        self._platform_ids = self._detect_platform_ids()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._remind_loop())
        # 首次启动立即尝试准备数据（失败不阻塞，循环内会重试）
        asyncio.create_task(self._refresh_data(force=False))
        logger.info(
            "明日方舟活动提醒已启动：已订阅群 %d 个",
            sum(len(gids) for gids in self._groups.values()),
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                logger.debug("活动提醒轮询任务已随插件停止")
            self._task = None

    # ------------------------------------------------------------------
    # 后台轮询
    # ------------------------------------------------------------------
    async def _remind_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("活动提醒 tick 异常: %s", e)
            interval = int(self.config.get("ar_poll_interval", DEFAULT_POLL_INTERVAL) or DEFAULT_POLL_INTERVAL)
            await asyncio.sleep(max(MIN_POLL_INTERVAL, interval))

    async def _tick(self) -> None:
        """每分钟检查一次：数据就绪/定时刷新 + 实时/定时提醒发送。"""
        if not bool(self.config.get("ar_enabled", True)):
            return
        now = datetime.now().replace(second=0, microsecond=0)

        if not self._data.ready:
            await self._refresh_data(force=False)
            if not self._data.ready:
                return
        else:
            refresh_interval = int(
                self.config.get("ar_data_refresh_interval", DEFAULT_REFRESH_INTERVAL)
                or DEFAULT_REFRESH_INTERVAL
            )
            if self._last_refresh and (now - self._last_refresh) >= timedelta(seconds=refresh_interval):
                await self._refresh_data(force=False)

        groups = self._enabled_groups()
        if not groups:
            return

        send_async = bool(self.config.get("ar_send_async", False))
        send_interval = float(self.config.get("ar_send_interval", 0.2) or 0.2)
        send_gacha = bool(self.config.get("ar_send_gacha_pool", True))
        send_tower = bool(self.config.get("ar_send_tower_season", True))
        send_realtime = bool(self.config.get("ar_realtime_remind", True))
        send_time_configs = _norm_send_time(self.config.get("ar_send_time"))

        realtime_content = ""
        scheduled_groups: dict[str, str] = {}

        for remind in self._data.remind_list:
            if not send_gacha and remind.get("type") == "卡池":
                continue
            if not send_tower and remind.get("type") == "保全派驻周期":
                continue

            remind_time = datetime.fromtimestamp(remind["timestamp"])

            # 实时提醒：到点（凌晨 4 点顺延到 10 点）
            if send_realtime:
                adjusted = (
                    remind_time.replace(hour=10, minute=0)
                    if remind_time.hour == 4
                    else remind_time
                )
                if adjusted == now:
                    node_info = f" {remind['node']}" if remind.get("node") else ""
                    realtime_content += f"{remind['type']} <{remind['name']}>{node_info} {remind['remind_type']}\n"

            # 定时提醒：按配置分组
            current_time = now.time()
            for item_config in send_time_configs:
                try:
                    cfg_time = datetime.strptime(item_config["time"], "%H:%M").time()
                except (ValueError, TypeError):
                    continue
                if current_time != cfg_time:
                    continue
                time_diff = remind_time - now
                if remind_time >= now and time_diff.days == item_config["forward"]:
                    if item_config["forward"] == 0:
                        hours_ahead = time_diff.seconds // 3600
                        how_long = f"{hours_ahead}小时后" if hours_ahead > 0 else f"{time_diff.seconds // 60}分钟后"
                    else:
                        how_long = f"{item_config['forward']}天后"
                    node_info = f" {remind['node']}" if remind.get("node") else ""
                    remind_text = f"{remind['type']} <{remind['name']}>{node_info} 将于{how_long}{remind['remind_type']}\n"
                    remind_type = item_config["remind_type"]
                    scheduled_groups[remind_type] = scheduled_groups.get(remind_type, "") + remind_text

        # 发送实时提醒
        if realtime_content:
            logger.info("发送活动实时提醒，目标群数: %d", len(groups))
            await self._push_to_groups(realtime_content, groups, send_async=send_async, send_interval=send_interval)

        # 按配置分组发送定时提醒
        for remind_type, content in scheduled_groups.items():
            if not content:
                continue
            logger.info("发送活动定时提醒（%s），目标群数: %d", remind_type, len(groups))
            await self._push_to_groups(
                content,
                groups,
                remind_type=remind_type,
                send_async=send_async,
                send_interval=send_interval,
            )

    # ------------------------------------------------------------------
    # 数据准备/刷新
    # ------------------------------------------------------------------
    async def _refresh_data(self, force: bool) -> bool:
        """准备或刷新提醒数据。返回是否成功。"""
        async with self._data_lock:
            source = str(self.config.get("ar_data_source", "auto") or "auto").strip().lower()
            timeout = float(self.config.get("ar_request_timeout", 90) or 90)
            urls = _norm_list(self.config.get("ar_gamedata_mirrors"))
            base_url = str(self.config.get("ar_gamedata_base_url", "") or "").strip()
            if base_url:
                urls = [base_url] + urls

            shared_ok = self._data._dir_has_all_tables(SHARED_JSON_DIR)
            cache_ok = self._data._dir_has_all_tables(CACHE_JSON_DIR)

            if source == "shared":
                if not shared_ok:
                    self._data.last_error = "共享 gamedata 不可用：请先安装并配置 arknights-query 等提供 gamedata 的插件，或改用 auto/download 数据源。"
                    logger.warning("%s", self._data.last_error)
                    return False
            elif source == "download":
                if force or not cache_ok:
                    if not await self._data.download_tables(urls, timeout):
                        logger.warning("活动提醒：自行下载游戏数据失败")
                        return False
            else:  # auto
                if not (shared_ok or cache_ok):
                    if not await self._data.download_tables(urls, timeout):
                        logger.warning("活动提醒：无本地数据且下载失败")
                        return False

            if not self._data.has_local_data():
                self._data.last_error = "游戏数据不完整（缺少所需 JSON 表）。"
                return False

            try:
                self._data.build_remind_list()
            except Exception as e:  # noqa: BLE001
                logger.exception("构建活动提醒列表失败: %s", e)
                self._data.last_error = f"构建提醒列表失败: {e}"
                return False
            self._last_refresh = datetime.now()
            return True

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------
    def _enabled_groups(self) -> list[tuple[str, str]]:
        """返回所有已开启提醒的 (platform_id, group_id) 列表。"""
        out = []
        for pid, gids in self._groups.items():
            for gid in gids:
                if str(gid).strip().isdigit():
                    out.append((pid, str(gid).strip()))
        return out

    async def _push_to_groups(
        self,
        content: str,
        groups: list[tuple[str, str]],
        remind_type: str = "普通",
        send_async: bool = False,
        send_interval: float = 0.2,
    ) -> int:
        """向目标群推送文本。remind_type 支持 普通 / @所有人 / 连发三遍。返回成功发送的群数。"""
        candidates = self._push_platform_candidates()
        chain = MessageChain().message(content)
        if remind_type == "@所有人":
            chain.at_all()
        send_count = 3 if remind_type == "连发三遍" else 1

        tasks = []
        sent = 0
        for pid, gid in groups:
            session = f"{pid}:{MessageType.GROUP_MESSAGE.value}:{gid}"
            for i in range(send_count):
                if send_async:
                    tasks.append(asyncio.create_task(self._send_one(session, chain, pid, gid, i)))
                    continue
                try:
                    ok = await self.context.send_message(session, chain)
                    if ok:
                        sent += 1
                except Exception as e:  # noqa: BLE001
                    logger.error("推送群 %s（平台 %s）失败: %s", gid, pid, e)
                if not (i == send_count - 1):
                    await asyncio.sleep(send_interval)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sent += sum(1 for r in results if r is True)
        return sent

    async def _send_one(self, session: str, chain: MessageChain, pid: str, gid: str, i: int) -> bool:
        try:
            return bool(await self.context.send_message(session, chain))
        except Exception as e:  # noqa: BLE001
            logger.error("推送群 %s（平台 %s）失败: %s", gid, pid, e)
            return False

    def _push_platform_candidates(self) -> list[str]:
        """推送时尝试的平台 ID 列表：优先配置值，其次自动探测。"""
        configured = str(self.config.get("ar_platform_id", "") or "").strip()
        detected = self._platform_ids or self._detect_platform_ids()
        if detected and not self._platform_ids:
            self._platform_ids = detected
        candidates: list[str] = []
        for pid in ([configured] if configured else []) + detected:
            if pid and pid not in candidates:
                candidates.append(pid)
        if not candidates:
            candidates = ["aiocqhttp"]
        return candidates

    def _detect_platform_ids(self) -> list[str]:
        """自动探测当前运行中的平台适配器 ID，用于主动推送。"""
        ids: list[str] = []
        pm = getattr(self.context, "platform_manager", None)
        insts = getattr(pm, "platform_insts", None) if pm else None
        if not insts and pm is not None:
            getter = getattr(pm, "get_insts", None)
            if callable(getter):
                try:
                    insts = getter()
                except Exception as e:  # noqa: BLE001
                    logger.debug("获取平台实例列表失败: %s", e)
        for p in insts or []:
            try:
                meta = p.meta()
                pid = str(getattr(meta, "id", "") or "")
                if pid and pid not in ids:
                    ids.append(pid)
            except Exception as e:  # noqa: BLE001
                logger.debug("探测平台 ID 失败: %s", e)
        return ids

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("开启活动提醒")
    async def enable_remind(self, event: AstrMessageEvent):
        """群内开启活动提醒（管理员）。"""
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            event.stop_event()
            yield event.make_result().message("抱歉，活动提醒只能在本机器人所在群里开启。")
            return
        pid = event.get_platform_id() or ""
        gid = str(event.get_group_id() or "").strip()
        if not pid or not gid.isdigit():
            event.stop_event()
            yield event.make_result().message("无法识别当前群信息，请稍后再试。")
            return
        gids = self._groups.setdefault(pid, [])
        if gid not in gids:
            gids.append(gid)
            self._save_groups()
            logger.info("群 %s（平台 %s）已开启活动提醒", gid, pid)
        event.stop_event()
        yield event.make_result().message("已在本群开启活动提醒。")

    @filter.command("关闭活动提醒")
    async def disable_remind(self, event: AstrMessageEvent):
        """群内关闭活动提醒（管理员）。"""
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            event.stop_event()
            yield event.make_result().message("抱歉，活动提醒只能在本机器人所在群里关闭。")
            return
        pid = event.get_platform_id() or ""
        gid = str(event.get_group_id() or "").strip()
        gids = self._groups.get(pid, [])
        if gid in gids:
            gids.remove(gid)
            if not gids:
                self._groups.pop(pid, None)
            self._save_groups()
            logger.info("群 %s（平台 %s）已关闭活动提醒", gid, pid)
        event.stop_event()
        yield event.make_result().message("已在本群关闭活动提醒。")

    @filter.command("活动列表")
    async def remind_list(self, event: AstrMessageEvent):
        """查看活动提醒列表，可选类型过滤：活动列表 [类型]。"""
        filter_type = None
        parts = (event.get_message_str() or "").split()
        if len(parts) > 1:
            filter_type = parts[1].strip()

        items = self._data.remind_list
        if filter_type:
            items = [d for d in items if d.get("type") == filter_type]
        if not items:
            event.stop_event()
            yield event.make_result().message(
                "当前没有符合条件的活动提醒。"
                if filter_type
                else "当前没有活动提醒（可尝试「刷新活动数据」更新数据）。"
            )
            return

        send_gacha = bool(self.config.get("ar_send_gacha_pool", True))
        send_tower = bool(self.config.get("ar_send_tower_season", True))
        lines = []
        for remind in items[:MAX_LIST_ITEMS]:
            if not send_gacha and remind.get("type") == "卡池":
                continue
            if not send_tower and remind.get("type") == "保全派驻周期":
                continue
            node_info = f" {remind['node']}" if remind.get("node") else ""
            lines.append(f"{remind['type']} <{remind['name']}>{node_info} 将于\n{remind['time_str']} {remind['remind_type']}\n")
        if not lines:
            event.stop_event()
            yield event.make_result().message("当前没有符合条件（含配置开关）的活动提醒。")
            return
        tail = f"\n（仅显示前 {MAX_LIST_ITEMS} 条）" if len(items) > MAX_LIST_ITEMS else ""
        event.stop_event()
        yield event.make_result().message("\n".join(lines) + tail)

    @filter.command("刷新活动数据")
    async def refresh_data(self, event: AstrMessageEvent):
        """强制刷新游戏数据并重建提醒列表（管理员）。"""
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        event.stop_event()
        ok = await self._refresh_data(force=True)
        if ok:
            yield event.make_result().message(
                f"活动数据已刷新：共 {len(self._data.remind_list)} 条提醒（构建于 {self._data.data_time}）。"
            )
        else:
            yield event.make_result().message(
                "活动数据刷新失败。" + (f"\n原因：{self._data.last_error}" if self._data.last_error else "")
            )

    @filter.command("活动提醒状态")
    async def remind_status(self, event: AstrMessageEvent):
        """查看插件运行状态。"""
        groups = self._enabled_groups()
        source = str(self.config.get("ar_data_source", "auto") or "auto")
        if not self._data.ready:
            data_text = "尚未就绪" + (f"（{self._data.last_error}）" if self._data.last_error else "")
        else:
            data_text = f"已就绪（{self._data.data_time}，共 {len(self._data.remind_list)} 条提醒）"
        lines = [
            "明日方舟活动提醒状态：",
            f"- 总开关：{'开启' if bool(self.config.get('ar_enabled', True)) else '关闭'}",
            f"- 数据来源：{source}",
            f"- 游戏数据：{data_text}",
            f"- 检查间隔：{self.config.get('ar_poll_interval', 60)} 秒",
            f"- 已开启提醒的群：{len(groups)} 个（{', '.join(gid for _, gid in groups[:8]) or '未开启'}）",
            f"- 推送平台：{', '.join(self._push_platform_candidates()) or '（未探测到）'}",
        ]
        if self._data.remind_list:
            upcoming = self._data.remind_list[:3]
            lines.append("- 最近提醒：")
            for r in upcoming:
                node_info = f" {r['node']}" if r.get("node") else ""
                lines.append(f"  {r['type']} <{r['name']}>{node_info} {r['time_str']} {r['remind_type']}")
        event.stop_event()
        yield event.make_result().message("\n".join(lines))

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------
    @staticmethod
    def _sender_field(event: AstrMessageEvent, field: str, default="") -> str:
        """兼容 dict/对象两种 sender 结构读取字段（aiocqhttp 的 sender 是 dict）。"""
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        if isinstance(sender, dict):
            return str(sender.get(field) or default or "")
        value = getattr(sender, field, None)
        if value is None and sender is not None and callable(getattr(sender, "get", None)):
            value = sender.get(field)
        return str(value or default or "")

    def _check_manage_permission(self, event: AstrMessageEvent) -> str | None:
        """管理指令权限校验。返回 None 表示放行，否则返回拒绝提示。"""
        if not bool(self.config.get("ar_permission_enabled", True)):
            return None
        sender_id = self._sender_field(event, "user_id").strip()
        admin_ids = _norm_list(self.config.get("ar_admin_ids"))
        if sender_id and sender_id in admin_ids:
            return None
        role = self._sender_field(event, "role").lower()
        admin_roles = [
            x.lower()
            for x in _norm_list(self.config.get("ar_admin_role", ["owner", "admin"]))
        ]
        if role and role in admin_roles:
            return None
        return "该操作需要管理权限。请联系管理员在插件配置（ar_admin_ids / ar_admin_role）中添加你的权限。"

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _load_groups(self) -> None:
        try:
            if os.path.exists(GROUPS_FILE):
                with open(GROUPS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                self._groups = {
                    str(k): [str(g) for g in (v or []) if str(g).strip().isdigit()]
                    for k, v in (data or {}).items()
                    if isinstance(v, list)
                }
        except Exception as e:  # noqa: BLE001
            logger.error("读取活动提醒订阅群列表失败: %s", e)

    def _save_groups(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = GROUPS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._groups, f, ensure_ascii=False, indent=2)
            os.replace(tmp, GROUPS_FILE)
        except Exception as e:  # noqa: BLE001
            logger.error("保存活动提醒订阅群列表失败: %s", e)
