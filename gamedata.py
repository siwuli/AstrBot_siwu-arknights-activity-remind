# -*- coding: utf-8 -*-
"""数据层：明日方舟活动提醒所需的游戏数据（移植自 AmiyaBot arknights-activity-remind-2_0）。

仅需 4 张 excel JSON 表：activity_table / crisis_v2_table / gacha_table / climb_tower_table。
数据来源两种：
1. 共享 gamedata：其它插件（如 siwu-arknights-query）已下载到
   {astrbot_data}/resource/gamedata/gamedata/excel/ 的数据，直接复用、不重复下载；
2. 自行下载：从 ar_gamedata_base_url / ar_gamedata_mirrors 按需下载这 4 张表到
   {astrbot_data}/arknights_activity_remind/excel/。
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime

import aiohttp

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

# 构建提醒列表所需的 JSON 表（相对 excel 目录，不含扩展名）
REQUIRED_TABLES = (
    "activity_table",
    "crisis_v2_table",
    "gacha_table",
    "climb_tower_table",
)

# 其它插件下载的共享 gamedata 目录（siwu-arknights-query 等）
SHARED_JSON_DIR = os.path.join(
    get_astrbot_data_path(), "resource", "gamedata", "gamedata", "excel"
)

# 本插件自己的数据缓存目录
DATA_DIR = os.path.join(get_astrbot_data_path(), "arknights_activity_remind")
CACHE_JSON_DIR = os.path.join(DATA_DIR, "excel")

# 活动类型中英文映射（与 AmiyaBot 一致）
ACT_TYPE_MAP = {
    "ACTIVITY": "活动",
    "1": "活动",
    "CRISIS": "危机合约",
    "2": "危机合约",
    "5": "危机合约",
    "MAINLINE": "新主题曲",
    "3": "新主题曲",
    "7": "新主题曲",
    "ROGUELIKE": "集成战略",
    "4": "集成战略",
    "SANDBOX": "生息演算",
    "6": "生息演算",
}

# 忽略的卡池类型：常驻标准寻访 / 中坚寻访 / 中坚甄选 / 中坚选调 / 前路回响等
IGNORE_POOL_LIST = {"NORMAL", "0", "CLASSIC", "4", "FESCLASSIC", "6", "CLASSIC_DOUBLE", "10"}


class RemindData:
    """活动提醒数据：读取/下载 JSON 表并构建提醒列表。"""

    def __init__(self) -> None:
        self.remind_list: list[dict] = []
        self.ready = False
        self.data_time = ""  # 数据构建时间（%Y-%m-%d %H:%M:%S）
        self.last_error = ""
        self._cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 表读取
    # ------------------------------------------------------------------
    def _read_table_from(self, name: str, base_dir: str) -> dict:
        """从指定目录读取一张表，失败返回空 dict。"""
        path = os.path.join(base_dir, f"{name}.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 %s 失败: %s", path, e)
            return {}

    def read_table(self, name: str) -> dict:
        """优先读本插件缓存，其次读共享 gamedata。"""
        if name in self._cache:
            return self._cache[name]
        table = self._read_table_from(name, CACHE_JSON_DIR) or self._read_table_from(
            name, SHARED_JSON_DIR
        )
        self._cache[name] = table
        return table

    def clear_cache(self, name: str = None) -> None:
        """清空表缓存（name 为空时清空全部）。"""
        if name:
            self._cache.pop(name, None)
        else:
            self._cache = {}

    # ------------------------------------------------------------------
    # 数据可用性
    # ------------------------------------------------------------------
    @staticmethod
    def _dir_has_all_tables(base_dir: str) -> bool:
        """目录中是否已包含全部所需表。"""
        return all(os.path.exists(os.path.join(base_dir, f"{name}.json")) for name in REQUIRED_TABLES)

    def has_local_data(self) -> bool:
        """本插件缓存或共享 gamedata 是否已具备全部所需表。"""
        return self._dir_has_all_tables(CACHE_JSON_DIR) or self._dir_has_all_tables(
            SHARED_JSON_DIR
        )

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    async def download_tables(
        self, base_urls: list[str], timeout: float = 90.0
    ) -> bool:
        """按顺序尝试各数据源，下载缺失的 JSON 表到本插件缓存目录。

        Args:
            base_urls: 数据源地址列表（需包含 excel 子目录）。
            timeout: 单次请求超时（秒）。

        Returns:
            全部所需表可用时返回 True。
        """
        os.makedirs(CACHE_JSON_DIR, exist_ok=True)
        headers = {"User-Agent": "Mozilla/5.0 (AstrBot arknights-activity-remind)"}
        last_error = ""
        for base_url in base_urls:
            base_url = (base_url or "").strip().rstrip("/")
            if not base_url:
                continue
            try:
                ok = await self._download_from(base_url, timeout, headers)
            except Exception as e:  # noqa: BLE001
                logger.warning("数据源 %s 下载失败: %s", base_url, e)
                last_error = str(e)
                continue
            if ok:
                return True
        if last_error:
            self.last_error = last_error
        return self.has_local_data()

    async def _download_from(self, base_url: str, timeout: float, headers: dict) -> bool:
        """从单个数据源并发下载 4 张表；全部落盘后返回是否齐全。"""
        sem = asyncio.Semaphore(2)  # 控制并发，避免大文件占用过多内存
        tasks = [self._download_one(base_url, name, timeout, headers, sem) for name in REQUIRED_TABLES]
        await asyncio.gather(*tasks)
        return self._dir_has_all_tables(CACHE_JSON_DIR)

    async def _download_one(
        self,
        base_url: str,
        name: str,
        timeout: float,
        headers: dict,
        sem: asyncio.Semaphore,
    ) -> None:
        """下载单张表并写缓存（失败仅记日志，不中断其它表）。"""
        url = f"{base_url}/excel/{name}.json"
        path = os.path.join(CACHE_JSON_DIR, f"{name}.json")
        async with sem:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            logger.warning("下载 %s 失败: HTTP %s", url, resp.status)
                            return
                        data = await resp.json(encoding="utf-8")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                logger.info("已下载 %s（%d bytes）", name, os.path.getsize(path))
            except Exception as e:  # noqa: BLE001
                logger.warning("下载 %s 异常: %s", url, e)

    # ------------------------------------------------------------------
    # 构建提醒列表（移植自 AmiyaBot init_actlist）
    # ------------------------------------------------------------------
    def build_remind_list(self) -> None:
        """读取全部表并构建按时间排序的提醒列表。"""
        self.clear_cache()
        now = int(datetime.now().timestamp())

        activity_table = self.read_table("activity_table")
        crisis_table = self.read_table("crisis_v2_table")
        gacha_table = self.read_table("gacha_table")
        tower_table = self.read_table("climb_tower_table")

        remind_list: list[dict] = []

        # 活动/主题曲：开始、结束、奖励兑换截止
        basic_info = activity_table.get("basicInfo") or {}
        for active in basic_info.values():
            if active.get("startTime", 0) >= now:
                act_type = "新主题曲" if active.get("type") == "TYPE_MAINSS" else "活动"
                remind_list.append(
                    {
                        "timestamp": active["startTime"],
                        "type": act_type,
                        "name": active.get("name", ""),
                        "time_str": datetime.fromtimestamp(active["startTime"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "开始",
                    }
                )
            if active.get("endTime", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": active["endTime"],
                        "type": "活动",
                        "name": active.get("name", ""),
                        "time_str": datetime.fromtimestamp(active["endTime"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "结束",
                    }
                )
            reward_end = active.get("rewardEndTime", 0)
            if reward_end >= now and reward_end != active.get("endTime", 0):
                remind_list.append(
                    {
                        "timestamp": reward_end,
                        "type": "活动",
                        "name": active.get("name", ""),
                        "node": "奖励兑换",
                        "time_str": datetime.fromtimestamp(reward_end).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "结束",
                    }
                )

        # 活动节点提醒（actThemes 的 timeNodes）
        pattern = re.compile(r"<([^>]*)>")
        for item in activity_table.get("actThemes") or []:
            time_nodes = item.get("timeNodes") or []
            if not time_nodes:
                continue
            act_name = ""
            match = pattern.search(str(time_nodes[0].get("title") or ""))
            if match:
                act_name = match.group(1)
            act_type = "活动"
            for key, value in ACT_TYPE_MAP.items():
                if key in str(item.get("type") or ""):
                    act_type = value
                    break
            for i, node in enumerate(time_nodes):
                if node.get("ts", 0) >= now:
                    result = {
                        "timestamp": node["ts"],
                        "type": act_type,
                        "name": act_name,
                        "time_str": datetime.fromtimestamp(node["ts"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "开始",
                    }
                    if i > 0:
                        result["node"] = str(node.get("title") or "").replace("已开放", "")
                        result["remind_type"] = "开放"
                    remind_list.append(result)

        # 危机合约季节
        for crisis in (crisis_table.get("seasonInfoDataMap") or {}).values():
            if crisis.get("startTs", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": crisis["startTs"],
                        "type": "危机合约",
                        "name": crisis.get("name", ""),
                        "time_str": datetime.fromtimestamp(crisis["startTs"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "开始",
                    }
                )
            if crisis.get("endTs", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": crisis["endTs"],
                        "type": "危机合约",
                        "name": crisis.get("name", ""),
                        "time_str": datetime.fromtimestamp(crisis["endTs"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "结束",
                    }
                )

        # 卡池（忽略常驻/中坚等类型，以及特殊情况卡池）
        for pool in gacha_table.get("gachaPoolClient") or []:
            if pool.get("gachaRuleType") in IGNORE_POOL_LIST:
                continue
            if pool.get("gachaPoolName") == "适合多种场合的强力干员":
                continue
            if pool.get("openTime", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": pool["openTime"],
                        "type": "卡池",
                        "name": pool.get("gachaPoolName", ""),
                        "time_str": datetime.fromtimestamp(pool["openTime"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "开始",
                    }
                )
            if pool.get("endTime", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": pool["endTime"],
                        "type": "卡池",
                        "name": pool.get("gachaPoolName", ""),
                        "time_str": datetime.fromtimestamp(pool["endTime"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "结束",
                    }
                )

        # 保全派驻周期
        for season in (tower_table.get("seasonInfos") or {}).values():
            if season.get("startTs", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": season["startTs"],
                        "type": "保全派驻周期",
                        "name": season.get("name", ""),
                        "time_str": datetime.fromtimestamp(season["startTs"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "开始",
                    }
                )
            if season.get("endTs", 0) >= now:
                remind_list.append(
                    {
                        "timestamp": season["endTs"],
                        "type": "保全派驻周期",
                        "name": season.get("name", ""),
                        "time_str": datetime.fromtimestamp(season["endTs"]).strftime("%Y-%m-%d %H:%M"),
                        "remind_type": "结束",
                    }
                )

        # 去除重复并按时间戳排序
        seen = set()
        unique_list = []
        for d in remind_list:
            key = tuple(sorted(d.items()))
            if key not in seen:
                seen.add(key)
                unique_list.append(d)
        unique_list.sort(key=lambda x: x["timestamp"])
        self.remind_list = unique_list
        self.data_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_error = ""
        self.ready = True
        logger.info("活动提醒数据构建完成: %d 条提醒（%s）", len(self.remind_list), self.data_time)
