"""Regression coverage for the client-verified opening GP progression tier."""

from __future__ import annotations

import sys
import threading
import uuid
import json
import struct
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402


@contextmanager
def temporary_profile():
    path = BACKEND_DIR / f".first-gp-test-{uuid.uuid4().hex}.json"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def profile_app(state_file: Path):
    state = backend.load_profile_state(state_file, 100_000_987)
    app = SimpleNamespace(
        state_file=state_file,
        player_id=100_000_987,
        roguelike_lock=threading.RLock(),
    )
    for name, value in state.items():
        setattr(app, name, value)
    app.persisted_inventory_items = dict(app.inventory_items)
    app.persisted_part_fragments = dict(app.part_fragments)
    app.persisted_part_levels = dict(app.part_levels)
    app.persisted_roguelike_search_points = dict(app.roguelike_search_points)
    app.persisted_lineups = backend.normalize_lineups(app.lineups)
    app.persisted_roguelike_keys = int(app.roguelike_keys)
    app.persisted_roguelike_map_state = app.roguelike_map_state
    app.persisted_pve_info = app.pve_info
    return app


def task_states(container: bytes) -> dict[int, tuple[int, int]]:
    task_list = backend.protobuf_length_delimited_values(container, 2)[0]
    result = {}
    for entry in backend.protobuf_length_delimited_values(task_list, 1):
        fields = backend.protobuf_varint_values(entry)
        state = backend.protobuf_length_delimited_values(entry, 2)[0]
        state_fields = backend.protobuf_varint_values(state)
        result[fields[1]] = (state_fields[1], state_fields[2])
    return result


def backend_test_message_payload(frame: bytes) -> tuple[int, bytes]:
    marker = frame[1] & 0x7F
    header_size = 2 if marker < 126 else (4 if marker == 126 else 10)
    payload = frame[header_size:]
    _length, message_id = struct.unpack(">IH", payload[:6])
    return message_id, payload[6:]


class FirstGpMissionTests(unittest.TestCase):
    def test_client_config_confirms_first_tier_requirements_and_five_gp(self):
        row = backend.LEVEL_PROGRESSION_ROWS[2]
        self.assertEqual(row["LevelTasks"], [1278226737, 1278226738])
        self.assertEqual(row["rewardGPA"], 5)
        self.assertEqual(row["lvUp_TaskNum"], 2)
        self.assertEqual(
            (backend.LEVEL_PROGRESSION_TASKS[1278226737]["condition"],
             backend.LEVEL_PROGRESSION_TASKS[1278226737]["maxProcess"],
             backend.LEVEL_PROGRESSION_TASKS[1278226737]["Param"]),
            (23, 1, 2),  # Upgrade one owned part to level 2.
        )
        self.assertEqual(
            (backend.LEVEL_PROGRESSION_TASKS[1278226738]["condition"],
             backend.LEVEL_PROGRESSION_TASKS[1278226738]["maxProcess"]),
            (16, 3),  # Own three parts.
        )
        championship_tier = backend.LEVEL_PROGRESSION_ROWS[3]
        # Recovered LevelOpen row 3 offers three candidate objectives; its
        # lvUp_TaskNum=2 means two candidates complete the tier. The Adventure
        # objective is therefore not a neighboring-tier row accidentally
        # appended by serialization.
        self.assertEqual(championship_tier["LevelTasks"], [
            1278226993, 1278226994, 1278226995,
        ])
        self.assertEqual(championship_tier["lvUp_TaskNum"], 2)
        self.assertEqual(
            (backend.LEVEL_PROGRESSION_TASKS[1278226993]["condition"],
             backend.LEVEL_PROGRESSION_TASKS[1278226993]["maxProcess"],
             backend.LEVEL_PROGRESSION_TASKS[1278226993]["Param"]),
            (23, 10, 1),  # Unlock ten different parts.
        )
        championship_task = backend.LEVEL_PROGRESSION_TASKS[1278226994]
        self.assertEqual(
            (championship_task["condition"], championship_task["RaceMarks"],
             championship_task["maxProcess"]),
            (0, 2, 1),  # One completed Championship race, not two wins.
        )
        self.assertEqual(
            (backend.LEVEL_PROGRESSION_TASKS[1278226995]["condition"],
             backend.LEVEL_PROGRESSION_TASKS[1278226995]["maxProcess"]),
            (30, 1),  # Start one Adventure.
        )

    def test_recovered_progression_catalog_covers_later_tiers_and_exchange_rewards(self):
        self.assertEqual(set(backend.LEVEL_PROGRESSION_ROWS), set(range(2, 41)))
        self.assertEqual(len(backend.LEVEL_PROGRESSION_EXCHANGE_ROWS), 39)
        self.assertEqual(
            backend.LEVEL_PROGRESSION_ROWS[4]["LevelTasks"],
            [1278227249, 1278227250, 1278227252],
        )
        self.assertIn(1278227249, backend.LEVEL_PROGRESSION_TASKS)
        self.assertEqual(backend.level_first_reward_exchange_id(2),
                         int.from_bytes(b"LV02", "big"))
        self.assertEqual(backend.LEVEL_PROGRESSION_EXCHANGE_REWARDS[
            int.from_bytes(b"LV02", "big")], ((5000, 1),))
        # TaskData localized strings are referenced by task ID; the backend
        # sends the actual IDs/progress and the shipped client supplies text.
        later = task_states(backend.make_level_task_container(3, {}, {}, {}, {}))
        self.assertTrue({1278227249, 1278227250, 1278227252}.issubset(later))
        self.assertNotIn(1278227505, later)  # Next tier does not leak into level 3.

    def test_fresh_profile_requires_a_real_upgrade_but_owns_three_starters(self):
        container = backend.make_level_task_container(1, {})
        states = task_states(container)
        self.assertEqual(set(states), {1278226737, 1278226738})
        self.assertEqual(states[1278226737], (0, backend.TASK_STATUS_DOING))
        self.assertEqual(states[1278226738], (3, backend.TASK_STATUS_COMPLETE))
        self.assertEqual(backend.owned_part_levels(), {
            part_id: 1 for part_id, _part_as in backend.SYNTHETIC_STARTER_PARTS
        })
        sync = backend.make_player_data_sync(100_000_987, level=1)
        frame_header = 2 if (sync[1] & 0x7F) < 126 else (
            4 if (sync[1] & 0x7F) == 126 else 10
        )
        player_data = sync[frame_header + 6:]
        synced_tasks = [
            value for value in backend.protobuf_length_delimited_values(player_data, 4)
            if backend.protobuf_varint_values(value).get(1) == backend.LEVEL_TASK_TYPE
        ]
        self.assertEqual(len(synced_tasks), 1)
        self.assertEqual(task_states(synced_tasks[0]), states)

    def test_client_catalog_costed_upgrade_completes_only_opening_upgrade_task(self):
        self.assertIn(5001, backend.ITEM_UPGRADE_ROWS)
        self.assertEqual(backend.PART_UPGRADE_KEYS.get(2001), 5001)
        self.assertEqual(backend.ITEM_UPGRADE_ROWS[5001]["Level1"]["piece"], 2)
        self.assertEqual(backend.ITEM_UPGRADE_ROWS[5001]["Level1"]["item"][0],
                         {"id": backend.GOLD_ITEM_ID, "num": 20})
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 20)
            self.assertEqual(app.part_fragments[2001], 2)
            result = backend.apply_part_level_upgrade(app, 2001)
            self.assertTrue(result["changed"])
            self.assertEqual(result["level_before"], 1)
            self.assertEqual(result["level_after"], 2)
            backend.record_successful_part_upgrade_progress(app)
            self.assertEqual(app.part_levels[2001], 2)
            self.assertEqual(app.inventory_items.get(backend.GOLD_ITEM_ID, 0), 0)
            self.assertEqual(app.part_fragments.get(2001, 0), 0)
            self.assertEqual(app.level_task_state[str(1278226737)], {
                "process": 1, "status": backend.TASK_STATUS_COMPLETE,
            })
            self.assertEqual(backend.level_task_progress(
                backend.LEVEL_PROGRESSION_TASKS[1278226738], app.inventory_items,
                app.level_task_state, app.progression_counters, app.part_levels,
            )[1], backend.TASK_STATUS_COMPLETE)
            backend.save_profile_state(app)
            restored = backend.load_profile_state(state_file, app.player_id)
            self.assertEqual(restored["part_levels"][2001], 2)
            self.assertEqual(restored["inventory_items"].get(backend.GOLD_ITEM_ID, 0), 0)
            self.assertEqual(restored["part_fragments"].get(2001, 0), 0)
            self.assertTrue(restored["tutorial_upgrade_resources_granted"])

    def test_failed_part_upgrade_does_not_complete_opening_upgrade_task(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            app.part_fragments[2001] = 1
            result = backend.apply_part_level_upgrade(app, 2001)
            self.assertFalse(result["changed"])
            self.assertEqual(result["item_opr_ret"], backend.ITEM_OPR_RET_PIECE_LIMIT)
            self.assertNotIn(backend.FIRST_LEVEL_UPGRADE_COUNTER,
                             app.progression_counters)
            self.assertEqual(backend.level_task_progress(
                backend.LEVEL_PROGRESSION_TASKS[1278226737], app.inventory_items,
                app.level_task_state, app.progression_counters, app.part_levels,
            ), (0, backend.TASK_STATUS_DOING))
    def test_task_update_persists_across_profile_reload(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            self.assertEqual(backend.level_task_progress(
                backend.LEVEL_PROGRESSION_TASKS[1278226737],
                app.inventory_items, app.level_task_state,
                app.progression_counters, app.part_levels,
            ), (0, backend.TASK_STATUS_DOING))
            advances = backend.record_successful_part_upgrade_progress(app)
            self.assertEqual(advances[backend.FIRST_LEVEL_UPGRADE_COUNTER], 1)
            result, updated = backend.update_active_level_task(
                1278226737, level=app.level,
                inventory_items=app.inventory_items,
                level_task_state=app.level_task_state,
                progression_counters=app.progression_counters,
                part_levels=app.part_levels,
            )
            self.assertEqual((result, updated), (2, True))
            backend.save_profile_state(app)

            restored = backend.load_profile_state(state_file, app.player_id)
            self.assertEqual(restored["level_task_state"]["1278226737"], {
                "process": 1, "status": backend.TASK_STATUS_COMPLETE,
            })
            self.assertEqual(
                task_states(backend.make_level_task_container(
                    restored["level"], restored["inventory_items"],
                    restored["level_task_state"], restored["progression_counters"],
                    restored["part_levels"],
                ))[1278226737][1],
                backend.TASK_STATUS_COMPLETE,
            )

    def test_first_claim_awards_five_gp_once_and_persists_claim(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            upgrade = backend.apply_part_level_upgrade(app, 2001)
            self.assertTrue(upgrade["changed"])
            backend.record_successful_part_upgrade_progress(app)
            before_inventory = dict(app.inventory_items)
            first = backend.apply_level_first_reward_claim(
                app, exchange_id=backend.LEVEL_FIRST_REWARD_ID, sub_ids=[],
            )
            self.assertTrue(first["requirements_met"])
            self.assertTrue(first["success"])
            self.assertEqual((app.level, app.power), (2, 5))
            self.assertEqual(app.task_exchange_claims, [backend.LEVEL_FIRST_REWARD_ID])
            saved = backend.load_profile_state(state_file, app.player_id)
            self.assertEqual(saved["level"], 2)
            self.assertEqual(saved["power"], 5)
            self.assertEqual(saved["task_exchange_claims"], [backend.LEVEL_FIRST_REWARD_ID])
            for item_id, quantity in first["grant_items"]:
                if item_id in backend.PART_ITEM_LOCATIONS:
                    self.assertGreaterEqual(
                        saved["part_fragments"].get(item_id, 0), quantity,
                    )
                else:
                    self.assertEqual(
                        saved["inventory_items"].get(item_id, 0),
                        before_inventory.get(item_id, 0) + quantity,
                    )

            snapshot = (dict(app.inventory_items), dict(app.part_fragments),
                        app.level, app.power)
            duplicate = backend.apply_level_first_reward_claim(
                app, exchange_id=backend.LEVEL_FIRST_REWARD_ID, sub_ids=[],
            )
            self.assertFalse(duplicate["success"])
            self.assertTrue(duplicate["already_claimed"])
            self.assertEqual(
                (app.inventory_items, app.part_fragments, app.level, app.power),
                snapshot,
            )

    def test_level_two_tier_claim_accepts_two_of_three_and_persists_once(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            app.level = 2
            app.power = 5
            app.task_exchange_claims = [backend.LEVEL_FIRST_REWARD_ID]
            app.level_task_state = {
                "1278226993": {"process": 10, "status": backend.TASK_STATUS_COMPLETE},
                "1278226994": {"process": 0, "status": backend.TASK_STATUS_DOING},
                "1278226995": {"process": 1, "status": backend.TASK_STATUS_COMPLETE},
            }
            exchange_id = backend.level_first_reward_exchange_id(2)
            self.assertEqual(exchange_id, int.from_bytes(b"LV02", "big"))

            result = backend.apply_level_first_reward_claim(
                app, exchange_id=exchange_id, sub_ids=[],
            )
            self.assertTrue(result["requirements_met"])
            self.assertTrue(result["success"])
            self.assertFalse(result["already_claimed"])
            self.assertEqual(result["grant_items"], [(5000, 1)])
            self.assertEqual((app.level, app.power), (3, 10))
            self.assertEqual(app.inventory_items[5000], 1)
            self.assertEqual(
                app.task_exchange_claims,
                [backend.LEVEL_FIRST_REWARD_ID, exchange_id],
            )
            # Only the two completed candidates are marked rewarded in the
            # saved state; the unused Adventure objective remains incomplete.
            self.assertEqual(app.level_task_state["1278226993"]["status"],
                             backend.TASK_STATUS_REWARDED)
            self.assertEqual(app.level_task_state["1278226995"]["status"],
                             backend.TASK_STATUS_REWARDED)
            self.assertEqual(app.level_task_state["1278226994"]["status"],
                             backend.TASK_STATUS_DOING)

            saved = backend.load_profile_state(state_file, app.player_id)
            self.assertEqual(saved["level"], 3)
            self.assertEqual(saved["power"], 10)
            self.assertEqual(saved["inventory_items"][5000], 1)
            self.assertIn(exchange_id, saved["task_exchange_claims"])
            self.assertEqual(saved["level_task_state"]["1278226994"]["status"],
                             backend.TASK_STATUS_DOING)

            # The protocol response uses the client-defined success enum and
            # echoes the requested ExchangeID (message 147).
            message_id, payload = backend_test_message_payload(
                backend.make_task_exchange_opr_response(3, exchange_id),
            )
            self.assertEqual(message_id, 147)
            self.assertEqual(backend.protobuf_varint_values(payload),
                             {1: 3, 2: exchange_id})

            # Recreate the profile application object from disk to model a
            # relogin/backend reload before the duplicate claim attempt.
            restored = profile_app(state_file)
            before = (dict(restored.inventory_items), dict(restored.part_fragments),
                      restored.level, restored.power,
                      list(restored.task_exchange_claims))
            duplicate = backend.apply_level_first_reward_claim(
                restored, exchange_id=exchange_id, sub_ids=[],
            )
            self.assertFalse(duplicate["success"])
            self.assertTrue(duplicate["already_claimed"])
            self.assertEqual(
                (restored.inventory_items, restored.part_fragments, restored.level,
                 restored.power, restored.task_exchange_claims),
                before,
            )

            # Item type 13 is serialized as BaseInfo.DefaultItems, including
            # in an immediate sync and the native SC_GetItems reward envelope.
            player_sync = backend.make_player_data_sync(
                restored.player_id, inventory_items=restored.inventory_items,
                part_levels=restored.part_levels,
                part_fragments=restored.part_fragments,
            )
            player_data = backend_test_message_payload(player_sync)[1]
            base = backend.protobuf_length_delimited_values(player_data, 1)[0]
            default_items = [backend.protobuf_varint_values(row)
                             for row in backend.protobuf_length_delimited_values(base, 22)]
            self.assertIn({1: 5000, 2: 1}, default_items)
            receipt_id, receipt_payload = backend_test_message_payload(
                backend.make_task_exchange_get_items(exchange_id, ((5000, 1),)),
            )
            self.assertEqual(receipt_id, 143)
            detail = backend.protobuf_length_delimited_values(receipt_payload, 2)[0]
            item_rows = [backend.protobuf_varint_values(row)
                         for row in backend.protobuf_length_delimited_values(detail, 1)]
            self.assertEqual(item_rows, [{1: 5000, 2: 1}])

    def test_level_two_tier_claim_rejects_only_one_completed_candidate(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            app.level = 2
            app.power = 5
            app.task_exchange_claims = [backend.LEVEL_FIRST_REWARD_ID]
            app.level_task_state = {
                "1278226993": {"process": 10, "status": backend.TASK_STATUS_COMPLETE},
                "1278226994": {"process": 0, "status": backend.TASK_STATUS_DOING},
                "1278226995": {"process": 0, "status": backend.TASK_STATUS_DOING},
            }
            before = (app.level, app.power, list(app.task_exchange_claims))
            result = backend.apply_level_first_reward_claim(
                app,
                exchange_id=backend.level_first_reward_exchange_id(2),
                sub_ids=[],
            )
            self.assertFalse(result["requirements_met"])
            self.assertFalse(result["success"])
            self.assertEqual((app.level, app.power, app.task_exchange_claims), before)

    def test_level_two_tier_claim_rejects_nonempty_sub_ids(self):
        app = SimpleNamespace(
            level=2, power=5, task_exchange_claims=[], guide_step=0,
            inventory_items={}, part_levels={}, progression_counters={},
            level_task_state={
                "1278226993": {"process": 10, "status": backend.TASK_STATUS_COMPLETE},
                "1278226995": {"process": 1, "status": backend.TASK_STATUS_COMPLETE},
            },
        )
        result = backend.apply_level_first_reward_claim(
            app, exchange_id=backend.level_first_reward_exchange_id(2),
            sub_ids=[123],
        )
        self.assertTrue(result["requirements_met"])
        self.assertFalse(result["success"])
        self.assertEqual((app.level, app.power, app.task_exchange_claims), (2, 5, []))

    def test_later_tier_claim_uses_recovered_exchange_and_exact_reward_rows(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            app.level = 3
            app.power = 10
            app.task_exchange_claims = [
                backend.level_first_reward_exchange_id(1),
                backend.level_first_reward_exchange_id(2),
            ]
            row = backend.LEVEL_PROGRESSION_ROWS[4]
            selected_tasks = row["LevelTasks"][:int(row["lvUp_TaskNum"])]
            app.level_task_state = {
                str(task_id): {
                    "process": int(backend.LEVEL_PROGRESSION_TASKS[task_id]["maxProcess"]),
                    "status": backend.TASK_STATUS_COMPLETE,
                }
                for task_id in selected_tasks
            }
            exchange_id = backend.level_first_reward_exchange_id(3)
            configured = backend.LEVEL_PROGRESSION_EXCHANGE_REWARDS[exchange_id]
            result = backend.apply_level_first_reward_claim(
                app, exchange_id=exchange_id, sub_ids=[],
            )
            self.assertTrue(result["success"])
            self.assertEqual(tuple(result["grant_items"]), configured)
            restored = backend.load_profile_state(state_file, app.player_id)
            self.assertIn(exchange_id, restored["task_exchange_claims"])
            for item_id, quantity in configured:
                if item_id in backend.UPGRADEABLE_PART_LOCATIONS:
                    self.assertGreaterEqual(restored["part_fragments"].get(item_id, 0), quantity)
                    self.assertGreaterEqual(restored["part_levels"].get(item_id, 0), 1)
                else:
                    self.assertGreaterEqual(restored["inventory_items"].get(item_id, 0), quantity)

    def test_old_claimed_profile_gets_missing_five_gp_without_duplicate_reward(self):
        with temporary_profile() as state_file:
            original = {
                "player_id": 100_000_987,
                "level": 2,
                "power": 0,
                "schema_version": 8,
                "task_exchange_claims": [backend.LEVEL_FIRST_REWARD_ID],
                "inventory_items": {1228156978: 1},
            }
            state_file.write_text(json.dumps(original), encoding="utf-8")
            app = profile_app(state_file)
            backend.refresh_shared_profile_state(app)
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(app.power, 5)
            self.assertEqual(persisted["power"], 5)
            self.assertEqual(persisted["level"], 2)
            self.assertEqual(persisted["inventory_items"]["1228156978"], 1)

            # Migration is a floor, so it preserves later legitimate GP.
            original["power"] = 12
            state_file.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(
                backend.load_profile_state(state_file, 100_000_987)["power"], 12,
            )

    def test_other_task_update_and_exchange_are_not_unlocked_by_first_tier(self):
        state = {}
        result, updated = backend.update_active_level_task(
            1278226737 + 50, level=1, inventory_items={}, level_task_state=state,
        )
        self.assertEqual((result, updated, state), (3, False, {}))
        self.assertFalse(backend.can_claim_level_first_reward(
            exchange_id=backend.LEVEL_FIRST_REWARD_ID + 1,
            sub_ids=[], already_claimed=False, requirements_met=True,
            guide_step=backend.GUIDE_MARK_OVER_MASK, has_known_reward=False,
        ))


if __name__ == "__main__":
    unittest.main()
