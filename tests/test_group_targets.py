"""跨平台群号解析的离线自测。

用法：python tests/test_group_targets.py [插件目录]
不依赖 AstrBot 运行环境，也不依赖 playwright / aiohttp。
"""

import importlib.util
import json
import os
import sys
import tempfile
import types

PLUGIN_DIR = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
PKG = "target_plugin"
TMP = tempfile.mkdtemp(prefix="targets_test_")
ALIAS_FILE = os.path.join(TMP, "platform_alias", "aliases.json")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(detail)) if (detail and not cond) else ""))


def mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Silent:
    def __getattr__(self, _i):
        return lambda *a, **k: None


class _Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}


class _MT:
    GROUP_MESSAGE = types.SimpleNamespace(value="GroupMessage")
    FRIEND_MESSAGE = types.SimpleNamespace(value="FriendMessage")


mod("astrbot")
mod("astrbot.api", logger=_Silent(), star=types.SimpleNamespace(Star=_Star))
mod("astrbot.api.all", AstrBotConfig=dict, AstrMessageEvent=object, MessageChain=object)
mod("astrbot.api.event", filter=types.SimpleNamespace(command=lambda *a, **k: (lambda f: f)))
mod("astrbot.core.platform.message_type", MessageType=_MT)
mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: TMP)
try:
    import aiohttp  # noqa: F401
except Exception:
    mod("aiohttp", ClientSession=object, ClientTimeout=object)

pkg = mod(PKG)
pkg.__path__ = [PLUGIN_DIR]
mod(PKG + ".gamedata", CACHE_JSON_DIR=TMP, SHARED_JSON_DIR=TMP, RemindData=type("RemindData", (), {}))
mod(PKG + ".render", shutdown=lambda: None)
mod(PKG + ".rules", DEFAULT_IGNORE_KEYWORDS=[], item_images=lambda *a, **k: [], should_push=lambda *a, **k: True)
mod(PKG + ".policy", load_policy=lambda *a, **k: {})
mod(PKG + ".rss_feed", fetch=lambda *a, **k: [])
mod(PKG + ".bili_api", BiliAPIError=Exception, BiliDynamicClient=object)

spec = importlib.util.spec_from_file_location(PKG + ".main", os.path.join(PLUGIN_DIR, "main.py"))
main = importlib.util.module_from_spec(spec)
sys.modules[PKG + ".main"] = main
spec.loader.exec_module(main)
print("已加载:", os.path.basename(PLUGIN_DIR), "->", [n for n in dir(main) if n.endswith("Plugin")])


class _Ctx:
    def __init__(self, pids):
        self.platform_manager = types.SimpleNamespace(
            platform_insts=[
                types.SimpleNamespace(config={"id": p}, meta=lambda p=p: types.SimpleNamespace(id=p))
                for p in pids
            ]
        )


def make_plugin(pids, groups):
    cls = [getattr(main, n) for n in dir(main) if n.endswith("Plugin") and isinstance(getattr(main, n), type)][0]
    p = cls(_Ctx(pids), {})
    p._groups = groups
    p._platform_ids = list(pids)
    return p


def write_alias(mapping):
    os.makedirs(os.path.dirname(ALIAS_FILE), exist_ok=True)
    with open(ALIAS_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "groups": mapping, "users": {}}, f)


GROUP_NO = "602519154"
OTHER = "700000001"

print("\n[1] 没有映射表：原平台在跑就照旧，平台没了就如实报失效")
if os.path.exists(ALIAS_FILE):
    os.remove(ALIAS_FILE)
p = make_plugin(["qq_official_bot"], {"AstrBot": [GROUP_NO]})
check("平台没了且未登记时不盲投", p._enabled_groups() == [], p._enabled_groups())
check("平台没了且未登记时列为失效", p._stale_groups() == [("AstrBot", [GROUP_NO])], p._stale_groups())

p2 = make_plugin(["AstrBot", "qq_official_bot"], {"AstrBot": [GROUP_NO]})
check("原平台还在跑就用原平台", p2._enabled_groups() == [("AstrBot", GROUP_NO)], p2._enabled_groups())
check("原平台在跑时无失效订阅", p2._stale_groups() == [], p2._stale_groups())

p3 = make_plugin(["p1", "p2", "p3"], {"AstrBot": [GROUP_NO]})
check("多平台且无映射时不再瞎猜", p3._enabled_groups() == [], p3._enabled_groups())
check("多平台且无映射时列为失效", p3._stale_groups() == [("AstrBot", [GROUP_NO])], p3._stale_groups())

print("\n[2] 有映射表：按群号精确投递")
write_alias({GROUP_NO: {"native": {"qq_official_bot": "8FF0A5DE2922DE19925E1BE9EF3D58A2"}}})
p4 = make_plugin(["AstrBot", "qq_official_bot"], {"AstrBot": [GROUP_NO]})
check("优先投映射表登记的平台", p4._enabled_groups() == [("qq_official_bot", GROUP_NO)], p4._enabled_groups())
check("已改投则不算失效", p4._stale_groups() == [], p4._stale_groups())

p5 = make_plugin(["qq_official_bot"], {"AstrBot": [GROUP_NO], "qq_official_bot": [GROUP_NO]})
check("重复订阅去重", p5._enabled_groups() == [("qq_official_bot", GROUP_NO)], p5._enabled_groups())

write_alias({GROUP_NO: {"native": {"AstrBot": GROUP_NO, "qq_official_bot": "8FF0"}}})
p6 = make_plugin(["AstrBot", "qq_official_bot"], {"AstrBot": [GROUP_NO]})
check("登记了两个平台就都投", sorted(p6._enabled_groups()) == [("AstrBot", GROUP_NO), ("qq_official_bot", GROUP_NO)], p6._enabled_groups())

print("\n[3] 混合：部分能救、部分救不了（两个平台在跑，不触发单平台改投）")
write_alias({GROUP_NO: {"native": {"qq_official_bot": "8FF0"}}})
p7 = make_plugin(["qq_official_bot", "another_bot"], {"AstrBot": [GROUP_NO, OTHER]})
check("能救的改投", p7._enabled_groups() == [("qq_official_bot", GROUP_NO)], p7._enabled_groups())
check("救不了的列为失效", p7._stale_groups() == [("AstrBot", [OTHER])], p7._stale_groups())

print("\n[4] 边界")
p8 = make_plugin([], {"AstrBot": [GROUP_NO]})
check("探测不到平台时保持原样", p8._enabled_groups() == [("AstrBot", GROUP_NO)], p8._enabled_groups())
p9 = make_plugin(["qq_official_bot"], {"qq_official_bot": ["", "bad id", GROUP_NO]})
check("非法群 ID 被过滤", p9._enabled_groups() == [("qq_official_bot", GROUP_NO)], p9._enabled_groups())
p10 = make_plugin(["qq_official_bot"], {"AstrBot": []})
check("空订阅不出错", p10._enabled_groups() == [] and p10._stale_groups() == [])

print("\n[5] 映射表热更新（改文件后无需重启插件）")
write_alias({GROUP_NO: {"native": {"qq_official_bot": "NEW"}}})
p11 = make_plugin(["qq_official_bot"], {"AstrBot": [GROUP_NO]})
check("首次读取", p11._enabled_groups() == [("qq_official_bot", GROUP_NO)])
write_alias({GROUP_NO: {"native": {"another_bot": "X"}}})
p12 = make_plugin(["qq_official_bot", "another_bot"], {"AstrBot": [GROUP_NO]})
check("改文件后立即生效", p12._enabled_groups() == [("another_bot", GROUP_NO)], p12._enabled_groups())

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败用例：" + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
