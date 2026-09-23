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
import re
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


# ----------------------------------------------------------------------
# 多平台兼容
# ----------------------------------------------------------------------
# 各适配器的「群」会话标识形态并不统一，不能假定为纯数字：
#   - OneBot v11 / aiocqhttp：纯数字 QQ 群号，如 602519154
#   - QQ 官方机器人        ：十六进制 group_openid，如 8FF0A5DE2922DE19925E1BE9EF3D58A2
#   - Telegram            ：负数 chat_id，如 -1001234567890
#   - Discord             ：雪花 ID
# 会话键由 "平台ID:消息类型:群ID" 拼接，因此群 ID 只要求非空且不含分隔符。
_GROUP_ID_RE = re.compile(r"^[^:\s]{1,128}$")


def _is_valid_group_id(gid) -> bool:
    """平台无关的群标识校验：非空且不含会话分隔符 ':'。"""
    return bool(_GROUP_ID_RE.match(str(gid or "").strip()))


def _short_id(value, keep: int = 8) -> str:
    """长 ID 显示时截断（QQ 官方 group_openid 有 32 位，直接打印会刷屏）。"""
    text = str(value or "").strip()
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}…{text[-4:]}"


def _parse_target(raw) -> tuple[str, str]:
    """解析推送目标为 (platform_id, group_id)，便于多框架混用与换平台。

    支持两种写法：
      - "602519154"                    → 平台留空，推送时自动探测
      - "qq_official_bot:8FF0A5DE..."  → 指定平台，精确投递
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if ":" in text:
        pid, _, gid = text.partition(":")
        pid, gid = pid.strip(), gid.strip()
        if pid and _is_valid_group_id(gid):
            return pid, gid
    return "", text


# ----------------------------------------------------------------------
# 跨平台群号解析
# ----------------------------------------------------------------------
# platform_alias 插件会把 QQ 官方机器人的 group_openid 透明翻译成真实群号，并把
# 「群号 -> 该群存在于哪些平台」写进 aliases.json。这里直接读那个文件（不 import
# 对方插件，避免加载顺序耦合），于是换平台之后，老配置里的群号依然可用。
_ALIAS_FILE = os.path.join(get_astrbot_data_path(), "platform_alias", "aliases.json")
_ALIAS_CACHE: dict = {"mtime": None, "data": None}


def _alias_group_platforms(group_id) -> list[str]:
    """群号 -> 已知存在该群的平台适配器 ID 列表；没有映射表时返回 []。"""
    gid = str(group_id or "").strip()
    if not gid:
        return []
    try:
        st = os.stat(_ALIAS_FILE)
    except OSError:
        return []
    # 时间戳 + 文件大小一起做键：某些文件系统的时间戳粒度较粗，只看 mtime 会漏掉改动
    stamp = (st.st_mtime_ns, st.st_size)
    if _ALIAS_CACHE["mtime"] != stamp or _ALIAS_CACHE["data"] is None:
        try:
            with open(_ALIAS_FILE, "r", encoding="utf-8") as f:
                _ALIAS_CACHE["data"] = json.load(f)
            _ALIAS_CACHE["mtime"] = stamp
        except Exception as e:  # noqa: BLE001
            logger.debug("读取平台映射表失败（不影响推送）: %s", e)
            return []
    rec = ((_ALIAS_CACHE["data"] or {}).get("groups") or {}).get(gid)
    if not isinstance(rec, dict):
        return []
    return [str(p).strip() for p in (rec.get("native") or {}) if str(p).strip()]


def _resolve_group_targets(group_id, recorded_pid: str, live_pids) -> list[str]:
    """决定某个订阅群号该投递给哪些平台，返回平台 ID 列表。

    0) 探测不到任何平台 -> 原样返回（启动早期平台还没注册，不做任何猜测）；
    1) 映射表登记了该群在哪些平台 -> 投这些里正在运行的（换平台免重订阅）；
    2) 否则原平台还在跑 -> 用原平台（保持旧行为）；
    3) 都不满足 -> 返回空，交给「清理」指令提示处理。

    刻意不做「只剩一个平台就盲投」的推断：群号在不同平台未必存在，
    盲投只会把消息发错群或刷一堆 API 报错。要迁移就在新平台执行一次
    「群绑定」，映射表登记后 1) 会自动接管。
    """
    live = [str(p) for p in (live_pids or []) if str(p)]
    if not live:
        return [str(recorded_pid)] if str(recorded_pid) else []
    known = [p for p in _alias_group_platforms(group_id) if p in live]
    if known:
        return known
    if str(recorded_pid) in live:
        return [str(recorded_pid)]
    return []


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
    def _live_platform_ids(self) -> list[str]:
        """当前运行中的平台适配器 ID。

        平台适配器可能晚于插件加载完成，因此这里惰性重探测并刷新缓存，
        换平台后无需重启插件即可识别到新平台。
        """
        detected = self._detect_platform_ids()
        if detected:
            self._platform_ids = detected
        return list(self._platform_ids)

    def _enabled_groups(self) -> list[tuple[str, str]]:
        """返回所有已开启提醒的 (platform_id, group_id)。

        群 ID 不再限定为纯数字，兼容 QQ 官方 group_openid、Telegram 负数
        chat_id 等形态。若 platform_alias 登记了「该群号存在于哪个平台」就以它
        为准；原平台失效而只剩一个平台在跑时自动改投，换平台后无需重新订阅。
        """
        live = self._live_platform_ids()
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pid, gids in self._groups.items():
            for gid in gids:
                gid = str(gid).strip()
                if not _is_valid_group_id(gid):
                    continue
                for target in _resolve_group_targets(gid, pid, live):
                    if (target, gid) not in seen:
                        seen.add((target, gid))
                        out.append((target, gid))
        return out

    def _stale_groups(self) -> list[tuple[str, list[str]]]:
        """返回确实无处可投的订阅 [(platform_id, [group_id, ...])]，用于提示与清理。"""
        live = self._live_platform_ids()
        if not live:
            return []
        out: list[tuple[str, list[str]]] = []
        for pid, gids in self._groups.items():
            if pid in live:
                continue
            remaining = [
                str(g).strip()
                for g in gids
                if _is_valid_group_id(g)
                and not _resolve_group_targets(str(g).strip(), pid, live)
            ]
            if remaining:
                out.append((pid, remaining))
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
        """可用于推送的平台 ID 列表：配置值优先，其次当前运行中的平台。

        不再硬编码回退到 "aiocqhttp"，以便在任意适配器（含 QQ 官方机器人）
        上直接工作。
        """
        configured = str(self.config.get("ar_platform_id", "") or "").strip()
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        for pid in self._live_platform_ids():
            if pid not in candidates:
                candidates.append(pid)
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
        if not pid or not _is_valid_group_id(gid):
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
            f"- 推送平台：{', '.join(self._push_platform_candidates()) or '（未探测到）'}",
            f"- 已开启提醒的群：{len(groups)} 个"
            f"（{', '.join(_short_id(gid) for _, gid in groups[:8]) or '未开启'}）",
        ]
        stale = self._stale_groups()
        if stale:
            detail = "、".join(f"{pid}（{len(gids)} 个群）" for pid, gids in stale)
            lines.append(f"- 失效平台订阅：{detail}，可执行「活动提醒清理」移除")
        if self._data.remind_list:
            upcoming = self._data.remind_list[:3]
            lines.append("- 最近提醒：")
            for r in upcoming:
                node_info = f" {r['node']}" if r.get("node") else ""
                lines.append(f"  {r['type']} <{r['name']}>{node_info} {r['time_str']} {r['remind_type']}")
        event.stop_event()
        yield event.make_result().message("\n".join(lines))

    @filter.command("活动提醒清理")
    async def purge_stale_remind(self, event: AstrMessageEvent):
        """清理平台已不存在的订阅（换平台后遗留的旧平台记录）。"""
        denied = self._check_manage_permission(event)
        if denied:
            event.stop_event()
            yield event.make_result().message(denied)
            return
        live = self._live_platform_ids()
        if not live:
            event.stop_event()
            yield event.make_result().message("未探测到运行中的平台适配器，暂不清理。")
            return
        removed = []
        for pid in list(self._groups.keys()):
            if pid not in live:
                removed.append((pid, self._groups.pop(pid)))
        if not removed:
            event.stop_event()
            yield event.make_result().message("没有需要清理的订阅。")
            return
        self._save_groups()
        lines = ["已清理以下失效平台的订阅（可在新平台重新执行「开启活动提醒」）："]
        for pid, gids in removed:
            lines.append(f"- {pid}：{len(gids)} 个群")
        event.stop_event()
        yield event.make_result().message("\n".join(lines))

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------
    @staticmethod
    def _sender_field(event: AstrMessageEvent, field: str, default="") -> str:
        """兼容多种 sender 结构读取字段，返回字符串。"""
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        value = None
        if isinstance(sender, dict):
            value = sender.get(field)
        else:
            value = getattr(sender, field, None)
            if value is None and sender is not None and callable(getattr(sender, "get", None)):
                value = sender.get(field)
        if value in (None, ""):
            # aiocqhttp：message_obj.sender 是 MessageMember（无 role 字段），
            # 原始 OneBot 事件在 raw_message 中，sender 为 dict
            raw = getattr(message_obj, "raw_message", None)
            if raw is not None:
                raw_sender = raw.get("sender") if isinstance(raw, dict) else getattr(raw, "sender", None)
                if isinstance(raw_sender, dict):
                    raw_value = raw_sender.get(field)
                    if raw_value not in (None, ""):
                        return str(raw_value)
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
                    str(k): [str(g).strip() for g in (v or []) if _is_valid_group_id(g)]
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
