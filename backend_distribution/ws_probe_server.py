#!/usr/bin/env python3
"""Loopback-only WebSocket probe for the Spiral Warrior test clone.

It records frame metadata and protobuf field numbers. On ECS_LoginNode=1111,
it sends synthetic PlayerData and PlayerInfo sync packets followed by a
synthetic successful SC_Login response. Adventure map state is synchronized
with RogueLikeInfoProto message 5 as the local player enters and clears nodes.
No original backend is contacted.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import secrets
import socketserver
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from account_store import AccountStore
from mail_fixture import DEFAULT_STATE_FILE, update_mail_state

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEST_ACHIEVEMENT_ID = 1093677105  # Static AchievementData row: Veteran / CHAMPION_RACE.
TEST_ROGUELIKE_KEYS = 5  # Local-only Adventure starter keys.
ROGUELIKE_KEY_CAPACITY = 6  # Live client on MuMu reports a 6-key limit for this profile.
ROGUELIKE_FREE_KEY_REWARD = 1  # X3Define.RogueLikeExchangeKeyByADNum.
ROGUELIKE_RET_EXCHANGE_KEY_AD_ERROR = 49
ROGUELIKE_RET_EXCHANGE_KEY_SUCCESS = 50
# The client uses ui.getDateTime2018() as the epoch for SysInfo.ServerTime and
# RogueLikeMap.Version. Runtime on LDPlayer reports 1514736000000 ms for it.
GAME_TIME_EPOCH_MS = 1514736000000
LEVEL_TASK_TYPE = 2
DAILY_TASK_TYPE = 3
TASK_STATUS_INVALID = 0
TASK_STATUS_DOING = 1
TASK_STATUS_COMPLETE = 2
TASK_STATUS_REWARDED = 3
TASK_OPR_GET_REWARD = 1
TASK_OPR_UPDATE = 2
ITEM_OPR_UPGRADE_PART = 1
ITEM_OPR_USE_ITEM = 3
ITEM_OPR_BOX_COUNTDOWN = 4
ITEM_OPR_BOX_READY_OPEN = 5
ITEM_OPR_REDUCE_BOX_COUNTDOWN = 7
ITEM_OPR_CHANGE_READY_BOX = 21
ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP = 13
ITEM_OPR_RET_INVALID_ITEM = 1
ITEM_OPR_RET_PIECE_LIMIT = 3
ITEM_OPR_RET_MAX_LEVEL = 6
ITEM_OPR_RET_COST_NOT_ENOUGH = 7
ITEM_OPR_RET_SYSTEM_ERROR = 8
ITEM_OPR_RET_UPGRADE_OK = 11
ITEM_OPR_RET_USE_OK = 14
ITEM_OPR_RET_MONEY_NOT_ENOUGH = 17
ITEM_OPR_RET_COUNTING_DOWN = 15
ITEM_OPR_RET_SUCCEED = 16
ITEM_OPR_RET_BOX_HAVE_READY_OPEN = 18
ITEM_OPR_RET_BOX_READY_OK = 19
ITEM_OPR_RET_BOX_REDUCE_OK = 20
ITEM_OPR_RET_CHANGE_READY_OK = 42
ITEM_OPR_MONEY_GOLD = 1
ITEM_OPR_MONEY_DIAMOND = 2
EMITTER_MATERIAL_COMPOSE_RULES = {
    # Live Cocos EmitterUI.createShowItem resolves these item IDs; the
    # ItemCompose sender passes the lower-tier item and output count.
    1397567537: {"output_item_id": 1397567538, "pieces_per_output": 8,
                 "gold_per_output": 200},
    1397567538: {"output_item_id": 1397567539, "pieces_per_output": 8,
                 "gold_per_output": 1000},
}
DIAMOND_ITEM_ID = 1227894834
GOLD_ITEM_ID = 1227894833
# The recovered client requires a positive PlayerInfo limit, but this project
# has no original-server GoldLimit table. Use a documented local policy rather
# than the previous int32-max workaround that produced a visibly bogus UI.
COMMUNITY_GOLD_DAILY_LIMIT = 10_000
TASK_SCORE_ITEM_ID = 1227894838
LEVEL_FIRST_REWARD_ID = 1280716849
# GuideMark.Over is bit index 2 in the client enum, represented as mask 4.
GUIDE_MARK_OVER_MASK = 1 << 2
PROFILE_SCHEMA_VERSION = 10
FIRST_LEVEL_UPGRADE_TASK_ID = 1278226737
FIRST_LEVEL_UPGRADE_COUNTER = "tutorial:first_successful_part_upgrade"
CHAMPIONSHIP_INTRO_TASK_ID = 1278226994
TUTORIAL_UPGRADE_PART_ID = 2001
TUTORIAL_UPGRADE_PART_PIECES = 2
TUTORIAL_UPGRADE_GOLD = 20
# Tutorial guide Part 2011 is the Shuriken middle component (Item.itemType=1).
# A usable owned row starts at the component's recovered ItemUpgrade.initLevel.
FIRST_CHEST_SHURIKEN_PART_ID = 2011
try:
    _progression_catalog = json.loads(
        (Path(__file__).resolve().parent / "data" / "level_progression_catalog.json").read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError):
    _progression_catalog = {}
LEVEL_PROGRESSION_ROWS = {
    int(row["Id"]): row for row in _progression_catalog.get("levels", [])
    if isinstance(row, dict) and "Id" in row
}
LEVEL_PROGRESSION_TASKS = {
    int(row["TaskID"]): row for row in _progression_catalog.get("tasks", [])
    if isinstance(row, dict) and "TaskID" in row and int(row.get("taskType", -1)) == LEVEL_TASK_TYPE
}
LEVEL_PROGRESSION_EXCHANGE_ROWS = {
    int(row["level"]): row
    for row in _progression_catalog.get("level_first_rewards", [])
    if isinstance(row, dict) and int(row.get("level", 0)) > 0
    and int(row.get("ExchangeID", 0)) > 0
}
LEVEL_PROGRESSION_EXCHANGE_REWARDS = {
    int(row["ExchangeID"]): tuple(
        (int(item.get("ID", 0)), int(item.get("Num", 0)))
        for item in row.get("RewardList", [])
        if isinstance(item, dict) and int(item.get("ID", 0)) > 0
        and int(item.get("Num", 0)) > 0
    )
    for row in _progression_catalog.get("level_first_rewards", [])
    if isinstance(row, dict) and int(row.get("ExchangeID", 0)) > 0
}
# Keep a narrow opening-tier fallback for installations where the derived
# client catalog is unavailable. When present, recovered LevelOpen/TaskData
# rows above take precedence and define all supported tiers.
FIRST_LEVEL_PROGRESSION_ROW = {
    "Id": 2,
    "LevelTasks": [1278226737, 1278226738],
    "levelGPA": 5,
    "rewardGPA": 5,
    "lvUp_TaskNum": 2,
}
FIRST_LEVEL_PROGRESSION_TASKS = (
    {"TaskID": 1278226737, "taskType": LEVEL_TASK_TYPE,
     "condition": 23, "maxProcess": 1, "Param": 2},
    {"TaskID": 1278226738, "taskType": LEVEL_TASK_TYPE,
     "condition": 16, "maxProcess": 3, "Param": 0},
)
LEVEL_PROGRESSION_ROWS.setdefault(2, FIRST_LEVEL_PROGRESSION_ROW)
for _first_level_task in FIRST_LEVEL_PROGRESSION_TASKS:
    LEVEL_PROGRESSION_TASKS.setdefault(
        int(_first_level_task["TaskID"]), _first_level_task,
    )
# Narrow fallback for the recovered next tier; it does not overwrite the full
# original rows when the derived catalog is present.
LEVEL_THREE_PROGRESSION_ROW = {
    "Id": 3,
    "LevelTasks": [1278226993, CHAMPIONSHIP_INTRO_TASK_ID, 1278226995],
    "levelGPA": 10,
    "rewardGPA": 5,
    "lvUp_TaskNum": 2,
}
LEVEL_THREE_PROGRESSION_TASKS = (
    {"TaskID": 1278226993, "taskType": LEVEL_TASK_TYPE,
     "condition": 23, "maxProcess": 10, "Param": 1},
    {"TaskID": CHAMPIONSHIP_INTRO_TASK_ID, "taskType": LEVEL_TASK_TYPE,
     "RaceMarks": 2, "condition": 0, "maxProcess": 1, "Param": 0},
    {"TaskID": 1278226995, "taskType": LEVEL_TASK_TYPE,
     "condition": 30, "maxProcess": 1, "Param": 0},
)
LEVEL_PROGRESSION_ROWS.setdefault(3, LEVEL_THREE_PROGRESSION_ROW)
for _level_three_task in LEVEL_THREE_PROGRESSION_TASKS:
    LEVEL_PROGRESSION_TASKS.setdefault(
        int(_level_three_task["TaskID"]), _level_three_task,
    )
# The packaged client TopPartData/English text tables group these components
# by toyTop and location: Behemoth (1), Athena (2), Kaguyahime (10).
STARTER_BLADE_ASSEMBLIES = (
    (2001, 2002, 2003),   # Behemoth: top, middle, bottom.
    (2004, 2005, 2006),   # Athena: top, middle, bottom.
    (2028, 2029, 2030),   # Kaguyahime: top, middle, bottom.
)
SYNTHETIC_STARTER_PARTS = tuple(
    (part_id, part_as)
    for assembly in STARTER_BLADE_ASSEMBLIES
    for part_as, part_id in enumerate(assembly, start=1)
)
# This local Adventure status describes one active ToyTop, so it continues to
# use the first (Behemoth) assembly rather than listing all owned blades on it.
SYNTHETIC_BATTLE_PARTS = SYNTHETIC_STARTER_PARTS[:3]
TUTORIAL_TASK_EXCHANGE_REWARDS = {
    # GuideData 1412445232 calls DoLevelUp("LV01"). The original client table
    # maps LV01 (int32 0x4c563031) to one Basic Accessory Box.
    1280716849: ((1228156978, 1),),
}
# ChanceManager row 891 confirms that the LV01 Basic Accessory Box can award
# part 2001; NumRank1=2 is the level-1 quantity. BoxList drives the original
# BoxUI, which emits GuideEvent.OnBoxUIExit when dismissed.
TUTORIAL_TASK_EXCHANGE_BOX_CONTENTS = {
    1280716849: ((1228156978, ((2001, 2),)),),
}
LINEUP_PART_FIELDS = {"top": 1, "mid": 2, "bot": 3}
PURCHASE_PART_LOCATIONS = {
    # Confirmed through the live recharge catalog and PartDataCache.getPartSelf
    # on LDPlayer: location 0=Top, 1=Middle, 2=Bottom.
    2034: 0, 2035: 1, 2036: 2, 2070: 0,
}
PART_ITEM_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "part_item_catalog.json"


def load_part_item_locations() -> dict[int, int]:
    """Load client-confirmed part item IDs and their top/middle/bottom slots."""
    try:
        catalog = json.loads(PART_ITEM_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    locations: dict[int, int] = {}
    for location, item_ids in (catalog.get("items_by_location", {}) or {}).items():
        try:
            slot = int(location)
        except (TypeError, ValueError):
            continue
        if slot not in (0, 1, 2) or not isinstance(item_ids, list):
            continue
        for item_id in item_ids:
            try:
                parsed_id = int(item_id)
            except (TypeError, ValueError):
                continue
            if parsed_id > 0:
                locations[parsed_id] = slot
    return locations


# The optional catalog can extend known parts, but the synthetic starter
# inventory must remain resolvable when that local-only file is absent.
PART_ITEM_LOCATIONS = load_part_item_locations()
PART_ITEM_LOCATIONS.update({part_id: part_as - 1
                            for part_id, part_as in SYNTHETIC_STARTER_PARTS})
# The recovered guide explicitly selects part 2011 and Cocos metadata maps it
# to the middle slot. This mapping enables real profile-owned state to serialize;
# it does not grant the part or claim that the unavailable chest awarded it.
PART_ITEM_LOCATIONS.setdefault(2011, 1)
BOX_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "box_catalog.json"


def load_box_catalog() -> dict[str, object]:
    """Load the workstation-local extract of the encrypted Item/Chance tables."""
    try:
        catalog = json.loads(BOX_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"box_items": [], "items": [], "chance_rows": {},
                "box_get_process": []}
    if not isinstance(catalog, dict):
        return {"box_items": [], "items": [], "chance_rows": {},
                "box_get_process": []}
    return catalog


BOX_CATALOG = load_box_catalog()
BOX_ITEM_ROWS = {
    int(row["ItemId"]): row for row in BOX_CATALOG.get("box_items", [])
    if isinstance(row, dict) and int(row.get("ItemId", 0)) > 0
    and int(row.get("itemType", 0)) in (5, 6)
}
BOX_ITEM_META = {
    int(row["ItemId"]): row for row in BOX_CATALOG.get("items", [])
    if isinstance(row, dict) and int(row.get("ItemId", 0)) > 0
}
BOX_CHANCE_ROWS = {
    int(box_id): rows for box_id, rows in BOX_CATALOG.get("chance_rows", {}).items()
    if isinstance(rows, list)
}
BOX_GET_PROCESS_ROWS = BOX_CATALOG.get("box_get_process", [])
# Keep box-reward parts separate from the backend's pre-existing recognized
# part catalog. This serializes chest fragments without reinterpreting legacy
# generic inventory IDs as owned equipment.
BOX_REWARD_PART_LOCATIONS = {
    int(item_id): int(row["itemType"])
    for item_id, row in BOX_ITEM_META.items()
    if int(row.get("itemType", -1)) in (0, 1, 2)
}
UPGRADEABLE_PART_LOCATIONS = {**PART_ITEM_LOCATIONS, **BOX_REWARD_PART_LOCATIONS}
ALL_SERIALIZED_PART_LOCATIONS = dict(UPGRADEABLE_PART_LOCATIONS)
# Item.itemType 0/1/2 is the client's Top/Middle/Bottom part category. These
# are genuine part IDs from the recovered Item table, not arbitrary inventory
# IDs. Their fragment count and acquired Level are separate persisted values.
PVE_CHAPTER_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "chapter_pve_catalog.json"
PVE_BATTLE_STAGES_PER_CHAPTER = 5
PVE_CHAPTER_REWARD_INDEX = PVE_BATTLE_STAGES_PER_CHAPTER + 1
PVE_CHAPTER_OPR_QUERY = 0
PVE_CHAPTER_OPR_WIN = 1
PVE_CHAPTER_OPR_REWARD = 2
PVE_CHAPTER_OPR_BEGIN_RACE = 3
PVE_CHAPTER_OPR_INVALID_CHAPTER = 100
PVE_CHAPTER_OPR_NEED_LAST_WIN = 101
PVE_CHAPTER_OPR_VERSION_ERROR = 102
PVE_CHAPTER_OPR_NOT_BIG_WIN = 103
PVE_CHAPTER_OPR_REWARD_ALREADY_CLAIMED = 104
PVE_CHAPTER_OPR_INVALID_RESULT = 105
def load_pve_chapter_rows() -> dict[int, dict[str, object]]:
    """Load client-extracted campaign PvE rows, keyed by the native stage ID."""
    try:
        catalog = json.loads(PVE_CHAPTER_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if catalog.get("client_version") != "1.1.0.96":
        return {}
    rows: dict[int, dict[str, object]] = {}
    for row in catalog.get("rows", []):
        if not isinstance(row, dict):
            continue
        try:
            chapter_id = int(row["ChapterID"])
            index = int(row["IndexInChapter"])
            version = int(row["Version"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= chapter_id <= 10 or not 1 <= index <= PVE_CHAPTER_REWARD_INDEX or version <= 0:
            continue
        rows[chapter_id * 10000 + index] = row
    return rows


PVE_CHAPTER_ROWS = load_pve_chapter_rows()
GUAJI_PVE_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "guaji_pve_catalog.json"
GUAJI_PVE_BEGIN_RACE = 4
GUAJI_PVE_WIN = 5
GUAJI_PVE_LOST = 12
GUAJI_PVE_PROCESS_INVALID = 101


def load_guaji_pve_rows() -> dict[int, dict[str, object]]:
    """Load the runtime-exported Guaji stages, keyed by Process."""
    try:
        catalog = json.loads(GUAJI_PVE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows: dict[int, dict[str, object]] = {}
    for entry in catalog.get("rows", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("row"), dict):
            continue
        try:
            process = int(entry["row"]["Process"])
        except (KeyError, TypeError, ValueError):
            continue
        if process > 0:
            rows[process] = entry["row"]
    return rows


GUAJI_PVE_ROWS = load_guaji_pve_rows()
ITEM_UPGRADE_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "item_upgrade_catalog.json"


def load_item_upgrade_catalog() -> tuple[dict[int, dict[str, object]], dict[int, int]]:
    """Load live-extracted ItemUpgrade rows and their part-key mapping."""
    try:
        catalog = json.loads(ITEM_UPGRADE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    upgrades: dict[int, dict[str, object]] = {}
    for row in catalog.get("upgrades", []):
        if not isinstance(row, dict):
            continue
        try:
            key = int(row["UpgradeKey"])
        except (KeyError, TypeError, ValueError):
            continue
        if key > 0:
            upgrades[key] = row
    part_keys: dict[int, int] = {}
    for row in catalog.get("parts", []):
        if not isinstance(row, dict):
            continue
        try:
            part_id = int(row["partId"])
            upgrade_key = int(row["upgradeKey"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            item_type = int(row.get("itemType", -1))
        except (TypeError, ValueError):
            item_type = -1
        if item_type in (0, 1, 2):
            # Item.itemType is the recovered client's Top/Middle/Bottom enum.
            # A part row remains a fragment until Item_LvUp raises its Level.
            UPGRADEABLE_PART_LOCATIONS[part_id] = item_type
            ALL_SERIALIZED_PART_LOCATIONS[part_id] = item_type
        if part_id in UPGRADEABLE_PART_LOCATIONS and upgrade_key in upgrades:
            part_keys[part_id] = upgrade_key
    return upgrades, part_keys


ITEM_UPGRADE_ROWS, PART_UPGRADE_KEYS = load_item_upgrade_catalog()
SYNTHETIC_STARTER_LEVEL = 1  # GuideData explicitly requires part 2001 to be level 1.
LEGACY_SYNTHETIC_STARTER_LEVEL = 10  # Preserve the old synthetic levels on existing profiles.
CHAMPION_RACE_TYPE = 5  # E_RaceType.CHAMPION_RACE from the client protobuf enum.
SYNTHETIC_CHAMPION_VOUCHERS = 10
COMMUNITY_CHAMPION_STARTING_VOUCHERS = 7
COMMUNITY_CHAMPION_PERIOD_SECONDS = 24 * 60 * 60
COMMUNITY_CHAMPION_MATCH_GOLD = 500
# Client GameEnd falls back to this named result chest for Championship.
CHAMPION_RESULT_CHEST_ID = int.from_bytes(b"I203", "big")
COMMUNITY_CHAMPION_SAVEBOX_SLOTS = 4  # Constants.MAX_SAVEBOX_NUM in recovered client.
CHAMPION_ROBOT_DISPLAY_NAMES = (
    "Astra", "Mira", "Kai", "Luna", "Rin", "Orion", "Nova", "Sora",
    "Yuki", "Kira", "Ren", "Aria", "Niko", "Hana", "Leo", "Mei",
    "Riku", "Iris", "Theo", "Aiko", "Juno", "Ezra", "Milo", "Nami",
    "Rei", "Cleo", "Tara", "Kaito", "Zara", "Eden", "Noah", "Akira",
)
CHAMPION_ROBOT_RANK_ROWS = 10  # Client includes the player in its initial ten rows.
CHAMPION_MAX_RANK_ROWS = 15
CAREER_STAT_IDS = {
    "battles_joined": 1095118897,
    "championship_participations": 1095118898,
    "championship_victories": 1095118899,
    "survival_participations": 1095118900,
    "times_survived": 1095118901,
    "gold_obtained": 1095118902,
    "parts_unlocked": 1095118903,
    "painting_unlocked": 1095118904,
    "launchers_unlocked": 1095118905,
}
CAREER_BASELINE_VALUES = {
    CAREER_STAT_IDS["battles_joined"]: 0,
    CAREER_STAT_IDS["championship_participations"]: 0,
    CAREER_STAT_IDS["championship_victories"]: 0,
    CAREER_STAT_IDS["survival_participations"]: 0,
    CAREER_STAT_IDS["times_survived"]: 0,
    CAREER_STAT_IDS["gold_obtained"]: 0,
    CAREER_STAT_IDS["parts_unlocked"]: len(SYNTHETIC_STARTER_PARTS),
    CAREER_STAT_IDS["painting_unlocked"]: 0,
    CAREER_STAT_IDS["launchers_unlocked"]: 0,
}
ROGUELIKE_EXPORT_ITEMS_PATH = Path(__file__).resolve().parent / "data" / "roguelike_export_items.json"
ROGUELIKE_EVENT_TYPE_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "roguelike_event_type_catalog.json"


def load_roguelike_event_types() -> dict[int, int]:
    """Load client-extracted Adventure event IDs and their event types."""
    try:
        catalog = json.loads(ROGUELIKE_EVENT_TYPE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[int, int] = {}
    for row in catalog.get("events", []):
        if not isinstance(row, dict):
            continue
        try:
            event_id, event_type = int(row["EventID"]), int(row["EventType"])
        except (KeyError, TypeError, ValueError):
            continue
        if event_id > 0:
            result[event_id] = event_type
    return result


ROGUELIKE_EVENT_TYPE_BY_ID = load_roguelike_event_types()
# EventType values were confirmed against live Cocos grid nodes and its UI
# dispatcher: 1/2 are Lv1/Lv2 battles, 5 is the final boss, 8 is a reward,
# and 9 is the Adventure shop.
ROGUELIKE_LEVEL_TASK_CONDITION_BY_EVENT_TYPE = {
    1: 32, 2: 33, 5: 29, 8: 34, 9: 19,
}


def load_roguelike_export_items() -> dict[str, dict[str, int]]:
    """Load the item-only reward rows extracted from the original client table."""
    try:
        data = json.loads(ROGUELIKE_EXPORT_ITEMS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {
        str(export_id): {key: int(value) for key, value in row.items()
                         if isinstance(value, int)}
        for export_id, row in data.get("items", {}).items()
    }


ROGUELIKE_EXPORT_ITEMS = load_roguelike_export_items()


def load_roguelike_chiji_exports() -> dict[str, dict[str, object]]:
    """Load RewardType.ChijiItem exports from the original client table."""
    try:
        data = json.loads(ROGUELIKE_EXPORT_ITEMS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    result: dict[str, dict[str, object]] = {}
    for export_id, row in data.get("chiji_items", {}).items():
        parsed: dict[str, object] = {}
        for key, value in row.items():
            if isinstance(value, int):
                parsed[key] = int(value)
            elif key == "chapter_levels" and isinstance(value, list):
                parsed[key] = [int(level) for level in value]
        result[str(export_id)] = parsed
    return result


ROGUELIKE_CHIJI_EXPORTS = load_roguelike_chiji_exports()


def load_roguelike_recover_hp_exports() -> dict[str, dict[str, object]]:
    """Load RewardType.RecoverHp rows from the original repair-station table."""
    try:
        data = json.loads(ROGUELIKE_EXPORT_ITEMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, dict[str, object]] = {}
    for export_id, row in data.get("recover_hp", {}).items():
        if not isinstance(row, dict):
            continue
        parsed: dict[str, object] = {}
        for key, value in row.items():
            if isinstance(value, int):
                parsed[key] = int(value)
            elif key == "chapter_levels" and isinstance(value, list):
                parsed[key] = [int(level) for level in value]
        result[str(export_id)] = parsed
    return result


ROGUELIKE_RECOVER_HP_EXPORTS = load_roguelike_recover_hp_exports()


def load_roguelike_search_point_exports() -> dict[str, dict[str, int]]:
    """Load RewardType.SearchPoint rows from the original client table."""
    try:
        data = json.loads(ROGUELIKE_EXPORT_ITEMS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {
        str(export_id): {key: int(value) for key, value in row.items()
                         if isinstance(value, int)}
        for export_id, row in data.get("search_points", {}).items()
    }


ROGUELIKE_SEARCH_POINT_EXPORTS = load_roguelike_search_point_exports()

ROGUELIKE_SHOP_PRICES_PATH = Path(__file__).resolve().parent / "data" / "roguelike_shop_prices.json"


def load_roguelike_shop_prices() -> dict[str, dict[str, int]]:
    """Load client-extracted Adventure shop prices keyed by export ID."""
    try:
        catalog = json.loads(ROGUELIKE_SHOP_PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if int(catalog.get("event_type", 0)) != 9 or "1.1.0.96" not in str(catalog.get("source", "")):
        return {}
    result: dict[str, dict[str, int]] = {}
    rows = catalog.get("items", [])
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            continue
        try:
            export_id, worth_id, worth_num = (int(value) for value in row)
        except (TypeError, ValueError):
            continue
        export_key = str(export_id)
        if (export_id <= 0 or worth_id <= 0 or worth_num <= 0
                or export_key not in ROGUELIKE_EXPORT_ITEMS
                and export_key not in ROGUELIKE_CHIJI_EXPORTS):
            continue
        result[export_key] = {"worth_id": worth_id, "worth_num": worth_num}
    return result


ROGUELIKE_SHOP_PRICES = load_roguelike_shop_prices()


def roguelike_search_point_item_id(chapter_id: int) -> int:
    """Mirror the client's verified string2int32("GSNN") search-point ID."""
    chapter_id = int(chapter_id)
    suffix = "XX" if chapter_id == -1 else f"{chapter_id:02d}"
    return int.from_bytes(f"GS{suffix}".encode("ascii"), "big", signed=False)


LOTTERY_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "lottery_catalog.json"


def load_lottery_catalog() -> dict[str, object]:
    """Load the chance and lottery rows captured from the shipped client tables."""
    try:
        data = json.loads(LOTTERY_CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"metadata": {}, "lotteries": [], "chance_rows": {}}
    if not isinstance(data, dict):
        return {"metadata": {}, "lotteries": [], "chance_rows": {}}
    lotteries = data.get("lotteries", [])
    chance_rows = data.get("chance_rows", {})
    if not isinstance(lotteries, list) or not isinstance(chance_rows, dict):
        return {"metadata": {}, "lotteries": [], "chance_rows": {}}
    return data


LOTTERY_CATALOG = load_lottery_catalog()
LOTTERY_CONFIGS = {
    int(row["LotteryId"]): row for row in LOTTERY_CATALOG.get("lotteries", [])
    if isinstance(row, dict) and int(row.get("LotteryId", 0)) > 0
}
LOTTERY_CHANCE_ROWS = {
    int(box_id): rows for box_id, rows in LOTTERY_CATALOG.get("chance_rows", {}).items()
    if isinstance(rows, list)
}
LOTTERY_ITEM_ROWS = {
    int(row["ItemId"]): row for row in LOTTERY_CATALOG.get("items", [])
    if isinstance(row, dict) and int(row.get("ItemId", 0)) > 0
}


def lottery_pool_is_open(config: dict[str, object], now: datetime | None = None) -> bool:
    """Apply the recovered LuckyLottery Begin/End window; blank means unbounded."""
    now = now or datetime.now(timezone.utc)

    def parse_bound(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # An unknown schedule encoding is not guessed open.
            raise ValueError("unsupported LuckyLottery date format")
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    try:
        begin = parse_bound(config.get("Begin"))
        end = parse_bound(config.get("End"))
    except (TypeError, ValueError):
        return False
    now = now.astimezone(timezone.utc)
    return not ((begin is not None and now < begin)
                or (end is not None and now >= end))


def normalize_lottery_info(raw: object = None) -> dict[str, object]:
    """Normalize the per-player values encoded by PlayerData.LotteryInfo."""
    saved = raw if isinstance(raw, dict) else {}
    source_lotteries = saved.get("lotteries", {})
    if not isinstance(source_lotteries, dict):
        source_lotteries = {}
    lotteries: dict[str, dict[str, object]] = {}
    for lottery_id, config in LOTTERY_CONFIGS.items():
        source = source_lotteries.get(str(lottery_id), source_lotteries.get(lottery_id, {}))
        if not isinstance(source, dict):
            source = {}
        source_rarity = source.get("rarity_times", {})
        if not isinstance(source_rarity, dict):
            source_rarity = {}
        rarity_times: dict[str, int] = {}
        for allowance in config.get("Allowances") or []:
            rarity = int(allowance.get("rarity", 0))
            if rarity > 0:
                value = source_rarity.get(str(rarity), source_rarity.get(rarity, 0))
                rarity_times[str(rarity)] = max(0, int(value))
        lotteries[str(lottery_id)] = {
            "allowance": max(0, int(source.get("allowance", 0))),
            "rarity_times": rarity_times,
        }
    source_costs = saved.get("cost_record", {})
    if not isinstance(source_costs, dict):
        source_costs = {}
    # The client reads CostRecord[Cost.ID] before every draw and sends that
    # value as CS_Lottery.hasCost. Seed every configured currency key, not only
    # the ticket key, so Gold-pool requests carry the same replay counter.
    cost_record = {
        str(int((config.get("Cost") or {}).get("ID", 0))): 0
        for config in LOTTERY_CONFIGS.values()
        if int((config.get("Cost") or {}).get("ID", 0)) > 0
    }
    for key, value in source_costs.items():
        try:
            item_id, count = int(key), max(0, int(value))
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            cost_record[str(item_id)] = count
    return {"lotteries": lotteries, "cost_record": cost_record}


def choose_lottery_row(rows: list[dict[str, object]],
                       inventory_items: dict[int, int],
                       part_fragments: dict[int, int] | None = None,
                       part_levels: dict[int, int] | None = None) -> dict[str, object] | None:
    """Choose one client-table row, applying its owned-item weight when applicable."""
    part_fragments = part_fragments or {}
    part_levels = part_levels or {}
    candidates: list[tuple[dict[str, object], int]] = []
    for row in rows:
        reward = row.get("reward", {})
        if not isinstance(reward, dict):
            continue
        item_id = int(reward.get("ID", 0))
        quantity = int(reward.get("Num", 0))
        if item_id <= 0 or quantity <= 0:
            continue
        owns_item = (
            int(inventory_items.get(item_id, inventory_items.get(str(item_id), 0))) > 0
            or int(part_fragments.get(item_id, part_fragments.get(str(item_id), 0))) > 0
            or int(part_levels.get(item_id, part_levels.get(str(item_id), 0))) > 0
        )
        raw_weight = row.get("OwnedPvalue") if owns_item else row.get("Pvalue")
        weight = max(0, int(raw_weight or 0))
        candidates.append((row, weight))
    total = sum(weight for _, weight in candidates)
    if total <= 0:
        # Some client rows omit OwnedPvalue. Use Pvalue as a safe fallback for
        # the entire pool so a profile can still receive a valid reward.
        candidates = []
        for row in rows:
            reward = row.get("reward", {})
            if not isinstance(reward, dict):
                continue
            if int(reward.get("ID", 0)) <= 0 or int(reward.get("Num", 0)) <= 0:
                continue
            candidates.append((row, max(0, int(row.get("Pvalue", 0) or 0))))
        total = sum(weight for _, weight in candidates)
    if total <= 0:
        return None
    roll = secrets.randbelow(total)
    for row, weight in candidates:
        if roll < weight:
            return row
        roll -= weight
    return candidates[-1][0]


def resolve_lottery_reward(lottery_id: int,
                           inventory_items: dict[int, int],
                           lottery_state: dict[str, object],
                           part_fragments: dict[int, int] | None = None,
                           part_levels: dict[int, int] | None = None) -> dict[str, int] | None:
    """Resolve one pool draw through its original nested Chance rows."""
    config = LOTTERY_CONFIGS.get(int(lottery_id))
    if config is None:
        return None
    lottery_map = lottery_state.get("lotteries", {})
    current = lottery_map.get(str(lottery_id), {}) if isinstance(lottery_map, dict) else {}
    if not isinstance(current, dict):
        current = {}
    rarity_counts = current.get("rarity_times", {})
    if not isinstance(rarity_counts, dict):
        rarity_counts = {}

    allowances = [row for row in (config.get("Allowances") or [])
                  if int(row.get("rarity", 0)) > 0 and int(row.get("max", 0)) > 0]
    forced = next((row for row in sorted(allowances,
                                        key=lambda item: int(item.get("rarity", 0)),
                                        reverse=True)
                   if int(rarity_counts.get(str(int(row["rarity"])), 0))
                   >= int(row["max"]) - 1), None)
    if forced:
        tier_box = int(forced["BoxId"])
    else:
        pool_id = int(config.get("PoolBoxId", 0))
        pool_row = choose_lottery_row(
            LOTTERY_CHANCE_ROWS.get(pool_id, []), inventory_items,
            part_fragments, part_levels,
        )
        if pool_row is None:
            return None
        reward = pool_row.get("reward", {})
        tier_box = int(reward.get("ID", 0)) if isinstance(reward, dict) else 0
        if tier_box <= 0:
            return None

    tier_rows = LOTTERY_CHANCE_ROWS.get(tier_box, [])
    item_row = choose_lottery_row(
        tier_rows, inventory_items, part_fragments, part_levels,
    ) if tier_rows else None
    if item_row is None:
        if not forced and 'pool_row' in locals():
            item_row = pool_row
            tier_box = int(pool_row.get("BoxID", config.get("PoolBoxId", 0)))
        else:
            return None
    reward = item_row.get("reward", {})
    if not isinstance(reward, dict):
        return None
    item_id, quantity = int(reward.get("ID", 0)), int(reward.get("Num", 0))
    item = LOTTERY_ITEM_ROWS.get(item_id)
    if item_id <= 0 or quantity <= 0 or item is None:
        return None
    tier_rarity = next((int(row["rarity"]) for row in allowances
                        if int(row.get("BoxId", 0)) == tier_box), 0)
    return {
        "item_id": item_id,
        "num": quantity,
        "box_id": tier_box,
        "tier_rarity": tier_rarity,
    }


def default_lineups() -> dict[str, dict[str, dict[str, int]]]:
    """Three teams, each with the client's Behemoth/Athena/Kaguyahime lineup."""
    return {
        str(lineup_index): {
            str(toy_index): {
                slot: part_id
                for slot, part_id in zip(LINEUP_PART_FIELDS, assembly)
            }
            for toy_index, assembly in enumerate(STARTER_BLADE_ASSEMBLIES, start=1)
        }
        for lineup_index in range(1, 4)
    }


def normalize_lineups(raw: object) -> dict[str, dict[str, dict[str, int]]]:
    result = default_lineups()
    if not isinstance(raw, dict):
        return result
    for lineup_index in range(1, 4):
        source_team = raw.get(str(lineup_index), raw.get(lineup_index))
        if not isinstance(source_team, dict):
            continue
        for toy_index in range(1, 4):
            source_toy = source_team.get(str(toy_index), source_team.get(toy_index))
            if not isinstance(source_toy, dict):
                continue
            for slot in LINEUP_PART_FIELDS:
                try:
                    part_id = int(source_toy.get(slot, result[str(lineup_index)][str(toy_index)][slot]))
                except (TypeError, ValueError):
                    continue
                if part_id > 0:
                    result[str(lineup_index)][str(toy_index)][slot] = part_id
    # Earlier local profiles were initialized by repeating Behemoth in every
    # slot. Migrate only that exact generated default; any customized lineup is
    # preserved. Re-running normalization after migration is idempotent.
    legacy_default = {
        str(lineup_index): {
            str(toy_index): {"top": 2001, "mid": 2002, "bot": 2003}
            for toy_index in range(1, 4)
        }
        for lineup_index in range(1, 4)
    }
    if result == legacy_default:
        return default_lineups()
    return result


def merge_lineup_changes(shared_lineups: object, local_lineups: object,
                         persisted_lineups: object) -> tuple[dict, list[dict]]:
    """Apply only this connection's lineup edits to the newest shared profile."""
    merged = normalize_lineups(shared_lineups)
    local = normalize_lineups(local_lineups)
    baseline = normalize_lineups(
        persisted_lineups if isinstance(persisted_lineups, dict) else shared_lineups
    )
    changed: list[dict] = []
    for lineup_index in range(1, 4):
        team_key = str(lineup_index)
        for toy_index in range(1, 4):
            toy_key = str(toy_index)
            for slot in LINEUP_PART_FIELDS:
                value = local[team_key][toy_key][slot]
                if value == baseline[team_key][toy_key][slot]:
                    continue
                merged[team_key][toy_key][slot] = value
                changed.append({"lineup_index": lineup_index,
                                "toy_index": toy_index,
                                "slot": slot,
                                "part_id": value})
    return merged, changed


def normalize_pve_info(raw_info: object) -> dict[str, object]:
    """Normalize the client's durable idle-PvE state from PlayerData.PveInfo."""
    raw = raw_info if isinstance(raw_info, dict) else {}
    integer_fields = {
        "Process": (0, max(GUAJI_PVE_ROWS, default=0)),
        "BlessPart": (0, 2**31 - 1),
        "LastCalcTime": (0, 2**31 - 1),
        "Gold": (0, 2**31 - 1),
        "FreeRewardTimes": (0, 2**31 - 1),
        "GoldRewardConfig": (0, 2**31 - 1),
        "MaxCanCalcTime": (0, 2**31 - 1),
        "NextCanSmashTime": (0, 2**31 - 1),
        "DailyHasSmashTimes": (0, 2**31 - 1),
    }
    result: dict[str, object] = {}
    for field, (minimum, maximum) in integer_fields.items():
        try:
            value = int(raw.get(field, 0))
        except (TypeError, ValueError):
            value = 0
        result[field] = max(minimum, min(maximum, value))
    for field in ("PartRewards", "PartRewardsConfig"):
        values = raw.get(field, {})
        if isinstance(values, list):
            values = {str(index + 1): value for index, value in enumerate(values)}
        if not isinstance(values, dict):
            values = {}
        normalized = {}
        for key, value in values.items():
            try:
                index, quantity = int(key), int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= 8 and quantity >= 0:
                normalized[index] = min(2**31 - 1, quantity)
        result[field] = normalized
    return result


GUAJI_MAX_OVERLAP_SECONDS = 43_200
GUAJI_GOLD_REWARD_SECONDS_PER_UNIT = 3_600
GUAJI_PART_SAVE_RATIO = 100_000
GUAJI_PART_REWARD_RARITIES = 6


def calculate_guaji_idle_reward_data(elapsed_seconds: int, pve_info: object) -> dict[str, object]:
    """Match the client's GuajiData reward projection for supplied server rates.

    Runtime evidence from the 1.1.0.96 client shows gold accrues at
    GoldRewardConfig units per hour and part progress accrues against a
    100,000-unit fixed-point scale. Both projections cap elapsed time at 12h.
    This helper does not choose rates or grant items; the backend must still
    receive those server-owned values from verified profile/config data.
    """
    state = normalize_pve_info(pve_info)
    try:
        elapsed = max(0, min(GUAJI_MAX_OVERLAP_SECONDS, int(elapsed_seconds)))
    except (TypeError, ValueError):
        elapsed = 0
    gold_rate_per_hour = int(state["GoldRewardConfig"])
    gold = elapsed * gold_rate_per_hour // GUAJI_GOLD_REWARD_SECONDS_PER_UNIT

    saved_parts = state["PartRewards"]
    daily_rates = state["PartRewardsConfig"]
    part_rewards = []
    denominator = GUAJI_PART_SAVE_RATIO * 86_400
    for rarity in range(1, GUAJI_PART_REWARD_RARITIES + 1):
        saved_progress = int(saved_parts.get(rarity, 0))
        daily_rate = int(daily_rates.get(rarity, 0))
        accumulated = saved_progress * 86_400 + daily_rate * GUAJI_PART_SAVE_RATIO * elapsed
        part_rewards.append(accumulated // denominator)
    return {"Gold": gold, "Part": part_rewards}


def merge_guaji_pve_attempts(existing: object, incoming: object) -> dict[str, object]:
    """Keep the newest stage attempt and never downgrade a completed result."""
    candidates = []
    for source in (existing, incoming):
        if not isinstance(source, dict):
            continue
        try:
            process = int(source.get("process", 0))
            attempt_id = int(source.get("attempt_id", 0))
        except (TypeError, ValueError):
            continue
        status = str(source.get("status", ""))
        fingerprint = str(source.get("fingerprint", ""))[:128]
        if process > 0 and process in GUAJI_PVE_ROWS and attempt_id > 0 \
                and status in ("started", "completed", "lost") \
                and (status != "completed" or fingerprint):
            candidates.append({"process": process, "attempt_id": attempt_id,
                               "status": status, "fingerprint": fingerprint})
    if not candidates:
        return {}
    candidates.sort(key=lambda row: (
        int(row["process"]), int(row["attempt_id"]),
        int(row["status"] == "completed"),
    ))
    return candidates[-1]


def merge_pve_info(existing: object, incoming: object) -> dict[str, object]:
    """Merge monotonic idle-PvE progress and retain the saved display fields."""
    previous = normalize_pve_info(existing)
    latest = normalize_pve_info(incoming)
    merged = dict(latest)
    merged["Process"] = max(int(previous["Process"]), int(latest["Process"]))
    # Rates and timers are server-owned display/config values. Keep a previously
    # populated value when a stale process still has the all-zero legacy state.
    for field in ("GoldRewardConfig", "MaxCanCalcTime", "NextCanSmashTime"):
        if int(merged[field]) == 0 and int(previous[field]) > 0:
            merged[field] = previous[field]
    if not merged["PartRewardsConfig"] and previous["PartRewardsConfig"]:
        merged["PartRewardsConfig"] = dict(previous["PartRewardsConfig"])
    return merged


def merge_chapter_battle_attempts(existing: object, incoming: object) -> dict[int, dict[str, object]]:
    """Merge attempt records without letting a stale process undo a completed battle."""
    merged: dict[int, dict[str, object]] = {}
    for source in (existing, incoming):
        if not isinstance(source, dict):
            continue
        for raw_stage_id, raw_record in source.items():
            try:
                stage_id = int(raw_stage_id)
                attempt_id = int(raw_record.get("attempt_id", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            stage = PVE_CHAPTER_ROWS.get(stage_id)
            if (not stage or int(stage["IndexInChapter"]) > PVE_BATTLE_STAGES_PER_CHAPTER
                    or attempt_id < 1):
                continue
            status = str(raw_record.get("status", ""))
            if status not in ("started", "completed"):
                continue
            fingerprint = str(raw_record.get("fingerprint", ""))[:128]
            if status == "completed" and not fingerprint:
                continue
            candidate = {"attempt_id": attempt_id, "status": status,
                         "fingerprint": fingerprint}
            previous = merged.get(stage_id)
            if previous is None or attempt_id > int(previous["attempt_id"]):
                merged[stage_id] = candidate
            elif attempt_id == int(previous["attempt_id"]):
                if previous["status"] != "completed" and status == "completed":
                    merged[stage_id] = candidate
    return merged


def normalize_save_boxes(raw_boxes: object) -> dict[str, dict[str, object]]:
    """Normalize BaseInfo.SaveBoxList records without discarding occupied slots."""
    if isinstance(raw_boxes, list):
        rows = {str(index): row for index, row in enumerate(raw_boxes)}
    elif isinstance(raw_boxes, dict):
        rows = raw_boxes
    else:
        return {}
    result: dict[str, dict[str, object]] = {}
    for raw_key, raw_box in rows.items():
        try:
            key = int(raw_key)
            if not isinstance(raw_box, dict):
                continue
            item_id = int(raw_box.get("item_id", raw_box.get("ItemID", 0)))
            if key < 0 or item_id <= 0:
                continue
            result[str(key)] = {
                "item_id": item_id,
                "can_open_time": max(0, int(raw_box.get(
                    "can_open_time", raw_box.get("CanOpenTime", 0),
                ))),
                "ready_countdown_time": max(0, int(raw_box.get(
                    "ready_countdown_time", raw_box.get("ReadyCountDownTime", 0),
                ))),
                "reason": str(raw_box.get("reason", raw_box.get("Reason", "")))[:64],
                "pile": max(1, int(raw_box.get("pile", raw_box.get("Pile", 1)))),
            }
        except (TypeError, ValueError):
            continue
    return result


def refresh_save_box_timers(app, *, now: int | None = None) -> bool:
    """Promote queued boxes whose absolute queue-start time has arrived."""
    now = game_time_seconds() if now is None else int(now)
    boxes = normalize_save_boxes(getattr(app, "save_boxes", {}))
    changed = False
    for key, box in boxes.items():
        queued_at = int(box["ready_countdown_time"])
        if queued_at <= 0 or queued_at > now:
            continue
        item = BOX_ITEM_ROWS.get(int(box["item_id"]))
        if not item or int(item.get("itemType", 0)) != 6:
            continue
        box["can_open_time"] = queued_at + max(0, int(item.get("DelayOpenTime", 0)))
        box["ready_countdown_time"] = 0
        changed = True
    app.save_boxes = boxes
    return changed


def _chance_level_for_score(score: int) -> int:
    score = max(0, int(score))
    if score <= 5:
        return 1
    if score <= 10:
        return 2
    if score <= 15:
        return 3
    if score <= 20:
        return 4
    if score <= 25:
        return 5
    if score <= 30:
        return 6
    return 7


def _weighted_chance_row(rows: list[dict[str, object]], app,
                         draw=secrets.randbelow) -> dict[str, object] | None:
    """Community draw rule: Pvalue/OwnedPvalue weights within a RandomIndex."""
    candidates: list[tuple[dict[str, object], int]] = []
    for row in rows:
        reward = row.get("reward")
        if not isinstance(reward, dict):
            continue
        reward_id = int(reward.get("ID", 0))
        owns = (int(getattr(app, "inventory_items", {}).get(reward_id, 0)) > 0
                or int(getattr(app, "part_fragments", {}).get(reward_id, 0)) > 0
                or int(getattr(app, "part_levels", {}).get(reward_id, 0)) > 0)
        weight = max(0, int(row.get("OwnedPvalue" if owns else "Pvalue", 0) or 0))
        if reward_id > 0:
            candidates.append((row, weight))
    total = sum(weight for _, weight in candidates)
    if total <= 0:
        # Match the backend's existing lottery compatibility rule: some rows
        # have no OwnedPvalue weight. Falling back to Pvalue avoids a valid
        # configured pool becoming unopenable for an owner of every reward.
        candidates = [
            (row, max(0, int(row.get("Pvalue", 0) or 0)))
            for row, _ in candidates
        ]
        total = sum(weight for _, weight in candidates)
    if total <= 0:
        return None
    roll = int(draw(total))
    for row, weight in candidates:
        if roll < weight:
            return row
        roll -= weight
    return candidates[-1][0]


def resolve_configured_box_rewards(app, box_id: int, *, race_score: int | None = None,
                                   draw=secrets.randbelow) -> list[dict[str, int]] | None:
    """Resolve Chance rows by RandomIndex with a local weighted nested-pool rule.

    The tables and the client's score gates/NumRank selector are recovered.
    The original server's random draw algorithm is unavailable; independently
    drawing once for each RandomIndex and using Pvalue/OwnedPvalue is an
    explicitly community compatibility choice, not a claim of original rates.
    """
    box_id = int(box_id)
    if race_score is None:
        state = getattr(app, "championship_state", {})
        race_score = int(state.get("race_score", 0)) if isinstance(state, dict) else 0
    # The persisted community profile may keep a fresh player's score at 0,
    # but make_champion_history advertises the client's valid rank-key floor
    # (1). Chance.Level is checked against that same client-visible RaceScore
    # in the recovered box UI. Use the wire value here too, or a fresh player
    # sees only I203's Level=0 Gold pool while all Level=1 part pools vanish.
    score = champion_client_race_score(race_score)
    rank = _chance_level_for_score(score)
    inventory = getattr(app, "inventory_items", {})
    part_fragments = getattr(app, "part_fragments", {})
    part_levels = getattr(app, "part_levels", {})

    def resolve(pool_id: int, stack: tuple[int, ...]) -> list[dict[str, int]] | None:
        if pool_id in stack:
            return None
        pool_rows = BOX_CHANCE_ROWS.get(pool_id, [])
        if not pool_rows:
            return None
        groups: dict[int, list[dict[str, object]]] = {}
        for row in pool_rows:
            if int(row.get("Level", 0)) <= score:
                groups.setdefault(int(row.get("RandomIndex", 0)), []).append(row)
        rewards: list[dict[str, int]] = []
        for random_index in sorted(groups):
            selected = _weighted_chance_row(groups[random_index], app, draw=draw)
            if selected is None:
                return None
            reward = selected.get("reward")
            if not isinstance(reward, dict):
                return None
            reward_id = int(reward.get("ID", 0))
            quantity = max(0, int(selected.get(f"NumRank{rank}", 0) or 0))
            if quantity <= 0:
                continue
            # A Chance reward whose ID is itself a configured chance pool
            # represents nested contents, as the client's chest preview does.
            if reward_id in BOX_CHANCE_ROWS:
                for _ in range(quantity):
                    nested = resolve(reward_id, (*stack, pool_id))
                    if nested is None:
                        return None
                    rewards.extend(nested)
                continue
            item = BOX_ITEM_META.get(reward_id)
            if item is None:
                return None
            item_type = int(item.get("itemType", -1))
            if item_type not in (0, 1, 2, 3, 4):
                # Do not consume a box when the table yields a reward type that
                # this backend cannot preserve in the player's real schema.
                return None
            rewards.append({"item_id": reward_id, "num": quantity,
                            "item_type": item_type})
        return rewards if rewards else None

    return resolve(box_id, ())


def _box_paid_open_cost(seconds: int) -> int:
    """Mirror recovered client ratios: free under 600s, then 1 diamond/1200s."""
    remaining = max(0, int(seconds))
    if remaining < 600:
        return 0
    return (remaining + 1199) // 1200


def ensure_first_chest_shuriken(
    app, rewards: list[dict[str, int]],
) -> tuple[list[dict[str, int]], bool] | None:
    """Apply the local tutorial guarantee once, after a valid reward was resolved.

    Item 2011 is the tutorial's middle-slot component. Normal box drops remain
    fragments; this one-time tutorial exception grants an owned component at
    its recovered ItemUpgrade.initLevel so it is immediately selectable.
    Returning None means the required recovered config is unavailable.
    """
    if bool(getattr(app, "first_chest_shuriken_claimed", False)):
        return rewards, False
    part_id = FIRST_CHEST_SHURIKEN_PART_ID
    levels = getattr(app, "part_levels", None)
    if not isinstance(levels, dict):
        levels = {}
        app.part_levels = levels
    # Legacy profiles may represent an owned catalog part in DefaultItems
    # instead of part_levels. Use the same ownership rule as serialization and
    # lineup validation so that such an account is not granted a second usable
    # Shuriken by the one-time guarantee.
    current_level = max(0, int(owned_part_levels(
        getattr(app, "inventory_items", {}), levels,
    ).get(part_id, 0)))
    if current_level <= 0:
        upgrade_key = PART_UPGRADE_KEYS.get(part_id)
        upgrade = ITEM_UPGRADE_ROWS.get(upgrade_key or 0)
        item = BOX_ITEM_META.get(part_id)
        if upgrade is None or item is None or int(item.get("itemType", -1)) != 1:
            return None
        owned_level = int(upgrade.get("initLevel", 0))
        if owned_level < 1:
            return None
        # Do not count a random fragment of the same component as a second
        # copy on this opening. The first-open exception is one usable part.
        rewards = [reward for reward in rewards
                   if int(reward.get("item_id", 0)) != part_id]
        levels[part_id] = owned_level
        rewards.append({"item_id": part_id, "num": 1, "item_type": 1})
        newly_owned = True
    else:
        newly_owned = False
    app.first_chest_shuriken_claimed = True
    return rewards, newly_owned


def apply_box_item_operation(app, operation: int, item_id: int, param1: int = 0,
                             param2: int = 0, *, now: int | None = None,
                             draw=secrets.randbelow) -> dict[str, object]:
    """Apply supported ItemOpr box actions to a profile without partial grants."""
    now = game_time_seconds() if now is None else int(now)
    operation, item_id = int(operation), int(item_id)
    param1, param2 = int(param1), int(param2)
    item = BOX_ITEM_ROWS.get(item_id)
    if item is None:
        return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
    item_type = int(item.get("itemType", 0))

    if operation in (ITEM_OPR_BOX_COUNTDOWN, ITEM_OPR_BOX_READY_OPEN,
                     ITEM_OPR_CHANGE_READY_BOX, ITEM_OPR_REDUCE_BOX_COUNTDOWN):
        if item_type != 6:
            return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
        refresh_save_box_timers(app, now=now)
        boxes = normalize_save_boxes(getattr(app, "save_boxes", {}))
        key = str(param1)
        box = boxes.get(key)
        if box is None or int(box["item_id"]) != item_id:
            return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
        if operation == ITEM_OPR_BOX_COUNTDOWN:
            if int(box["can_open_time"]) > now or int(box["ready_countdown_time"]) > now:
                return {"item_opr_ret": ITEM_OPR_RET_COUNTING_DOWN, "changed": False}
            if any(other_key != key and int(other["can_open_time"]) > now
                   for other_key, other in boxes.items()):
                return {"item_opr_ret": ITEM_OPR_RET_COUNTING_DOWN, "changed": False}
            box["can_open_time"] = now + max(0, int(item.get("DelayOpenTime", 0)))
            box["ready_countdown_time"] = 0
            app.save_boxes = boxes
            return {"item_opr_ret": ITEM_OPR_RET_SUCCEED, "changed": True}
        if operation == ITEM_OPR_BOX_READY_OPEN:
            if int(box["can_open_time"]) > now or int(box["ready_countdown_time"]) > now:
                return {"item_opr_ret": ITEM_OPR_RET_BOX_HAVE_READY_OPEN, "changed": False}
            finishes = [int(other["can_open_time"]) for other_key, other in boxes.items()
                        if other_key != key and int(other["can_open_time"]) > now]
            finishes.extend(
                int(other["ready_countdown_time"])
                + max(0, int(BOX_ITEM_ROWS.get(int(other["item_id"]), {}).get("DelayOpenTime", 0)))
                for other_key, other in boxes.items()
                if other_key != key and int(other["ready_countdown_time"]) > now
            )
            box["can_open_time"] = 0
            box["ready_countdown_time"] = max([now, *finishes])
            app.save_boxes = boxes
            return {"item_opr_ret": ITEM_OPR_RET_BOX_READY_OK, "changed": True}
        if operation == ITEM_OPR_CHANGE_READY_BOX:
            other = boxes.get(str(param2))
            if (other is None or other is box or int(other["ready_countdown_time"]) <= now
                    or int(box["ready_countdown_time"]) <= now):
                return {"item_opr_ret": 43, "changed": False}
            box["ready_countdown_time"], other["ready_countdown_time"] = (
                int(other["ready_countdown_time"]), int(box["ready_countdown_time"]),
            )
            app.save_boxes = boxes
            return {"item_opr_ret": ITEM_OPR_RET_CHANGE_READY_OK, "changed": True}
        # The ad callback's actual time reduction is a server/talent rule that
        # the recovered client does not expose in this request. Never fabricate
        # an ad-based reduction amount.
        return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR, "changed": False,
                "reason": "ad_speedup_amount_not_established"}

    if operation != ITEM_OPR_USE_ITEM:
        return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR, "changed": False}

    if item_type == 6:
        refresh_save_box_timers(app, now=now)
        boxes = normalize_save_boxes(getattr(app, "save_boxes", {}))
        key = str(param1)
        box = boxes.get(key)
        if box is None or int(box["item_id"]) != item_id:
            return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
        is_paid_open = not bool(param2)
        if is_paid_open:
            if int(box["can_open_time"]) > now:
                remaining = int(box["can_open_time"]) - now
            elif int(box["ready_countdown_time"]) > now:
                remaining = int(item.get("DelayOpenTime", 0))
            elif any(other_key != key and int(other["can_open_time"]) > now
                     for other_key, other in boxes.items()):
                remaining = int(item.get("DelayOpenTime", 0))
            elif int(box["can_open_time"]) > 0:
                remaining = 0
            else:
                return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
            cost = _box_paid_open_cost(remaining)
            diamonds = max(0, int(app.inventory_items.get(DIAMOND_ITEM_ID, 0)))
            if diamonds < cost:
                return {"item_opr_ret": ITEM_OPR_RET_MONEY_NOT_ENOUGH,
                        "changed": False, "money_type": ITEM_OPR_MONEY_DIAMOND,
                        "diamond_cost": cost, "diamond_before": diamonds}
        else:
            if int(box["can_open_time"]) <= 0 or int(box["can_open_time"]) > now:
                return {"item_opr_ret": ITEM_OPR_RET_COUNTING_DOWN,
                        "changed": False}
            cost = 0
            diamonds = max(0, int(app.inventory_items.get(DIAMOND_ITEM_ID, 0)))
        rewards = resolve_configured_box_rewards(app, item_id, draw=draw)
        if rewards is None:
            return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR,
                    "changed": False, "reason": "box_reward_table_unresolved"}
        guaranteed = ensure_first_chest_shuriken(app, rewards)
        if guaranteed is None:
            return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR,
                    "changed": False, "reason": "first_chest_part_config_unresolved"}
        rewards, newly_owned_shuriken = guaranteed
        if cost:
            app.inventory_items[DIAMOND_ITEM_ID] = diamonds - cost
        if int(box["pile"]) > 1:
            box["pile"] = int(box["pile"]) - 1
            removed_save_box_slots = []
        else:
            boxes.pop(key)
            removed_save_box_slots = [int(key)]
        app.save_boxes = boxes
    elif item_type == 5:
        inventory = getattr(app, "inventory_items", {})
        quantity = max(0, int(inventory.get(item_id, 0)))
        if quantity < 1:
            return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
        rewards = resolve_configured_box_rewards(app, item_id, draw=draw)
        if rewards is None:
            return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR,
                    "changed": False, "reason": "box_reward_table_unresolved"}
        guaranteed = ensure_first_chest_shuriken(app, rewards)
        if guaranteed is None:
            return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR,
                    "changed": False, "reason": "first_chest_part_config_unresolved"}
        rewards, newly_owned_shuriken = guaranteed
        inventory[item_id] = quantity - 1
        if inventory[item_id] == 0:
            inventory.pop(item_id, None)
        app.inventory_items = inventory
    else:
        return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}

    for reward in rewards:
        reward_id = int(reward["item_id"])
        if newly_owned_shuriken and reward_id == FIRST_CHEST_SHURIKEN_PART_ID:
            # The guaranteed ItemInfo is a whole usable part at initLevel;
            # don't also count it as a fragment.
            continue
        credit_box_reward(app, reward_id, int(reward["num"]))
    save_profile_state(app)
    return {"item_opr_ret": ITEM_OPR_RET_USE_OK, "changed": True,
            "rewards": rewards, "diamond_cost": cost if item_type == 6 else 0,
            "removed_save_box_slots": (removed_save_box_slots
                                        if item_type == 6 else [])}


def load_profile_state(path: Path, player_id: int) -> dict[str, object]:
    """Load this synthetic local player's inventory and Adventure state."""
    profile_exists = path.is_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict) or int(saved.get("player_id", player_id)) != player_id:
        saved = {}
    daily_gold_day = max(-1, int(saved.get("daily_gold_day", -1)))
    daily_gold_earned = max(0, int(saved.get("daily_gold_earned", 0)))
    current_daily_day = game_local_day_index()
    if daily_gold_day < current_daily_day:
        # Profiles created before this field existed have no trustworthy
        # historical counter. Start a new baseline without touching balances.
        daily_gold_day = current_daily_day
        daily_gold_earned = 0
    save_boxes = normalize_save_boxes(saved.get("save_boxes", {}))
    inventory = saved.get("inventory_items", {})
    if not isinstance(inventory, dict):
        inventory = {}
    inventory_items = {
        int(item_id): max(0, int(quantity))
        for item_id, quantity in inventory.items()
        if int(item_id) > 0 and int(quantity) > 0
    }
    raw_part_levels = saved.get("part_levels", {})
    if not isinstance(raw_part_levels, dict):
        raw_part_levels = {}
    part_levels = {
        int(part_id): max(1, min(15, int(level)))
        for part_id, level in raw_part_levels.items()
        if int(part_id) in UPGRADEABLE_PART_LOCATIONS and int(level) > 0
    }
    # A usable part is represented in the client BaseInfo part list with a
    # positive Level. If an older profile already owns tutorial part 2011,
    # treat the guarantee as satisfied rather than duplicating it later.
    first_chest_shuriken_claimed = bool(
        saved.get("first_chest_shuriken_claimed", False)
        or part_levels.get(FIRST_CHEST_SHURIKEN_PART_ID, 0) > 0
    )
    recorded_part_levels = dict(part_levels)
    # Existing schema-8 profiles relied on implicit level-10 starter parts.
    # Persist those levels during migration so changing the fresh-profile
    # default cannot silently downgrade an existing player's equipment.
    schema_version = max(0, int(saved.get("schema_version", 0)))
    if profile_exists and schema_version < PROFILE_SCHEMA_VERSION:
        for part_id, _part_as in SYNTHETIC_STARTER_PARTS:
            part_levels.setdefault(part_id, LEGACY_SYNTHETIC_STARTER_LEVEL)
    search_points = saved.get("roguelike_search_points", {})
    if not isinstance(search_points, dict):
        search_points = {}
    search_points = {
        int(item_id): max(0, int(quantity))
        for item_id, quantity in search_points.items()
        if int(item_id) > 0 and int(quantity) > 0
    }
    map_state = saved.get("roguelike_map_state")
    if isinstance(map_state, dict):
        rows = map_state.get("nodes", {})
        normalized_nodes = {}
        if isinstance(rows, dict):
            for location, raw_row in rows.items():
                if not isinstance(raw_row, dict):
                    continue
                try:
                    location_id = int(location)
                except (TypeError, ValueError):
                    continue
                row = dict(raw_row)
                row["status"] = max(0, min(3, int(row.get("status", 0))))
                row["event_id"] = max(0, int(row.get("event_id", 0)))
                exports = row.get("location_exports", [])
                row["location_exports"] = [
                    int(value) for value in exports
                    if str(value).lstrip("-").isdigit() and int(value) > 0
                ] if isinstance(exports, list) else []
                if row.get("export_bit") is not None:
                    row["export_bit"] = max(0, int(row.get("export_bit", 0)))
                records = row.get("shop_purchase_records", {})
                row["shop_purchase_records"] = records if isinstance(records, dict) else {}
                normalized_nodes[location_id] = row
        map_state["nodes"] = normalized_nodes
    else:
        map_state = None
    local_purchases = saved.get("local_purchases", {})
    if not isinstance(local_purchases, dict):
        local_purchases = {}
    raw_part_fragments = saved.get("part_fragments")
    if isinstance(raw_part_fragments, dict):
        part_fragments = {
            int(item_id): max(0, int(quantity))
            for item_id, quantity in raw_part_fragments.items()
            if int(item_id) in ALL_SERIALIZED_PART_LOCATIONS and int(quantity) > 0
        }
    else:
        # Migrate old profiles where Adventure parts were stored as generic
        # inventory items. Direct part purchases already create an owned-part
        # row, so subtract their recorded grants from the fragment balance.
        purchased_parts: dict[int, int] = {}
        processed = local_purchases.get("processed_nonces", {})
        if isinstance(processed, dict):
            for record in processed.values():
                if not isinstance(record, dict):
                    continue
                for item in record.get("granted_items", []) or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_id, quantity = int(item.get("item_id", 0)), int(item.get("num", 0))
                    except (TypeError, ValueError):
                        continue
                    if item_id in UPGRADEABLE_PART_LOCATIONS and quantity > 0:
                        purchased_parts[item_id] = purchased_parts.get(item_id, 0) + quantity
        part_fragments = {}
        for item_id in UPGRADEABLE_PART_LOCATIONS:
            quantity = max(0, int(inventory_items.get(item_id, 0))
                           - int(purchased_parts.get(item_id, 0)))
            if quantity:
                part_fragments[item_id] = quantity
    # Compatibility migration for profiles that already persisted positive
    # part fragments as Level=0: one valid part fragment establishes basic
    # ownership, while the full fragment count remains available for upgrades.
    for part_id, quantity in part_fragments.items():
        if quantity > 0 and part_id in UPGRADEABLE_PART_LOCATIONS:
            acquired_level = part_acquisition_level(part_id)
            part_levels[part_id] = max(int(part_levels.get(part_id, 0)), acquired_level)
    raw_exchange_claims = saved.get("task_exchange_claims", [])
    exchange_claims = sorted({int(value) for value in raw_exchange_claims
                              if int(value) > 0}) if isinstance(raw_exchange_claims, list) else []
    level_task_state = saved.get("level_task_state", {})
    if not isinstance(level_task_state, dict):
        level_task_state = {}
    level_task_state = {
        str(int(task_id)): {
            "process": max(0, int(row.get("process", 0))),
            "status": max(0, min(3, int(row.get("status", 1)))),
        }
        for task_id, row in level_task_state.items()
        if str(task_id).lstrip("-").isdigit() and isinstance(row, dict)
    }
    progression_counters = saved.get("progression_counters", {})
    if not isinstance(progression_counters, dict):
        progression_counters = {}
    progression_counters = {
        str(key): max(0, int(value))
        for key, value in progression_counters.items()
        if isinstance(value, (int, float))
    }
    level = max(1, min(40, int(saved.get("level", 1))))
    exp = max(0, int(saved.get("exp", 0)))
    power = max(0, int(saved.get("power", 0)))
    achievement_claimed = bool(saved.get("achievement_claimed", False))
    achievement_reward_applied = bool(saved.get("achievement_reward_applied", False))
    # Migrate profiles from the old achievement handler, which marked the
    # achievement claimed but sent a hard-coded five-gem snapshot without
    # recording that reward in the account inventory.
    if achievement_claimed and not achievement_reward_applied:
        inventory_items[1227894834] = inventory_items.get(1227894834, 0) + 5
        achievement_reward_applied = True
    # Older builds could record the LV01 exchange/chest while the missing
    # progression row caused its five GP to be omitted. Repair only profiles
    # that already hold the one-time claim, preserving any higher GP total and
    # never granting the chest a second time.
    if 1280716849 in exchange_claims and level == 1 and power == 0:
        level, power = 2, 5
    elif 1280716849 in exchange_claims and level >= 2:
        power = max(power, 5)
    task_exchange_claims = list(exchange_claims)
    tutorial_battle_complete = bool(saved.get("tutorial_battle_complete", False))
    guide_step = max(0, int(saved.get("guide_step", 0)))
    if profile_exists and schema_version < PROFILE_SCHEMA_VERSION:
        if guide_step & GUIDE_MARK_OVER_MASK:
            # The old backend set Over on the opening battle. The client guide
            # reports Over later, after the LV01 step and Adventure transition.
            # Claim/level evidence keeps profiles that reached later progression
            # complete; ambiguous profiles resume the guide without losing any
            # other GuideMark bits or saved player state.
            later_progress = (
                LEVEL_FIRST_REWARD_ID in exchange_claims
                or level > 1 or power > 0
                or bool(saved.get("roguelike_map_state"))
                # The client only exposes this guide row when part 2001 is
                # exactly level 1. Preserve completion for legacy level-10
                # profiles rather than trapping them on an impossible row.
                or int(part_levels.get(TUTORIAL_UPGRADE_PART_ID,
                                       LEGACY_SYNTHETIC_STARTER_LEVEL)) != 1
            )
            tutorial_battle_complete = True
            if not later_progress:
                guide_step &= ~GUIDE_MARK_OVER_MASK
        # Existing task completion derived from implicit level-10 starters is
        # not proof of an upgrade. Only explicit levels saved by a successful
        # prior upgrade carry that evidence forward.
        explicit_upgrade = any(int(value) > 1 for value in recorded_part_levels.values())
        if explicit_upgrade:
            progression_counters[FIRST_LEVEL_UPGRADE_COUNTER] = max(
                1, int(progression_counters.get(FIRST_LEVEL_UPGRADE_COUNTER, 0)),
            )
        elif LEVEL_FIRST_REWARD_ID not in exchange_claims:
            old_task = level_task_state.get(str(FIRST_LEVEL_UPGRADE_TASK_ID))
            if isinstance(old_task, dict) and int(old_task.get("status", 0)) < TASK_STATUS_REWARDED:
                level_task_state[str(FIRST_LEVEL_UPGRADE_TASK_ID)] = {
                    "process": 0, "status": TASK_STATUS_DOING,
                }
    tutorial_upgrade_resources_granted = bool(
        saved.get("tutorial_upgrade_resources_granted", False)
    )
    tutorial_active = (
        level == 1 and LEVEL_FIRST_REWARD_ID not in exchange_claims
        and not (guide_step & GUIDE_MARK_OVER_MASK)
        and int(part_levels.get(TUTORIAL_UPGRADE_PART_ID, SYNTHETIC_STARTER_LEVEL)) == 1
    )
    if not tutorial_upgrade_resources_granted and (
        not profile_exists or (profile_exists and schema_version < PROFILE_SCHEMA_VERSION
                               and tutorial_active)
    ):
        # ItemUpgrade 5001 (part 2001) requires two pieces and 20 Gold for its
        # first level. Provide that exact local tutorial budget once so the
        # guide action is possible without inventing a championship reward.
        inventory_items[GOLD_ITEM_ID] = max(
            int(inventory_items.get(GOLD_ITEM_ID, 0)), TUTORIAL_UPGRADE_GOLD,
        )
        part_fragments[TUTORIAL_UPGRADE_PART_ID] = max(
            int(part_fragments.get(TUTORIAL_UPGRADE_PART_ID, 0)),
            TUTORIAL_UPGRADE_PART_PIECES,
        )
        tutorial_upgrade_resources_granted = True
    daily_task_claimed = bool(saved.get("daily_task_claimed", False))
    task_score = max(0, int(saved.get("task_score", 0)))
    if daily_task_claimed:
        task_score = max(task_score, 10)
    raw_chapter_results = saved.get("chapter_battle_results", {})
    if not isinstance(raw_chapter_results, dict):
        raw_chapter_results = {}
    chapter_battle_results = {}
    for raw_stage_id, raw_result in raw_chapter_results.items():
        try:
            stage_id, result = int(raw_stage_id), int(raw_result)
        except (TypeError, ValueError):
            continue
        row = PVE_CHAPTER_ROWS.get(stage_id)
        if (row and int(row["IndexInChapter"]) <= PVE_BATTLE_STAGES_PER_CHAPTER
                and result & 1 and result <= 7):
            chapter_battle_results[stage_id] = result
    raw_chapter_rewards = saved.get("chapter_rewards_claimed", [])
    if isinstance(raw_chapter_rewards, dict):
        raw_chapter_rewards = [chapter for chapter, claimed in raw_chapter_rewards.items() if claimed]
    chapter_rewards_claimed = sorted({
        int(chapter_id) for chapter_id in raw_chapter_rewards
        if str(chapter_id).lstrip("-").isdigit() and 1 <= int(chapter_id) <= 10
    }) if isinstance(raw_chapter_rewards, list) else []
    chapter_battle_attempts = merge_chapter_battle_attempts(
        {}, saved.get("chapter_battle_attempts", {}),
    )
    pve_info = normalize_pve_info(saved.get("pve_info", {}))
    championship_state = normalize_championship_state(
        saved.get("championship_state"),
        default_vouchers=SYNTHETIC_CHAMPION_VOUCHERS,
    )
    guaji_pve_attempt = merge_guaji_pve_attempts(
        {}, saved.get("guaji_pve_attempt", {}),
    )
    return {
        "inventory_items": inventory_items,
        "part_fragments": part_fragments,
        "part_levels": part_levels,
        "first_chest_shuriken_claimed": first_chest_shuriken_claimed,
        "roguelike_search_points": search_points,
        "level": level,
        "exp": exp,
        "power": power,
        "task_score": task_score,
        "task_score_exchange_process": max(0, int(saved.get("task_score_exchange_process", 0))),
        "achievement_total_score": max(0, int(saved.get("achievement_total_score", 0))),
        "achievement_reward_applied": achievement_reward_applied,
        "level_task_state": level_task_state,
        "progression_counters": progression_counters,
        "roguelike_keys": max(0, min(ROGUELIKE_KEY_CAPACITY,
                                     int(saved.get("roguelike_keys", TEST_ROGUELIKE_KEYS)))),
        "roguelike_key_ad_claim_day": max(-1, int(saved.get("roguelike_key_ad_claim_day", -1))),
        "roguelike_map_state": map_state,
        "local_purchases": local_purchases,
        "daily_task_claimed": daily_task_claimed,
        "achievement_claimed": achievement_claimed,
        "lineups": normalize_lineups(saved.get("lineups")),
        # Zero keeps the original introductory tutorial available. The client
        # stores individual guide-grid completion in its own GuideGrid:* keys.
        "guide_step": guide_step,
        "tutorial_battle_complete": tutorial_battle_complete,
        "tutorial_upgrade_resources_granted": tutorial_upgrade_resources_granted,
        "task_exchange_claims": task_exchange_claims,
        "lottery_info": normalize_lottery_info(saved.get("lottery_info")),
        "chapter_battle_results": chapter_battle_results,
        "chapter_rewards_claimed": chapter_rewards_claimed,
        "chapter_battle_attempts": chapter_battle_attempts,
        "pve_info": pve_info,
        "championship_state": championship_state,
        "guaji_pve_attempt": guaji_pve_attempt,
        "daily_gold_day": daily_gold_day,
        "daily_gold_earned": daily_gold_earned,
        "save_boxes": save_boxes,
    }


def save_profile_state(app) -> None:
    """Atomically persist the disposable synthetic profile used by this backend."""
    with app.roguelike_lock:
        baseline = getattr(app, "persisted_inventory_items", dict(app.inventory_items))
        search_point_baseline = getattr(
            app, "persisted_roguelike_search_points",
            dict(getattr(app, "roguelike_search_points", {})),
        )
        part_fragment_baseline = getattr(
            app, "persisted_part_fragments", dict(getattr(app, "part_fragments", {})),
        )
        part_level_baseline = getattr(
            app, "persisted_part_levels", dict(getattr(app, "part_levels", {})),
        )
        if app.state_file.is_file():
            shared = load_profile_state(app.state_file, int(app.player_id))
            shared_inventory = dict(shared["inventory_items"])
            shared_search_points = dict(shared["roguelike_search_points"])
            shared_part_fragments = dict(shared["part_fragments"])
            shared_part_levels = dict(shared["part_levels"])
            shared_roguelike_keys = int(shared["roguelike_keys"])
            shared_roguelike_map_state = copy.deepcopy(shared["roguelike_map_state"])
            shared_pve_info = normalize_pve_info(shared["pve_info"])
            shared_purchases = shared["local_purchases"]
            shared_flags = shared
            shared_lineups = shared["lineups"]
        else:
            shared_inventory = dict(baseline)
            shared_search_points = dict(search_point_baseline)
            shared_part_fragments = dict(part_fragment_baseline)
            shared_part_levels = dict(part_level_baseline)
            shared_roguelike_keys = int(getattr(app, "roguelike_keys", TEST_ROGUELIKE_KEYS))
            shared_roguelike_map_state = copy.deepcopy(
                getattr(app, "roguelike_map_state", None),
            )
            shared_pve_info = normalize_pve_info(getattr(app, "pve_info", {}))
            shared_purchases = app.local_purchases
            shared_flags = {}
            shared_lineups = normalize_lineups(getattr(app, "lineups", None))

        # The HTTP purchase handler can update this same profile while the
        # WebSocket process is alive. Apply only this process's gameplay delta
        # to the latest disk inventory so a stale in-memory copy cannot erase
        # a purchase and a later PvE reward is still retained.
        item_ids = set(shared_inventory) | set(app.inventory_items) | set(baseline)
        merged_inventory = {}
        for item_id in item_ids:
            delta = int(app.inventory_items.get(item_id, 0)) - int(baseline.get(item_id, 0))
            quantity = max(0, int(shared_inventory.get(item_id, 0)) + delta)
            if quantity:
                merged_inventory[item_id] = quantity
        app.inventory_items = merged_inventory
        search_point_ids = (
            set(shared_search_points)
            | set(getattr(app, "roguelike_search_points", {}))
            | set(search_point_baseline)
        )
        merged_search_points = {}
        for item_id in search_point_ids:
            delta = (
                int(getattr(app, "roguelike_search_points", {}).get(item_id, 0))
                - int(search_point_baseline.get(item_id, 0))
            )
            quantity = max(0, int(shared_search_points.get(item_id, 0)) + delta)
            if quantity:
                merged_search_points[item_id] = quantity
        app.roguelike_search_points = merged_search_points
        part_fragment_ids = (
            set(shared_part_fragments)
            | set(getattr(app, "part_fragments", {}))
            | set(part_fragment_baseline)
        )
        merged_part_fragments = {}
        for item_id in part_fragment_ids:
            delta = (
                int(getattr(app, "part_fragments", {}).get(item_id, 0))
                - int(part_fragment_baseline.get(item_id, 0))
            )
            quantity = max(0, int(shared_part_fragments.get(item_id, 0)) + delta)
            if quantity:
                merged_part_fragments[item_id] = quantity
        app.part_fragments = merged_part_fragments
        part_level_ids = (
            set(shared_part_levels)
            | set(getattr(app, "part_levels", {}))
            | set(part_level_baseline)
        )
        merged_part_levels = {}
        for part_id in part_level_ids:
            if int(part_id) not in UPGRADEABLE_PART_LOCATIONS:
                continue
            level = max(
                int(shared_part_levels.get(part_id, 0)),
                int(getattr(app, "part_levels", {}).get(part_id, 0)),
                int(part_level_baseline.get(part_id, 0)),
            )
            if level > 0:
                merged_part_levels[int(part_id)] = min(15, level)
        app.part_levels = merged_part_levels
        persisted_key_baseline = int(getattr(
            app, "persisted_roguelike_keys", shared_roguelike_keys,
        ))
        key_delta = int(getattr(app, "roguelike_keys", shared_roguelike_keys)) - persisted_key_baseline
        app.roguelike_keys = max(
            0, min(ROGUELIKE_KEY_CAPACITY, shared_roguelike_keys + key_delta),
        )
        persisted_map_baseline = copy.deepcopy(getattr(
            app, "persisted_roguelike_map_state", shared_roguelike_map_state,
        ))
        local_map_state = copy.deepcopy(getattr(app, "roguelike_map_state", None))
        if local_map_state == persisted_map_baseline:
            # An unrelated save from a stale socket must not revert a newer map.
            merged_map_state = shared_roguelike_map_state
        elif shared_roguelike_map_state == persisted_map_baseline:
            # Only this connection changed the map since its last profile read.
            merged_map_state = local_map_state
        else:
            # Conflicting map changes are resolved in favor of the newest disk
            # state. Map-mutating handlers refresh under the account lock first,
            # so this only discards an obsolete concurrent write.
            merged_map_state = shared_roguelike_map_state
        app.roguelike_map_state = copy.deepcopy(merged_map_state)
        app.roguelike_key_ad_claim_day = max(
            int(shared_flags.get("roguelike_key_ad_claim_day", -1)),
            int(getattr(app, "roguelike_key_ad_claim_day", -1)),
        )
        app.local_purchases = shared_purchases
        app.daily_task_claimed = bool(getattr(app, "daily_task_claimed", False) or shared_flags.get("daily_task_claimed", False))
        app.achievement_claimed = bool(getattr(app, "achievement_claimed", False) or shared_flags.get("achievement_claimed", False))
        app.level = max(int(getattr(app, "level", 1)), int(shared_flags.get("level", 1)))
        app.exp = max(int(getattr(app, "exp", 0)), int(shared_flags.get("exp", 0)))
        app.power = max(int(getattr(app, "power", 0)), int(shared_flags.get("power", 0)))
        app.task_score = max(int(getattr(app, "task_score", 0)), int(shared_flags.get("task_score", 0)))
        app.task_score_exchange_process = max(
            int(getattr(app, "task_score_exchange_process", 0)),
            int(shared_flags.get("task_score_exchange_process", 0)),
        )
        app.achievement_total_score = max(
            int(getattr(app, "achievement_total_score", 0)),
            int(shared_flags.get("achievement_total_score", 0)),
        )
        app.achievement_reward_applied = bool(
            getattr(app, "achievement_reward_applied", False)
            or shared_flags.get("achievement_reward_applied", False)
        )
        merged_level_tasks = dict(shared_flags.get("level_task_state", {}))
        for task_id, task_row in getattr(app, "level_task_state", {}).items():
            previous = merged_level_tasks.get(str(task_id), {})
            merged_level_tasks[str(task_id)] = {
                "process": max(int(previous.get("process", 0)), int(task_row.get("process", 0))),
                "status": max(int(previous.get("status", 0)), int(task_row.get("status", 0))),
            }
        app.level_task_state = merged_level_tasks
        merged_counters = dict(shared_flags.get("progression_counters", {}))
        for key, value in getattr(app, "progression_counters", {}).items():
            merged_counters[str(key)] = max(int(merged_counters.get(str(key), 0)), int(value))
        app.progression_counters = merged_counters
        app.task_exchange_claims = sorted(
            set(int(value) for value in shared_flags.get("task_exchange_claims", []))
            | set(int(value) for value in getattr(app, "task_exchange_claims", []))
        )
        app.lineups, _lineup_changes = merge_lineup_changes(
            shared_lineups,
            getattr(app, "lineups", shared_lineups),
            getattr(app, "persisted_lineups", shared_lineups),
        )
        merged_chapter_results = dict(shared_flags.get("chapter_battle_results", {}))
        for stage_id, result in getattr(app, "chapter_battle_results", {}).items():
            stage_id, result = int(stage_id), int(result)
            merged_chapter_results[stage_id] = max(
                int(merged_chapter_results.get(stage_id, 0)), result,
            )
        app.chapter_battle_results = {
            stage_id: result for stage_id, result in merged_chapter_results.items()
            if stage_id in PVE_CHAPTER_ROWS
            and int(PVE_CHAPTER_ROWS[stage_id]["IndexInChapter"]) <= PVE_BATTLE_STAGES_PER_CHAPTER
            and int(result) & 1 and int(result) <= 7
        }
        app.chapter_rewards_claimed = sorted(
            set(int(value) for value in shared_flags.get("chapter_rewards_claimed", []))
            | set(int(value) for value in getattr(app, "chapter_rewards_claimed", []))
        )
        app.chapter_battle_attempts = merge_chapter_battle_attempts(
            shared_flags.get("chapter_battle_attempts", {}),
            getattr(app, "chapter_battle_attempts", {}),
        )
        persisted_pve_info = normalize_pve_info(getattr(
            app, "persisted_pve_info", shared_pve_info,
        ))
        local_pve_info = normalize_pve_info(getattr(app, "pve_info", shared_pve_info))
        if local_pve_info == persisted_pve_info:
            # A stale socket saving an unrelated field must preserve the latest
            # idle-PvE timers, rates, and fractional part progress.
            app.pve_info = shared_pve_info
        elif shared_pve_info == persisted_pve_info:
            app.pve_info = merge_pve_info(shared_pve_info, local_pve_info)
        else:
            # Resolve concurrent edits in favor of the latest persisted PVE
            # state. ChapterPVE handlers refresh it under the account lock.
            app.pve_info = shared_pve_info
        app.guaji_pve_attempt = merge_guaji_pve_attempts(
            shared_flags.get("guaji_pve_attempt", {}),
            getattr(app, "guaji_pve_attempt", {}),
        )
        shared_championship = normalize_championship_state(
            shared_flags.get("championship_state"),
        )
        local_championship = normalize_championship_state(
            getattr(app, "championship_state", shared_championship),
        )
        persisted_championship = normalize_championship_state(
            getattr(app, "persisted_championship_state", local_championship),
        )
        if local_championship == persisted_championship:
            app.championship_state = shared_championship
        elif shared_championship == persisted_championship:
            app.championship_state = local_championship
        else:
            # Concurrent Championship operations are serialized by the
            # account lock; if an older unrelated socket reaches save, prefer
            # the newest on-disk progress over its stale copy.
            app.championship_state = shared_championship
        shared_daily_day = max(-1, int(shared_flags.get("daily_gold_day", -1)))
        shared_daily_earned = max(0, int(shared_flags.get("daily_gold_earned", 0)))
        local_daily_day = max(-1, int(getattr(app, "daily_gold_day", shared_daily_day)))
        local_daily_earned = max(0, int(getattr(
            app, "daily_gold_earned", shared_daily_earned,
        )))
        baseline_daily_day = int(getattr(
            app, "persisted_daily_gold_day", shared_daily_day,
        ))
        baseline_daily_earned = int(getattr(
            app, "persisted_daily_gold_earned", shared_daily_earned,
        ))
        if local_daily_day > shared_daily_day:
            app.daily_gold_day = local_daily_day
            app.daily_gold_earned = local_daily_earned
        elif shared_daily_day > local_daily_day:
            app.daily_gold_day = shared_daily_day
            app.daily_gold_earned = shared_daily_earned
        elif local_daily_day != baseline_daily_day:
            app.daily_gold_day = local_daily_day
            app.daily_gold_earned = local_daily_earned
        else:
            # Merge only this connection's new Gold earnings onto the latest
            # persisted daily counter; stale unrelated saves cannot roll it back.
            delta = max(0, local_daily_earned - baseline_daily_earned)
            app.daily_gold_day = shared_daily_day
            app.daily_gold_earned = shared_daily_earned + delta

        shared_boxes = normalize_save_boxes(shared_flags.get("save_boxes", {}))
        baseline_boxes = normalize_save_boxes(getattr(
            app, "persisted_save_boxes", shared_boxes,
        ))
        local_boxes = normalize_save_boxes(getattr(app, "save_boxes", shared_boxes))
        merged_boxes = dict(shared_boxes)
        # Apply only slot additions, removals, or updates made by this connection.
        # Unchanged slots from a stale socket never erase a newer reward.
        for key, old_box in baseline_boxes.items():
            local_box = local_boxes.get(key)
            if local_box is None:
                merged_boxes.pop(key, None)
            elif local_box != old_box and merged_boxes.get(key) == old_box:
                merged_boxes[key] = local_box
        for key, local_box in local_boxes.items():
            if key not in baseline_boxes and key not in merged_boxes:
                merged_boxes[key] = local_box
        app.save_boxes = merged_boxes
        state = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "player_id": int(app.player_id),
            "inventory_items": {str(item_id): int(quantity)
                                for item_id, quantity in sorted(merged_inventory.items())
                                if quantity > 0},
            "roguelike_search_points": {
                str(item_id): int(quantity)
                for item_id, quantity in sorted(merged_search_points.items())
                if quantity > 0
            },
            "part_fragments": {
                str(item_id): int(quantity)
                for item_id, quantity in sorted(merged_part_fragments.items())
                if quantity > 0
            },
            "part_levels": {
                str(part_id): int(level)
                for part_id, level in sorted(merged_part_levels.items())
                if level > 0
            },
            "first_chest_shuriken_claimed": bool(
                shared_flags.get("first_chest_shuriken_claimed", False)
                or getattr(app, "first_chest_shuriken_claimed", False)
                or merged_part_levels.get(FIRST_CHEST_SHURIKEN_PART_ID, 0) > 0
            ),
            "roguelike_keys": int(app.roguelike_keys),
            "roguelike_key_ad_claim_day": int(getattr(app, "roguelike_key_ad_claim_day", -1)),
            "roguelike_map_state": app.roguelike_map_state,
            "local_purchases": app.local_purchases,
            "level": int(app.level),
            "exp": int(app.exp),
            "power": int(app.power),
            "task_score": int(app.task_score),
            "task_score_exchange_process": int(app.task_score_exchange_process),
            "achievement_total_score": int(app.achievement_total_score),
            "achievement_reward_applied": bool(app.achievement_reward_applied),
            "level_task_state": app.level_task_state,
            "progression_counters": app.progression_counters,
            "daily_task_claimed": bool(getattr(app, "daily_task_claimed", False)),
            "achievement_claimed": bool(getattr(app, "achievement_claimed", False)),
            "lineups": app.lineups,
            # GuideStep is a bit mask (GuideMark.Over=4); merge flags rather
            # than using max so independently reported guide bits survive.
            "guide_step": (max(0, int(shared_flags.get("guide_step", 0)))
                           | max(0, int(getattr(app, "guide_step", 0)))),
            "tutorial_battle_complete": bool(
                shared_flags.get("tutorial_battle_complete", False)
                or getattr(app, "tutorial_battle_complete", False)
            ),
            "tutorial_upgrade_resources_granted": bool(
                shared_flags.get("tutorial_upgrade_resources_granted", False)
                or getattr(app, "tutorial_upgrade_resources_granted", False)
            ),
            "task_exchange_claims": app.task_exchange_claims,
            "lottery_info": normalize_lottery_info(getattr(app, "lottery_info", shared_flags.get("lottery_info"))),
            "chapter_battle_results": {
                str(stage_id): int(result)
                for stage_id, result in sorted(app.chapter_battle_results.items())
            },
            "chapter_rewards_claimed": app.chapter_rewards_claimed,
            "chapter_battle_attempts": {
                str(stage_id): record
                for stage_id, record in sorted(app.chapter_battle_attempts.items())
            },
            "pve_info": app.pve_info,
            "guaji_pve_attempt": app.guaji_pve_attempt,
            "championship_state": app.championship_state,
            "daily_gold_day": int(app.daily_gold_day),
            "daily_gold_earned": int(app.daily_gold_earned),
            "save_boxes": app.save_boxes,
        }
        app.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = app.state_file.with_suffix(app.state_file.suffix + f".{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                             encoding="utf-8")
        temporary.replace(app.state_file)
        app.persisted_inventory_items = dict(merged_inventory)
        app.persisted_roguelike_search_points = dict(merged_search_points)
        app.persisted_part_fragments = dict(merged_part_fragments)
        app.persisted_part_levels = dict(merged_part_levels)
        app.first_chest_shuriken_claimed = bool(
            shared_flags.get("first_chest_shuriken_claimed", False)
            or getattr(app, "first_chest_shuriken_claimed", False)
            or merged_part_levels.get(FIRST_CHEST_SHURIKEN_PART_ID, 0) > 0
        )
        app.persisted_lineups = normalize_lineups(app.lineups)
        app.persisted_roguelike_keys = int(app.roguelike_keys)
        app.persisted_roguelike_map_state = copy.deepcopy(app.roguelike_map_state)
        app.persisted_pve_info = copy.deepcopy(app.pve_info)
        app.persisted_championship_state = copy.deepcopy(app.championship_state)
        app.persisted_daily_gold_day = int(app.daily_gold_day)
        app.persisted_daily_gold_earned = int(app.daily_gold_earned)
        app.persisted_save_boxes = copy.deepcopy(app.save_boxes)


def game_time_seconds(now_ms: int | None = None) -> int:
    """Return seconds since the client's fixed 2018 game-time epoch."""
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    return (int(now_ms) - GAME_TIME_EPOCH_MS) // 1000


def game_local_day_index(now_ms: int | None = None) -> int:
    """Return the client's day index from its fixed 2018-01-01 UTC+8 epoch.

    Runtime inspection of UiDefine.getDayIdx showed it floors elapsed seconds
    since getDateTime2018(). The client reports that epoch as 1514736000000 ms
    (2017-12-31 16:00 UTC), so using the host's calendar date would shift the
    daily boundary by as much as eight hours.
    """
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    return (int(now_ms) - GAME_TIME_EPOCH_MS) // 86_400_000


def record_daily_gold_earned(app, amount: int, *, now_ms: int | None = None) -> int:
    """Track positive Gold grants on the same day boundary used by the client."""
    amount = max(0, int(amount))
    current_day = game_local_day_index(now_ms)
    saved_day = int(getattr(app, "daily_gold_day", current_day))
    if saved_day < current_day:
        app.daily_gold_day = current_day
        app.daily_gold_earned = 0
    elif saved_day > current_day:
        # Do not let a clock rollback erase a newer persisted day.
        return max(0, int(getattr(app, "daily_gold_earned", 0)))
    if amount:
        app.daily_gold_earned = max(0, int(getattr(app, "daily_gold_earned", 0))) + amount
    return int(app.daily_gold_earned)


def add_champion_save_box(app, item_id: int) -> int | None:
    """Place an earned chest into the first free client-visible SaveBox slot."""
    boxes = normalize_save_boxes(getattr(app, "save_boxes", {}))
    for slot in range(COMMUNITY_CHAMPION_SAVEBOX_SLOTS):
        key = str(slot)
        if key not in boxes:
            boxes[key] = {
                "item_id": int(item_id),
                "can_open_time": 0,
                "ready_countdown_time": 0,
                "reason": "championship reward",
                "pile": 1,
            }
            app.save_boxes = boxes
            return slot
    app.save_boxes = boxes
    return None


def roguelike_free_key_claimed_today(app, *, day_index: int | None = None) -> bool:
    if day_index is None:
        day_index = game_local_day_index()
    return int(getattr(app, "roguelike_key_ad_claim_day", -1)) == int(day_index)


def apply_roguelike_free_key_claim(app, *, day_index: int | None = None) -> dict[str, object]:
    """Grant the client's one-per-day Adventure key without invoking an ad SDK."""
    if day_index is None:
        day_index = game_local_day_index()
    if roguelike_free_key_claimed_today(app, day_index=day_index):
        return {"response_code": ROGUELIKE_RET_EXCHANGE_KEY_AD_ERROR,
                "granted": False, "reason": "already_claimed_today"}
    before = max(0, int(getattr(app, "roguelike_keys", 0)))
    if before >= ROGUELIKE_KEY_CAPACITY:
        return {"response_code": ROGUELIKE_RET_EXCHANGE_KEY_AD_ERROR,
                "granted": False, "reason": "key_capacity_reached",
                "key_num": before}
    granted = min(ROGUELIKE_FREE_KEY_REWARD, ROGUELIKE_KEY_CAPACITY - before)
    app.roguelike_keys = before + granted
    app.roguelike_key_ad_claim_day = int(day_index)
    return {"response_code": ROGUELIKE_RET_EXCHANGE_KEY_SUCCESS,
            "granted": True, "key_num_before": before,
            "key_num_after": app.roguelike_keys, "key_num_granted": granted,
            "claim_day": int(day_index)}


def refresh_shared_profile_state(app) -> None:
    """Reload purchases credited by the local HTTP harness before a new login sync."""
    with app.roguelike_lock:
        try:
            raw_state = json.loads(app.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw_state = {}
        needs_migration = not isinstance(raw_state, dict) or int(raw_state.get("schema_version", 0)) < PROFILE_SCHEMA_VERSION
        if isinstance(raw_state, dict):
            raw_claims = raw_state.get("task_exchange_claims", [])
            if isinstance(raw_claims, list) and LEVEL_FIRST_REWARD_ID in {
                int(value) for value in raw_claims if str(value).isdigit()
            }:
                raw_level = max(1, int(raw_state.get("level", 1)))
                raw_power = max(0, int(raw_state.get("power", 0)))
                needs_migration = needs_migration or (
                    (raw_level == 1 and raw_power == 0)
                    or (raw_level >= 2 and raw_power < 5)
                )
        state = load_profile_state(app.state_file, app.player_id)
        app.inventory_items = state["inventory_items"]
        app.part_fragments = state["part_fragments"]
        app.part_levels = state["part_levels"]
        app.first_chest_shuriken_claimed = bool(state["first_chest_shuriken_claimed"])
        app.roguelike_key_ad_claim_day = int(state["roguelike_key_ad_claim_day"])
        app.roguelike_search_points = state["roguelike_search_points"]
        app.level = int(state["level"])
        app.exp = int(state["exp"])
        app.power = int(state["power"])
        app.task_score = int(state["task_score"])
        app.task_score_exchange_process = int(state["task_score_exchange_process"])
        app.achievement_total_score = int(state["achievement_total_score"])
        app.achievement_reward_applied = bool(state["achievement_reward_applied"])
        app.level_task_state = state["level_task_state"]
        app.progression_counters = state["progression_counters"]
        app.roguelike_keys = int(state["roguelike_keys"])
        app.roguelike_map_state = state["roguelike_map_state"]
        app.persisted_roguelike_keys = int(state["roguelike_keys"])
        app.persisted_roguelike_map_state = copy.deepcopy(state["roguelike_map_state"])
        app.local_purchases = state["local_purchases"]
        app.daily_task_claimed = bool(state["daily_task_claimed"])
        app.achievement_claimed = bool(state["achievement_claimed"])
        app.guide_step = int(state["guide_step"])
        app.tutorial_battle_complete = bool(state["tutorial_battle_complete"])
        app.tutorial_upgrade_resources_granted = bool(
            state["tutorial_upgrade_resources_granted"]
        )
        app.task_exchange_claims = list(state["task_exchange_claims"])
        app.lottery_info = state["lottery_info"]
        app.lineups = state["lineups"]
        app.persisted_lineups = normalize_lineups(state["lineups"])
        app.chapter_battle_results = state["chapter_battle_results"]
        app.chapter_rewards_claimed = state["chapter_rewards_claimed"]
        app.chapter_battle_attempts = state["chapter_battle_attempts"]
        app.pve_info = state["pve_info"]
        app.persisted_pve_info = copy.deepcopy(state["pve_info"])
        app.guaji_pve_attempt = state["guaji_pve_attempt"]
        app.championship_state = state["championship_state"]
        app.persisted_championship_state = copy.deepcopy(state["championship_state"])
        app.daily_gold_day = int(state["daily_gold_day"])
        app.daily_gold_earned = int(state["daily_gold_earned"])
        app.persisted_daily_gold_day = int(state["daily_gold_day"])
        app.persisted_daily_gold_earned = int(state["daily_gold_earned"])
        app.save_boxes = state["save_boxes"]
        app.persisted_save_boxes = copy.deepcopy(state["save_boxes"])
        app.persisted_inventory_items = dict(state["inventory_items"])
        app.persisted_part_fragments = dict(state["part_fragments"])
        app.persisted_part_levels = dict(state["part_levels"])
        app.persisted_roguelike_search_points = dict(state["roguelike_search_points"])
        if needs_migration:
            save_profile_state(app)


def persist_local_tutorial_battle(app) -> bool:
    """Persist only the locally simulated opening battle, not the full guide."""
    with app.roguelike_lock:
        # Reload first so a stale WebSocket process cannot overwrite progress
        # written by another local service for this account.
        refresh_shared_profile_state(app)
        changed = not bool(getattr(app, "tutorial_battle_complete", False))
        app.tutorial_battle_complete = True
        if changed:
            save_profile_state(app)
        return changed


def persist_client_guide_step(app, reported_mask: int) -> bool:
    """Persist client-reported GuideMark bits independently of battle state."""
    reported_mask = max(0, int(reported_mask))
    if reported_mask == 0 or reported_mask > 0x7FFFFFFF:
        return False
    with app.roguelike_lock:
        refresh_shared_profile_state(app)
        previous = max(0, int(app.guide_step))
        app.guide_step = previous | reported_mask
        changed = app.guide_step != previous
        if changed:
            save_profile_state(app)
        return changed


def read_exact(sock, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        part = sock.recv(count - len(chunks))
        if not part:
            raise EOFError
        chunks.extend(part)
    return bytes(chunks)


def read_headers(sock) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(4096)
        if not part:
            raise EOFError
        data.extend(part)
        if len(data) > 16384:
            raise ValueError("oversized WebSocket handshake")
    return bytes(data)


def parse_frame(sock) -> tuple[int, bytes]:
    first, second = read_exact(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(sock, 8))[0]
    if length > 2 * 1024 * 1024:
        raise ValueError("frame exceeds 2 MiB probe limit")
    mask = read_exact(sock, 4) if second & 0x80 else None
    payload = read_exact(sock, length)
    if mask:
        payload = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
    return opcode, payload


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(data) and shift <= 63:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
    raise ValueError("malformed protobuf varint")


def protobuf_field_summary(data: bytes) -> list[dict[str, int]]:
    fields: list[dict[str, int]] = []
    pos = 0
    while pos < len(data) and len(fields) < 64:
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            break
        fields.append({"field": number, "wire": wire})
        if wire == 0:
            _, pos = read_varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            pos += size
        elif wire == 5:
            pos += 4
        else:
            break
        if pos > len(data):
            break
    return fields


def protobuf_varint_values(data: bytes) -> dict[int, int]:
    """Read scalar varint fields from a small client request protobuf."""
    values: dict[int, int] = {}
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            break
        if wire == 0:
            values[number], pos = read_varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            pos += size
        elif wire == 5:
            pos += 4
        else:
            break
        if pos > len(data):
            raise ValueError("protobuf field exceeds message length")
    return values


def int32_value(value: int) -> int:
    """Interpret a protobuf varint as a signed int32 when its high bit is set."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def protobuf_repeated_int32_values(data: bytes, field_number: int) -> list[int]:
    """Read repeated int32 values encoded unpacked or in packed wire-2 form."""
    values: list[int] = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            break
        if wire == 0:
            value, pos = read_varint(data, pos)
            if number == field_number:
                values.append(int32_value(value))
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            end = pos + size
            if end > len(data):
                raise ValueError("protobuf packed field exceeds message length")
            if number == field_number:
                while pos < end:
                    value, pos = read_varint(data, pos)
                    values.append(int32_value(value))
                if pos != end:
                    raise ValueError("protobuf packed field has an invalid boundary")
            else:
                pos = end
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > len(data):
            raise ValueError("protobuf field exceeds message length")
    return values


def resolve_roguelike_export_rewards(export_ids: list[int]) -> tuple[list[dict[str, int]], list[int]]:
    """Resolve original LocationExports IDs to aggregated ItemInfo rows."""
    by_item: dict[int, int] = {}
    unknown: list[int] = []
    for export_id in export_ids:
        row = ROGUELIKE_EXPORT_ITEMS.get(str(export_id))
        if row is None:
            if (str(export_id) not in ROGUELIKE_CHIJI_EXPORTS
                    and str(export_id) not in ROGUELIKE_RECOVER_HP_EXPORTS
                    and str(export_id) not in ROGUELIKE_SEARCH_POINT_EXPORTS):
                unknown.append(export_id)
            continue
        item_id, quantity = row.get("item_id", 0), row.get("num", 0)
        if item_id > 0 and quantity > 0:
            by_item[item_id] = by_item.get(item_id, 0) + quantity
    return ([{"item_id": item_id, "num": quantity}
             for item_id, quantity in sorted(by_item.items())], unknown)


def resolve_roguelike_search_point_rewards(export_ids: list[int]) -> list[dict[str, int]]:
    """Resolve actual SearchPoint exports into the client's per-chapter item IDs.

    The old catalog mislabeled all 34 EventType 7 RecoverHp rows as search
    points. Those rows are now separated into ROGUELIKE_RECOVER_HP_EXPORTS.
    """
    by_item: dict[int, int] = {}
    for export_id in export_ids:
        row = ROGUELIKE_SEARCH_POINT_EXPORTS.get(str(export_id))
        if row is None:
            continue
        item_id = roguelike_search_point_item_id(int(row.get("chapter", -1)))
        quantity = max(0, int(row.get("num", 0)))
        if quantity > 0:
            by_item[item_id] = by_item.get(item_id, 0) + quantity
    return [{"item_id": item_id, "num": quantity}
            for item_id, quantity in sorted(by_item.items())]


def resolve_roguelike_recover_hp_buff_exports(buff_item_ids: list[int], *,
                                              chapter_id: int,
                                              chapter_level: int,
                                              event_id: int
                                              ) -> tuple[list[int], list[int]]:
    """Map repair-station RecoverHp ExportItemIds back to table ExportKeys."""
    if ROGUELIKE_EVENT_TYPE_BY_ID.get(int(event_id)) != 7:
        return [], [int(value) for value in buff_item_ids if int(value) > 0]
    resolved: list[int] = []
    unknown: list[int] = []
    for item_id in dict.fromkeys(int(value) for value in buff_item_ids if int(value) > 0):
        candidates = [
            int(export_id)
            for export_id, row in ROGUELIKE_RECOVER_HP_EXPORTS.items()
            if (int(row.get("item_id", 0)) == item_id
                and int(row.get("chapter", -99999)) == int(chapter_id)
                and int(row.get("event_type", -99999)) == 7
                and int(chapter_level) in row.get("chapter_levels", []))
        ]
        if len(candidates) == 1:
            resolved.append(candidates[0])
        elif candidates:
            # Repeated rows are only safe to collapse when the HP effect is
            # identical; otherwise the client selection is underspecified.
            effects = {
                int(ROGUELIKE_RECOVER_HP_EXPORTS[str(candidate)].get("num", 0))
                for candidate in candidates
            }
            if len(effects) == 1:
                resolved.append(min(candidates))
            else:
                unknown.append(item_id)
        else:
            unknown.append(item_id)
    return resolved, unknown


def resolve_roguelike_chiji_rewards(export_ids: list[int]) -> list[dict[str, int]]:
    """Resolve selected RogueLike modules, which belong in PlayerStatus.EventBuff."""
    by_item: dict[int, int] = {}
    for export_id in export_ids:
        row = ROGUELIKE_CHIJI_EXPORTS.get(str(export_id))
        if row is None:
            continue
        item_id, quantity = row.get("item_id", 0), row.get("num", 0)
        if item_id > 0 and quantity > 0:
            by_item[item_id] = by_item.get(item_id, 0) + quantity
    return [{"item_id": item_id, "num": quantity}
            for item_id, quantity in sorted(by_item.items())]


def resolve_roguelike_chiji_buff_exports(buff_item_ids: list[int], *,
                                         chapter_id: int, chapter_level: int,
                                         event_id: int,
                                         cached_export_ids: list[int] | None = None
                                         ) -> tuple[list[int], list[int]]:
    """Resolve the selected Chiji item IDs carried in RogueLikeToyTop.Buffs.

    RogueLikeUI_Select_Top.confirmEvent calls
    RogueLike_Trigger_Export_Select with the selected ExportItemId in Buffs,
    then clears CurNode.Event.LocationExports before sending the request. The
    item ID is therefore mapped back to its export row using the active
    chapter, level, and event type. Cached exports narrow the match when the
    client supplied them on EnterNode.
    """
    event_type = ROGUELIKE_EVENT_TYPE_BY_ID.get(int(event_id))
    cached_ids = set(int(value) for value in (cached_export_ids or []))
    resolved: list[int] = []
    unknown: list[int] = []
    for item_id in dict.fromkeys(int(value) for value in buff_item_ids if int(value) > 0):
        candidates = []
        for export_id, row in ROGUELIKE_CHIJI_EXPORTS.items():
            if (int(row.get("item_id", 0)) != item_id
                    or int(row.get("chapter", -99999)) != int(chapter_id)
                    or int(row.get("event_type", -99999)) != int(event_type or -99998)
                    or int(chapter_level) not in row.get("chapter_levels", [])):
                continue
            candidates.append(int(export_id))
        if cached_ids:
            narrowed = [value for value in candidates if value in cached_ids]
            if narrowed:
                candidates = narrowed
        if len(candidates) == 1:
            resolved.append(candidates[0])
        elif candidates:
            # Some later-chapter rows repeat the same Chiji item in several
            # event tables. If every candidate grants the identical item and
            # quantity, use a stable canonical export ID. Reject only when the
            # selected item would map to different rewards.
            signatures = {
                (int(ROGUELIKE_CHIJI_EXPORTS[str(candidate)].get("item_id", 0)),
                 int(ROGUELIKE_CHIJI_EXPORTS[str(candidate)].get("num", 0)))
                for candidate in candidates
            }
            if len(signatures) == 1:
                resolved.append(min(candidates))
            else:
                unknown.append(item_id)
        else:
            # Repair selections also travel in RogueLikeToyTop.Buffs, but
            # RecoverHp's ExportItemId is a table selector (usually 1), not a
            # Chiji inventory ID. Resolve it only through EventType 7 rows.
            healed, heal_unknown = resolve_roguelike_recover_hp_buff_exports(
                [item_id], chapter_id=chapter_id,
                chapter_level=chapter_level, event_id=event_id,
            )
            if healed and not heal_unknown:
                resolved.extend(healed)
            else:
                unknown.append(item_id)
    return resolved, unknown


def select_roguelike_location_export_ids(export_ids: list[int], trigger_location: int) -> list[int]:
    """Select the cached shop exports named by TriggerLocation's index mask."""
    mask = max(0, int(trigger_location))
    if mask == 0:
        return list(export_ids)
    return [int(export_id) for index, export_id in enumerate(export_ids)
            if mask & (1 << index)]


def parse_roguelike_trigger_request(request: bytes) -> dict[str, object]:
    """Extract event metadata, HP, LocationExports, and selected Chiji IDs."""
    trigger_rows = protobuf_length_delimited_values(request, 2)
    trigger = trigger_rows[0] if trigger_rows else b""
    values = protobuf_varint_values(request)
    trigger_values = protobuf_varint_values(trigger)
    cur_rows = protobuf_length_delimited_values(trigger, 1)
    cur_node_bytes = cur_rows[0] if cur_rows else b""
    cur_node = protobuf_varint_values(cur_node_bytes)
    location = int32_value(cur_node.get(1, trigger_values.get(3, 0)))
    event_rows = protobuf_length_delimited_values(cur_node_bytes, 2)
    event_bytes = event_rows[0] if event_rows else b""
    event_values = protobuf_varint_values(event_bytes)
    export_ids = protobuf_repeated_int32_values(event_bytes, 2)
    toy_tops = protobuf_length_delimited_values(trigger, 4)
    hp_values = [
        int32_value(protobuf_varint_values(toy_top).get(1, 0))
        for toy_top in toy_tops
    ]
    # RogueLikeToyTop.Buffs is repeated int32 field 2. The live sender's
    # RogueLike_Trigger_Export_Select puts the selected Chiji ExportItemId here
    # after clearing CurNode.Event.LocationExports.
    buff_item_ids = [
        buff_item_id
        for toy_top in toy_tops
        for buff_item_id in protobuf_repeated_int32_values(toy_top, 2)
        if buff_item_id > 0
    ]
    return {
        "location": location,
        "chapter_id": int32_value(values.get(3, 0)),
        "event_id": int32_value(event_values.get(1, 0)),
        "export_ids": export_ids,
        "trigger_location": int32_value(trigger_values.get(3, 0)),
        "hp_values": hp_values,
        "buff_item_ids": buff_item_ids,
    }


def parse_roguelike_enter_node_request(request: bytes) -> dict[str, object]:
    """Extract the node event and its client-table reward IDs on node entry."""
    enter_rows = protobuf_length_delimited_values(request, 2)
    enter = enter_rows[0] if enter_rows else b""
    values = protobuf_varint_values(request)
    node_rows = protobuf_length_delimited_values(enter, 1)
    node_bytes = node_rows[0] if node_rows else b""
    node = protobuf_varint_values(node_bytes)
    event_rows = protobuf_length_delimited_values(node_bytes, 2)
    event_bytes = event_rows[0] if event_rows else b""
    event_values = protobuf_varint_values(event_bytes)
    return {
        "location": int32_value(node.get(1, 0)),
        "chapter_id": int32_value(values.get(3, 0)),
        "event_id": int32_value(event_values.get(1, 0)),
        "export_ids": protobuf_repeated_int32_values(event_bytes, 2),
    }


def protobuf_length_delimited_values(data: bytes, field_number: int) -> list[bytes]:
    values: list[bytes] = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            break
        if wire == 0:
            _, pos = read_varint(data, pos)
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            end = pos + size
            if end > len(data):
                raise ValueError("protobuf field exceeds message length")
            if number == field_number:
                values.append(data[pos:end])
            pos = end
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > len(data):
            raise ValueError("protobuf field exceeds message length")
    return values


def parse_information_opr_request(data: bytes) -> dict[str, object]:
    """Decode the observed CS_InformationOpr lineup-change fields."""
    outer = protobuf_varint_values(data)
    by_param_rows = protobuf_length_delimited_values(data, 5)  # byParam
    updates = []
    if by_param_rows:
        lineup_proto = by_param_rows[0]
        for row in protobuf_length_delimited_values(lineup_proto, 1):  # ToyTops
            wrapper = protobuf_varint_values(row)
            toy_index = int32_value(wrapper.get(1, 0))
            toy_proto_rows = protobuf_length_delimited_values(row, 2)  # ToyTopProto
            if not toy_proto_rows:
                continue
            part_values = protobuf_varint_values(toy_proto_rows[0])
            updates.append({
                "toy_index": toy_index,
                "parts": {
                    slot: int32_value(part_values[field_number])
                    for slot, field_number in LINEUP_PART_FIELDS.items()
                    if field_number in part_values
                },
            })
    return {
        "opr_type": int32_value(outer.get(1, 0)),
        "i_param": int32_value(outer.get(4, 0)),
        "toy_top_updates": updates,
    }


def apply_lineup_part_updates(lineups: dict | None, inventory_items: dict[int, int],
                              part_levels: dict[int, int], lineup_index: int,
                              updates: object) -> tuple[bool, dict, list[dict], str | None]:
    """Validate a complete native part-change request before changing lineup state."""
    unchanged = normalize_lineups(lineups)
    try:
        lineup_index = int(lineup_index)
    except (TypeError, ValueError):
        return False, unchanged, [], "invalid_lineup_index"
    if not 1 <= lineup_index <= 3:
        return False, unchanged, [], "invalid_lineup_index"
    if not isinstance(updates, list) or not updates:
        return False, unchanged, [], "empty_updates"

    # Match the same ownership contract used by PlayerData sync. Fragment-only
    # rows stay locked until the client has unlocked the part (Level >= 1).
    owned_parts = owned_part_levels(inventory_items, part_levels)
    location_by_part = {
        int(part_id): int(UPGRADEABLE_PART_LOCATIONS[part_id])
        for part_id in owned_parts
        if part_id in UPGRADEABLE_PART_LOCATIONS
    }
    candidate = normalize_lineups(lineups)
    changed_parts: list[dict] = []
    changed_slots: set[tuple[int, str]] = set()

    # Validate every row first. In particular, a bad second row must not leave
    # the first row applied only in memory after the server rejects the request.
    for update in updates:
        if not isinstance(update, dict):
            return False, unchanged, [], "invalid_update"
        try:
            toy_index = int(update.get("toy_index", 0))
        except (TypeError, ValueError):
            return False, unchanged, [], "invalid_toy_index"
        parts = update.get("parts")
        if not 1 <= toy_index <= 3:
            return False, unchanged, [], "invalid_toy_index"
        if not isinstance(parts, dict) or not parts:
            return False, unchanged, [], "empty_part_update"
        for slot, raw_part_id in parts.items():
            if slot not in LINEUP_PART_FIELDS:
                return False, unchanged, [], "invalid_part_slot"
            try:
                part_id = int(raw_part_id)
            except (TypeError, ValueError):
                return False, unchanged, [], "invalid_part_id"
            expected_location = LINEUP_PART_FIELDS[slot] - 1
            if location_by_part.get(part_id) != expected_location:
                return False, unchanged, [], "part_not_owned_or_wrong_slot"
            change_key = (toy_index, slot)
            if change_key in changed_slots:
                return False, unchanged, [], "duplicate_part_slot"
            changed_slots.add(change_key)
            previous_part_id = candidate[str(lineup_index)][str(toy_index)][slot]
            if previous_part_id != part_id:
                candidate[str(lineup_index)][str(toy_index)][slot] = part_id
                changed_parts.append({"toy_index": toy_index,
                                      "slot": slot,
                                      "part_id": part_id})
    # UI taps and reconnect retries can legitimately repeat a request after
    # the server has already persisted it. Treat that as success so the
    # handler can resend authoritative PlayerData and repair a stale client
    # lineup instead of returning an error for an already-correct state.
    return True, candidate, changed_parts, None


def varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def protobuf_string(field: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return varint((field << 3) | 2) + varint(len(encoded)) + encoded


def protobuf_bytes(field: int, value: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(value)) + value


def protobuf_int(field: int, value: int) -> bytes:
    encoded = value if value >= 0 else (1 << 64) + value
    return varint(field << 3) + varint(encoded)


def make_achievement_proto(*, level: int, reward_time: int, value: int, total_score: int) -> bytes:
    # PlayerDataProto.Achievement=14; AchievementProto.Detail=1 (repeated
    # ContainerIntAchievementDetail(idx=1,val=2)); TotalScore=2. The nested
    # AchievementDetail fields are Level=1, RewardTime=2 and Value=3.
    def entry(achievement_id: int, row_level: int, row_reward_time: int, row_value: int) -> bytes:
        detail = b"".join((
            protobuf_int(1, row_level),
            protobuf_int(2, row_reward_time),
            protobuf_int(3, row_value),
        ))
        return protobuf_int(1, achievement_id) + protobuf_bytes(2, detail)

    rows = [protobuf_bytes(1, entry(TEST_ACHIEVEMENT_ID, level, reward_time, value))]
    rows.extend(
        protobuf_bytes(1, entry(achievement_id, 1, 0, career_value))
        for achievement_id, career_value in CAREER_BASELINE_VALUES.items()
    )
    return b"".join(rows) + protobuf_int(2, total_score)


def make_achievement_reply(code: int = 0) -> bytes:
    # Runtime handler[177] decodes SC_AchievementOpr { code: int32 }.
    proto = protobuf_int(1, code)
    body = struct.pack(">IH", 6 + len(proto), 177)
    return websocket_binary_frame(body + proto)


def make_player_achievement_sync(*, level: int, reward_time: int, value: int,
                                 total_score: int, diamond_count: int) -> bytes:
    achievement = make_achievement_proto(
        level=level, reward_time=reward_time, value=value, total_score=total_score,
    )
    base_info = protobuf_int(4, max(0, int(diamond_count)))  # Authoritative wallet balance.
    player_data = protobuf_bytes(1, base_info) + protobuf_bytes(14, achievement)
    body = struct.pack(">IH", 6 + len(player_data), 1) + player_data
    return websocket_binary_frame(body)


def websocket_binary_frame(payload: bytes) -> bytes:
    if len(payload) < 126:
        header = bytes([0x82, len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytes([0x82, 126]) + struct.pack(">H", len(payload))
    else:
        header = bytes([0x82, 127]) + struct.pack(">Q", len(payload))
    return header + payload


def make_public_svr_sync(server_time: int | None = None,
                         session_token: str | None = None,
                         public_http_url: str = "http://127.0.0.1:18765",
                         public_ws_url: str = "ws://127.0.0.1:18766") -> bytes:
    # ClientProtoRegistor.handler[2] is PlayerInfoProto. SysInfo is field 1
    # with ServerTime at field 1; ObtainItemLimitedNum is repeated field 7;
    # PublicSvr is field 12 and fields 1-4 are Url, Id, Token and WsUrl.
    # The original Gold cap is not present in the recovered tables. The
    # community server advertises its explicit 10,000 Gold/day policy.
    if server_time is None:
        server_time = game_time_seconds()
    ws_url = public_ws_url.rstrip("/")
    if session_token:
        ws_url += "/?session=" + quote(session_token, safe="")
    sys_info = protobuf_int(1, server_time)
    public_svr = b"".join((
        protobuf_string(1, public_http_url.rstrip("/")),
        protobuf_string(2, "local-test-app"),
        protobuf_string(3, "local-test-token"),
        protobuf_string(4, ws_url),
    ))
    gold_limit = protobuf_int(1, GOLD_ITEM_ID) + protobuf_int(2, COMMUNITY_GOLD_DAILY_LIMIT)
    player_info = (protobuf_bytes(1, sys_info)
                   + protobuf_bytes(7, gold_limit)
                   + protobuf_bytes(12, public_svr))
    body = struct.pack(">IH", 6 + len(player_info), 2) + player_info
    return websocket_binary_frame(body)


def make_synthetic_champion_history(vouchers: int = SYNTHETIC_CHAMPION_VOUCHERS) -> bytes:
    """Encode a fresh all-robot Champion history for local beta profiles.

    The client selects RobotRankData when RaceScore is at or below
    MAX_ALL_ROBOT_CHAMPION. LeagueIndex 1 is the first Bronze division. This
    record is a local tutorial fixture, not real competitive progress.
    """
    history = b"".join((
        protobuf_int(1, 0),   # RaceHistoryProto.Win
        protobuf_int(2, 0),   # Lose
        protobuf_int(3, 0),   # Tied
        protobuf_int(4, 0),   # StreakWinning
        protobuf_int(5, 0),   # MaxStreakWinning
        protobuf_int(6, 1),   # Minimum client rank key; still in all-robot range.
        protobuf_int(7, 0),   # BigWin
        protobuf_int(8, 0),   # MaxScore
        protobuf_int(9, 1),   # LeagueIndex: Bronze I
        protobuf_int(10, 0),  # FinalRace
        protobuf_int(11, 0),  # JoinIndex
        protobuf_int(13, max(0, int(vouchers))),  # Vouchers
    ))
    # ContainerIntRaceHistoryProto: idx=E_RaceType.CHAMPION_RACE, val=history.
    return protobuf_int(1, CHAMPION_RACE_TYPE) + protobuf_bytes(2, history)


def normalize_championship_state(raw: object = None, *, now: int | None = None,
                                 default_vouchers: int = SYNTHETIC_CHAMPION_VOUCHERS) -> dict[str, object]:
    """Normalize local Championship state without resetting existing balances."""
    if now is None:
        now = int(time.time())
    source = raw if isinstance(raw, dict) else {}
    started = max(0, int(source.get("period_started_at", 0) or 0))
    if started <= 0:
        started = int(now)
    vouchers = max(0, int(source.get("vouchers", default_vouchers)))
    losses = source.get("lose_times", {})
    if not isinstance(losses, dict):
        losses = {}
    lose_times = {}
    for enemy_id, count in losses.items():
        try:
            enemy_id, count = int(enemy_id), max(0, int(count))
        except (TypeError, ValueError):
            continue
        if enemy_id < 0 and count:
            lose_times[str(enemy_id)] = count
    active = source.get("active_match")
    if not isinstance(active, dict):
        active = None
    else:
        try:
            enemy_id = int(active["enemy_id"])
            join_index = int(active["join_index"])
            league_index = int(active["league_index"])
            cost = int(active["ticket_cost"])
            if enemy_id >= 0 or not 0 <= join_index < CHAMPION_MAX_RANK_ROWS or league_index < 1 or cost < 1:
                active = None
            else:
                active = {"enemy_id": enemy_id, "join_index": join_index,
                          "league_index": league_index, "ticket_cost": cost,
                          "entered_at": max(0, int(active.get("entered_at", now)))}
        except (KeyError, TypeError, ValueError):
            active = None
    return {
        "league_index": max(1, min(999, int(source.get("league_index", 1)))),
        # Keep the persisted score 0 for new profiles. The wire serializer
        # advertises the client's rank-key floor (1) without mutating progress.
        # The client switches from its local RobotRankData to QueryWithScore
        # above score 2. Keep the full configured league range so local
        # season promotion can enter that supported synthetic robot branch.
        "race_score": max(0, min(31, int(source.get("race_score", 0)))),
        "win_mask": max(0, int(source.get("win_mask", 0))) & 0x7FFF,
        "losses": max(0, int(source.get("losses", 0))),
        "wins": max(0, int(source.get("wins", 0))),
        "streak": max(0, int(source.get("streak", 0))),
        "max_streak": max(0, int(source.get("max_streak", 0))),
        "vouchers": vouchers,
        "lose_times": lose_times,
        "active_match": active,
        "period_started_at": started,
        "period_id": max(0, int(source.get("period_id", started))),
        "periods_completed": max(0, int(source.get("periods_completed", 0))),
        "last_promotion_period": max(0, int(source.get("last_promotion_period", 0))),
        "last_period_rank": max(0, int(source.get("last_period_rank", 0))),
        # This upper-tier display balance is independent from regular RaceScore.
        # Retain it if present; local match settlement does not invent cups.
        "top_champion_cups": max(0, int(source.get("top_champion_cups", 0))),
        "top_champion_init_date": max(0, int(source.get("top_champion_init_date", 0))),
    }


def champion_client_race_score(race_score: object) -> int:
    """Return the score value the client can safely use as a LeagueRank key."""
    return max(1, max(0, int(race_score or 0)))


def champion_robot_ids(player_id: int, race_score: int = 0) -> list[int]:
    """Reproduce the client's deterministic negative RobotRankData IDs."""
    # The server advertises a minimum score of 1 on the wire because robot
    # profile initialization indexes LeagueRank directly. Generate IDs from
    # that same visible score, while keeping persisted progression unchanged.
    score = champion_client_race_score(race_score)
    first = -(((score + int(player_id)) % 10) * CHAMPION_MAX_RANK_ROWS + 1)
    # Client emits 10 initial rows including self; it can append rows up to 15.
    return [first - offset for offset in range(CHAMPION_MAX_RANK_ROWS - 1)]


def champion_available_robot_ids(player_id: int,
                                 state: dict[str, object]) -> list[int]:
    state = normalize_championship_state(state)
    race_score = champion_client_race_score(state["race_score"])
    if race_score > 2:
        # ChampDataMgr.robotId2JoinIdx decodes -id - RaceScore*15.
        # QueryWithScore rows use slots 1..14 for a local 15-row group.
        return [-(race_score * CHAMPION_MAX_RANK_ROWS + slot)
                for slot in range(1, CHAMPION_MAX_RANK_ROWS)]
    wins = int(state["win_mask"]).bit_count()
    # RobotRankData starts with ten rank rows including self. It extends the
    # row array by one for each self-win beyond eight, to the 15-row cap.
    opponent_count = min(CHAMPION_MAX_RANK_ROWS - 1,
                         CHAMPION_ROBOT_RANK_ROWS - 1 + max(0, wins - 8))
    return champion_robot_ids(player_id, race_score)[:opponent_count]


def parse_repeated_int64(data: bytes, field_number: int) -> list[int]:
    values: list[int] = []
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            break
        if wire == 0:
            value, pos = read_varint(data, pos)
            if number == field_number:
                values.append(value - (1 << 64) if value >= (1 << 63) else value)
        elif wire == 1:
            pos += 8
        elif wire == 2:
            size, pos = read_varint(data, pos)
            end = pos + size
            if end > len(data):
                raise ValueError("protobuf field exceeds message length")
            if number == field_number:
                packed_pos = pos
                while packed_pos < end:
                    value, packed_pos = read_varint(data, packed_pos)
                    values.append(value - (1 << 64) if value >= (1 << 63) else value)
            pos = end
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > len(data):
            raise ValueError("protobuf field exceeds message length")
    return values


def make_champion_history(state: dict[str, object]) -> bytes:
    state = normalize_championship_state(state)
    history = b"".join((
        protobuf_int(1, int(state["win_mask"])),
        protobuf_int(2, int(state["losses"])),
        protobuf_int(3, 0),
        protobuf_int(4, int(state["streak"])),
        protobuf_int(5, int(state["max_streak"])),
        protobuf_int(6, champion_client_race_score(state["race_score"])),
        protobuf_int(7, 0), protobuf_int(8, int(state["wins"])),
        protobuf_int(9, int(state["league_index"])),
        protobuf_int(10, 0), protobuf_int(11, 0),
        *(protobuf_bytes(12, protobuf_int(1, int(enemy_id)) + protobuf_int(2, int(count)))
          for enemy_id, count in sorted(state["lose_times"].items(), key=lambda item: int(item[0]))),
        protobuf_int(13, int(state["vouchers"])),
    ))
    return protobuf_int(1, CHAMPION_RACE_TYPE) + protobuf_bytes(2, history)


def champion_period_rollover(state: dict[str, object], *, now: int) -> tuple[dict[str, object], bool]:
    """Apply the community 24-hour personal low-robot ranking rollover once."""
    state = normalize_championship_state(state, now=now)
    if int(now) - int(state["period_started_at"]) < COMMUNITY_CHAMPION_PERIOD_SECONDS:
        return state, False
    wins = int(state["win_mask"]).bit_count()
    # Client robot ranking places a player at rank 9-wins in the initial group.
    # Top-five promotion is the player's recollection, applied as a local rule.
    rank = max(1, CHAMPION_ROBOT_RANK_ROWS - 1 - wins)
    prior_period = int(state["period_id"])
    if rank <= 5 and int(state["last_promotion_period"]) != prior_period:
        # Community top-five fallback: advance one tier. Above score 2 the
        # client calls QueryWithScore; the HTTP service supplies local robots
        # there instead of requiring an unavailable public ranking service.
        state["race_score"] = min(31, int(state["race_score"]) + 1)
        state["last_promotion_period"] = prior_period
    state["last_period_rank"] = rank
    state["periods_completed"] = int(state["periods_completed"]) + 1
    state["period_started_at"] = int(now)
    state["period_id"] = int(now)
    state["win_mask"] = 0
    state["wins"] = 0
    state["losses"] = 0
    state["streak"] = 0
    state["lose_times"] = {}
    return state, True


def apply_champion_entry(state: dict[str, object], *, player_id: int, enemy_id: int,
                         join_index: int, league_index: int, now: int) -> tuple[dict[str, object], int, str]:
    state, _rolled = champion_period_rollover(state, now=now)
    if enemy_id not in champion_available_robot_ids(player_id, state):
        return state, 45, "invalid_enemy"
    if league_index != int(state["league_index"]) or join_index != ((-enemy_id) % CHAMPION_MAX_RANK_ROWS):
        return state, 45, "invalid_match_identity"
    active = state.get("active_match")
    if active:
        if (int(active["enemy_id"]) == enemy_id and int(active["join_index"]) == join_index
                and int(active["league_index"]) == league_index):
            return state, 44, "duplicate_entry_reused"
        return state, 45, "another_match_active"
    cost = int(state["lose_times"].get(str(enemy_id), 0)) + 1
    if int(state["win_mask"]) & (1 << join_index):
        return state, 46, "opponent_already_defeated"
    if int(state["vouchers"]) < cost:
        return state, 47, "insufficient_vouchers"
    state["vouchers"] = int(state["vouchers"]) - cost
    state["active_match"] = {"enemy_id": enemy_id, "join_index": join_index,
                             "league_index": league_index, "ticket_cost": cost,
                             "entered_at": int(now)}
    return state, 44, "match_entered"


def apply_champion_result(state: dict[str, object], *, enemy_id: int, join_index: int,
                          league_index: int, won: bool, now: int) -> tuple[dict[str, object], int, str]:
    state = normalize_championship_state(state, now=now)
    # Settle an already-started match before rolling the personal period.
    if not state.get("active_match"):
        state, _rolled = champion_period_rollover(state, now=now)
    active = state.get("active_match")
    if not active:
        return state, 46, "no_active_match"
    if (int(active["enemy_id"]) != enemy_id or int(active["join_index"]) != join_index
            or int(active["league_index"]) != league_index):
        return state, 45, "result_does_not_match_active_match"
    if won:
        state["win_mask"] = int(state["win_mask"]) | (1 << join_index)
        state["wins"] = int(state["wins"]) + 1
        state["streak"] = int(state["streak"]) + 1
        state["max_streak"] = max(int(state["max_streak"]), int(state["streak"]))
    else:
        state["losses"] = int(state["losses"]) + 1
        state["streak"] = 0
        loss_map = state["lose_times"]
        loss_map[str(enemy_id)] = int(loss_map.get(str(enemy_id), 0)) + 1
    # The local client simulates the battle, but the result remains gated on a
    # persisted active entry. The player-provided community reward applies to
    # wins only; a loss records Championship state without changing inventory.
    state["active_match"] = None
    return state, 43, "result_recorded"


def complete_champion_match(app, *, enemy_id: int, join_index: int,
                             league_index: int, won: bool,
                             now: int) -> tuple[int, str, tuple[tuple[int, int], ...]]:
    """Apply one eligible result and its exactly-once local participation reward."""
    state, code, reason = apply_champion_result(
        app.championship_state, enemy_id=enemy_id, join_index=join_index,
        league_index=league_index, won=won, now=now,
    )
    app.championship_state = state
    if code != 43:
        return code, reason, ()
    record_championship_intro_task_progress(app)
    if not won:
        return code, reason, ()
    app.inventory_items[GOLD_ITEM_ID] = (
        int(app.inventory_items.get(GOLD_ITEM_ID, 0)) + COMMUNITY_CHAMPION_MATCH_GOLD
    )
    record_daily_gold_earned(app, COMMUNITY_CHAMPION_MATCH_GOLD)
    app.inventory_items[CHAMPION_RESULT_CHEST_ID] = (
        int(app.inventory_items.get(CHAMPION_RESULT_CHEST_ID, 0)) + 1
    )
    add_champion_save_box(app, CHAMPION_RESULT_CHEST_ID)
    return code, reason, ((GOLD_ITEM_ID, COMMUNITY_CHAMPION_MATCH_GOLD),
                          (CHAMPION_RESULT_CHEST_ID, 1))


def make_champion_display_info(player_id: int, enemy_id: int, league_index: int, *,
                               nickname: str | None = None,
                               profile=None, is_robot: bool = True) -> bytes:
    """Encode a Championship display profile (synthetic robot or local player)."""
    rank_seed = abs(int(enemy_id))
    if profile is not None:
        lineup_state = normalize_lineups(getattr(profile, "lineups", None))["1"]
        assemblies = tuple(
            tuple(int(lineup_state[str(blade)][part]) for part in ("top", "mid", "bot"))
            for blade in range(1, 4)
        )
        nickname = nickname or "Local Player"
        profile_level = max(1, int(getattr(profile, "level", 1)))
        part_levels = getattr(profile, "part_levels", {}) or {}
        avatar = "UI/TouXiang/TouXiang_010.png"
    else:
        assemblies = STARTER_BLADE_ASSEMBLIES
        part_levels = {}
        profile_level = max(1, int(league_index))
        avatar = "UI/TouXiang/TouXiang_010.png"
        if is_robot:
            nickname = CHAMPION_ROBOT_DISPLAY_NAMES[rank_seed % len(CHAMPION_ROBOT_DISPLAY_NAMES)]
        else:
            nickname = nickname or "Local Player"
    parts = bytearray()
    for blade_index, assembly in enumerate(assemblies):
        default_level = (min(15, max(1, league_index) + ((rank_seed + blade_index) % 2))
                         if is_robot else profile_level)
        # ToytopParts.PartAs identifies which of the three blade slots owns a
        # component; component location (top/middle/bottom) comes from part ID
        # configuration. All three components in one assembly share PartAs.
        part_as = blade_index + 1
        for part_id in assembly:
            raw_level = (part_levels.get(part_id, part_levels.get(str(part_id), default_level))
                         if isinstance(part_levels, dict) else default_level)
            level = min(15, max(1, int(raw_level)))
            toy_part = b"".join((
                protobuf_int(1, level), protobuf_int(2, 0),
                protobuf_int(3, part_as), protobuf_int(10, part_id),
            ))
            parts.extend(protobuf_bytes(9, toy_part))
    return b"".join((
        protobuf_int(1, enemy_id),
        protobuf_string(2, str(nickname or "Local Player")[:24]),
        protobuf_int(3, profile_level if not is_robot else max(1, league_index)),
        protobuf_string(4, avatar),
        protobuf_int(7, len(assemblies) * 3),
        protobuf_int(8, 1 if is_robot else 0),
        bytes(parts),
    ))


def make_champion_display_response(request: bytes, player_id: int,
                                   championship: dict[str, object],
                                   player_profile=None) -> bytes:
    fields = protobuf_varint_values(request)
    query_type = int32_value(fields.get(1, 0))
    ids = parse_repeated_int64(request, 2)
    valid_ids = set(champion_available_robot_ids(player_id, championship))
    valid_ids.add(int(player_id))
    nickname = "Local Player"
    if player_profile is not None:
        account = getattr(player_profile, "account", {}) or {}
        nickname = (account.get("username")
                    or (account.get("email") or "Local Player").split("@", 1)[0])
    profiles = [
        make_champion_display_info(
            player_id, enemy_id, int(championship.get("race_score", 0)) + 1,
            nickname=nickname if enemy_id == int(player_id) else None,
            profile=player_profile if enemy_id == int(player_id) else None,
            is_robot=enemy_id != int(player_id),
        )
        for enemy_id in ids if enemy_id in valid_ids
    ]
    proto = protobuf_int(1, query_type)
    proto += b"".join(protobuf_bytes(2, profile) for profile in profiles)
    proto += b"".join(protobuf_int(3, enemy_id) for enemy_id in ids if enemy_id in valid_ids)
    return websocket_binary_frame(struct.pack(">IH", 6 + len(proto), 162) + proto)


def make_champion_rank_item_response(request: bytes, player_id: int,
                                     nickname: str = "Local Player",
                                     player_score: int = 0) -> bytes:
    """Answer QueryUserRankItem using the generated client's DisplayItem schema."""
    query_type = int32_value(protobuf_varint_values(request).get(1, 0))
    ids = parse_repeated_int64(request, 2)
    items = []
    for user_id in ids:
        if user_id == int(player_id):
            name = str(nickname or "Local Player")[:24]
            score = max(0, int(player_score))
        elif -10_000_000_000 < user_id <= -9_000_000:
            slot = (-user_id - 9_000_000) % 100
            if not 1 <= slot < CHAMPION_MAX_RANK_ROWS:
                continue
            name = f"Local Champion {slot:02d}"
            score = max(0, 300 - slot * 10)
        else:
            continue
        item = b"".join((
            protobuf_int(1, user_id),
            protobuf_string(2, name),
            protobuf_int(3, 1),
            protobuf_string(4, "UI/TouXiang/TouXiang_010.png"),
            protobuf_string(5, ""),
            protobuf_int(7, score),
            protobuf_int(8, 1),
        ))
        items.append(item)
    proto = protobuf_int(1, query_type)
    proto += b"".join(protobuf_int(3, user_id) for user_id in ids)
    proto += b"".join(protobuf_bytes(4, item) for item in items)
    return websocket_binary_frame(struct.pack(">IH", 6 + len(proto), 162) + proto)


def make_tutorial_quick_race_history() -> bytes:
    """Record the local tutorial win used by Guide.beginGuide on relogin."""
    # The client chooses the opening battle only when QUICK_RACE.Total is zero.
    # This is the locally simulated tutorial win, not a competitive result.
    history = protobuf_int(1, 1) + protobuf_int(2, 0) + protobuf_int(3, 0)
    return protobuf_int(1, 1) + protobuf_bytes(2, history)  # QUICK_RACE=1.


def make_lottery_info_proto(lottery_info: dict[str, object] | None = None) -> bytes:
    """Encode LotteryInfoProto using the runtime-confirmed fields 1 and 2."""
    state = normalize_lottery_info(lottery_info)
    proto = bytearray()
    lotteries = state.get("lotteries", {})
    if isinstance(lotteries, dict):
        for lottery_id, raw in sorted(lotteries.items(), key=lambda pair: int(pair[0])):
            if not isinstance(raw, dict):
                continue
            lottery_value = protobuf_int(1, max(0, int(raw.get("allowance", 0))))
            rarity_times = raw.get("rarity_times", {})
            if isinstance(rarity_times, dict):
                for rarity, count in sorted(rarity_times.items(), key=lambda pair: int(pair[0])):
                    entry = protobuf_int(1, int(rarity)) + protobuf_int(2, max(0, int(count)))
                    lottery_value += protobuf_bytes(2, entry)
            container = protobuf_int(1, int(lottery_id)) + protobuf_bytes(2, lottery_value)
            proto += protobuf_bytes(1, container)
    cost_record = state.get("cost_record", {})
    if isinstance(cost_record, dict):
        for item_id, count in sorted(cost_record.items(), key=lambda pair: int(pair[0])):
            entry = protobuf_int(1, int(item_id)) + protobuf_int(2, max(0, int(count)))
            proto += protobuf_bytes(2, entry)
    return bytes(proto)


def owned_part_levels(inventory_items: dict[int, int] | None = None,
                     part_levels: dict[int, int] | None = None) -> dict[int, int]:
    """Return locally owned part IDs and their persisted synthetic level."""
    inventory_items = inventory_items or {}
    parts = {part_id: SYNTHETIC_STARTER_LEVEL for part_id, _ in SYNTHETIC_STARTER_PARTS}
    for part_id in PART_ITEM_LOCATIONS:
        if int(inventory_items.get(part_id, inventory_items.get(str(part_id), 0))) > 0:
            parts.setdefault(part_id, 1)
    for part_id, level in (part_levels or {}).items():
        part_id, level = int(part_id), int(level)
        if part_id in UPGRADEABLE_PART_LOCATIONS and level > 0:
            parts[part_id] = min(15, level)
    return parts


def level_task_progress(task: dict, inventory_items: dict[int, int],
                        saved_state: dict[str, object] | None,
                        progression_counters: dict[str, object] | None = None,
                        part_levels: dict[int, int] | None = None) -> tuple[int, int]:
    """Calculate verified inventory-backed level-task progress, preserving saved progress."""
    task_id = int(task["TaskID"])
    maximum = max(0, int(task.get("maxProcess", 0)))
    state = (saved_state or {}).get(str(task_id), {})
    state = state if isinstance(state, dict) else {}
    progress = max(0, int(state.get("process", 0)))
    parts = owned_part_levels(inventory_items, part_levels)
    condition = int(task.get("condition", -1))
    if condition == 16:  # GetPartNum: owned part count.
        progress = max(progress, min(maximum, len(parts)))
    elif condition == 23:  # OwnLevelPartNum: owned parts that meet Param level.
        if task_id == FIRST_LEVEL_UPGRADE_TASK_ID:
            # The guide text requires an upgrade action. Old synthetic level-10
            # starters looked complete without an action. Count successful
            # upgrade events for this opening task only.
            progress = max(progress, min(
                maximum,
                int((progression_counters or {}).get(FIRST_LEVEL_UPGRADE_COUNTER, 0)),
            ))
        else:
            required_level = max(1, int(task.get("Param", 1)))
            qualified = sum(1 for part_level in parts.values() if part_level >= required_level)
            progress = max(progress, min(maximum, qualified))
    elif condition == 52:  # MaxPartLevel: highest owned part level.
        progress = max(progress, min(maximum, max(parts.values(), default=0)))
    else:
        counters = progression_counters or {}
        counter_key = progression_task_counter_key(task)
        progress = max(progress, min(maximum, int(counters.get(counter_key, 0))))
    progress = min(maximum, progress)
    status = max(0, min(TASK_STATUS_REWARDED, int(state.get("status", TASK_STATUS_DOING))))
    if progress >= maximum and maximum > 0 and status < TASK_STATUS_COMPLETE:
        status = TASK_STATUS_COMPLETE
    return progress, status


def progression_task_counter_key(task: dict) -> str:
    """Build a stable counter key, keeping chapter-filtered tasks separate."""
    condition = int(task.get("condition", -1))
    parameter = int(task.get("Param", 0))
    location = int(task.get("Location", 0))
    key = f"condition:{condition}:{parameter}:{location}"
    chapter = max(0, int(task.get("RLChapter", 0) or 0))
    chapter_level = max(0, int(task.get("RLLevel", 0) or 0))
    if chapter or chapter_level:
        key += f":chapter:{chapter}:level:{chapter_level}"
    return key


def increment_level_task_counters(progression_counters: dict[str, object],
                                  condition: int, amount: int = 1, *,
                                  chapter_id: int | None = None,
                                  chapter_level: int | None = None) -> dict[str, int]:
    """Advance catalog-backed counters that match the confirmed event context."""
    increment = max(0, int(amount))
    if increment == 0:
        return {}
    maxima: dict[str, int] = {}
    for task in LEVEL_PROGRESSION_TASKS.values():
        if int(task.get("condition", -1)) != int(condition):
            continue
        required_chapter = max(0, int(task.get("RLChapter", 0) or 0))
        required_level = max(0, int(task.get("RLLevel", 0) or 0))
        if required_chapter and (chapter_id is None or int(chapter_id) != required_chapter):
            continue
        if required_level and (chapter_level is None or int(chapter_level) != required_level):
            continue
        key = progression_task_counter_key(task)
        maxima[key] = max(maxima.get(key, 0), max(0, int(task.get("maxProcess", 0))))
    advanced: dict[str, int] = {}
    for key, maximum in maxima.items():
        old_value = max(0, int(progression_counters.get(key, 0)))
        new_value = min(maximum, old_value + increment)
        if new_value != old_value:
            progression_counters[key] = new_value
            advanced[key] = new_value - old_value
    return advanced


def backfill_active_adventure_progression(progression_counters: dict[str, object],
                                         has_active_map: bool) -> bool:
    """Credit one legacy Adventure entry when an active map predates counters."""
    if not has_active_map:
        return False
    key = "condition:30:0:3"
    maximum = max((max(0, int(task.get("maxProcess", 0)))
                   for task in LEVEL_PROGRESSION_TASKS.values()
                   if int(task.get("condition", -1)) == 30
                   and int(task.get("Param", 0)) == 0
                   and int(task.get("Location", 0)) == 3), default=0)
    if maximum <= 0:
        return False
    previous = max(0, int(progression_counters.get(key, 0)))
    updated = min(maximum, max(previous, 1))
    if updated == previous:
        return False
    progression_counters[key] = updated
    return True


def make_level_task_container(level: int, inventory_items: dict[int, int],
                              level_task_state: dict[str, object] | None = None,
                              progression_counters: dict[str, object] | None = None,
                              part_levels: dict[int, int] | None = None) -> bytes:
    """Encode completed historical and current LevelTask rows for this account."""
    current_level = max(1, int(level))
    current_row_id = current_level + 1
    if current_row_id not in LEVEL_PROGRESSION_ROWS:
        return b""
    task_entries = []
    for row_id in range(2, current_row_id + 1):
        level_row = LEVEL_PROGRESSION_ROWS.get(row_id, {})
        for task_id in level_row.get("LevelTasks", []) or []:
            task = LEVEL_PROGRESSION_TASKS.get(int(task_id))
            if task is None:
                continue
            if row_id < current_row_id:
                process = max(0, int(task.get("maxProcess", 0)))
                status = TASK_STATUS_REWARDED
            else:
                process, status = level_task_progress(
                    task, inventory_items, level_task_state, progression_counters,
                    part_levels,
                )
            db_task = b"".join((
                protobuf_int(1, process),
                protobuf_int(2, status),
                protobuf_int(3, 0),
            ))
            entry = protobuf_int(1, int(task_id)) + protobuf_bytes(2, db_task)
            task_entries.append(protobuf_bytes(1, entry))
    if not task_entries:
        return b""
    task_list = b"".join((
        *task_entries,
        protobuf_int(2, 0),  # EndSymbol
        protobuf_int(3, 0),  # Param
        protobuf_int(4, 0),  # Version
    ))
    return protobuf_int(1, LEVEL_TASK_TYPE) + protobuf_bytes(2, task_list)


def update_active_level_task(task_id: int, *, level: int,
                             inventory_items: dict[int, int],
                             level_task_state: dict[str, object],
                             progression_counters: dict[str, object] | None = None,
                             part_levels: dict[int, int] | None = None) -> tuple[int, bool]:
    """Recompute and persist one task only when it belongs to the active tier."""
    active_row = LEVEL_PROGRESSION_ROWS.get(int(level) + 1, {})
    active_ids = {int(value) for value in active_row.get("LevelTasks", []) or []}
    task = LEVEL_PROGRESSION_TASKS.get(int(task_id))
    if task is None or int(task_id) not in active_ids:
        return 3, False  # E_TASK_OPR_RET_INVALID_TASK
    process, status = level_task_progress(
        task, inventory_items, level_task_state, progression_counters, part_levels,
    )
    level_task_state[str(task_id)] = {"process": process, "status": status}
    return 2, True  # E_TASK_OPR_RET_UPDATE_OK


def record_championship_intro_task_progress(app) -> bool:
    """Record one accepted Championship race completion for the active GP tier.

    TaskData 1278226994 is RaceOver (condition 0), RaceMark.ChampRace (2),
    maxProcess 1. Its completion is independent of the win-only reward branch.
    The caller only invokes this after an active match has been successfully
    settled, so replayed or mismatched result messages cannot advance it.
    """
    task = LEVEL_PROGRESSION_TASKS.get(CHAMPIONSHIP_INTRO_TASK_ID)
    level = max(1, int(getattr(app, "level", 1)))
    active_row = LEVEL_PROGRESSION_ROWS.get(level + 1, {})
    active_ids = {int(value) for value in active_row.get("LevelTasks", []) or []}
    if task is None or CHAMPIONSHIP_INTRO_TASK_ID not in active_ids:
        return False

    task_state = getattr(app, "level_task_state", None)
    if not isinstance(task_state, dict):
        return False
    task_key = str(CHAMPIONSHIP_INTRO_TASK_ID)
    previous = task_state.get(task_key, {})
    previous = previous if isinstance(previous, dict) else {}
    process, status = level_task_progress(
        task, getattr(app, "inventory_items", {}), task_state,
        getattr(app, "progression_counters", {}),
        getattr(app, "part_levels", {}),
    )
    maximum = max(0, int(task.get("maxProcess", 0)))
    new_process = min(maximum, max(process, int(previous.get("process", 0)), 1))
    new_status = max(status, int(previous.get("status", TASK_STATUS_DOING)),
                     TASK_STATUS_COMPLETE)
    if (new_process == int(previous.get("process", 0))
            and new_status == int(previous.get("status", TASK_STATUS_DOING))):
        return False
    task_state[task_key] = {"process": new_process, "status": new_status}
    return True


def record_successful_part_upgrade_progress(app) -> dict[str, int]:
    """Update event-backed opening task progress after a real successful upgrade."""
    advances = increment_level_task_counters(app.progression_counters, 18)
    if int(app.level) == 1:
        app.progression_counters[FIRST_LEVEL_UPGRADE_COUNTER] = 1
        _ret, updated = update_active_level_task(
            FIRST_LEVEL_UPGRADE_TASK_ID,
            level=app.level,
            inventory_items=app.inventory_items,
            level_task_state=app.level_task_state,
            progression_counters=app.progression_counters,
            part_levels=app.part_levels,
        )
        if updated:
            advances[FIRST_LEVEL_UPGRADE_COUNTER] = 1
    return advances


def make_player_level_sync(*, level: int, exp: int, power: int, task_score: int,
                           task_score_exchange_process: int,
                           inventory_items: dict[int, int],
                           level_task_state: dict[str, object] | None = None,
                           progression_counters: dict[str, object] | None = None,
                           part_levels: dict[int, int] | None = None) -> bytes:
    """Send an authoritative progression update and the next level's tasks."""
    base_info = b"".join((
        protobuf_int(3, max(1, int(level))),
        protobuf_int(4, max(0, int(inventory_items.get(DIAMOND_ITEM_ID, 0)))),
        protobuf_int(5, max(0, int(inventory_items.get(GOLD_ITEM_ID, 0)))),
        protobuf_int(7, max(0, int(exp))),
        protobuf_int(19, max(0, int(task_score))),
        protobuf_int(20, max(0, int(task_score_exchange_process))),
        protobuf_int(52, max(0, int(power))),
    ))
    player_data = protobuf_bytes(1, base_info)
    level_tasks = make_level_task_container(
        level, inventory_items, level_task_state, progression_counters,
        part_levels,
    )
    if level_tasks:
        player_data += protobuf_bytes(4, level_tasks)
    body = struct.pack(">IH", 6 + len(player_data), 1)
    return websocket_binary_frame(body + player_data)


def make_roguelike_item_proto(key_num: int,
                              search_points: dict[int, int] | None = None) -> bytes:
    """Encode RogueLikeItem.NowItems and KeyNum in PlayerData field 17."""
    proto = bytearray()
    if isinstance(search_points, dict):
        for item_id, quantity in sorted(search_points.items(), key=lambda pair: int(pair[0])):
            if int(item_id) > 0 and int(quantity) > 0:
                entry = protobuf_int(1, int(item_id)) + protobuf_int(2, int(quantity))
                proto.extend(protobuf_bytes(2, entry))  # NowItems: ContainerIntIntProto.
    proto.extend(protobuf_int(3, max(0, int(key_num))))  # RogueLikeItemProto.KeyNum.
    return bytes(proto)


def make_player_daily_proto(*, exchange_key_by_ad_times: int = 0,
                            gold_earned: int = 0) -> bytes:
    """Encode DailyProto.ObtainItemNum (9) and ExchangeKeyByADTimes (12)."""
    gold_count = protobuf_int(1, GOLD_ITEM_ID) + protobuf_int(2, max(0, int(gold_earned)))
    return (protobuf_bytes(9, gold_count)
            + protobuf_int(12, max(0, int(exchange_key_by_ad_times))))


def make_pve_info_proto(pve_info: dict[str, object] | None = None) -> bytes:
    """Encode the runtime-confirmed PlayerDataProto.PveInfo field 11."""
    state = normalize_pve_info(pve_info or {})
    proto = b"".join((
        protobuf_int(1, int(state["Process"])),
        protobuf_int(2, int(state["BlessPart"])),
        protobuf_int(3, int(state["LastCalcTime"])),
        protobuf_int(4, int(state["Gold"])),
    ))
    for field, field_number in (("PartRewards", 5), ("PartRewardsConfig", 8)):
        values = state[field]
        if isinstance(values, dict):
            for index, quantity in sorted(values.items()):
                entry = protobuf_int(1, int(index)) + protobuf_int(2, int(quantity))
                proto += protobuf_bytes(field_number, entry)
    proto += b"".join((
        protobuf_int(6, int(state["FreeRewardTimes"])),
        protobuf_int(7, int(state["GoldRewardConfig"])),
        protobuf_int(9, int(state["MaxCanCalcTime"])),
        protobuf_int(10, int(state["NextCanSmashTime"])),
        protobuf_int(11, int(state["DailyHasSmashTimes"])),
    ))
    return proto


def make_player_data_sync(player_id: int, *, nickname: str = "Local Test",
                          roguelike_key_num: int = TEST_ROGUELIKE_KEYS,
                          roguelike_search_points: dict[int, int] | None = None,
                          roguelike_key_ad_claimed: bool = False,
                          daily_task_claimed: bool = False,
                          achievement_claimed: bool = False,
                          inventory_items: dict[int, int] | None = None,
                          part_fragments: dict[int, int] | None = None,
                          lineups: dict | None = None,
                          guide_step: int = 0,
                          tutorial_battle_complete: bool = False,
                          lottery_info: dict[str, object] | None = None,
                          level: int = 1, exp: int = 0, power: int = 0,
                          task_score: int | None = None,
                          task_score_exchange_process: int = 0,
                          level_task_state: dict[str, object] | None = None,
                          progression_counters: dict[str, object] | None = None,
                          part_levels: dict[int, int] | None = None,
                          chapter_battle_results: dict[int, int] | None = None,
                          chapter_rewards_claimed: list[int] | None = None,
                          pve_info: dict[str, object] | None = None,
                          championship_state: dict[str, object] | None = None,
                          daily_gold_earned: int = 0,
                          save_boxes: dict[str, dict[str, object]] | None = None,
                          deleted_save_box_slots: list[int] | None = None) -> bytes:
    # Runtime protobuf schema: PlayerDataProto.BaseInfo=1; BaseInfo.Id=1,
    # NickName=2, Level=3, Lineups=23; ContainerIntLineup(idx=1,val=2);
    # Lineup(ToyTops=1,MainTop=2,Status=3,Name=4,Emitter=5);
    # ContainerIntToyTop(idx=1,val=2); ToyTop(Top=1,Mid=2,Bot=3).
    # PlayerData.DBTaskLists=4; ContainerIntDBTaskList(idx=1,val=2),
    # DBTaskList.Tasks=1, EndSymbol=2, Param=3, Version=4; task entries
    # ContainerIntDBTask(idx=1,val=2) and DBTask(TaskProcess=1,
    # TaskStatus=2,BeginTime=3).
    # Local config identifies starter parts 2001/2002/2003 as Top/Mid/Bot.
    # Every saved lineup has three complete ToyTops. The client indexes both
    # the three teams and each team's three ToyTops without bounds checks.
    lineup_state = normalize_lineups(lineups)
    lineup_containers = []
    for lineup_index in range(1, 4):
        toy_containers = []
        for toy_index in range(1, 4):
            parts = lineup_state[str(lineup_index)][str(toy_index)]
            toy_top = b"".join(
                protobuf_int(field_number, int(parts[slot]))
                for slot, field_number in LINEUP_PART_FIELDS.items()
            )
            toy_containers.append(protobuf_bytes(
                1, protobuf_int(1, toy_index) + protobuf_bytes(2, toy_top)
            ))
        lineup = b"".join((
            *toy_containers,
            protobuf_int(2, 1),  # Client's one-based MainTop maps to mainIndex zero.
            protobuf_int(3, 1 if lineup_index == 1 else 0),  # Team A is Main.
            protobuf_string(4, nickname[:24]),
            protobuf_int(5, 0),
        ))
        lineup_containers.append(
            protobuf_bytes(23, protobuf_int(1, lineup_index) + protobuf_bytes(2, lineup))
        )
    # BaseInfo owns all three starter parts. The slot values are the runtime
    # ToytopParts.PartAs enum values used when composing a ToyTop.
    def owned_part(part_id: int, part_as: int, *, level: int = SYNTHETIC_STARTER_LEVEL,
                   piece: int = 0) -> bytes:
        part = b"".join((
            protobuf_int(1, level),  # Level
            protobuf_int(2, piece),  # Piece
            protobuf_int(3, part_as),
        ))
        return protobuf_int(1, part_id) + protobuf_bytes(2, part)

    inventory_items = inventory_items or {}
    def inventory_count(item_id: int) -> int:
        return max(0, int(inventory_items.get(item_id, inventory_items.get(str(item_id), 0))))

    parts_by_location = {0: [], 1: [], 2: []}
    owned_parts = {
        part_id: (part_as, SYNTHETIC_STARTER_LEVEL)
        for part_id, part_as in SYNTHETIC_STARTER_PARTS
    }
    # Non-starter parts are serialized as owned only when they are present in
    # profile inventory or part-level state. Part 2011 is a later guide
    # acquisition; do not synthesize ownership before that action occurs.
    for part_id, location in PART_ITEM_LOCATIONS.items():
        if inventory_count(part_id) > 0:
            owned_parts[part_id] = (location + 1, 1)
    for part_id, part_level in (part_levels or {}).items():
        part_id, part_level = int(part_id), int(part_level)
        if part_id in UPGRADEABLE_PART_LOCATIONS and part_level > 0:
            location = UPGRADEABLE_PART_LOCATIONS[part_id]
            owned_parts[part_id] = (location + 1, min(15, part_level))
    part_fragments = part_fragments or {}
    for part_id, location in ALL_SERIALIZED_PART_LOCATIONS.items():
        fragment_count = max(0, int(part_fragments.get(part_id, part_fragments.get(str(part_id), 0))))
        if fragment_count > 0 and part_id not in owned_parts:
            # A positive valid part-fragment count represents a successful
            # acquisition. Keep this serialization safeguard aligned with
            # reward crediting and legacy-profile migration.
            owned_parts[part_id] = (location + 1, part_acquisition_level(part_id))
        if part_id in owned_parts:
            part_as, part_level = owned_parts[part_id]
            piece_count = fragment_count
        elif fragment_count > 0:
            # With Level=0 the client keeps the part locked, while its item
            # counter still reads Piece+1 from the matching BaseInfo list.
            part_as, part_level = location + 1, 0
            piece_count = fragment_count - 1
        else:
            continue
        parts_by_location[location].append(owned_part(
            part_id, part_as, level=part_level, piece=piece_count,
        ))
    # The client stores its two wallet currencies outside DefaultItems:
    # BaseInfo.Diamond=field 4 and BaseInfo.Gold=field 5. The game metadata
    # marks these IDs as MoneyDiamond/MoneyGold, so getItemNumByID reads these
    # scalar fields and ignores duplicate values under DefaultItems.
    base_info = b"".join((
        protobuf_int(1, player_id),
        protobuf_string(2, nickname[:24]),
        protobuf_int(3, max(1, int(level))),
        protobuf_int(4, inventory_count(DIAMOND_ITEM_ID)),
        protobuf_int(5, inventory_count(GOLD_ITEM_ID)),
        protobuf_int(7, max(0, int(exp))),
        protobuf_int(19, max(0, int(10 if daily_task_claimed else 0) if task_score is None else int(task_score))),
        protobuf_int(20, max(0, int(task_score_exchange_process))),
        protobuf_int(42, max(0, int((championship_state or {}).get("top_champion_cups", 0)))),
        protobuf_int(44, max(0, int((championship_state or {}).get("top_champion_init_date", 0)))),
        protobuf_int(52, max(0, int(power))),
        # Preserve the default element at index 0; BaseInfo.Lineups entries
        # 1..3 are Team A/B/C and each contains three ToyTops.
        protobuf_bytes(23, b""),
        *lineup_containers,
    ))
    for slot, box in sorted(normalize_save_boxes(save_boxes).items(),
                            key=lambda row: int(row[0])):
        save_box = b"".join((
            protobuf_int(1, int(box["item_id"])),
            protobuf_int(2, int(box["can_open_time"])),
            protobuf_int(3, int(box["ready_countdown_time"])),
            protobuf_string(4, str(box["reason"])),
            protobuf_int(5, int(box["pile"])),
        ))
        slot_entry = protobuf_int(1, int(slot)) + protobuf_bytes(2, save_box)
        base_info += protobuf_bytes(9, slot_entry)  # BaseInfo.SaveBoxList.
    # SyncDataManager merges repeated SaveBoxList entries into its local map.
    # An absent slot is not a deletion; a slot container with idx and no val
    # is the client-defined tombstone that removes an opened chest immediately.
    for slot in sorted({int(value) for value in (deleted_save_box_slots or [])
                        if int(value) >= 0}):
        base_info += protobuf_bytes(9, protobuf_int(1, slot))
    for location, field_number in ((0, 15), (1, 16), (2, 17)):
        for part_row in parts_by_location[location]:
            base_info += protobuf_bytes(field_number, part_row)
    # BaseInfo.DefaultItems is repeated ContainerIntIntProto at field 22;
    # the verified runtime schema maps idx=item ID and val=owned quantity.
    for item_id, quantity in sorted(inventory_items.items()):
        if (int(item_id) not in (DIAMOND_ITEM_ID, GOLD_ITEM_ID, TASK_SCORE_ITEM_ID)
                and int(item_id) not in UPGRADEABLE_PART_LOCATIONS
                and int(item_id) > 0 and int(quantity) > 0):
            item_count = protobuf_int(1, int(item_id)) + protobuf_int(2, int(quantity))
            base_info += protobuf_bytes(22, item_count)
    # The static TaskData table has a DailyStaticTask row 1146368048
    # (LoginTimes, target 1). Give this disposable profile one completed,
    # unclaimed daily task so the Missions page has a real local row to render.
    daily_task = b"".join((
        protobuf_int(1, 1),  # TaskProcess
        protobuf_int(2, 3 if daily_task_claimed else 2),  # COMPLETE or REWARDED
        protobuf_int(3, 0),  # BeginTime
    ))
    daily_task_entry = protobuf_int(1, 1146368048) + protobuf_bytes(2, daily_task)
    daily_task_list = b"".join((
        protobuf_bytes(1, daily_task_entry),
        protobuf_int(2, 0),  # EndSymbol
        protobuf_int(3, 0),  # Param
        protobuf_int(4, 0),  # Version
    ))
    daily_task_container = protobuf_int(1, DAILY_TASK_TYPE) + protobuf_bytes(2, daily_task_list)
    level_task_container = make_level_task_container(
        level, inventory_items, level_task_state, progression_counters,
        part_levels,
    )

    # GuideMark.Over is bit 2 (mask 4). Keep new local profiles at zero so the
    # original client can run its introductory tutorial; later guide grids
    # persist independently through the client's GuideGrid:* local-storage keys.
    # PlayerDataProto.RogueLikeItem=17; NowItems=2, KeyNum=3. Search-point
    # items use ItemType.RogueLikeItem and the client reads NowItems by ID.
    roguelike_item = make_roguelike_item_proto(roguelike_key_num, roguelike_search_points)
    player_data = b"".join((
        protobuf_bytes(1, base_info),
        protobuf_bytes(2, make_player_daily_proto(
            exchange_key_by_ad_times=1 if roguelike_key_ad_claimed else 0,
            gold_earned=daily_gold_earned,
        )),
        protobuf_bytes(3, make_champion_history(
            championship_state or normalize_championship_state(),
        )),
        protobuf_bytes(4, daily_task_container),
        protobuf_int(9, max(0, int(guide_step))),
        protobuf_bytes(17, roguelike_item),
        protobuf_bytes(11, make_pve_info_proto(pve_info)),
        # One explicitly synthetic, claimable achievement lets us test the
        # client flow without pretending the local profile has real progress.
        protobuf_bytes(14, make_achievement_proto(
            level=2 if achievement_claimed else 1,
            reward_time=1514736000 if achievement_claimed else 0,
            value=10, total_score=5 if achievement_claimed else 0,
        )),
        protobuf_bytes(25, make_lottery_info_proto(lottery_info)),
    ))
    if tutorial_battle_complete:
        player_data += protobuf_bytes(3, make_tutorial_quick_race_history())
    for stage_id, result in sorted((chapter_battle_results or {}).items(),
                                   key=lambda pair: int(pair[0])):
        stage_id, result = int(stage_id), int(result)
        if stage_id in PVE_CHAPTER_ROWS and int(PVE_CHAPTER_ROWS[stage_id]["IndexInChapter"]) <= PVE_BATTLE_STAGES_PER_CHAPTER and result & 1:
            entry = protobuf_int(1, stage_id) + protobuf_int(2, min(7, result))
            player_data += protobuf_bytes(6, entry)  # PlayerData.ChapterBattleResult.
    for chapter_id in sorted({int(value) for value in (chapter_rewards_claimed or [])}):
        if 1 <= chapter_id <= 10:
            entry = protobuf_int(1, chapter_id) + protobuf_int(2, 1)
            player_data += protobuf_bytes(7, entry)  # PlayerData.ChapterReward.
    if level_task_container:
        player_data += protobuf_bytes(4, level_task_container)
    body = struct.pack(">IH", 6 + len(player_data), 1) + player_data
    return websocket_binary_frame(body)


def make_app_player_data_sync(app, *, deleted_save_box_slots: list[int] | None = None) -> bytes:
    """Build a full authoritative player sync from one local account profile."""
    refresh_save_box_timers(app)
    account = getattr(app, "account", {}) or {}
    nickname = (account.get("username")
                or (account.get("email") or "Beta Tester").split("@", 1)[0])
    return make_player_data_sync(
        int(app.player_id), nickname=nickname,
        roguelike_key_num=int(app.roguelike_keys),
        roguelike_search_points=getattr(app, "roguelike_search_points", {}),
        roguelike_key_ad_claimed=roguelike_free_key_claimed_today(app),
        daily_task_claimed=bool(getattr(app, "daily_task_claimed", False)),
        achievement_claimed=bool(getattr(app, "achievement_claimed", False)),
        inventory_items=getattr(app, "inventory_items", {}),
        part_fragments=getattr(app, "part_fragments", {}),
        lineups=getattr(app, "lineups", None),
        guide_step=int(getattr(app, "guide_step", 0)),
        tutorial_battle_complete=bool(getattr(app, "tutorial_battle_complete", False)),
        lottery_info=getattr(app, "lottery_info", None),
        level=int(getattr(app, "level", 1)), exp=int(getattr(app, "exp", 0)),
        power=int(getattr(app, "power", 0)),
        task_score=int(getattr(app, "task_score", 0)),
        task_score_exchange_process=int(getattr(app, "task_score_exchange_process", 0)),
        level_task_state=getattr(app, "level_task_state", {}),
        progression_counters=getattr(app, "progression_counters", {}),
        part_levels=getattr(app, "part_levels", {}),
        chapter_battle_results=getattr(app, "chapter_battle_results", {}),
        chapter_rewards_claimed=getattr(app, "chapter_rewards_claimed", []),
        pve_info=getattr(app, "pve_info", {}),
        championship_state=getattr(app, "championship_state", None),
        daily_gold_earned=int(getattr(app, "daily_gold_earned", 0)),
        save_boxes=getattr(app, "save_boxes", {}),
        deleted_save_box_slots=deleted_save_box_slots,
    )


def make_roguelike_key_sync(key_num: int,
                            search_points: dict[int, int] | None = None) -> bytes:
    """Sync the Adventure key count and search-point items through PlayerData."""
    rogue_item = make_roguelike_item_proto(key_num, search_points)
    player_data = protobuf_bytes(17, rogue_item)  # PlayerDataProto.RogueLikeItem
    body = struct.pack(">IH", 6 + len(player_data), 1) + player_data
    return websocket_binary_frame(body)


def make_initial_roguelike_map_state(*, version: int, chapter_id: int,
                                     chapter_level: int, seed: int,
                                     start_location: int,
                                     hp: list[int]) -> dict:
    """Create a fresh Adventure state with its Start tile already traversable."""
    return {
        "version": int(version),
        "chapter_id": int(chapter_id),
        "chapter_level": int(chapter_level),
        "seed": int(seed),
        "location": int(start_location),
        "start_location": int(start_location),
        "hp": list(hp),
        # Cocos creates the Start grid locally as NodeInit. Its movement
        # predicate only accepts a NodeCompleted current tile, so this status
        # must be present in the first RogueLikeInfo sync.
        "nodes": {
            str(int(start_location)): {
                "status": 3,  # NodeCompleted
                "event_id": 0,
                "location_exports": [],
            },
        },
    }


def make_roguelike_info_sync(state: dict) -> bytes:
    """Build message 5 (RogueLikeInfoProto) for the local Adventure session.

    The client derives RogueLikeManager.lineup from PlayerStatus.MainTop,
    Emitter and ToyTops. Sending only Location leaves that lineup empty even
    though PlayerData contains a main lineup, so BattleInit later receives a
    local MatchPlayer with no MatchToyTopInfo entries.
    """
    status = b"".join((
        protobuf_int(1, int(state.get("location", 0))),  # PlayerStatus.Location
        protobuf_int(2, int(state.get("main_top", 1))),  # MainTop is one-based.
        protobuf_int(3, int(state.get("emitter", 0))),   # 0 falls back to InitEmitter in the client.
    ))
    # RogueLikeStatus.EventBuff is repeated ContainerIntIntProto field 5.
    # The client derives the Modules panel directly from this map.
    event_buffs = state.get("event_buffs", {})
    if isinstance(event_buffs, dict):
        for item_id, quantity in sorted(event_buffs.items(), key=lambda pair: int(pair[0])):
            if int(item_id) > 0 and int(quantity) > 0:
                entry = protobuf_int(1, int(item_id)) + protobuf_int(2, int(quantity))
                status += protobuf_bytes(5, entry)

    # RogueLikeStatus.ToyTops is repeated ContainerIntBattleToyTopProto.
    # Each toy top stores HP plus Parts keyed by the actual part IDs; the
    # BattlePart values contain Level and CurSkin. These IDs match the
    # synthetic parts already owned in make_player_data_sync().
    hp_values = state.get("hp") or [0]
    if hp_values:
        battle_top = protobuf_int(1, max(0, int(hp_values[0])))  # BattleToyTop.HP
        for part_id, _part_as in SYNTHETIC_BATTLE_PARTS:
            battle_part = b"".join((
                protobuf_int(1, SYNTHETIC_STARTER_LEVEL),  # BattlePart.Level
                protobuf_int(2, 0),  # BattlePart.CurSkin
            ))
            part_entry = protobuf_int(1, part_id) + protobuf_bytes(2, battle_part)
            battle_top += protobuf_bytes(3, part_entry)  # BattleToyTop.Parts
        top_entry = protobuf_int(1, 1) + protobuf_bytes(2, battle_top)
        status += protobuf_bytes(4, top_entry)  # PlayerStatus.ToyTops

    map_proto = b"".join((
        protobuf_int(1, int(state.get("version", 0))),
        protobuf_bytes(2, status),
        protobuf_int(3, int(state.get("chapter_id", 0))),
        protobuf_int(4, int(state.get("chapter_level", 1))),
        protobuf_int(5, int(state.get("seed", 0))),
    ))
    for location, row in sorted(state.get("nodes", {}).items()):
        node_proto = protobuf_int(1, int(row.get("status", 0)))
        # loadMap reads EventId for every persisted node, including the start.
        event_proto = protobuf_int(1, int(row.get("event_id", 0)))
        if row.get("export_bit") is not None:
            event_proto += protobuf_int(3, int(row["export_bit"]))
        node_proto += protobuf_bytes(2, event_proto)
        entry = protobuf_int(1, int(location)) + protobuf_bytes(2, node_proto)
        map_proto += protobuf_bytes(6, entry)  # RogueLikeMapProto.Nodes

    info = protobuf_bytes(1, map_proto)  # RogueLikeInfoProto.RogueLikeMap
    body = struct.pack(">IH", 6 + len(info), 5) + info
    return websocket_binary_frame(body)


def make_roguelike_reply(code: int, *, location: int | None = None,
                         chapter_id: int | None = None,
                         rewards: bytes | None = None) -> bytes:
    """Build ESC_RogueLikeRet (message 180) from the verified runtime schema."""
    proto = protobuf_int(1, code)  # SC_RogueLikeRet.code
    if location is not None:
        proto += protobuf_int(4, location)
    if chapter_id is not None:
        proto += protobuf_int(5, chapter_id)
    if rewards is not None:
        proto += protobuf_bytes(3, rewards)  # SC_RogueLikeRet.Rewards (SC_GetItems)
    body = struct.pack(">IH", 6 + len(proto), 180) + proto
    return websocket_binary_frame(body)


def make_item_opr_response(item_opr_ret: int, *, item_key: int = 0,
                           param1: int | None = None) -> bytes:
    """Encode SC_ItemOpr (136) with the live-confirmed client schema."""
    proto = protobuf_int(1, int(item_opr_ret))
    if item_key:
        proto += protobuf_int(2, int(item_key))
    if param1 is not None:
        proto += protobuf_int(3, int(param1))
    body = struct.pack(">IH", 6 + len(proto), 136) + proto
    return websocket_binary_frame(body)


def make_box_open_get_items(box_id: int,
                            rewards: list[dict[str, int]]) -> bytes:
    """Build the client's GIF_OPENBOX receipt after authoritative profile sync."""
    item_infos = b"".join(
        protobuf_bytes(1, protobuf_int(1, int(row["item_id"]))
                      + protobuf_int(2, int(row["num"])))
        for row in rewards if int(row.get("item_id", 0)) > 0
        and int(row.get("num", 0)) > 0
    )
    items_detail = protobuf_bytes(2, item_infos) if item_infos else b""
    proto = (protobuf_int(1, 1)  # GetItemFrom.GIF_OPENBOX.
             + protobuf_bytes(2, items_detail)
             + protobuf_int(4, int(box_id)))
    body = struct.pack(">IH", 6 + len(proto), 143)  # ESC_GetItems / SC_GetItems.
    return websocket_binary_frame(body + proto)


def make_roguelike_trigger_rewards(chapter_id: int,
                                   item_infos: list[dict[str, int]] | None = None) -> bytes:
    """Create SC_GetItems with the verified client ItemInfo schema.

    ItemInfo.ID and Num are fields 1 and 2; ItemsDetail.ItemInfos is repeated
    field 1. Empty rewards retain a non-null envelope so the client can use its
    own static LocationExports fallback.
    """
    rewards = b"".join(
        protobuf_bytes(1, protobuf_int(1, int(item["item_id"])) +
                      protobuf_int(2, int(item["num"])))
        for item in (item_infos or [])
        if int(item.get("item_id", 0)) > 0 and int(item.get("num", 0)) > 0
    )
    items_detail = protobuf_bytes(2, rewards) if rewards else b""
    return b"".join((
        protobuf_int(1, 16),  # GetItemFrom.GIF_ROGUE_LIKE_TRIGGER
        items_detail,         # SC_GetItems.Items -> ItemsDetail.ItemInfos
        protobuf_int(4, chapter_id),  # KeyParam: the active Roguelike chapter
    ))


def make_roguelike_trigger_response_frames(app, *, chapter_id: int, location: int,
                                           result: dict[str, object],
                                           progression_sync: bool = False) -> list[bytes]:
    """Build Adventure trigger syncs before the native reward receipt.

    The client's process_GetItems reads the already-synced inventory balance
    to calculate the old/new count shown in its reward UI. SC_RogueLikeRet
    carries the receipt, but does not update BaseInfo.DefaultItems or wallet
    totals on its own, so successful item rewards need PlayerData first.
    """
    frames: list[bytes] = []
    response_code = int(result["response_code"])
    delivery_items = list(result["delivery_items"])
    search_point_rewards = list(result["search_point_rewards"])
    sync_state = result["sync_state"]

    if sync_state is not None:
        frames.append(make_roguelike_info_sync(sync_state))
    if response_code == 31 and (
            delivery_items or progression_sync or result.get("inventory_changed")):
        # Full authoritative sync updates currencies, normal items, and part
        # fragments before process_RogueLikeRet -> process_GetItems runs. It
        # also carries updated LevelTask progress when a node advances GP.
        frames.append(make_app_player_data_sync(app))
    if response_code == 31 and search_point_rewards:
        frames.append(make_roguelike_key_sync(
            app.roguelike_keys, app.roguelike_search_points,
        ))

    reward_popup_items = delivery_items + (
        search_point_rewards if response_code == 31 else []
    )
    reward_envelope = (
        make_roguelike_trigger_rewards(chapter_id, reward_popup_items)
        if response_code == 31 else None
    )
    frames.append(make_roguelike_reply(
        response_code, location=location, chapter_id=chapter_id,
        rewards=reward_envelope,
    ))
    return frames


def credit_item_reward(inventory_items: dict[int, int],
                       part_fragments: dict[int, int],
                       item_id: int, quantity: int,
                       part_levels: dict[int, int] | None = None) -> str:
    """Credit a reward to the client-owned store for that item's type."""
    item_id, quantity = int(item_id), max(0, int(quantity))
    if item_id in UPGRADEABLE_PART_LOCATIONS:
        part_fragments[item_id] = int(part_fragments.get(item_id, 0)) + quantity
        if quantity > 0:
            # Recovered client ownership checks use BaseInfo part Level >= 1;
            # its item counter separately reads Piece + 1. The original server
            # acquisition rule is unavailable, but the reported game behavior
            # and client data support unlocking a usable part on its first
            # valid fragment reward while retaining the fragments for upgrades.
            acquired_level = part_acquisition_level(item_id)
            if isinstance(part_levels, dict):
                part_levels[item_id] = max(int(part_levels.get(item_id, 0)), acquired_level)
        return "part_fragments"
    inventory_items[item_id] = int(inventory_items.get(item_id, 0)) + quantity
    return "inventory_items"


def part_acquisition_level(part_id: int) -> int:
    """Return the client's configured starting level for a valid part."""
    upgrade = ITEM_UPGRADE_ROWS.get(PART_UPGRADE_KEYS.get(int(part_id), 0), {})
    return min(15, max(1, int(upgrade.get("initLevel", 1))))


def credit_profile_item_reward(app, item_id: int, quantity: int) -> str:
    fragments = getattr(app, "part_fragments", {})
    app.part_fragments = fragments
    if int(item_id) == GOLD_ITEM_ID and int(quantity) > 0:
        record_daily_gold_earned(app, int(quantity))
    levels = getattr(app, "part_levels", None)
    if not isinstance(levels, dict):
        levels = {}
        app.part_levels = levels
    return credit_item_reward(
        app.inventory_items, fragments, item_id, quantity,
        levels,
    )


def credit_box_reward(app, item_id: int, quantity: int) -> str:
    """Credit a recovered box reward using its Item.itemType classification."""
    item_id, quantity = int(item_id), max(0, int(quantity))
    item = BOX_ITEM_META.get(item_id)
    if item is not None and int(item.get("itemType", -1)) in (0, 1, 2):
        return credit_profile_item_reward(app, item_id, quantity)
    return credit_profile_item_reward(app, item_id, quantity)


def apply_part_level_upgrade(app, part_id: int) -> dict[str, object]:
    """Apply one native part upgrade using the client's ItemUpgrade table."""
    part_id = int(part_id)
    if part_id not in UPGRADEABLE_PART_LOCATIONS:
        return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
    upgrade_key = PART_UPGRADE_KEYS.get(part_id)
    upgrade = ITEM_UPGRADE_ROWS.get(upgrade_key or 0)
    if upgrade is None:
        return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR, "changed": False}

    levels = owned_part_levels(app.inventory_items, getattr(app, "part_levels", {}))
    stored_level = int(levels.get(part_id, 0))
    init_level = max(1, int(upgrade.get("initLevel", 1)))
    if int(upgrade.get("initLevel", 0)) <= 0:
        return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}
    # The client raises an unowned part's displayed level to initLevel in
    # TopPartsDetailsUI.getSDataAndUpData before sending Item_LvUp. Do the same
    # here so a fragment-only part can be unlocked/upgraded on its first level.
    current_level = max(stored_level, init_level)
    max_level = max(1, min(15, int(upgrade.get("maxLevel", 15))))
    if current_level >= max_level:
        return {"item_opr_ret": ITEM_OPR_RET_MAX_LEVEL, "changed": False,
                "current_level": current_level}

    level_row = upgrade.get(f"Level{current_level}")
    if not isinstance(level_row, dict) or int(level_row.get("level", 0)) <= 0:
        return {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR, "changed": False,
                "current_level": current_level}
    required_piece = max(0, int(level_row.get("piece", 0)))
    current_piece = max(0, int(app.part_fragments.get(part_id, 0)))
    if current_piece < required_piece:
        return {"item_opr_ret": ITEM_OPR_RET_PIECE_LIMIT, "changed": False,
                "current_level": current_level,
                "required_piece": required_piece, "current_piece": current_piece}

    gold_cost = 0
    other_costs: dict[int, int] = {}
    rewards: dict[int, int] = {}
    for row in level_row.get("item") or []:
        if not isinstance(row, dict):
            continue
        item_id = int(row.get("id", 0))
        amount = int(row.get("num", 0))
        if item_id <= 0 or amount == 0:
            continue
        if amount > 0 and item_id == GOLD_ITEM_ID:
            gold_cost += amount
        elif amount > 0:
            other_costs[item_id] = other_costs.get(item_id, 0) + amount
        elif amount < 0:
            # ItemUpgrade uses negative Item.Num for gains on the successful
            # level-up row; the row's positive Gold Num is the payable cost.
            rewards[item_id] = rewards.get(item_id, 0) - amount

    current_gold = max(0, int(app.inventory_items.get(GOLD_ITEM_ID, 0)))
    if current_gold < gold_cost:
        return {"item_opr_ret": ITEM_OPR_RET_MONEY_NOT_ENOUGH, "changed": False,
                "money_type": ITEM_OPR_MONEY_GOLD,
                "gold_cost": gold_cost, "gold_before": current_gold}
    for item_id, cost in other_costs.items():
        if max(0, int(app.inventory_items.get(item_id, 0))) < cost:
            return {"item_opr_ret": ITEM_OPR_RET_COST_NOT_ENOUGH, "changed": False,
                    "cost_item_id": item_id, "cost_required": cost}

    app.inventory_items[GOLD_ITEM_ID] = current_gold - gold_cost
    for item_id, cost in other_costs.items():
        app.inventory_items[item_id] = max(0, int(app.inventory_items.get(item_id, 0)) - cost)
        if app.inventory_items[item_id] == 0:
            app.inventory_items.pop(item_id, None)
    app.part_fragments[part_id] = current_piece - required_piece
    if app.part_fragments[part_id] == 0:
        app.part_fragments.pop(part_id, None)
    for item_id, quantity in rewards.items():
        credit_profile_item_reward(app, item_id, quantity)

    new_level = min(max_level, current_level + int(level_row["level"]))
    app.part_levels[part_id] = new_level
    return {
        "item_opr_ret": ITEM_OPR_RET_UPGRADE_OK,
        "changed": True,
        "part_id": part_id,
        "upgrade_key": upgrade_key,
        "part_was_owned": stored_level > 0,
        "level_before": current_level,
        "level_after": new_level,
        "piece_required": required_piece,
        "piece_before": current_piece,
        "piece_after": max(0, current_piece - required_piece),
        "gold_cost": gold_cost,
        "gold_before": current_gold,
        "gold_after": max(0, current_gold - gold_cost),
        "rewards": [{"item_id": item_id, "num": quantity}
                    for item_id, quantity in sorted(rewards.items())],
    }


def apply_emitter_material_upgrade(app, source_item_id: int,
                                   compose_num: int) -> dict[str, object]:
    """Combine emitter materials using the live client conversion table."""
    source_item_id, compose_num = int(source_item_id), int(compose_num)
    rule = EMITTER_MATERIAL_COMPOSE_RULES.get(source_item_id)
    if rule is None or compose_num <= 0:
        return {"item_opr_ret": ITEM_OPR_RET_INVALID_ITEM, "changed": False}

    pieces_required = int(rule["pieces_per_output"]) * compose_num
    gold_required = int(rule["gold_per_output"]) * compose_num
    source_before = max(0, int(app.inventory_items.get(source_item_id, 0)))
    gold_before = max(0, int(app.inventory_items.get(GOLD_ITEM_ID, 0)))
    if source_before < pieces_required:
        return {"item_opr_ret": ITEM_OPR_RET_COST_NOT_ENOUGH,
                "changed": False, "source_item_id": source_item_id,
                "source_before": source_before,
                "pieces_required": pieces_required}
    if gold_before < gold_required:
        return {"item_opr_ret": ITEM_OPR_RET_MONEY_NOT_ENOUGH,
                "changed": False, "money_type": ITEM_OPR_MONEY_GOLD,
                "gold_before": gold_before, "gold_required": gold_required}

    output_item_id = int(rule["output_item_id"])
    output_before = max(0, int(app.inventory_items.get(output_item_id, 0)))
    app.inventory_items[source_item_id] = source_before - pieces_required
    if app.inventory_items[source_item_id] == 0:
        app.inventory_items.pop(source_item_id, None)
    app.inventory_items[output_item_id] = output_before + compose_num
    app.inventory_items[GOLD_ITEM_ID] = gold_before - gold_required
    return {
        "item_opr_ret": 27,  # E_ITEMRET_EMITTER_MATERIAL_LEVEL_UP_OK.
        "changed": True,
        "source_item_id": source_item_id,
        "output_item_id": output_item_id,
        "compose_num": compose_num,
        "pieces_required": pieces_required,
        "source_before": source_before,
        "source_after": source_before - pieces_required,
        "gold_required": gold_required,
        "gold_before": gold_before,
        "gold_after": gold_before - gold_required,
        "output_before": output_before,
        "output_after": output_before + compose_num,
    }


def apply_roguelike_shop_trigger(app, *, chapter_id: int, location: int,
                                  event_id: int, export_ids: list[int],
                                  hp_values: list[int], trigger_location: int
                                  ) -> dict[str, object]:
    """Apply one price-checked Adventure shop offer, idempotently.

    The client sends TriggerLocation as a one-bit selection mask and clears
    CurNode.Event.LocationExports. Prices come from the client-extracted
    RogueLikeExport rows, while EnterNode's cached exports provide the index.
    """
    response_code = 32  # TriggerLocationError unless a valid active shop exists.
    sync_state = None
    should_save = False
    repeated = False
    reward_items: list[dict[str, int]] = []
    search_point_rewards: list[dict[str, int]] = []
    chiji_rewards: list[dict[str, int]] = []
    unknown_export_ids: list[int] = []
    effective_export_ids: list[int] = []
    cost_item_id = 0
    cost_num = 0
    cost_before = 0
    cost_after = 0
    node_status = None
    export_bit = None
    shop_offer_index = None

    with app.roguelike_lock:
        refresh_shared_profile_state(app)
        state = app.roguelike_map_state
        if state is not None and int(state.get("chapter_id", -1)) == int(chapter_id):
            previous = state["nodes"].get(int(location), {})
            cached_exports = [int(value) for value in previous.get("location_exports", [])]
            cached_event_id = int(previous.get("event_id", 0))
            invalid_event = bool(event_id and cached_event_id and int(event_id) != cached_event_id)
            mask = int(trigger_location)
            if invalid_event:
                response_code = 32
            elif not cached_exports or mask <= 0 or mask & (mask - 1):
                response_code = 38  # TriggerInvalidTriggerLocation.
            else:
                shop_offer_index = mask.bit_length() - 1
                if shop_offer_index >= len(cached_exports):
                    response_code = 38
                else:
                    selected_export_id = cached_exports[shop_offer_index]
                    effective_export_ids = [selected_export_id]
                    if export_ids and selected_export_id not in [int(value) for value in export_ids]:
                        response_code = 38
                    else:
                        price = ROGUELIKE_SHOP_PRICES.get(str(selected_export_id))
                        reward_items, unknown_export_ids = resolve_roguelike_export_rewards(
                            [selected_export_id],
                        )
                        search_point_rewards = resolve_roguelike_search_point_rewards(
                            [selected_export_id],
                        )
                        chiji_rewards = resolve_roguelike_chiji_rewards([selected_export_id])
                        if (price is None or unknown_export_ids
                                or not (reward_items or search_point_rewards or chiji_rewards)):
                            response_code = 35  # TriggerTableError.
                        else:
                            cost_item_id = int(price["worth_id"])
                            cost_num = int(price["worth_num"])
                            existing_bit = max(0, int(previous.get("export_bit", 0)))
                            purchase_records = previous.get("shop_purchase_records", {})
                            if not isinstance(purchase_records, dict):
                                purchase_records = {}
                            saved_purchase = purchase_records.get(str(shop_offer_index), {})
                            already_purchased = bool(existing_bit & mask)
                            if already_purchased:
                                repeated = True
                                if (isinstance(saved_purchase, dict)
                                        and int(saved_purchase.get("export_id", 0)) == selected_export_id):
                                    reward_items = [
                                        {"item_id": int(row["item_id"]), "num": int(row["num"])}
                                        for row in saved_purchase.get("delivery_items", [])
                                        if isinstance(row, dict)
                                    ]
                                    search_point_rewards = [
                                        {"item_id": int(row["item_id"]), "num": int(row["num"])}
                                        for row in saved_purchase.get("search_point_rewards", [])
                                        if isinstance(row, dict)
                                    ]
                                    chiji_rewards = [
                                        {"item_id": int(row["item_id"]), "num": int(row["num"])}
                                        for row in saved_purchase.get("chiji_rewards", [])
                                        if isinstance(row, dict)
                                    ]
                                    sync_state = json.loads(json.dumps(state))
                                    response_code = 31
                                    node_status = int(previous.get("status", 2))
                                    export_bit = existing_bit
                                    cost_item_id = int(saved_purchase.get("cost_item_id", cost_item_id))
                                    cost_num = int(saved_purchase.get("cost_num", cost_num))
                                else:
                                    response_code = 33  # TriggerRepeated without a saved receipt.
                                    reward_items = []
                                    search_point_rewards = []
                                    chiji_rewards = []
                            elif int(previous.get("status", 0)) >= 3:
                                response_code = 33
                                reward_items = []
                                search_point_rewards = []
                                chiji_rewards = []
                            else:
                                inventory = getattr(app, "inventory_items", {})
                                cost_before = max(0, int(inventory.get(
                                    cost_item_id, inventory.get(str(cost_item_id), 0),
                                )))
                                if cost_before < cost_num:
                                    response_code = 37  # TriggerWorthError.
                                    reward_items = []
                                    search_point_rewards = []
                                    chiji_rewards = []
                                else:
                                    cost_after = cost_before - cost_num
                                    inventory[cost_item_id] = cost_after
                                    if cost_after == 0:
                                        inventory.pop(str(cost_item_id), None)
                                    app.inventory_items = inventory
                                    for item in reward_items:
                                        credit_profile_item_reward(
                                            app, int(item["item_id"]), int(item["num"]),
                                        )
                                    current_search_points = getattr(
                                        app, "roguelike_search_points", {},
                                    )
                                    app.roguelike_search_points = current_search_points
                                    for item in search_point_rewards:
                                        item_id, quantity = int(item["item_id"]), int(item["num"])
                                        current_search_points[item_id] = (
                                            int(current_search_points.get(item_id, 0)) + quantity
                                        )
                                    event_buffs = state.setdefault("event_buffs", {})
                                    if not isinstance(event_buffs, dict):
                                        event_buffs = {}
                                        state["event_buffs"] = event_buffs
                                    for item in chiji_rewards:
                                        item_id, quantity = int(item["item_id"]), int(item["num"])
                                        event_buffs[str(item_id)] = (
                                            int(event_buffs.get(str(item_id), 0)) + quantity
                                        )

                                    new_export_bit = existing_bit | mask
                                    all_offers_mask = (1 << len(cached_exports)) - 1
                                    node_status = (
                                        3 if new_export_bit & all_offers_mask == all_offers_mask
                                        else 2  # NodeTrigger keeps a partially purchased shop movable.
                                    )
                                    saved_records = dict(purchase_records)
                                    saved_records[str(shop_offer_index)] = {
                                        "export_id": selected_export_id,
                                        "cost_item_id": cost_item_id,
                                        "cost_num": cost_num,
                                        "delivery_items": reward_items,
                                        "search_point_rewards": search_point_rewards,
                                        "chiji_rewards": chiji_rewards,
                                    }
                                    updated_node = dict(previous)
                                    updated_node.update({
                                        "status": node_status,
                                        "event_id": cached_event_id or int(event_id),
                                        "location_exports": cached_exports,
                                        "export_bit": new_export_bit,
                                        "shop_purchase_records": saved_records,
                                        "reward_export_ids": sorted({
                                            int(value) for value in previous.get("reward_export_ids", [])
                                        } | {selected_export_id}),
                                        "trigger_location": mask,
                                    })
                                    state["location"] = int(location)
                                    if hp_values:
                                        state["hp"] = [int(value) for value in hp_values]
                                    state["nodes"][int(location)] = updated_node
                                    export_bit = new_export_bit
                                    sync_state = json.loads(json.dumps(state))
                                    response_code = 31
                                    should_save = True

        if should_save:
            save_profile_state(app)
        inventory_item_count = len(getattr(app, "inventory_items", {}))
    return {
        "response_code": response_code,
        "delivery_items": reward_items if response_code == 31 else [],
        "part_fragment_rewards": [
            item for item in reward_items if int(item["item_id"]) in UPGRADEABLE_PART_LOCATIONS
        ] if response_code == 31 else [],
        "search_point_rewards": search_point_rewards if response_code == 31 else [],
        "chiji_rewards": chiji_rewards if response_code == 31 else [],
        "sync_state": sync_state,
        "repeated": repeated,
        "unknown_export_ids": unknown_export_ids,
        "effective_export_ids": effective_export_ids,
        "export_id_source": "entered_node_shop_mask" if shop_offer_index is not None else "none",
        "buff_item_ids": [],
        "unresolved_buff_item_ids": [],
        "trigger_location": int(trigger_location),
        "backend_inventory_item_count": inventory_item_count,
        "inventory_changed": bool(response_code == 31),
        "node_status": node_status if response_code == 31 else None,
        "export_bit": export_bit if response_code == 31 else None,
        "shop_purchase": {
            "export_id": effective_export_ids[0] if effective_export_ids else None,
            "offer_index": shop_offer_index,
            "cost_item_id": cost_item_id,
            "cost_num": cost_num,
            "cost_before": cost_before,
            "cost_after": cost_after,
        } if shop_offer_index is not None else None,
    }


def apply_roguelike_trigger(app, *, chapter_id: int, location: int, event_id: int,
                            export_ids: list[int], hp_values: list[int],
                            trigger_location: int = 0,
                            buff_item_ids: list[int] | None = None) -> dict[str, object]:
    """Apply a PvE result once and replay an identical response on retry.

    The client can retry a trigger if the connection drops after the server
    commits the node. Keep the first reward in the saved node record; an exact
    retry receives that same reward envelope without crediting the profile a
    second time. A different payload for an already completed node is rejected
    with the client's TriggerRepeated result code.
    """
    current_state = getattr(app, "roguelike_map_state", None)
    current_node = (
        current_state.get("nodes", {}).get(int(location), {})
        if isinstance(current_state, dict) else {}
    )
    actual_event_id = int(event_id or current_node.get("event_id", 0))
    if ROGUELIKE_EVENT_TYPE_BY_ID.get(actual_event_id) == 9:
        return apply_roguelike_shop_trigger(
            app, chapter_id=chapter_id, location=location,
            event_id=actual_event_id, export_ids=export_ids,
            hp_values=hp_values, trigger_location=trigger_location,
        )

    sync_state = None
    repeated = False
    response_code = 31  # ERogueLikeCode.TriggerSuccess
    should_save = False
    reward_items: list[dict[str, int]] = []
    part_fragment_rewards: list[dict[str, int]] = []
    search_point_rewards: list[dict[str, int]] = []
    chiji_rewards: list[dict[str, int]] = []
    unknown_export_ids: list[int] = []
    effective_export_ids: list[int] = []
    unresolved_buff_item_ids: list[int] = []
    export_id_source = "none"
    buff_item_ids = [int(value) for value in (buff_item_ids or []) if int(value) > 0]

    with app.roguelike_lock:
        state = app.roguelike_map_state
        if state is None or int(state.get("chapter_id", -1)) != chapter_id:
            # No matching active map means there is no valid node to complete.
            response_code = 32  # ERogueLikeCode.TriggerLocationError
            delivery_items: list[dict[str, int]] = []
        else:
            previous = state["nodes"].get(location, {})
            cached_exports = [int(item) for item in previous.get("location_exports", [])]
            if export_ids:
                effective_export_ids = list(export_ids)
                export_id_source = "trigger_request"
            elif buff_item_ids:
                effective_export_ids, unresolved_buff_item_ids = resolve_roguelike_chiji_buff_exports(
                    buff_item_ids,
                    chapter_id=chapter_id,
                    chapter_level=int(state.get("chapter_level", 1)),
                    event_id=event_id or int(previous.get("event_id", 0)),
                    cached_export_ids=cached_exports,
                )
                export_id_source = (
                    "toy_top_recover_hp_selection"
                    if (effective_export_ids and all(
                        str(value) in ROGUELIKE_RECOVER_HP_EXPORTS
                        for value in effective_export_ids
                    )) else "toy_top_buffs" if effective_export_ids else
                    "toy_top_buffs_unmatched"
                )
            elif cached_exports:
                # The shopping UI sets TriggerLocation to 1 << selectedIndex,
                # clears CurNode.Event.LocationExports, and sends the trigger.
                # Keep the full event list from EnterNode and select only the
                # item the player actually bought.
                effective_export_ids = select_roguelike_location_export_ids(
                    cached_exports, trigger_location,
                )
                export_id_source = (
                    "entered_node_location_mask" if trigger_location else
                    "entered_node_event"
                ) if effective_export_ids else "entered_node_location_mask_unmatched"
            cached_event_id = int(previous.get("event_id", 0))
            invalid_event = bool(event_id and cached_event_id and event_id != cached_event_id)
            invalid_location_mask = bool(
                not export_ids and cached_exports and trigger_location and not effective_export_ids
            )
            invalid_buff_selection = bool(buff_item_ids and (
                unresolved_buff_item_ids or not effective_export_ids
            ))
            reward_items, unknown_export_ids = resolve_roguelike_export_rewards(
                effective_export_ids,
            )
            part_fragment_rewards = [
                item for item in reward_items
                if int(item["item_id"]) in UPGRADEABLE_PART_LOCATIONS
            ]
            search_point_rewards = resolve_roguelike_search_point_rewards(
                effective_export_ids,
            )
            chiji_rewards = resolve_roguelike_chiji_rewards(effective_export_ids)
            already_completed = int(previous.get("status", 0)) >= 3
            saved_reward_exports = previous.get("reward_export_ids")
            if not isinstance(saved_reward_exports, list):
                saved_reward_exports = previous.get("location_exports", [])
            exact_retry = (
                already_completed
                and int(previous.get("event_id", 0)) == event_id
                and int(previous.get("trigger_location", 0)) == int(trigger_location)
                and [int(item) for item in saved_reward_exports] == effective_export_ids
            )
            if already_completed:
                repeated = True
                if exact_retry:
                    # Replay the original result for a client-side retry; the
                    # inventory and selected modules were already credited.
                    delivery_items = reward_items
                    sync_state = json.loads(json.dumps(state))
                else:
                    response_code = 33  # ERogueLikeCode.TriggerRepeated
                    delivery_items = []
            elif invalid_event or invalid_location_mask or invalid_buff_selection:
                # Do not consume a node when its event identity or selected
                # shop-offer bit cannot be reconciled with the EnterNode data.
                response_code = 32  # ERogueLikeCode.TriggerLocationError
                delivery_items = []
            else:
                state["location"] = location
                if hp_values:
                    state["hp"] = hp_values
                state["nodes"][location] = {
                    "status": 3,
                    "event_id": event_id or int(previous.get("event_id", 0)),
                    "location_exports": cached_exports or effective_export_ids,
                    "reward_export_ids": effective_export_ids,
                    "trigger_location": int(trigger_location),
                }
                delivery_items = reward_items
                for item in delivery_items:
                    item_id, quantity = int(item["item_id"]), int(item["num"])
                    credit_profile_item_reward(app, item_id, quantity)
                current_search_points = getattr(app, "roguelike_search_points", {})
                app.roguelike_search_points = current_search_points
                for item in search_point_rewards:
                    item_id, quantity = int(item["item_id"]), int(item["num"])
                    current_search_points[item_id] = (
                        int(current_search_points.get(item_id, 0)) + quantity
                    )
                event_buffs = state.setdefault("event_buffs", {})
                if not isinstance(event_buffs, dict):
                    event_buffs = {}
                    state["event_buffs"] = event_buffs
                for item in chiji_rewards:
                    item_id, quantity = int(item["item_id"]), int(item["num"])
                    event_buffs[str(item_id)] = int(event_buffs.get(str(item_id), 0)) + quantity
                sync_state = json.loads(json.dumps(state))
                should_save = True
        if should_save:
            save_profile_state(app)
        inventory_item_count = len(app.inventory_items)
    return {
        "response_code": response_code,
        "delivery_items": delivery_items,
        "part_fragment_rewards": part_fragment_rewards if response_code == 31 else [],
        "search_point_rewards": search_point_rewards,
        "chiji_rewards": chiji_rewards,
        "sync_state": sync_state,
        "repeated": repeated,
        "unknown_export_ids": unknown_export_ids,
        "effective_export_ids": effective_export_ids,
        "export_id_source": export_id_source,
        "buff_item_ids": buff_item_ids,
        "unresolved_buff_item_ids": unresolved_buff_item_ids,
        "trigger_location": int(trigger_location),
        "backend_inventory_item_count": inventory_item_count,
        "inventory_changed": bool(response_code == 31 and not repeated and should_save),
        "node_status": 3 if response_code == 31 else None,
        "export_bit": None,
        "shop_purchase": None,
    }


def make_login_success(player_id: int) -> bytes:
    # SC_Login fields confirmed from the generated protobuf code in project.jsc:
    # id=1 (int64), code=2 (E_OK=1), ChapterNum=4 (int32), isRealNameAuth=6.
    proto = b"\x08" + varint(player_id) + b"\x10\x01\x20\x01\x30\x01"
    message_id = 129  # ESC_Login
    body = struct.pack(">IH", 6 + len(proto), message_id) + proto
    return websocket_binary_frame(body)


def make_bout_response(bout_opr: int, *, param1: int | None = None,
                       param2: int | None = None, param3: int | None = None,
                       reward_items: tuple[tuple[int, int], ...] = ()) -> bytes:
    # Runtime schema confirms SC_BoutOpr.boutOpr is required field 1. Preserve
    # the existing guide acknowledgement while allowing the supported local
    # Championship transitions to carry their result and reward envelope.
    proto = bytearray(protobuf_int(1, int(bout_opr)))
    if param1 is not None:
        proto.extend(protobuf_int(2, int(param1)))
    if param2 is not None:
        proto.extend(protobuf_int(3, int(param2)))
    if param3 is not None:
        proto.extend(protobuf_int(4, int(param3)))
    if reward_items:
        item_infos = b"".join(
            protobuf_bytes(1, protobuf_int(1, item_id) + protobuf_int(2, quantity))
            for item_id, quantity in reward_items if item_id > 0 and quantity > 0
        )
        # SC_GetItems.Items is ItemsDetail; its field 1 is already the
        # repeated ItemInfo encoding assembled above.
        get_items = protobuf_int(1, 7) + protobuf_bytes(2, item_infos)
        proto.extend(protobuf_bytes(7, get_items))
    proto = bytes(proto)
    body = struct.pack(">IH", 6 + len(proto), 140) + proto  # ESC_BoutOpr
    return websocket_binary_frame(body)


def make_race_state_sync(race_id: int, race_type: int,
                         race_step: int = 0,
                         player_id: int = 0) -> bytes:
    """Send the native RaceProto state consumed by event handler message 4."""
    # Runtime handler 4 decodes RaceProto, calls ResetRaceCallBack, then merges
    # it into PlayerDbDataProtoOpt._Race. Fields 1, 2, and 7 are RaceID,
    # RaceType, and RaceStep. The initial Survival probe needs this state before
    # ESC_BoutOpr 10; that handler dispatches on the current _Race.RaceType.
    proto = (protobuf_int(1, int(race_id))
             + protobuf_int(2, int(race_type))
             + protobuf_int(7, int(race_step)))
    # Runtime RaceProto field 10 is repeated ContainerIntRacePlayerProto,
    # whose fields are idx=1 and RacePlayerProto=2. Include the local player
    # and a synthetic local rival so Cocos can build its native match roster.
    for seat, (participant_id, nickname, level) in enumerate((
            (int(player_id), "Local Player", 1),
            (int(player_id) + 1, "Local Rival", 1))):
        race_player = b"".join((
            protobuf_int(1, participant_id),
            protobuf_string(2, nickname),
            protobuf_int(3, level),
            protobuf_int(4, 0),  # Score
            protobuf_int(5, 2001),  # MainTop: verified starter part
            protobuf_int(7, 0),  # Emitter
            protobuf_int(8, 1),  # EmitterStar
        ))
        container = (protobuf_int(1, seat)
                     + protobuf_bytes(2, race_player))
        proto += protobuf_bytes(10, container)
    body = struct.pack(">IH", 6 + len(proto), 4) + proto
    return websocket_binary_frame(body)  # ESC_Race


SC_INFORMATION_OPR_FAILED = 0
SC_INFORMATION_OPR_SUCCEEDED = 1


def make_information_opr_response(opr_type: int, code: int) -> bytes:
    # Runtime enum E_CommonCode: E_OPR_FAILED=0, E_OPR_SUCCEED=1.
    # Runtime schema for SC_InformationOpr: OprType=1, code=2 (int32).
    proto = protobuf_int(1, int(opr_type)) + protobuf_int(2, int(code))
    body = struct.pack(">IH", 6 + len(proto), 160) + proto  # ESC_InformationOpr
    return websocket_binary_frame(body)


def parse_chapter_opr_request(data: bytes) -> dict[str, int]:
    values = protobuf_varint_values(data)
    return {
        "operation": int32_value(values.get(1, 0)),
        "chapter_key": int32_value(values.get(2, 0)),
        "chapter_version": int32_value(values.get(3, 0)),
        "result": int32_value(values.get(4, 0)),
        "self_win_num": int32_value(values.get(5, 0)),
    }


def make_chapter_opr_response(response_code: int) -> bytes:
    """Encode the native SC_ChapterOpr response (message 167)."""
    proto = protobuf_int(1, int(response_code))  # SC_ChapterOpr.ChapterOpr.
    body = struct.pack(">IH", 6 + len(proto), 167)
    return websocket_binary_frame(body + proto)


def parse_chapter_pve_request(data: bytes) -> dict[str, object]:
    values = protobuf_varint_values(data)
    return {
        "operation": int32_value(values.get(1, 0)),
        "param1": int32_value(values.get(2, 0)),
        "param2": int32_value(values.get(3, 0)),
        "gold_ratio": int32_value(values.get(4, 0)),
        "part_ratio": int32_value(values.get(5, 0)),
        "self_win_num": int32_value(values.get(6, 0)),
        "use_parts": protobuf_repeated_int32_values(data, 7),
    }


def make_chapter_pve_response(response_code: int) -> bytes:
    """Encode the runtime-confirmed SC_ChapterPVE response (message 169)."""
    proto = protobuf_int(1, int(response_code))  # SC_ChapterPVE.Opr.
    body = struct.pack(">IH", 6 + len(proto), 169)
    return websocket_binary_frame(body + proto)


def apply_guaji_pve_operation(app, operation: dict[str, object], *,
                              request_fingerprint: str = "") -> dict[str, object]:
    """Apply the evidenced Guaji battle operations using the client's stage table."""
    opr = int(operation.get("operation", -1))
    pve_info = normalize_pve_info(getattr(app, "pve_info", {}))
    attempt = merge_guaji_pve_attempts(
        {}, getattr(app, "guaji_pve_attempt", {}),
    )
    app.pve_info = pve_info
    app.guaji_pve_attempt = attempt
    progress = int(pve_info["Process"])

    if opr == GUAJI_PVE_BEGIN_RACE:
        target_process = progress + 1
        if target_process not in GUAJI_PVE_ROWS:
            return {"response_code": GUAJI_PVE_PROCESS_INVALID, "changed": False,
                    "reason": "no_next_client_stage"}
        attempt_id = int(attempt.get("attempt_id", 0)) + 1
        app.guaji_pve_attempt = {
            "process": target_process, "attempt_id": attempt_id,
            "status": "started", "fingerprint": "",
        }
        return {"response_code": GUAJI_PVE_BEGIN_RACE, "changed": False,
                "persist": True, "process": target_process,
                "attempt_id": attempt_id, "reason": "guaji_battle_may_start"}

    if opr not in (GUAJI_PVE_WIN, GUAJI_PVE_LOST):
        return {"response_code": 99, "changed": False,
                "reason": "guaji_operation_not_restored_yet"}

    process = int(operation.get("param1", 0))
    if process not in GUAJI_PVE_ROWS:
        return {"response_code": GUAJI_PVE_PROCESS_INVALID, "changed": False,
                "reason": "unknown_guaji_process"}
    fingerprint = request_fingerprint or hashlib.sha256(
        json.dumps(operation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    current_attempt = attempt if int(attempt.get("process", 0)) == process else {}
    if current_attempt.get("status") == "completed":
        if current_attempt.get("fingerprint") == fingerprint and opr == GUAJI_PVE_WIN:
            return {"response_code": GUAJI_PVE_WIN, "changed": False,
                    "duplicate_request": True, "reason": "guaji_completed_retry"}
        return {"response_code": GUAJI_PVE_PROCESS_INVALID, "changed": False,
                "reason": "guaji_attempt_already_completed"}
    if current_attempt.get("status") == "lost":
        if current_attempt.get("fingerprint") == fingerprint and opr == GUAJI_PVE_LOST:
            return {"response_code": GUAJI_PVE_LOST, "changed": False,
                    "duplicate_request": True, "reason": "guaji_lost_retry"}
        return {"response_code": GUAJI_PVE_PROCESS_INVALID, "changed": False,
                "reason": "new_guaji_attempt_required"}

    if process != progress + 1:
        return {"response_code": GUAJI_PVE_PROCESS_INVALID, "changed": False,
                "reason": "guaji_process_not_next"}
    if opr == GUAJI_PVE_WIN and int(operation.get("self_win_num", 0)) <= 0:
        return {"response_code": 99, "changed": False,
                "reason": "guaji_win_has_no_self_win"}

    attempt_id = int(current_attempt.get("attempt_id", 0))
    if opr == GUAJI_PVE_LOST:
        app.guaji_pve_attempt = {
            "process": process, "attempt_id": max(1, attempt_id),
            "status": "lost", "fingerprint": fingerprint,
        }
        return {"response_code": GUAJI_PVE_LOST, "changed": False,
                "persist": True, "process": process,
                "attempt_id": max(1, attempt_id), "reason": "guaji_loss_recorded"}

    row = GUAJI_PVE_ROWS[process]
    rewards = []
    raw_rewards = row.get("Rewards", [])
    if isinstance(raw_rewards, list):
        for reward in raw_rewards:
            if not isinstance(reward, dict):
                continue
            try:
                item_id, quantity = int(reward.get("ID", 0)), int(reward.get("Num", 0))
            except (TypeError, ValueError):
                continue
            if item_id > 0 and quantity > 0:
                rewards.append({"item_id": item_id, "num": quantity})
    if not rewards:
        return {"response_code": 99, "changed": False,
                "reason": "guaji_stage_reward_missing"}

    app.pve_info = dict(pve_info)
    app.pve_info["Process"] = process
    app.inventory_items = getattr(app, "inventory_items", {})
    app.part_fragments = getattr(app, "part_fragments", {})
    for reward in rewards:
        credit_profile_item_reward(app, reward["item_id"], reward["num"])
    app.guaji_pve_attempt = {
        "process": process, "attempt_id": max(1, attempt_id),
        "status": "completed", "fingerprint": fingerprint,
    }
    counters = getattr(app, "progression_counters", {})
    counters["guaji_pve_wins"] = int(counters.get("guaji_pve_wins", 0)) + 1
    app.progression_counters = counters
    return {"response_code": GUAJI_PVE_WIN, "changed": True,
            "persist": True, "process": process,
            "attempt_id": max(1, attempt_id), "reward_items": rewards,
            "reason": "guaji_stage_win_saved"}


def _chapter_row_for_key(chapter_key: int) -> dict[str, object] | None:
    try:
        row = PVE_CHAPTER_ROWS.get(int(chapter_key))
    except (TypeError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def _chapter_reward_rows(row: dict[str, object]) -> list[dict[str, int]]:
    items = []
    raw_items = row.get("FirstRewards")
    if not isinstance(raw_items, list):
        return items
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            item_id, quantity = int(item.get("ID", 0)), int(item.get("Num", 0))
        except (TypeError, ValueError):
            continue
        if item_id > 0 and quantity > 0:
            items.append({"item_id": item_id, "num": quantity})
    return items


def apply_chapter_opr(app, operation: dict[str, int], *,
                      request_fingerprint: str = "") -> dict[str, object]:
    """Apply a verified local campaign-PvE request against bundled client data."""
    opr = int(operation.get("operation", -1))
    chapter_key = int(operation.get("chapter_key", 0))
    chapter_version = int(operation.get("chapter_version", 0))
    result = int(operation.get("result", 0))
    row = _chapter_row_for_key(chapter_key)
    if row is None:
        return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                "changed": False, "reason": "unknown_stage_key"}
    try:
        chapter_id = int(row["ChapterID"])
        stage_index = int(row["IndexInChapter"])
        expected_version = int(row["Version"])
    except (KeyError, TypeError, ValueError):
        return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                "changed": False, "reason": "invalid_catalog_row"}

    if opr not in (PVE_CHAPTER_OPR_QUERY, PVE_CHAPTER_OPR_WIN,
                   PVE_CHAPTER_OPR_REWARD, PVE_CHAPTER_OPR_BEGIN_RACE):
        return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                "changed": False, "reason": "unsupported_operation"}
    if opr != PVE_CHAPTER_OPR_BEGIN_RACE and chapter_version != expected_version:
        return {"response_code": PVE_CHAPTER_OPR_VERSION_ERROR,
                "changed": False, "reason": "chapter_version_mismatch"}

    results = getattr(app, "chapter_battle_results", {})
    if not isinstance(results, dict):
        results = {}
        app.chapter_battle_results = results
    claimed = getattr(app, "chapter_rewards_claimed", [])
    if not isinstance(claimed, list):
        claimed = []
        app.chapter_rewards_claimed = claimed
    attempts = getattr(app, "chapter_battle_attempts", {})
    if not isinstance(attempts, dict):
        attempts = {}
        app.chapter_battle_attempts = attempts

    if opr == PVE_CHAPTER_OPR_QUERY:
        return {"response_code": PVE_CHAPTER_OPR_QUERY, "changed": False,
                "chapter_key": chapter_key, "reason": "static_client_row_confirmed"}
    if opr == PVE_CHAPTER_OPR_BEGIN_RACE:
        if stage_index > PVE_BATTLE_STAGES_PER_CHAPTER:
            return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                    "changed": False, "reason": "chapter_reward_is_not_a_battle"}
        ordinal = (chapter_id - 1) * PVE_BATTLE_STAGES_PER_CHAPTER + stage_index
        progress = len(results)
        if ordinal > progress + 1:
            return {"response_code": PVE_CHAPTER_OPR_NEED_LAST_WIN,
                    "changed": False, "reason": "previous_stage_not_cleared"}
        previous_attempt = attempts.get(chapter_key, {})
        attempt_id = int(previous_attempt.get("attempt_id", 0)) + 1
        attempts[chapter_key] = {
            "attempt_id": attempt_id, "status": "started", "fingerprint": "",
        }
        return {"response_code": PVE_CHAPTER_OPR_BEGIN_RACE, "changed": False,
                "persist": True, "attempt_id": attempt_id,
                "chapter_key": chapter_key, "reason": "local_battle_may_start"}

    if opr == PVE_CHAPTER_OPR_WIN:
        if stage_index > PVE_BATTLE_STAGES_PER_CHAPTER:
            return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                    "changed": False, "reason": "chapter_reward_is_not_a_battle"}
        if result <= 0 or result > 7 or not (result & 1):
            return {"response_code": PVE_CHAPTER_OPR_INVALID_RESULT,
                    "changed": False, "reason": "result_has_no_win_star"}
        if int(operation.get("self_win_num", 0)) <= 0:
            return {"response_code": PVE_CHAPTER_OPR_INVALID_RESULT,
                    "changed": False, "reason": "no_self_win_reported"}
        ordinal = (chapter_id - 1) * PVE_BATTLE_STAGES_PER_CHAPTER + stage_index
        progress = len(results)
        if ordinal > progress + 1:
            return {"response_code": PVE_CHAPTER_OPR_NEED_LAST_WIN,
                    "changed": False, "reason": "previous_stage_not_cleared"}

        fingerprint = request_fingerprint or hashlib.sha256(
            f"{chapter_key}:{result}:{operation.get('self_win_num', 0)}".encode("ascii")
        ).hexdigest()
        previous_attempt = attempts.get(chapter_key, {})
        if previous_attempt.get("status") == "completed":
            if previous_attempt.get("fingerprint") == fingerprint:
                return {"response_code": PVE_CHAPTER_OPR_WIN, "changed": False,
                        "duplicate_request": True, "reason": "completed_attempt_retry"}
            return {"response_code": PVE_CHAPTER_OPR_INVALID_RESULT,
                    "changed": False, "reason": "new_begin_race_required"}
        attempt_id = max(1, int(previous_attempt.get("attempt_id", 0)))

        previous_result = int(results.get(chapter_key, 0))
        first_clear = previous_result == 0
        results[chapter_key] = max(previous_result, result)
        reward_row = row
        rewards = _chapter_reward_rows(reward_row) if first_clear else []
        if not first_clear:
            raw_repeat_rewards = row.get("Rewards")
            rewards = []
            if isinstance(raw_repeat_rewards, list):
                for item in raw_repeat_rewards:
                    if not isinstance(item, dict):
                        continue
                    try:
                        item_id, quantity = int(item.get("ID", 0)), int(item.get("Num", 0))
                    except (TypeError, ValueError):
                        continue
                    if item_id > 0 and quantity > 0:
                        rewards.append({"item_id": item_id, "num": quantity})
        app.part_fragments = getattr(app, "part_fragments", {})
        app.inventory_items = getattr(app, "inventory_items", {})
        for reward in rewards:
            credit_profile_item_reward(app, reward["item_id"], reward["num"])
        attempts[chapter_key] = {
            "attempt_id": attempt_id, "status": "completed",
            "fingerprint": fingerprint,
        }
        return {"response_code": PVE_CHAPTER_OPR_WIN, "changed": True,
                "persist": True, "attempt_id": attempt_id,
                "chapter_key": chapter_key, "chapter_id": chapter_id,
                "result": int(results[chapter_key]), "first_clear": first_clear,
                "reward_items": rewards, "reason": "local_match_result_saved"}

    if stage_index != PVE_CHAPTER_REWARD_INDEX:
        return {"response_code": PVE_CHAPTER_OPR_INVALID_CHAPTER,
                "changed": False, "reason": "not_a_chapter_reward_row"}
    if result != 1:
        return {"response_code": PVE_CHAPTER_OPR_INVALID_RESULT,
                "changed": False, "reason": "unexpected_chapter_reward_result"}
    if chapter_id in claimed:
        return {"response_code": PVE_CHAPTER_OPR_REWARD_ALREADY_CLAIMED,
                "changed": False, "reason": "chapter_reward_already_claimed"}
    missing_stages = [chapter_id * 10000 + index
                      for index in range(1, PVE_BATTLE_STAGES_PER_CHAPTER + 1)
                      if chapter_id * 10000 + index not in results]
    if missing_stages:
        return {"response_code": PVE_CHAPTER_OPR_NOT_BIG_WIN,
                "changed": False, "missing_stages": missing_stages,
                "reason": "chapter_not_cleared"}
    rewards = _chapter_reward_rows(row)
    if not rewards:
        return {"response_code": PVE_CHAPTER_OPR_INVALID_RESULT,
                "changed": False, "reason": "chapter_reward_missing_from_catalog"}
    claimed.append(chapter_id)
    claimed.sort()
    app.part_fragments = getattr(app, "part_fragments", {})
    app.inventory_items = getattr(app, "inventory_items", {})
    for reward in rewards:
        credit_profile_item_reward(app, reward["item_id"], reward["num"])
    return {"response_code": PVE_CHAPTER_OPR_REWARD, "changed": True,
            "chapter_key": chapter_key, "chapter_id": chapter_id,
            "reward_items": rewards, "reason": "chapter_reward_granted"}


def response_bout_opr_for_request(requested_bout_opr: int,
                                  race_type: int = 0) -> int:
    """Return acknowledged bout operations for flows implemented locally."""
    requested_bout_opr = int(requested_bout_opr)
    race_type = int(race_type)
    if requested_bout_opr == 6:  # E_RACE_GUIDE_WIN
        return 6
    if requested_bout_opr == 40 and race_type == 5:  # E_RACE_QUERY_CHAMPION
        return 40
    if requested_bout_opr == 44 and race_type == CHAMPION_RACE_TYPE:  # E_RACE_CHAMPION_ENTER
        return 44
    if requested_bout_opr == 10 and race_type == 3:  # E_RACE_ROBOT_GAME / MPO Survival
        return 10
    return 0


def can_claim_level_first_reward(*, exchange_id: int, sub_ids: list[int],
                                 already_claimed: bool, requirements_met: bool,
                                 guide_step: int, has_known_reward: bool,
                                 expected_exchange_id: int | None = LEVEL_FIRST_REWARD_ID) -> bool:
    """Allow only the current level's known LevelFirstReward exchange.

    LV01 is recovered directly from the guide data. Later LVnn identifiers are
    a local compatibility convention derived from that identifier because the
    encrypted TaskExchange rows are unavailable in this workspace.
    """
    if (expected_exchange_id is None
            or int(exchange_id) != int(expected_exchange_id) or sub_ids
            or already_claimed or not has_known_reward):
        return False
    tutorial_complete = bool(int(guide_step) & GUIDE_MARK_OVER_MASK)
    return bool(requirements_met or tutorial_complete)


def level_first_reward_exchange_id(current_level: int) -> int | None:
    """Return the exact recovered TaskExchange ID for this GP tier."""
    row = LEVEL_PROGRESSION_EXCHANGE_ROWS.get(int(current_level))
    return int(row["ExchangeID"]) if row else None


def apply_level_first_reward_claim(app, *, exchange_id: int,
                                   sub_ids: list[int]) -> dict[str, object]:
    """Apply one supported LevelFirstReward exchange exactly once."""
    claims = set(int(value) for value in app.task_exchange_claims)
    already_claimed = int(exchange_id) in claims
    current_level = max(1, int(app.level))
    row = LEVEL_PROGRESSION_ROWS.get(current_level + 1, {})
    candidate_task_ids = [int(value) for value in row.get("LevelTasks", []) or []]
    completed_task_ids = []
    for task_id in candidate_task_ids:
        task = LEVEL_PROGRESSION_TASKS.get(task_id)
        if task is None:
            continue
        process, status = level_task_progress(
            task, app.inventory_items, app.level_task_state,
            app.progression_counters, app.part_levels,
        )
        if process >= max(0, int(task.get("maxProcess", 0))) and status >= TASK_STATUS_COMPLETE:
            completed_task_ids.append(task_id)
    required_count = max(0, int(row.get("lvUp_TaskNum", 0)))
    requirements_met = (required_count > 0
                        and len(completed_task_ids) >= required_count)
    expected_exchange_id = level_first_reward_exchange_id(current_level)
    tutorial_fallback = (
        int(exchange_id) == LEVEL_FIRST_REWARD_ID
        and current_level == 1
        and bool(int(app.guide_step) & GUIDE_MARK_OVER_MASK)
        and not requirements_met
    )
    configured_tier_gp = max(0, int(row.get("rewardGPA", 0)))
    configured_rewards = LEVEL_PROGRESSION_EXCHANGE_REWARDS.get(int(exchange_id), ())
    has_known_reward = bool(
        expected_exchange_id is not None
        and int(exchange_id) == expected_exchange_id
        and (configured_rewards or configured_tier_gp > 0)
    )
    success = can_claim_level_first_reward(
        exchange_id=exchange_id,
        sub_ids=sub_ids,
        already_claimed=already_claimed,
        requirements_met=requirements_met,
        guide_step=int(app.guide_step),
        has_known_reward=has_known_reward,
        expected_exchange_id=expected_exchange_id,
    )
    level_before = power_before = None
    grant_items: list[tuple[int, int]] = []
    if success:
        level_before, power_before = int(app.level), int(app.power)
        # Grant the exact TaskExchange RewardList for the current level. The
        # one-time LV01 accessory-box contents remain the established local
        # guide compatibility behavior below.
        grant_items.extend(configured_rewards or
                           TUTORIAL_TASK_EXCHANGE_REWARDS.get(int(exchange_id), ()))
        box_contents = TUTORIAL_TASK_EXCHANGE_BOX_CONTENTS.get(int(exchange_id), ())
        for _, contents in box_contents:
            grant_items.extend(contents)
        for item_id, quantity in grant_items:
            credit_profile_item_reward(app, item_id, quantity)
        claims.add(int(exchange_id))
        app.task_exchange_claims = sorted(claims)
        next_level = int(app.level) + 1
        next_level_row = LEVEL_PROGRESSION_ROWS.get(next_level, {})
        reward_gp = max(0, int(next_level_row.get("rewardGPA", 0)))
        app.level = next_level
        app.power = max(
            int(app.power) + reward_gp,
            int(next_level_row.get("levelGPA", 0)),
        )
        for task_id in completed_task_ids:
            task = LEVEL_PROGRESSION_TASKS.get(task_id)
            if task is not None:
                app.level_task_state[str(task_id)] = {
                    "process": max(0, int(task.get("maxProcess", 0))),
                    "status": TASK_STATUS_REWARDED,
                }
        save_profile_state(app)
    return {
        "success": success,
        "already_claimed": already_claimed,
        "requirements_met": requirements_met,
        "tutorial_fallback": tutorial_fallback,
        "level_before": level_before,
        "power_before": power_before,
        "grant_items": grant_items,
    }


def make_task_opr_response(task_opr_ret: int, task_id: int) -> bytes:
    # Runtime schema: SC_TaskOpr fields 1=TaskOprRet and 2=TaskID.
    proto = protobuf_int(1, task_opr_ret) + protobuf_int(2, task_id)
    body = struct.pack(">IH", 6 + len(proto), 145) + proto  # ESC_TaskOpr
    return websocket_binary_frame(body)


def make_task_exchange_opr_response(result: int, exchange_id: int) -> bytes:
    # Runtime SC_TaskExchangeOpr schema: TaskExchangeOpr=field 1,
    # ExchangeID=field 2. Result 3 is TASK_EXCHANGE_OK; 1 is already claimed.
    proto = protobuf_int(1, int(result)) + protobuf_int(2, int(exchange_id))
    body = struct.pack(">IH", 6 + len(proto), 147)  # ESC_TaskExchangeOpr
    return websocket_binary_frame(body + proto)


def make_task_exchange_get_items(exchange_id: int,
                                 item_infos: tuple[tuple[int, int], ...],
                                 box_contents: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = ()) -> bytes:
    # Runtime SC_GetItems schema: GetItemFrom=1 (GIF_EXCHANGE), Items=2,
    # BoxList=3, KeyParam=4. ItemsDetail.ItemInfos=1, BoxId=2;
    # ItemInfo is ID=1, Num=2.
    encoded_items = b"".join(
        protobuf_bytes(1, protobuf_int(1, item_id) + protobuf_int(2, quantity))
        for item_id, quantity in item_infos if item_id > 0 and quantity > 0
    )
    proto_parts = [protobuf_int(1, 6)]  # GetItemFrom.GIF_EXCHANGE
    # For box exchanges, BoxList is the visual reward envelope. Sending the
    # box again in Items makes the native LevelFirstReward UI count it twice.
    if encoded_items:
        proto_parts.append(protobuf_bytes(2, encoded_items))
    for box_id, contents in box_contents:
        encoded_contents = b"".join(
            protobuf_bytes(1, protobuf_int(1, item_id) + protobuf_int(2, quantity))
            for item_id, quantity in contents if item_id > 0 and quantity > 0
        )
        if box_id > 0 and encoded_contents:
            detail = encoded_contents + protobuf_int(2, box_id)
            proto_parts.append(protobuf_bytes(3, detail))
    proto_parts.append(protobuf_int(4, int(exchange_id)))
    proto = b"".join(proto_parts)
    body = struct.pack(">IH", 6 + len(proto), 143)  # ESC_GetItems
    return websocket_binary_frame(body + proto)


def make_lottery_reply(code: int, lottery_id: int = 0,
                       draws: list[dict[str, int]] | None = None) -> bytes:
    """Encode ESC_Lottery (228) and its native SC_GetItems reward envelope."""
    draws = draws or []
    rewards = bytearray()
    aggregated: dict[int, int] = {}
    for result in draws:
        item_id, quantity = int(result.get("item_id", 0)), int(result.get("num", 0))
        if item_id <= 0 or quantity <= 0:
            continue
        aggregated[item_id] = aggregated.get(item_id, 0) + quantity
        item_info = protobuf_int(1, item_id) + protobuf_int(2, quantity)
        box_detail = protobuf_bytes(1, item_info)
        box_id = int(result.get("box_id", 0))
        if box_id > 0:
            box_detail += protobuf_int(2, box_id)
        rewards += protobuf_bytes(3, box_detail)  # SC_GetItems.BoxList
    if aggregated:
        items_detail = b"".join(
            protobuf_bytes(1, protobuf_int(1, item_id) + protobuf_int(2, quantity))
            for item_id, quantity in sorted(aggregated.items())
        )
        rewards = bytearray(protobuf_bytes(2, items_detail) + rewards)  # SC_GetItems.Items
        rewards += protobuf_int(4, int(lottery_id))  # SC_GetItems.KeyParam
    proto = protobuf_int(1, int(code))
    if rewards:
        proto += protobuf_bytes(2, bytes(rewards))  # SC_Lottery.rewards
    body = struct.pack(">IH", 6 + len(proto), 228)  # ESC_Lottery
    return websocket_binary_frame(body + proto)


def make_inventory_item_sync(item_id: int, quantity: int) -> bytes:
    return make_inventory_items_sync(((item_id, quantity),))


def make_inventory_items_sync(items: tuple[tuple[int, int], ...]) -> bytes:
    # Wallet and task-score items are scalar BaseInfo fields in the runtime
    # schema. Sending them as DefaultItems leaves the displayed balance stale.
    base_info = bytearray()
    scalar_fields = {
        DIAMOND_ITEM_ID: 4,  # BaseInfo.Diamond
        GOLD_ITEM_ID: 5,     # BaseInfo.Gold
        TASK_SCORE_ITEM_ID: 19,  # BaseInfo.TaskScore
    }
    for item_id, quantity in items:
        if item_id <= 0 or quantity < 0:
            continue
        field_number = scalar_fields.get(int(item_id))
        if field_number is not None:
            base_info += protobuf_int(field_number, int(quantity))
        elif quantity > 0:
            base_info += protobuf_bytes(22, protobuf_int(1, int(item_id)) + protobuf_int(2, int(quantity)))
    player_data = protobuf_bytes(1, bytes(base_info))  # PlayerData.BaseInfo
    body = struct.pack(">IH", 6 + len(player_data), 1)  # ESC_PlayerData sync
    return websocket_binary_frame(body + player_data)


def make_mark_mail_reply() -> bytes:
    # Runtime SC_MarkMailReply.decode skips unknown fields and has no data fields.
    body = struct.pack(">IH", 6, 156)  # ESC_MarkMailReply
    return websocket_binary_frame(body)


def make_tick_reply() -> bytes:
    # ECS_Tick (131) carries only the six-byte application envelope. The paired
    # server message is ESC_Tick (132), also with an empty body.
    return websocket_binary_frame(struct.pack(">IH", 6, 132))


def make_daily_task_status_sync(task_status: int, task_score: int,
                                task_score_exchange_process: int = 0) -> bytes:
    # Update PlayerData.BaseInfo.TaskScore and
    # DBTaskLists[DailyStaticTask][1146368048]. The client merges these fields.
    task = b"".join((
        protobuf_int(1, 1),  # TaskProcess
        protobuf_int(2, task_status),
        protobuf_int(3, 0),  # BeginTime
    ))
    entry = protobuf_int(1, 1146368048) + protobuf_bytes(2, task)
    task_list = b"".join((
        protobuf_bytes(1, entry),
        protobuf_int(2, 0),
        protobuf_int(3, 0),
        protobuf_int(4, 0),
    ))
    container = protobuf_int(1, 3) + protobuf_bytes(2, task_list)
    base_info = (protobuf_int(19, max(0, int(task_score)))
                 + protobuf_int(20, max(0, int(task_score_exchange_process))))
    player_data = protobuf_bytes(1, base_info) + protobuf_bytes(4, container)
    body = struct.pack(">IH", 6 + len(player_data), 1) + player_data
    return websocket_binary_frame(body)


class Handler(socketserver.BaseRequestHandler):
    @property
    def app(self):
        return getattr(self, "connection_app", self.server.app)  # type: ignore[attr-defined]

    def record(self, event: str, **extra) -> None:
        row = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": event, **extra}
        line = json.dumps(row, ensure_ascii=False)
        print(line, flush=True)
        if self.app.log_file:
            with self.app.log_file.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def handle(self) -> None:
        try:
            raw = read_headers(self.request)
            header_text = raw.decode("latin1")
            header_lines = header_text.split("\r\n")
            request_line = header_lines[0].split(" ") if header_lines else []
            request_target = request_line[1] if len(request_line) >= 2 else "/"
            headers = {}
            for line in header_lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            key = headers.get("sec-websocket-key")
            if not key:
                self.record("bad_handshake")
                return
            account_store = getattr(self.server.app, "account_store", None)  # type: ignore[attr-defined]
            if account_store:
                parsed_target = urlsplit(request_target)
                query = parse_qs(parsed_target.query, keep_blank_values=True)
                # The account token's ``entry`` URL uses ``session``. Also
                # accept ``local_session`` as a compatibility alias.
                session_token = query.get("local_session", query.get("session", [""]))[-1]
                account = account_store.resolve(session_token)
                if not account:
                    self.request.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
                    session_parameter = ("local_session" if query.get("local_session")
                                         else "session" if query.get("session")
                                         else "missing")
                    self.record("authentication_rejected", path=parsed_target.path,
                                query_keys=sorted(query), session_parameter=session_parameter,
                                session_token_length=len(session_token),
                                synthetic_token=session_token == "local-test-token")
                    return
                connection_app = copy.copy(self.server.app)  # type: ignore[attr-defined]
                connection_app.player_id = int(account["player_id"])
                connection_app.state_file = account_store.profile_path(account["id"])
                connection_app.mail_state_file = account_store.mail_path(account["id"])
                connection_app.account = account
                connection_app.session_token = session_token
                connection_app.inventory_items = {}
                connection_app.persisted_inventory_items = {}
                connection_app.part_fragments = {}
                connection_app.persisted_part_fragments = {}
                connection_app.roguelike_search_points = {}
                connection_app.persisted_roguelike_search_points = {}
                connection_app.roguelike_keys = TEST_ROGUELIKE_KEYS
                connection_app.roguelike_key_ad_claim_day = -1
                connection_app.roguelike_map_state = None
                connection_app.local_purchases = {}
                connection_app.lineups = default_lineups()
                connection_app.persisted_lineups = default_lineups()
                connection_app.roguelike_lock = account_store.lock_for(account["id"])
                refresh_shared_profile_state(connection_app)
                self.connection_app = connection_app
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            self.request.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii")
            )
            self.record("open", player_id=getattr(self.app, "player_id", 10001))
            replied = False
            daily_task_claimed = False
            achievement_claimed = False
            while True:
                opcode, frame = parse_frame(self.request)
                if opcode == 8:
                    self.record("close")
                    return
                if opcode == 9:
                    self.request.sendall(bytes([0x8A, len(frame)]) + frame)
                    continue
                if opcode != 2 or len(frame) < 6:
                    self.record("ignored_frame", opcode=opcode, bytes=len(frame))
                    continue
                declared_be = int.from_bytes(frame[:4], "big")
                message_be = int.from_bytes(frame[4:6], "big")
                declared_le = int.from_bytes(frame[:4], "little")
                message_le = int.from_bytes(frame[4:6], "little")
                if declared_be == len(frame):
                    byte_order, message_id = "big", message_be
                elif declared_le == len(frame):
                    byte_order, message_id = "little", message_le
                else:
                    byte_order, message_id = "unknown", message_be
                summary = {
                    "opcode": opcode,
                    "frame_bytes": len(frame),
                    "byte_order": byte_order,
                    "declared_bytes": declared_be if byte_order != "little" else declared_le,
                    "message_id": message_id,
                    "protobuf_fields": protobuf_field_summary(frame[6:]),
                }
                if message_id == 139:
                    # Preserve only scalar CS_BoutOpr fields, not raw gameplay
                    # payloads, so the local tutorial response can be matched
                    # to the operation the client actually requested.
                    bout_values = protobuf_varint_values(frame[6:])
                    summary["bout_request"] = {
                        "bout_opr": int32_value(bout_values.get(1, 0)),
                        "race_type": int32_value(bout_values.get(2, 0)),
                        "param1": int32_value(bout_values.get(3, 0)),
                        "param2": int32_value(bout_values.get(4, 0)),
                        "param3": int32_value(bout_values.get(5, 0)),
                        "param4": int32_value(bout_values.get(6, 0)),
                        "race_step": int32_value(bout_values.get(9, 0)),
                        "self_win_num": int32_value(bout_values.get(11, 0)),
                    }
                self.record("client_message", **summary)
                if message_id == 131:
                    self.request.sendall(make_tick_reply())
                    self.record("synthetic_tick_reply", request_id=131,
                                response_id=132, body_bytes=0,
                                purpose="acknowledge ECS_Tick so the client keeps its server session active")
                elif message_id == 1111 and not replied:
                    expired_map = False
                    resume_map = None
                    adventure_progression_backfilled = False
                    box_timers_refreshed = False
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        box_timers_refreshed = refresh_save_box_timers(self.app)
                        champion_state, champion_period_rolled = champion_period_rollover(
                            self.app.championship_state, now=int(time.time()),
                        )
                        self.app.championship_state = champion_state
                        daily_task_claimed = bool(self.app.daily_task_claimed)
                        achievement_claimed = bool(self.app.achievement_claimed)
                        save_boxes = copy.deepcopy(self.app.save_boxes)
                        daily_gold_earned = int(self.app.daily_gold_earned)
                        inventory_items = dict(self.app.inventory_items)
                        part_fragments = dict(self.app.part_fragments)
                        part_levels = dict(self.app.part_levels)
                        map_state = self.app.roguelike_map_state
                        if map_state is not None:
                            version = int(map_state.get("version", 0))
                            if game_time_seconds() - version >= 24 * 60 * 60:
                                self.app.roguelike_map_state = None
                                expired_map = True
                            else:
                                resume_map = json.loads(json.dumps(map_state))
                                adventure_progression_backfilled = backfill_active_adventure_progression(
                                    self.app.progression_counters, True,
                                )
                    if (expired_map or adventure_progression_backfilled
                            or champion_period_rolled or box_timers_refreshed):
                        save_profile_state(self.app)
                    if adventure_progression_backfilled:
                        self.record("synthetic_level_progression_backfill",
                                    condition=30, counter_key="condition:30:0:3",
                                    counter_value=int(self.app.progression_counters["condition:30:0:3"]),
                                    source="resumed persisted Adventure map",
                                    purpose="preserve one pre-counter Adventure entry for GP progression")
                    self.request.sendall(make_player_data_sync(
                        self.app.player_id,
                        nickname=(getattr(self.app, "account", {}).get("username")
                                  or (getattr(self.app, "account", {}).get("email") or "Beta Tester").split("@", 1)[0]
                                  if getattr(self.app, "account", None) else "Local Test"),
                        roguelike_key_num=self.app.roguelike_keys,
                        roguelike_search_points=self.app.roguelike_search_points,
                        daily_task_claimed=daily_task_claimed,
                        achievement_claimed=achievement_claimed,
                        inventory_items=inventory_items,
                        part_fragments=part_fragments,
                        part_levels=part_levels,
                        lineups=self.app.lineups,
                        guide_step=self.app.guide_step,
                        tutorial_battle_complete=self.app.tutorial_battle_complete,
                        lottery_info=self.app.lottery_info,
                        level=self.app.level,
                        exp=self.app.exp,
                        power=self.app.power,
                        task_score=self.app.task_score,
                        task_score_exchange_process=self.app.task_score_exchange_process,
                        level_task_state=self.app.level_task_state,
                        progression_counters=self.app.progression_counters,
                        chapter_battle_results=self.app.chapter_battle_results,
                        chapter_rewards_claimed=self.app.chapter_rewards_claimed,
                        championship_state=self.app.championship_state,
                        daily_gold_earned=daily_gold_earned,
                        save_boxes=save_boxes,
                    ))
                    self.record("synthetic_player_data_sync", message_id=1,
                                player_data_fields=["BaseInfo", "Daily", "RaceHistory", "DBTaskLists", "ChapterBattleResult", "ChapterReward", "GuideStep", "Achievement", "RogueLikeItem", "LotteryInfo"],
                                synthetic_champion_race_type=CHAMPION_RACE_TYPE,
                                synthetic_champion_league_index=1,
                                synthetic_champion_race_score=champion_client_race_score(
                                    (champion_state or {}).get("race_score", 0),
                                ),
                                synthetic_champion_vouchers=SYNTHETIC_CHAMPION_VOUCHERS,
                                base_info_fields=["Id", "NickName", "Level", "Diamond", "Gold", "TopPartList", "MidPartList", "BotPartList", "DefaultItems", "Lineups", "SaveBoxList"],
                                save_box_slots=sorted(int(slot) for slot in save_boxes),
                                daily_gold_earned=daily_gold_earned,
                                lineup_array_indexes=[1, 2, 3], lineup_status_main=1, main_top_index=1,
                                toy_top_array_indexes=[1, 2, 3], synthetic_toytop_count=9,
                                synthetic_toytop_part_ids=[part[0] for part in SYNTHETIC_STARTER_PARTS],
                                owned_starter_part_ids=[part[0] for part in SYNTHETIC_STARTER_PARTS],
                                owned_starter_part_level=SYNTHETIC_STARTER_LEVEL,
                                synthetic_career_stat_ids=list(CAREER_BASELINE_VALUES),
                                synthetic_parts_unlocked=len(SYNTHETIC_STARTER_PARTS),
                                synthetic_daily_task_type=3, synthetic_daily_task_id=1146368048,
                                synthetic_daily_task_status=2, synthetic_daily_task_process=1,
                                synthetic_achievement_id=TEST_ACHIEVEMENT_ID,
                                synthetic_achievement_level=1, synthetic_achievement_value=10,
                                synthetic_achievement_claimable=True,
                                synthetic_roguelike_key_num=self.app.roguelike_keys,
                                synthetic_inventory_item_count=len(inventory_items),
                                guide_step_value=self.app.guide_step,
                                guide_mark_over_mask=GUIDE_MARK_OVER_MASK,
                                guide_step_source="per-player profile; GuideMark.Over is persisted from the client's later guide report",
                                tutorial_battle_complete=self.app.tutorial_battle_complete)
                    time.sleep(0.05)
                    server_time = game_time_seconds()
                    self.request.sendall(make_public_svr_sync(
                        server_time,
                        session_token=getattr(self.app, "session_token", None),
                        public_http_url=self.app.public_http_url,
                        public_ws_url=self.app.public_ws_url,
                    ))
                    self.record("synthetic_player_info_sync", message_id=2,
                                player_info_field=12,
                                sys_info_server_time=server_time,
                                server_time_epoch_ms=GAME_TIME_EPOCH_MS,
                                public_svr_fields=["Url", "Id", "Token", "WsUrl"],
                                credentials_synthetic=True)
                    if resume_map is not None:
                        self.request.sendall(make_roguelike_info_sync(resume_map))
                        self.record("synthetic_roguelike_resume_sync", message_id=5,
                                    chapter_id=int(resume_map.get("chapter_id", 0)),
                                    chapter_level=int(resume_map.get("chapter_level", 1)),
                                    location=int(resume_map.get("location", 0)),
                                    node_count=len(resume_map.get("nodes", {})),
                                    remaining_hp=resume_map.get("hp", []),
                                    purpose="restore the active Adventure map on this local profile's next login")
                    time.sleep(0.05)
                    response = make_login_success(self.app.player_id)
                    self.request.sendall(response)
                    replied = True
                    self.record("synthetic_login_response", message_id=129,
                                code=1, player_id=self.app.player_id,
                                chapter_num=1, real_name_auth=1)
                elif message_id == 135:
                    request = frame[6:]
                    values = protobuf_varint_values(request)
                    item_opr = int32_value(values.get(1, 0))
                    item_key = int32_value(values.get(2, 0))
                    item_param1 = int32_value(values.get(3, 0))
                    item_param2 = int32_value(values.get(4, 0))
                    item_param3 = int32_value(values.get(5, 0))
                    result = {"item_opr_ret": ITEM_OPR_RET_SYSTEM_ERROR,
                              "changed": False}
                    progression_advances = {}
                    box_operations = (ITEM_OPR_USE_ITEM, ITEM_OPR_BOX_COUNTDOWN,
                                      ITEM_OPR_BOX_READY_OPEN,
                                      ITEM_OPR_REDUCE_BOX_COUNTDOWN,
                                      ITEM_OPR_CHANGE_READY_BOX)
                    box_result = None
                    if item_opr in (ITEM_OPR_UPGRADE_PART,
                                    ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP,
                                    *box_operations):
                        with self.app.roguelike_lock:
                            refresh_shared_profile_state(self.app)
                            if item_opr == ITEM_OPR_UPGRADE_PART:
                                result = apply_part_level_upgrade(self.app, item_key)
                            elif item_opr == ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP:
                                result = apply_emitter_material_upgrade(
                                    self.app, item_key, item_param1,
                                )
                            else:
                                result = apply_box_item_operation(
                                    self.app, item_opr, item_key, item_param1,
                                    item_param2,
                                )
                                box_result = result
                            if result["changed"]:
                                if item_opr == ITEM_OPR_UPGRADE_PART:
                                    progression_advances = record_successful_part_upgrade_progress(
                                        self.app,
                                    )
                                gold_spent = int(result.get(
                                    "gold_cost", result.get("gold_required", 0),
                                ))
                                if gold_spent > 0:
                                    cost_gold_advances = increment_level_task_counters(
                                        self.app.progression_counters, 20, gold_spent,
                                    )  # CostGold: add the exact gold deducted by this operation.
                                    for counter, amount in cost_gold_advances.items():
                                        progression_advances[counter] = (
                                            progression_advances.get(counter, 0) + amount
                                        )
                                save_profile_state(self.app)
                        if result["changed"]:
                            # Native ItemOpr success handlers emit UI events but
                            # do not update inventory. Sync the authoritative
                            # balances and any part/GP state before the reply.
                            self.request.sendall(make_app_player_data_sync(
                                self.app,
                                deleted_save_box_slots=(box_result or {}).get(
                                    "removed_save_box_slots", [],
                                ),
                            ))
                            if (box_result is not None and box_result.get("rewards")):
                                self.request.sendall(make_box_open_get_items(
                                    item_key, list(box_result["rewards"]),
                                ))
                    response_param1 = (
                        int(result.get("money_type", 0))
                        if int(result["item_opr_ret"]) == ITEM_OPR_RET_MONEY_NOT_ENOUGH
                        else None
                    )
                    self.request.sendall(make_item_opr_response(
                        int(result["item_opr_ret"]), item_key=item_key,
                        param1=response_param1,
                    ))
                    self.record("synthetic_item_opr_response", request_id=135,
                                response_id=136, item_opr=item_opr,
                                item_key=item_key, param1=item_param1,
                                param2=item_param2, param3=item_param3,
                                item_opr_ret=result["item_opr_ret"],
                                success=bool(result["changed"]),
                                part_upgrade=result if item_opr == ITEM_OPR_UPGRADE_PART else None,
                                box_operation=box_result,
                                emitter_material_upgrade=(
                                    result if item_opr == ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP
                                    else None
                                ),
                                progression_counters_advanced=progression_advances,
                                unsupported=item_opr not in (
                                    ITEM_OPR_UPGRADE_PART,
                                    ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP,
                                    *box_operations,
                                ),
                                purpose=("apply one catalog-costed part level upgrade"
                                         if item_opr == ITEM_OPR_UPGRADE_PART else
                                         "combine emitter material tiers using the live client rates"
                                         if item_opr == ITEM_OPR_EMITTER_MATERIAL_LEVEL_UP else
                                         "process a data-driven box lifecycle operation"
                                         if item_opr in box_operations else
                                         "return a handled system error for an operation not yet restored"))
                elif message_id == 227:
                    request = protobuf_varint_values(frame[6:])
                    lottery_id = int32_value(request.get(1, 0))
                    times = int32_value(request.get(2, 0))
                    has_cost = int32_value(request.get(3, 0)) if 3 in request else None
                    config = LOTTERY_CONFIGS.get(lottery_id)
                    response_code = 1  # LotteryType.LotterySuccess
                    draws: list[dict[str, int]] = []
                    cost_item_id = int(config.get("Cost", {}).get("ID", 0)) if config else 0
                    cost_per_draw = int(config.get("Cost", {}).get("Num", 0)) if config else 0
                    cost_required = cost_per_draw * times if cost_per_draw > 0 and times > 0 else 0
                    cost_before = 0
                    cost_after = 0
                    info_snapshot = normalize_lottery_info(self.app.lottery_info)
                    inventory_snapshot: dict[int, int] = {}
                    part_fragment_snapshot: dict[int, int] = {}
                    if config is None or not lottery_pool_is_open(config):
                        response_code = 2  # LotteryIdInvalid
                    elif times < 1 or times > 10 or cost_required <= 0:
                        response_code = 4  # LotteryParamError
                    else:
                        refresh_shared_profile_state(self.app)
                        with self.app.roguelike_lock:
                            candidate_items = dict(self.app.inventory_items)
                            candidate_part_fragments = dict(self.app.part_fragments)
                            candidate_part_levels = dict(self.app.part_levels)
                            candidate_info = normalize_lottery_info(self.app.lottery_info)
                            cost_before = max(0, int(candidate_items.get(cost_item_id, 0)))
                            current_lottery = candidate_info["lotteries"].get(str(lottery_id), {})
                            recorded_cost = max(0, int(candidate_info["cost_record"].get(
                                str(cost_item_id), 0,
                            )))
                            max_allowance = int(config.get("MaxAllowanceNum", 0))
                            lost_after_max = int(config.get("LostPoolAfterMax", 0))
                            if ((has_cost is not None and has_cost != recorded_cost)
                                    or (has_cost is None and recorded_cost > 0)):
                                response_code = 3  # LotteryRepeated; stale/replayed cost snapshot.
                            elif (max_allowance > 0 and lost_after_max > 0
                                    and int(current_lottery.get("allowance", 0)) >= max_allowance):
                                response_code = 7  # LotteryAllowanceMax
                            elif cost_before < cost_required:
                                response_code = 5  # LotteryCostError
                            else:
                                for _ in range(times):
                                    resolved = resolve_lottery_reward(
                                        lottery_id, candidate_items, candidate_info,
                                        candidate_part_fragments, candidate_part_levels,
                                    )
                                    if resolved is None:
                                        response_code = 4
                                        draws = []
                                        break
                                    reward_item = LOTTERY_ITEM_ROWS.get(int(resolved["item_id"]))
                                    reward_type = int(reward_item.get("itemType", -1)) if reward_item else -1
                                    if (reward_type in (0, 1, 2)
                                            and int(resolved["item_id"]) not in UPGRADEABLE_PART_LOCATIONS):
                                        response_code = 4
                                        draws = []
                                        break
                                    if reward_type not in (0, 1, 2, 3, 4):
                                        response_code = 4
                                        draws = []
                                        break
                                    candidate_items[cost_item_id] = max(
                                        0, int(candidate_items.get(cost_item_id, 0)) - cost_per_draw,
                                    )
                                    item_id = int(resolved["item_id"])
                                    quantity = int(resolved["num"])
                                    credit_item_reward(
                                        candidate_items, candidate_part_fragments,
                                        item_id, quantity, candidate_part_levels,
                                    )
                                    draws.append(resolved)

                                    current_lottery = candidate_info["lotteries"][str(lottery_id)]
                                    current_lottery["allowance"] = int(current_lottery["allowance"]) + 1
                                    rarity_times = current_lottery["rarity_times"]
                                    tier_rarity = int(resolved.get("tier_rarity", 0))
                                    for allowance in config.get("Allowances") or []:
                                        rarity = int(allowance.get("rarity", 0))
                                        if rarity <= 0:
                                            continue
                                        key = str(rarity)
                                        rarity_times[key] = (
                                            0 if tier_rarity >= rarity
                                            else int(rarity_times.get(key, 0)) + 1
                                        )

                                if response_code == 1 and len(draws) == times:
                                    cost_record = candidate_info["cost_record"]
                                    cost_record[str(cost_item_id)] = (
                                        int(cost_record.get(str(cost_item_id), 0)) + cost_required
                                    )
                                    self.app.inventory_items = candidate_items
                                    self.app.part_fragments = candidate_part_fragments
                                    self.app.part_levels = candidate_part_levels
                                    self.app.lottery_info = candidate_info
                                    record_daily_gold_earned(
                                        self.app,
                                        sum(int(row.get("num", 0)) for row in draws
                                            if int(row.get("item_id", 0)) == GOLD_ITEM_ID),
                                    )
                                    save_profile_state(self.app)
                                    inventory_snapshot = dict(self.app.inventory_items)
                                    part_fragment_snapshot = dict(self.app.part_fragments)
                                    info_snapshot = normalize_lottery_info(self.app.lottery_info)
                                    cost_after = max(0, int(inventory_snapshot.get(cost_item_id, 0)))
                                else:
                                    draws = []

                    if response_code == 1:
                        account = getattr(self.app, "account", {}) or {}
                        nickname = (account.get("username")
                                    or (account.get("email") or "Beta Tester").split("@", 1)[0])
                        self.request.sendall(make_player_data_sync(
                            self.app.player_id,
                            nickname=nickname,
                            roguelike_key_num=self.app.roguelike_keys,
                            roguelike_search_points=self.app.roguelike_search_points,
                            daily_task_claimed=bool(self.app.daily_task_claimed),
                            achievement_claimed=bool(self.app.achievement_claimed),
                            inventory_items=inventory_snapshot,
                            part_fragments=part_fragment_snapshot,
                            part_levels=self.app.part_levels,
                            lineups=self.app.lineups,
                            guide_step=self.app.guide_step,
                            lottery_info=info_snapshot,
                            level=self.app.level,
                            exp=self.app.exp,
                            power=self.app.power,
                            task_score=self.app.task_score,
                            task_score_exchange_process=self.app.task_score_exchange_process,
                            level_task_state=self.app.level_task_state,
                            progression_counters=self.app.progression_counters,
                        ))
                    self.request.sendall(make_lottery_reply(
                        response_code, lottery_id=lottery_id, draws=draws,
                    ))
                    self.record("synthetic_lottery_response", request_id=227,
                                response_id=228, lottery_id=lottery_id, times=times,
                                has_cost=has_cost, success=response_code == 1,
                                result_code=response_code, cost_item_id=cost_item_id,
                                cost_required=cost_required, cost_before=cost_before,
                                cost_after=cost_after,
                                reward_items=draws,
                                cost_record=info_snapshot.get("cost_record", {}),
                                lottery_state=info_snapshot.get("lotteries", {}).get(str(lottery_id), {}),
                                weight_source="client Chance table Pvalue/OwnedPvalue; owned rewards use OwnedPvalue",
                                guarantee_source="client LuckyLottery.Allowances and LotteryInfo.RarityTimes",
                                purpose="complete native tutorial draw and persist local ticket cost, reward, and counters")
                elif message_id == 159:
                    request = frame[6:]
                    operation = parse_information_opr_request(request)
                    opr_type = int(operation["opr_type"])
                    i_param = int(operation["i_param"])
                    lineup_index = i_param
                    accepted = False
                    changed_parts = []
                    rejection_reason = None
                    lineup_player_sync = None
                    if opr_type == 8:  # E_INFO_OPR_LINEUP_CHANGE_PART
                        with self.app.roguelike_lock:
                            # Another live WebSocket or the HTTP purchase route
                            # may have changed ownership or the lineup since
                            # this connection last synced. Validate against the
                            # latest account profile before applying a swap.
                            refresh_shared_profile_state(self.app)
                            accepted, candidate_lineups, changed_parts, rejection_reason = (
                                apply_lineup_part_updates(
                                    getattr(self.app, "lineups", None),
                                    getattr(self.app, "inventory_items", {}),
                                    getattr(self.app, "part_levels", {}),
                                    lineup_index,
                                    operation["toy_top_updates"],
                                )
                            )
                            if accepted:
                                if changed_parts:
                                    self.app.lineups = candidate_lineups
                                    save_profile_state(self.app)
                                # The native success handler calls
                                # player.updateToyTopData(), which rebuilds its
                                # lineup from PlayerData.BaseInfo.Lineups. Send
                                # the authoritative lineup before the success
                                # reply. This also repairs stale client state
                                # when a duplicate request is already applied.
                                lineup_player_sync = make_app_player_data_sync(self.app)
                    elif opr_type == 11:  # E_INFO_OPR_GUIDE_STEP; iParam is a GuideMark bit mask.
                        previous_guide_step = int(self.app.guide_step)
                        if 0 < i_param <= 0x7FFFFFFF:
                            persist_client_guide_step(self.app, i_param)
                            accepted = True
                        self.record("synthetic_guide_step_saved", previous=previous_guide_step,
                                    reported_mask=i_param,
                                    guide_step=int(self.app.guide_step),
                                    purpose="persist the client's native GuideMark progress per account")
                    elif opr_type in (12, 20, 21):
                        # E_INFO_OPR_LINEUP_EMITTER, REPORT_FRIEND_NUM, and
                        # REPORT_MAX_POWER are state/report notifications; the
                        # local backend accepts them without inventing data.
                        accepted = True
                    response_code = (SC_INFORMATION_OPR_SUCCEEDED if accepted
                                     else SC_INFORMATION_OPR_FAILED)
                    if lineup_player_sync is not None:
                        self.request.sendall(lineup_player_sync)
                    self.request.sendall(make_information_opr_response(opr_type, response_code))
                    self.record("synthetic_information_opr_response", request_id=159,
                                response_id=160, opr_type=opr_type,
                                i_param=i_param,
                                lineup_index=lineup_index if opr_type == 8 else None,
                                toy_top_updates=operation["toy_top_updates"],
                                changed_parts=changed_parts,
                                rejection_reason=rejection_reason,
                                code=response_code,
                                lineup_saved=bool(opr_type == 8 and accepted and changed_parts),
                                player_data_sync_sent=lineup_player_sync is not None,
                                lineup_sync_before_ack=lineup_player_sync is not None,
                                guide_step_saved=bool(opr_type == 11 and accepted),
                                accepted=accepted,
                                purpose={8: "persist the native lineup change",
                                         11: "persist the native tutorial completion bit",
                                         12: "acknowledge the native lineup emitter report",
                                         20: "acknowledge the native friend-count report",
                                         21: "acknowledge the native power report"}.get(opr_type, "reject unsupported InformationOpr"))
                elif message_id == 166:
                    request = frame[6:]
                    operation = parse_chapter_opr_request(request)
                    fingerprint = hashlib.sha256(request).hexdigest()
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        result = apply_chapter_opr(
                            self.app, operation, request_fingerprint=fingerprint,
                        )
                        if result["changed"] or result.get("persist"):
                            save_profile_state(self.app)
                    if result["changed"]:
                        self.request.sendall(make_app_player_data_sync(self.app))
                    self.request.sendall(make_chapter_opr_response(
                        int(result["response_code"]),
                    ))
                    self.record("synthetic_chapter_opr_response", request_id=166,
                                response_id=167, player_id=int(self.app.player_id),
                                operation=operation["operation"],
                                chapter_key=operation["chapter_key"],
                                chapter_version=operation["chapter_version"],
                                result_stars=operation["result"],
                                response_code=int(result["response_code"]),
                                changed=bool(result["changed"]),
                                attempt_id=result.get("attempt_id"),
                                duplicate_request=bool(result.get("duplicate_request", False)),
                                reward_items=result.get("reward_items", []),
                                reason=result.get("reason"),
                                chapter_battle_results=len(self.app.chapter_battle_results),
                                chapter_rewards_claimed=self.app.chapter_rewards_claimed,
                                reward_source="client-bundled ChapterData first-clear/replay rows")
                elif message_id == 168:
                    request = frame[6:]
                    operation = parse_chapter_pve_request(request)
                    fingerprint = hashlib.sha256(request).hexdigest()
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        result = apply_guaji_pve_operation(
                            self.app, operation, request_fingerprint=fingerprint,
                        )
                        if result["changed"] or result.get("persist"):
                            save_profile_state(self.app)
                    if result["changed"]:
                        self.request.sendall(make_app_player_data_sync(self.app))
                    self.request.sendall(make_chapter_pve_response(
                        int(result["response_code"]),
                    ))
                    self.record("synthetic_guaji_pve_response", request_id=168,
                                response_id=169, player_id=int(self.app.player_id),
                                operation=operation["operation"],
                                process=operation["param1"],
                                self_win_num=operation["self_win_num"],
                                use_parts=operation["use_parts"],
                                response_code=int(result["response_code"]),
                                changed=bool(result["changed"]),
                                attempt_id=result.get("attempt_id"),
                                duplicate_request=bool(result.get("duplicate_request", False)),
                                reward_items=result.get("reward_items", []),
                                reason=result.get("reason"),
                                pve_process=int(self.app.pve_info["Process"]),
                                reward_source="client ChapterPVEManager Rewards rows")
                elif message_id == 139:
                    bout_values = protobuf_varint_values(frame[6:])
                    requested_bout_opr = int32_value(bout_values.get(1, 0))
                    # E_RACE_GUIDE_WIN is generated by the client-side opening
                    # tutorial battle. Returning E_RACE_ERROR here raises the
                    # game's disconnected reminder, so acknowledge that exact
                    # operation; keep the existing no-match response for other
                    # request types until their server contract is recovered.
                    race_type = int32_value(bout_values.get(2, 0))
                    response_bout_opr = response_bout_opr_for_request(
                        requested_bout_opr, race_type
                    )
                    champion_sync = False
                    champion_reward: tuple[tuple[int, int], ...] = ()
                    champion_response = None
                    champion_reason = None
                    champion_result_won = False
                    if race_type == CHAMPION_RACE_TYPE and requested_bout_opr in (44, 43):
                        now = int(time.time())
                        with self.app.roguelike_lock:
                            refresh_shared_profile_state(self.app)
                            if requested_bout_opr == 44:
                                state, code, champion_reason = apply_champion_entry(
                                    self.app.championship_state,
                                    player_id=int(self.app.player_id),
                                    enemy_id=int32_value(bout_values.get(7, 0)),
                                    join_index=int32_value(bout_values.get(3, 0)),
                                    league_index=int32_value(bout_values.get(5, 0)),
                                    now=now,
                                )
                                self.app.championship_state = state
                                champion_response = code
                                champion_sync = code == 44
                                if champion_sync:
                                    save_profile_state(self.app)
                            else:
                                champion_result_won = bool(int32_value(bout_values.get(4, 0)))
                                code, champion_reason, champion_reward = complete_champion_match(
                                    self.app, enemy_id=int32_value(bout_values.get(7, 0)),
                                    join_index=int32_value(bout_values.get(3, 0)),
                                    league_index=int32_value(bout_values.get(5, 0)),
                                    won=champion_result_won, now=now,
                                )
                                champion_response = code
                                champion_sync = code == 43
                                if champion_sync:
                                    save_profile_state(self.app)
                        response_bout_opr = int(champion_response)
                    tutorial_completion_sync = None
                    if response_bout_opr == 6 and race_type == 1:
                        # Op 6 follows the locally simulated opening tutorial
                        # win. Persist that battle separately; GuideMark.Over
                        # is reported later by the client's Adventure guide grid.
                        changed = persist_local_tutorial_battle(self.app)
                        tutorial_completion_sync = make_app_player_data_sync(self.app)
                        self.record(
                            "synthetic_tutorial_battle_saved",
                            guide_step=int(self.app.guide_step),
                            tutorial_battle_complete=bool(self.app.tutorial_battle_complete),
                            guide_mark_over_mask=GUIDE_MARK_OVER_MASK,
                            changed=changed,
                            purpose="persist the local opening battle and its Quick Race total for relogin; GuideMark.Over remains client-reported",
                        )
                    if response_bout_opr == 10 and race_type == 3:
                        race_id = 900_000_000 + int(self.app.player_id)
                        self.request.sendall(make_race_state_sync(
                            race_id, race_type, race_step=0,
                            player_id=int(self.app.player_id),
                        ))
                        self.record("synthetic_race_state_sync", response_id=4,
                                    race_id=race_id, race_type=race_type,
                                    race_step=0, participant_count=2,
                                    opponent_id=int(self.app.player_id) + 1,
                                    purpose="seed native RaceProto roster before robot-match callback")
                    if tutorial_completion_sync is not None:
                        self.request.sendall(tutorial_completion_sync)
                    if champion_sync:
                        # Synchronize authoritative wallet, inventory and
                        # RaceHistory before the native result receipt is read.
                        self.request.sendall(make_app_player_data_sync(self.app))
                    self.request.sendall(make_bout_response(
                        response_bout_opr,
                        # The client result view adds param1 as one display
                        # reward. RaceReward therefore carries Gold only; the
                        # chest itself is already in the synchronized inventory.
                        # A loss uses zero values and an empty reward envelope
                        # so the client cannot render the chest as earned.
                        param1=(CHAMPION_RESULT_CHEST_ID if requested_bout_opr == 43
                                and response_bout_opr == 43 and champion_result_won
                                else 0 if requested_bout_opr == 43 and response_bout_opr == 43
                                else None),
                        param2=(1 if requested_bout_opr == 43
                                and response_bout_opr == 43 and champion_result_won
                                else 0 if requested_bout_opr == 43 and response_bout_opr == 43
                                else None),
                        reward_items=tuple(item for item in champion_reward
                                           if item[0] != CHAMPION_RESULT_CHEST_ID),
                    ))
                    self.record("synthetic_bout_response", request_id=139,
                                response_id=140,
                                requested_bout_opr=requested_bout_opr,
                                response_bout_opr=response_bout_opr,
                                response_name=({6: "E_RACE_GUIDE_WIN",
                                                10: "E_RACE_ROBOT_GAME",
                                                40: "E_RACE_QUERY_CHAMPION",
                                                44: "E_RACE_CHAMPION_ENTER",
                                                0: "E_RACE_ERROR"}.get(response_bout_opr)),
                                race_type=race_type,
                                purpose=("acknowledge the locally simulated opening tutorial win"
                                         if response_bout_opr == 6 else
                                         "acknowledge Survival matchmaking after seeding RaceProto"
                                         if response_bout_opr == 10 else
                                         "return a local no-op Champion query result without a live opponent"
                                         if response_bout_opr == 40 else
                                          "authorize a locally validated Champion robot challenge"
                                          if response_bout_opr == 44 else
                                          "settle one completed local robot Championship battle"
                                          if response_bout_opr == 43 else
                                          "reject an invalid or duplicate Championship transition"
                                          if response_bout_opr in (45, 46, 47) else
                                         "clear a non-tutorial match search without starting a race"))
                    if requested_bout_opr in (43, 44) and race_type == CHAMPION_RACE_TYPE:
                        self.record("local_championship_transition",
                                    request_operation=requested_bout_opr,
                                    response_operation=response_bout_opr,
                                    player_id=int(self.app.player_id),
                                    enemy_id=int32_value(bout_values.get(7, 0)),
                                    join_index=int32_value(bout_values.get(3, 0)),
                                    result=(bool(int32_value(bout_values.get(4, 0)))
                                            if requested_bout_opr == 43 else None),
                                    reward_items=list(champion_reward),
                                    reason=champion_reason,
                                    server_validation="client-simulated battle; entry and duplicate guarded, outcome not independently observed")
                elif message_id == 161:
                    request = frame[6:]
                    query_type = int32_value(protobuf_varint_values(request).get(1, 0))
                    if query_type == 6:  # QueryType.ChampionRace.
                        with self.app.roguelike_lock:
                            refresh_shared_profile_state(self.app)
                            self.app.championship_state, rolled = champion_period_rollover(
                                self.app.championship_state, now=int(time.time()),
                            )
                            if rolled:
                                save_profile_state(self.app)
                            response = make_champion_display_response(
                                request, int(self.app.player_id), self.app.championship_state,
                                player_profile=self.app,
                            )
                        self.request.sendall(response)
                        response_payload_offset = 4 if (response[1] & 0x7F) == 126 else 10 if (response[1] & 0x7F) == 127 else 2
                        response_message = response[response_payload_offset + 6:]
                        self.record("local_championship_display_response", request_id=161,
                                    response_id=162, player_id=int(self.app.player_id),
                                    query_type=query_type,
                                    requested_ids=parse_repeated_int64(request, 2),
                                    robot_details=len(protobuf_length_delimited_values(response_message, 2)),
                                    league_index=int(self.app.championship_state["league_index"]),
                                    mode=("synthetic score-ranked robot opponents"
                                          if int(self.app.championship_state["race_score"]) > 2
                                          else "display profiles for client-generated low-score robots"))
                    elif query_type == 100:  # QueryUserRankItem season-list profiles.
                        account = getattr(self.app, "account", {})
                        nickname = (account.get("username", "Local Player")
                                    if isinstance(account, dict) else "Local Player")
                        response = make_champion_rank_item_response(
                            request, int(self.app.player_id), nickname,
                            player_score=int(self.app.championship_state.get(
                                "top_champion_cups", 0,
                            )),
                        )
                        self.request.sendall(response)
                        response_payload_offset = (4 if (response[1] & 0x7F) == 126
                                                   else 10 if (response[1] & 0x7F) == 127 else 2)
                        response_message = response[response_payload_offset + 6:]
                        self.record("local_champion_rank_items_response", request_id=161,
                                    response_id=162, player_id=int(self.app.player_id),
                                    query_type=query_type,
                                    requested_ids=parse_repeated_int64(request, 2),
                                    returned_items=len(protobuf_length_delimited_values(response_message, 4)),
                                    mode="synthetic Top Champion season display profiles")
                    else:
                        self.record("unsupported_display_query_ignored", request_id=161,
                                    query_type=query_type,
                                    purpose="only Championship robot details are implemented locally")
                elif message_id == 181:
                    request = frame[6:]
                    enter_rows = protobuf_length_delimited_values(request, 2)
                    enter = enter_rows[0] if enter_rows else b""
                    outer_values = protobuf_varint_values(request)
                    values = protobuf_varint_values(enter)
                    chapter_id = int32_value(values.get(1, 0))
                    chapter_level = int32_value(values.get(2, 1))
                    requested_cost = int32_value(values.get(9, 1))
                    seed = int32_value(values.get(3, 0))
                    start_rows = protobuf_length_delimited_values(enter, 4)
                    start_values = protobuf_varint_values(start_rows[0]) if start_rows else {}
                    start_location = int32_value(start_values.get(1, 0))
                    hp_values = []
                    for toy_top in protobuf_length_delimited_values(enter, 5):
                        top_values = protobuf_varint_values(toy_top)
                        hp_values.append(int32_value(top_values.get(1, 0)))
                    progression_advances = {}
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        available = self.app.roguelike_keys
                        if requested_cost > available:
                            result_code = 18  # ERogueLikeCode.EnterMapKeyNumError
                            new_key_num = available
                        else:
                            result_code = 10  # ERogueLikeCode.EnterMapSuccess
                            new_key_num = max(0, available - requested_cost)
                            self.app.roguelike_keys = new_key_num
                            # Version and SysInfo.ServerTime use seconds since
                            # ui.getDateTime2018() on this client, not Unix time.
                            self.app.roguelike_map_state = make_initial_roguelike_map_state(
                                version=game_time_seconds(),
                                chapter_id=chapter_id,
                                chapter_level=chapter_level,
                                seed=seed,
                                start_location=start_location,
                                hp=hp_values,
                            )
                            # A successful Adventure entry is the catalog event
                            # RLEnterMap (condition 30), used by later GP tasks.
                            progression_advances = increment_level_task_counters(
                                self.app.progression_counters, 30,
                            )
                            save_profile_state(self.app)
                    if result_code == 10 and new_key_num != available:
                        self.request.sendall(make_roguelike_key_sync(
                            new_key_num, self.app.roguelike_search_points,
                        ))
                    if result_code == 10:
                        # The client merges repeated Nodes by key. Its local
                        # cache survives a give-up, so explicitly clear the
                        # previous run before syncing the newly seeded map.
                        self.request.sendall(make_roguelike_reply(
                            4, chapter_id=chapter_id,
                        ))  # ERogueLikeCode.RogueLikeNodesReset
                        self.request.sendall(make_roguelike_info_sync(self.app.roguelike_map_state))
                    self.request.sendall(make_roguelike_reply(
                        result_code, chapter_id=chapter_id,
                    ))
                    if progression_advances:
                        self.request.sendall(make_player_level_sync(
                            level=int(self.app.level), exp=int(self.app.exp),
                            power=int(self.app.power), task_score=int(self.app.task_score),
                            task_score_exchange_process=int(self.app.task_score_exchange_process),
                            inventory_items=self.app.inventory_items,
                            level_task_state=self.app.level_task_state,
                            progression_counters=self.app.progression_counters,
                            part_levels=self.app.part_levels,
                        ))
                    self.record("synthetic_roguelike_enter_map", request_id=181,
                                response_id=180, chapter_id=chapter_id,
                                chapter_level=chapter_level,
                                request_version=int32_value(outer_values.get(1, 0)),
                                sync_map_version=self.app.roguelike_map_state["version"] if result_code == 10 else None,
                                sync_map_version_epoch_ms=GAME_TIME_EPOCH_MS if result_code == 10 else None,
                                seed=seed, start_location=start_location,
                                initial_hp=hp_values,
                                requested_key_cost=requested_cost,
                                key_num_before=available, key_num_after=new_key_num,
                                result_code=result_code,
                                nodes_reset_code=4 if result_code == 10 else None,
                                progression_counters_advanced=progression_advances,
                                sync_message_id=5 if result_code == 10 else None,
                                purpose="clear stale nodes, seed the new local Adventure map, and allow entry")
                elif message_id == 182:
                    request = frame[6:]
                    enter_node = parse_roguelike_enter_node_request(request)
                    location = int(enter_node["location"])
                    chapter_id = int(enter_node["chapter_id"])
                    event_id = int(enter_node["event_id"])
                    event_exports = list(enter_node["export_ids"])
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        state = self.app.roguelike_map_state
                        node_status = 1
                        if state is not None:
                            previous_node = state["nodes"].get(location, {})
                            same_event = int(previous_node.get("event_id", 0)) == event_id
                            if same_event:
                                node_status = int(previous_node.get("status", 1))
                                if (ROGUELIKE_EVENT_TYPE_BY_ID.get(event_id) == 9
                                        and int(previous_node.get("export_bit", 0))
                                        and node_status < 3):
                                    node_status = 2  # NodeTrigger: a purchased shop remains traversable.
                            else:
                                node_status = 3 if location == state["start_location"] else 1
                            state["location"] = location
                            node_row = dict(previous_node) if same_event else {}
                            node_row.update({
                                "status": node_status,
                                "event_id": event_id,
                                "location_exports": event_exports or node_row.get("location_exports", []),
                            })
                            state["nodes"][location] = node_row
                            sync_state = json.loads(json.dumps(state))
                            save_profile_state(self.app)
                        else:
                            sync_state = None
                    if sync_state is not None:
                        self.request.sendall(make_roguelike_info_sync(sync_state))
                    self.request.sendall(make_roguelike_reply(
                        20, location=location, chapter_id=chapter_id,
                    ))  # ERogueLikeCode.EnterNodeSuccess
                    self.record("synthetic_roguelike_enter_node", request_id=182,
                                response_id=180, chapter_id=chapter_id,
                                location=location, event_id=event_id,
                                location_exports=event_exports,
                                location_export_count=len(event_exports),
                                node_status=node_status, result_code=20,
                                sync_message_id=5 if sync_state is not None else None,
                                purpose="persist event rewards and sync the entered Adventure node")
                elif message_id == 184:
                    request = frame[6:]
                    trigger_request = parse_roguelike_trigger_request(request)
                    location = int(trigger_request["location"])
                    chapter_id = int(trigger_request["chapter_id"])
                    event_id = int(trigger_request["event_id"])
                    export_ids = list(trigger_request["export_ids"])
                    trigger_location = int(trigger_request["trigger_location"])
                    buff_item_ids = list(trigger_request["buff_item_ids"])
                    hp_values = list(trigger_request["hp_values"])
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        result = apply_roguelike_trigger(
                            self.app,
                            chapter_id=chapter_id,
                            location=location,
                            event_id=event_id,
                            export_ids=export_ids,
                            hp_values=hp_values,
                            trigger_location=trigger_location,
                            buff_item_ids=buff_item_ids,
                        )
                        delivery_items = result["delivery_items"]
                        part_fragment_rewards = result["part_fragment_rewards"]
                        sync_state = result["sync_state"]
                        event_type = ROGUELIKE_EVENT_TYPE_BY_ID.get(event_id)
                        progression_condition = ROGUELIKE_LEVEL_TASK_CONDITION_BY_EVENT_TYPE.get(event_type)
                        progression_advances = {}
                        if (result["response_code"] == 31 and not result["repeated"]
                                and progression_condition is not None):
                            map_context = sync_state or self.app.roguelike_map_state or {}
                            chapter_level = int(map_context.get("chapter_level", 1))
                            progression_advances = increment_level_task_counters(
                                self.app.progression_counters,
                                progression_condition,
                                chapter_id=chapter_id,
                                chapter_level=chapter_level,
                            )
                            if progression_advances:
                                save_profile_state(self.app)
                    response_frames = make_roguelike_trigger_response_frames(
                        self.app, chapter_id=chapter_id, location=location,
                        result=result, progression_sync=bool(progression_advances),
                    )
                    for response_frame in response_frames:
                        self.request.sendall(response_frame)
                    # The last frame is ESC_RogueLikeRet (180), carrying the
                    # native Adventure reward receipt and completion code.
                    self.record("synthetic_roguelike_trigger", request_id=184,
                                response_id=180, chapter_id=chapter_id,
                                location=location, remaining_hp=hp_values,
                                trigger_location=trigger_location,
                                node_status=result.get("node_status"),
                                export_bit=result.get("export_bit"),
                                shop_purchase=result.get("shop_purchase"),
                                result_code=result["response_code"],
                                event_type=event_type,
                                progression_condition=progression_condition,
                                progression_counters_advanced=progression_advances,
                                reward_get_item_from=16,
                                reward_key_param=chapter_id,
                                trigger_export_ids=export_ids,
                                trigger_buff_item_ids=buff_item_ids,
                                resolved_export_ids=result["effective_export_ids"],
                                unresolved_buff_item_ids=result["unresolved_buff_item_ids"],
                                export_id_source=result["export_id_source"],
                                export_ids=export_ids,
                                reward_items=delivery_items,
                                part_fragment_rewards=part_fragment_rewards,
                                part_fragment_totals=self.app.part_fragments,
                                search_point_rewards=result["search_point_rewards"],
                                search_point_totals=self.app.roguelike_search_points,
                                chiji_rewards=result["chiji_rewards"],
                                event_buffs=(sync_state.get("event_buffs", {})
                                             if sync_state else {}),
                                repeated_trigger=result["repeated"],
                                backend_inventory_item_count=result["backend_inventory_item_count"],
                                unknown_export_ids=result["unknown_export_ids"],
                                reward_source="client 1.1.0.96 RogueLikeExportManager.getAllItem()",
                                reward_resolution=(
                                    "item rewards plus Adventure search points"
                                    if delivery_items and result["search_point_rewards"] else
                                    "backend-resolved ItemInfo" if delivery_items else
                                    "Adventure search-point sync" if result["search_point_rewards"] else
                                    "selected Chiji module sync" if result["chiji_rewards"] else
                                    "empty event reward list"
                                    if result["response_code"] == 31 else "none"
                                ),
                                sync_message_id=5 if sync_state is not None else None,
                                player_data_sync_id=(
                                    1 if (result["response_code"] == 31
                                          and (delivery_items or result.get("inventory_changed")))
                                    else None
                                ),
                                purpose="complete the local Adventure event and persist its node status")
                elif message_id == 183:
                    request = frame[6:]
                    misc_rows = protobuf_length_delimited_values(request, 2)
                    misc_bytes = misc_rows[0] if misc_rows else b""
                    misc = protobuf_varint_values(misc_bytes)
                    values = protobuf_varint_values(request)
                    chapter_id = int32_value(values.get(3, 0))
                    opr_type = int32_value(misc.get(1, 0))
                    response_code = {2: 60, 3: 61}.get(opr_type)
                    free_key_result = None
                    if opr_type == 2:
                        with self.app.roguelike_lock:
                            refresh_shared_profile_state(self.app)
                            self.app.roguelike_map_state = None
                            save_profile_state(self.app)
                    elif opr_type == 13:
                        # In the original UI this request is sent only after an
                        # ad callback. This revival build has no ads, so preserve
                        # the same one-key reward and server-side daily guard.
                        with self.app.roguelike_lock:
                            refresh_shared_profile_state(self.app)
                            free_key_result = apply_roguelike_free_key_claim(self.app)
                            response_code = int(free_key_result["response_code"])
                            if free_key_result["granted"]:
                                save_profile_state(self.app)
                        if free_key_result["granted"]:
                            # Refresh key count and the Daily claim bit before
                            # code 50 makes the client close and refresh the UI.
                            self.request.sendall(make_app_player_data_sync(self.app))
                    if response_code is not None:
                        self.request.sendall(make_roguelike_reply(
                            response_code, chapter_id=chapter_id,
                        ))
                    self.record("synthetic_roguelike_misc_response", request_id=183,
                                opr_type=opr_type, param=misc.get(2, 0),
                                chapter_id=chapter_id, response_code=response_code,
                                free_key_claim=free_key_result,
                                player_data_sync_id=(1 if free_key_result and free_key_result["granted"] else None),
                                purpose=("grant the local once-daily Adventure key without an ad"
                                         if opr_type == 13 else
                                         "acknowledge local Adventure give-up and post-battle selection"))
                elif message_id == 144:
                    request = protobuf_varint_values(frame[6:])
                    task_opr, task_id = request.get(1, 0), request.get(2, 0)
                    task_score_sync = None
                    level_task_sync = False
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        if task_opr == TASK_OPR_GET_REWARD and task_id == 1146368048:
                            success = not self.app.daily_task_claimed
                            if success:
                                self.app.daily_task_claimed = True
                                self.app.task_score += 10
                                save_profile_state(self.app)
                                task_score_sync = (
                                    int(self.app.task_score),
                                    int(self.app.task_score_exchange_process),
                                )
                                task_opr_ret = 1  # E_TASK_OPR_RET_GETREWARD_OK
                            else:
                                task_opr_ret = 5  # E_TASK_OPR_RET_ERROR_STATUS
                        elif task_opr == TASK_OPR_UPDATE:
                            task_opr_ret, task_updated = update_active_level_task(
                                task_id, level=int(self.app.level),
                                inventory_items=self.app.inventory_items,
                                level_task_state=self.app.level_task_state,
                                progression_counters=self.app.progression_counters,
                                part_levels=self.app.part_levels,
                            )
                            if task_updated:
                                save_profile_state(self.app)
                                level_task_sync = True
                        else:
                            task_opr_ret = 3  # E_TASK_OPR_RET_INVALID_TASK
                        daily_task_claimed = bool(self.app.daily_task_claimed)
                    if level_task_sync:
                        self.request.sendall(make_player_level_sync(
                            level=int(self.app.level), exp=int(self.app.exp),
                            power=int(self.app.power), task_score=int(self.app.task_score),
                            task_score_exchange_process=int(self.app.task_score_exchange_process),
                            inventory_items=self.app.inventory_items,
                            level_task_state=self.app.level_task_state,
                            progression_counters=self.app.progression_counters,
                            part_levels=self.app.part_levels,
                        ))
                    self.request.sendall(make_task_opr_response(task_opr_ret, task_id))
                    self.record("synthetic_task_opr_response", request_id=144,
                                response_id=145, task_opr=task_opr, task_id=task_id,
                                task_opr_ret=task_opr_ret,
                                success=task_opr_ret in (1, 2),
                                task_score=(task_score_sync[0] if task_score_sync else None),
                                purpose="persist daily task score or recompute verified LevelTask progress")
                    if task_score_sync:
                        time.sleep(0.05)
                        self.request.sendall(make_daily_task_status_sync(
                            3, task_score_sync[0], task_score_sync[1],
                        ))
                        self.record("synthetic_daily_task_status_sync", message_id=1,
                                    task_id=task_id, task_status=3,
                                    task_score=task_score_sync[0],
                                    purpose="mark the local task rewarded and persist its task-score reward")
                elif message_id == 146:
                    request = frame[6:]
                    request_values = protobuf_varint_values(request)
                    exchange_id = int32_value(request_values.get(1, 0))
                    sub_ids = protobuf_repeated_int32_values(request, 2)
                    rewards = TUTORIAL_TASK_EXCHANGE_REWARDS.get(exchange_id, ())
                    box_contents = TUTORIAL_TASK_EXCHANGE_BOX_CONTENTS.get(exchange_id, ())
                    grant_items = list(rewards)
                    for _, contents in box_contents:
                        grant_items.extend(contents)
                    progression_sync = False
                    level_before = None
                    power_before = None
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        claim = apply_level_first_reward_claim(
                            self.app, exchange_id=exchange_id, sub_ids=sub_ids,
                        )
                        success = bool(claim["success"])
                        already_claimed = bool(claim["already_claimed"])
                        requirements_met = bool(claim["requirements_met"])
                        tutorial_fallback = bool(claim["tutorial_fallback"])
                        level_before = claim["level_before"]
                        power_before = claim["power_before"]
                        grant_items = list(claim["grant_items"])
                        progression_sync = success
                        reward_counts = {
                            str(item_id): int(
                                self.app.part_fragments.get(item_id, 0)
                                if item_id in UPGRADEABLE_PART_LOCATIONS else
                                self.app.inventory_items.get(item_id, 0)
                            )
                            for item_id, _ in grant_items
                        }
                        normal_reward_counts = {
                            int(item_id): int(self.app.inventory_items.get(item_id, 0))
                            for item_id, _ in grant_items
                            if int(item_id) not in UPGRADEABLE_PART_LOCATIONS
                        }
                    result = 3 if success else (1 if already_claimed else 0)
                    if success:
                        self.request.sendall(make_app_player_data_sync(self.app))
                        if normal_reward_counts:
                            self.request.sendall(make_inventory_items_sync(tuple(
                                normal_reward_counts.items()
                            )))
                    self.request.sendall(make_task_exchange_opr_response(result, exchange_id))
                    if success:
                        # The tutorial chest has a native BoxList visual envelope
                        # and must not also be listed as a direct item. Ordinary
                        # LevelFirstReward items (including LV02's ticket) are
                        # sent in Items so the client reward popup can display them.
                        reward_item_infos = () if box_contents else tuple(grant_items)
                        self.request.sendall(make_task_exchange_get_items(
                            exchange_id, reward_item_infos, box_contents,
                        ))
                    self.record("synthetic_task_exchange_response", request_id=146,
                                response_id=147, reward_message_id=143 if success else None,
                                exchange_id=exchange_id, sub_ids=sub_ids,
                                result=result, success=success,
                                already_claimed=already_claimed,
                                tutorial_fallback=tutorial_fallback,
                                reward_items=[{"item_id": item_id, "num": quantity}
                                              for item_id, quantity in grant_items] if success else [],
                                box_contents=[{"box_id": box_id,
                                               "items":[{"item_id": item_id, "num": quantity}
                                                        for item_id, quantity in contents]}
                                              for box_id, contents in box_contents] if success else [],
                                inventory_counts=reward_counts,
                                claimed_exchanges=self.app.task_exchange_claims,
                                level_before=level_before, level_after=int(self.app.level),
                                power_before=power_before, power_after=int(self.app.power),
                                requirements_met=requirements_met,
                                purpose="claim the verified next-level exchange and persist its chest and GP")
                elif message_id == 176:
                    request = protobuf_varint_values(frame[6:])
                    achievement_id = request.get(1, 0)
                    with self.app.roguelike_lock:
                        refresh_shared_profile_state(self.app)
                        success = achievement_id == TEST_ACHIEVEMENT_ID and not self.app.achievement_claimed
                        if success:
                            self.app.achievement_claimed = True
                            self.app.inventory_items[DIAMOND_ITEM_ID] = (
                                int(self.app.inventory_items.get(DIAMOND_ITEM_ID, 0)) + 5
                            )
                            self.app.achievement_total_score += 5
                            self.app.achievement_reward_applied = True
                            save_profile_state(self.app)
                        achievement_claimed = bool(self.app.achievement_claimed)
                        diamond_count = int(self.app.inventory_items.get(DIAMOND_ITEM_ID, 0))
                        total_score = int(self.app.achievement_total_score)
                    if success:
                        # Sync state before the success event: the client's
                        # ACHIEVEMENT listener refreshes directly from PlayerData.
                        self.request.sendall(make_player_achievement_sync(
                            level=2, reward_time=1514736000, value=10,
                            total_score=total_score, diamond_count=diamond_count,
                        ))
                        time.sleep(0.05)
                    self.request.sendall(make_achievement_reply(0 if success else 1))
                    self.record("synthetic_achievement_opr_response", request_id=176,
                                response_id=177, achievement_id=achievement_id,
                                code=0 if success else 1, success=success,
                                total_score=total_score if success else 0,
                                gem_reward=5 if success else 0,
                                diamond_balance=diamond_count,
                                purpose="claim the seeded Veteran achievement and persist its five-gem reward")
                elif message_id == 155:
                    request = frame[6:]
                    scalar_fields = protobuf_varint_values(request)
                    mail_ids = [value.decode("utf-8") for value in protobuf_length_delimited_values(request, 3)]
                    mail_state = scalar_fields.get(4, 0)
                    changed = 0
                    # Runtime inspection confirmed that MailUI marks reads via
                    # HTTP MailService.MarkRead. This WS request is the
                    # attachment receipt path; only state 2 belongs here.
                    if mail_state == 2 and mail_ids:
                        with self.app.roguelike_lock:
                            changed = update_mail_state(mail_ids, state_value=mail_state,
                                                        path=self.app.mail_state_file)
                    self.request.sendall(make_mark_mail_reply())
                    self.record("synthetic_mail_state_reply", request_id=155, response_id=156,
                                mail_ids=mail_ids, mail_state=mail_state, updated=changed,
                                purpose="persist received state for the one local test mail")
        except (EOFError, ConnectionError, BrokenPipeError):
            return
        except Exception as exc:
            self.record("error", error=type(exc).__name__, detail=str(exc)[:240])


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allow-lan", action="store_true", help="Allow binding beyond loopback for an explicitly LAN-scoped test.")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--public-http-url", default="http://127.0.0.1:18765",
                        help="HTTP endpoint advertised to the game client.")
    parser.add_argument("--public-ws-url", default="ws://127.0.0.1:18766",
                        help="WebSocket endpoint advertised to the game client.")
    parser.add_argument("--player-id", type=int, default=10001)
    parser.add_argument("--mail-state-file", type=Path, default=DEFAULT_STATE_FILE,
                        help="Shared JSON state for the single synthetic local mail item.")
    parser.add_argument("--state-file", type=Path,
                        default=Path(__file__).resolve().parent / "data" / "local_profile_state.json",
                        help="Persistent JSON state for the synthetic local profile and Adventure run.")
    parser.add_argument("--accounts-db", type=Path,
                        help="Enable account sessions and load one isolated profile per player.")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_lan:
        parser.error("Non-loopback binding requires --allow-lan.")
    args.log_file = args.log_file.resolve() if args.log_file else None
    args.mail_state_file = args.mail_state_file.resolve()
    args.state_file = args.state_file.resolve()
    args.accounts_db = args.accounts_db.resolve() if args.accounts_db else None
    args.public_http_url = args.public_http_url.rstrip("/")
    args.public_ws_url = args.public_ws_url.rstrip("/")
    args.account_store = AccountStore(args.accounts_db) if args.accounts_db else None
    profile_state = load_profile_state(args.state_file, args.player_id)
    args.inventory_items = profile_state["inventory_items"]
    args.persisted_inventory_items = dict(args.inventory_items)
    args.part_fragments = profile_state["part_fragments"]
    args.persisted_part_fragments = dict(args.part_fragments)
    args.part_levels = profile_state["part_levels"]
    args.persisted_part_levels = dict(args.part_levels)
    args.first_chest_shuriken_claimed = bool(
        profile_state["first_chest_shuriken_claimed"]
    )
    args.roguelike_search_points = profile_state["roguelike_search_points"]
    args.persisted_roguelike_search_points = dict(args.roguelike_search_points)
    args.level = int(profile_state["level"])
    args.exp = int(profile_state["exp"])
    args.power = int(profile_state["power"])
    args.task_score = int(profile_state["task_score"])
    args.task_score_exchange_process = int(profile_state["task_score_exchange_process"])
    args.achievement_total_score = int(profile_state["achievement_total_score"])
    args.achievement_reward_applied = bool(profile_state["achievement_reward_applied"])
    args.level_task_state = profile_state["level_task_state"]
    args.progression_counters = profile_state["progression_counters"]
    args.roguelike_keys = int(profile_state["roguelike_keys"])
    args.roguelike_key_ad_claim_day = int(profile_state["roguelike_key_ad_claim_day"])
    args.roguelike_map_state = profile_state["roguelike_map_state"]
    args.local_purchases = profile_state["local_purchases"]
    args.lineups = profile_state["lineups"]
    args.persisted_lineups = normalize_lineups(profile_state["lineups"])
    args.daily_task_claimed = bool(profile_state["daily_task_claimed"])
    args.achievement_claimed = bool(profile_state["achievement_claimed"])
    args.guide_step = int(profile_state["guide_step"])
    args.tutorial_battle_complete = bool(profile_state["tutorial_battle_complete"])
    args.tutorial_upgrade_resources_granted = bool(
        profile_state["tutorial_upgrade_resources_granted"]
    )
    args.lottery_info = profile_state["lottery_info"]
    args.task_exchange_claims = list(profile_state["task_exchange_claims"])
    args.roguelike_lock = threading.RLock()
    args.chapter_battle_results = profile_state["chapter_battle_results"]
    args.chapter_rewards_claimed = profile_state["chapter_rewards_claimed"]
    args.chapter_battle_attempts = profile_state["chapter_battle_attempts"]
    args.pve_info = profile_state["pve_info"]
    args.guaji_pve_attempt = profile_state["guaji_pve_attempt"]
    server = Server((args.host, args.port), Handler)
    server.app = args  # type: ignore[attr-defined]
    source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    started_at = datetime.now(timezone.utc).isoformat()
    startup_event = {
        "time_utc": started_at,
        "event": "websocket_server_start",
        "source_sha256": source_sha256,
        "source_path": str(Path(__file__).resolve()),
        "host": args.host,
        "port": int(args.port),
        "accounts_enabled": bool(args.account_store),
        "client_version": "1.1.0.96",
    }
    if args.log_file:
        with args.log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(startup_event, ensure_ascii=False) + "\n")
    print(f"Listening on ws://{args.host}:{args.port}; build={source_sha256[:16]}; accounts={'enabled' if args.account_store else 'disabled'}; synthetic player sync + ESC_Login; Adventure keys={args.roguelike_keys}; inventory items={len(args.inventory_items)}; active Adventure={args.roguelike_map_state is not None}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
