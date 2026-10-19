"""Regression coverage for the data-driven timed box lifecycle."""

from __future__ import annotations

import sys
import json
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402


@contextmanager
def temporary_profile():
    path = BACKEND_DIR / f".box-test-{uuid.uuid4().hex}.json"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp").unlink(missing_ok=True)


def profile_app(path: Path, *, player_id: int = 100_000_981):
    state = backend.load_profile_state(path, player_id)
    app = SimpleNamespace(state_file=path, player_id=player_id,
                          roguelike_lock=threading.RLock())
    for name, value in state.items():
        setattr(app, name, value)
    app.account = {"username": "Box Test"}
    app.persisted_inventory_items = dict(app.inventory_items)
    app.persisted_part_fragments = dict(app.part_fragments)
    app.persisted_part_levels = dict(app.part_levels)
    app.persisted_roguelike_search_points = dict(app.roguelike_search_points)
    app.persisted_lineups = backend.normalize_lineups(app.lineups)
    app.persisted_roguelike_keys = app.roguelike_keys
    app.persisted_roguelike_map_state = app.roguelike_map_state
    app.persisted_pve_info = app.pve_info
    app.persisted_championship_state = backend.normalize_championship_state(
        app.championship_state,
    )
    app.persisted_daily_gold_day = app.daily_gold_day
    app.persisted_daily_gold_earned = app.daily_gold_earned
    app.persisted_save_boxes = backend.normalize_save_boxes(app.save_boxes)
    return app


def put_box(app, slot: int, item_id: int, *, can_open: int = 0,
            queued_at: int = 0, pile: int = 1):
    app.save_boxes[str(slot)] = {
        "item_id": item_id, "can_open_time": can_open,
        "ready_countdown_time": queued_at, "reason": "test", "pile": pile,
    }
    backend.save_profile_state(app)


def base_info_from_sync(sync: bytes) -> bytes:
    marker = sync[1] & 0x7F
    header_size = 2 if marker < 126 else (4 if marker == 126 else 10)
    player_data = sync[header_size + 6:]
    return backend.protobuf_length_delimited_values(player_data, 1)[0]


def box_slot_entries(sync: bytes) -> dict[int, tuple[bytes | None, bytes]]:
    """Return each SaveBoxList slot's optional value and encoded container."""
    entries = backend.protobuf_length_delimited_values(base_info_from_sync(sync), 9)
    result = {}
    for entry in entries:
        fields = backend.protobuf_varint_values(entry)
        values = backend.protobuf_length_delimited_values(entry, 2)
        result[int(fields[1])] = (values[0] if values else None, entry)
    return result


def websocket_message_payload(frame: bytes) -> tuple[int, bytes]:
    marker = frame[1] & 0x7F
    offset = 2 if marker < 126 else (4 if marker == 126 else 10)
    body = frame[offset:]
    return int.from_bytes(body[4:6], "big"), body[6:]


class BoxLifecycleTests(unittest.TestCase):
    def test_initial_player_sync_includes_existing_chests_and_timestamps(self):
        with temporary_profile() as path:
            app = profile_app(path)
            put_box(app, 2, 1228025907, can_open=1_900_000_000)
            put_box(app, 4, 1228025908, queued_at=1_900_000_001)
            loaded = backend.load_profile_state(path, app.player_id)
            sync = backend.make_player_data_sync(
                app.player_id, save_boxes=loaded["save_boxes"],
            )
            slots = box_slot_entries(sync)
            self.assertEqual(set(slots), {2, 4})
            self.assertEqual(backend.protobuf_varint_values(slots[2][0]), {
                1: 1228025907, 2: 1_900_000_000, 3: 0, 5: 1,
            })
            self.assertEqual(backend.protobuf_varint_values(slots[4][0]), {
                1: 1228025908, 2: 0, 3: 1_900_000_001, 5: 1,
            })

    def test_opening_sync_tombstones_removed_slot_and_preserves_queued_box(self):
        with temporary_profile() as path:
            app = profile_app(path)
            app.championship_state["race_score"] = 1
            app.inventory_items[backend.DIAMOND_ITEM_ID] = 100
            now = backend.game_time_seconds()
            put_box(app, 0, 1228025907, can_open=now)
            put_box(app, 1, 1228025908, queued_at=now + 500)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=now, draw=lambda _: 0,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(result["removed_save_box_slots"], [0])

            sync = backend.make_app_player_data_sync(
                app, deleted_save_box_slots=result["removed_save_box_slots"],
            )
            slots = box_slot_entries(sync)
            self.assertIsNone(slots[0][0])  # idx-only client deletion entry.
            self.assertEqual(backend.protobuf_varint_values(slots[0][1]), {1: 0})
            self.assertIsNotNone(slots[1][0])
            self.assertEqual(backend.protobuf_varint_values(slots[1][0]), {
                1: 1228025908, 2: 0, 3: now + 500, 5: 1,
            })
            persisted = backend.load_profile_state(path, app.player_id)
            self.assertEqual(set(persisted["save_boxes"]), {"1"})
            self.assertEqual(persisted["save_boxes"]["1"]["ready_countdown_time"], now + 500)

            retry = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=1_001, draw=lambda _: 0,
            )
            self.assertFalse(retry["changed"])
            self.assertEqual(backend.load_profile_state(path, app.player_id)["save_boxes"],
                             persisted["save_boxes"])

    def test_championship_loss_sync_does_not_create_or_reset_saved_chests(self):
        with temporary_profile() as path:
            app = profile_app(path)
            now = backend.game_time_seconds()
            put_box(app, 3, 1228025907, can_open=now + 900)
            saved_before = backend.normalize_save_boxes(app.save_boxes)
            enemy_id = backend.champion_robot_ids(app.player_id)[0]
            join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
            app.championship_state, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id,
                enemy_id=enemy_id, join_index=join_index, league_index=1,
                now=now,
            )
            self.assertEqual(code, 44)
            code, _, rewards = backend.complete_champion_match(
                app, enemy_id=enemy_id, join_index=join_index,
                league_index=1, won=False, now=now + 1,
            )
            self.assertEqual(code, 43)
            self.assertEqual(rewards, ())
            sync = backend.make_app_player_data_sync(app)
            self.assertEqual(box_slot_entries(sync)[3][0],
                             box_slot_entries(backend.make_player_data_sync(
                                 app.player_id, save_boxes=saved_before,
                             ))[3][0])
            self.assertEqual(backend.normalize_save_boxes(app.save_boxes), saved_before)
            self.assertEqual(backend.load_profile_state(path, app.player_id)["save_boxes"],
                             saved_before)

    def test_i203_uses_client_visible_minimum_race_score_for_part_eligibility(self):
        app = SimpleNamespace(inventory_items={}, part_fragments={}, part_levels={},
                              championship_state={"race_score": 0})
        low_score_rewards = backend.resolve_configured_box_rewards(
            app, 1228025907, race_score=0, draw=lambda _: 0,
        )
        eligible_rewards = backend.resolve_configured_box_rewards(
            app, 1228025907, race_score=1, draw=lambda _: 0,
        )
        # Fresh profiles persist 0, while the client receives RaceScore=1.
        # Both must therefore expose the same Chance.Level=1 part pools.
        self.assertEqual(low_score_rewards, eligible_rewards)
        self.assertIn({"item_id": 2001, "num": 3, "item_type": 0}, eligible_rewards)
        self.assertIn({"item_id": 1227894833, "num": 82, "item_type": 3}, eligible_rewards)

        with temporary_profile() as path:
            app = profile_app(path)
            app.championship_state["race_score"] = 0
            put_box(app, 0, 1228025907, can_open=100)
            gold_before = app.inventory_items.get(backend.GOLD_ITEM_ID, 0)
            fragments_before = app.part_fragments.get(2001, 0)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=100, draw=lambda _: 0,
            )
            expected_rewards = [
                row for row in eligible_rewards if row["item_id"] != 2011
            ] + [{"item_id": 2011, "num": 1, "item_type": 1}]
            self.assertEqual(result["rewards"], expected_rewards)
            receipt_id, receipt_payload = websocket_message_payload(
                backend.make_box_open_get_items(1228025907, result["rewards"]),
            )
            self.assertEqual(receipt_id, 143)
            receipt_items_detail = backend.protobuf_length_delimited_values(
                receipt_payload, 2,
            )[0]
            receipt_info_blob = backend.protobuf_length_delimited_values(
                receipt_items_detail, 2,
            )[0]
            receipt_item_infos = backend.protobuf_length_delimited_values(
                receipt_info_blob, 1,
            )
            receipt_rows = [backend.protobuf_varint_values(row)
                            for row in receipt_item_infos]
            self.assertEqual(receipt_rows, [
                {1: row["item_id"], 2: row["num"]} for row in expected_rewards
            ])
            stored = backend.load_profile_state(path, app.player_id)
            self.assertEqual(stored["inventory_items"][backend.GOLD_ITEM_ID], gold_before + 82)
            self.assertEqual(stored["part_fragments"][2001], fragments_before + 6)
            self.assertNotIn(2011, stored["part_fragments"])
            self.assertEqual(stored["part_levels"][2011], 3)
            sync = backend.make_app_player_data_sync(app)
            serialized_parts = backend.protobuf_length_delimited_values(
                base_info_from_sync(sync), 15,
            )
            part_rows = {backend.protobuf_varint_values(row)[1]:
                         backend.protobuf_varint_values(
                             backend.protobuf_length_delimited_values(row, 2)[0]
                         ) for row in serialized_parts}
            self.assertEqual(part_rows[2001][2], fragments_before + 6)

    def test_score_floor_does_not_bypass_higher_chance_level_requirements(self):
        box_id = 1228025907
        original_rows = backend.BOX_CHANCE_ROWS[box_id]
        backend.BOX_CHANCE_ROWS[box_id] = [
            {"RandomIndex": 1, "Level": 2, "Pvalue": 10, "OwnedPvalue": 10,
             "NumRank1": 1, "reward": {"ID": 2001, "Num": 1}},
            {"RandomIndex": 1, "Level": 1, "Pvalue": 10, "OwnedPvalue": 10,
             "NumRank1": 1, "reward": {"ID": 2005, "Num": 1}},
        ]
        try:
            rewards = backend.resolve_configured_box_rewards(
                SimpleNamespace(inventory_items={}, part_fragments={},
                                part_levels={}, championship_state={"race_score": 0}),
                box_id, race_score=0, draw=lambda _: 0,
            )
        finally:
            backend.BOX_CHANCE_ROWS[box_id] = original_rows
        self.assertEqual(rewards, [{"item_id": 2005, "num": 1, "item_type": 1}])

    def test_recovered_timed_box_inventory_has_distinct_delays_and_chance_rows(self):
        boxes = {box_id: row for box_id, row in backend.BOX_ITEM_ROWS.items()
                 if int(row["itemType"]) == 6}
        self.assertEqual(len(boxes), 8)
        self.assertEqual({box_id: int(row["DelayOpenTime"])
                          for box_id, row in boxes.items()}, {
            1228025907: 7200, 1228025908: 14400, 1228025909: 28800,
            1228025910: 43200, 1228025911: 43200, 1228025912: 86400,
            1228026420: 3600, 1228156977: 1,
        })
        self.assertTrue(all(backend.BOX_CHANCE_ROWS.get(box_id) for box_id in boxes))

    def test_each_timed_box_resolves_its_configured_reward_tree_and_score_gate(self):
        app = SimpleNamespace(inventory_items={}, part_fragments={}, part_levels={},
                              championship_state={"race_score": 100})
        for box_id in (1228025907, 1228025908, 1228025909, 1228025910,
                       1228025911, 1228025912, 1228026420, 1228156977):
            with self.subTest(box_id=box_id):
                rewards = backend.resolve_configured_box_rewards(
                    app, box_id, race_score=100, draw=lambda _: 0,
                )
                self.assertTrue(rewards)
                self.assertTrue(all(reward["item_type"] in (0, 1, 2, 3, 4)
                                    for reward in rewards))

        # The client-visible floor is 1 even when a new profile stores 0;
        # Level 1 content is eligible while higher Level requirements remain.
        floor_rewards = backend.resolve_configured_box_rewards(
            app, 1228025910, race_score=0, draw=lambda _: 0,
        )
        self.assertTrue(floor_rewards)
        self.assertTrue(all(row["item_type"] in (0, 1, 2)
                            for row in floor_rewards))

    def test_owned_reward_weight_falls_back_to_configured_pvalue(self):
        app = SimpleNamespace(inventory_items={101: 1}, part_fragments={},
                              part_levels={})
        rows = [
            {"reward": {"ID": 101}, "Pvalue": 2, "OwnedPvalue": 0},
            {"reward": {"ID": 102}, "Pvalue": 5, "OwnedPvalue": 0},
        ]
        # Total fallback weight is 7. A roll at 2 selects the second row.
        self.assertIs(backend._weighted_chance_row(rows, app, draw=lambda _: 2),
                      rows[1])

    def test_box_part_rewards_unlock_usable_part_and_preserve_fragment_progress(self):
        # Recovered ItemUpgrade.initLevel supplies basic ownership; the part's
        # own Piece count continues to track fragment progress.
        self.assertNotIn(2009, backend.PART_ITEM_LOCATIONS)
        self.assertNotIn(2027, backend.PART_ITEM_LOCATIONS)
        self.assertIn(2009, backend.UPGRADEABLE_PART_LOCATIONS)

        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_983)
            # Part 2009 is in the recovered box reward data as a bottom part.
            self.assertEqual(backend.credit_box_reward(app, 2009, 2),
                             "part_fragments")
            self.assertEqual(app.part_levels[2009], backend.part_acquisition_level(2009))
            backend.save_profile_state(app)
            loaded = backend.load_profile_state(path, app.player_id)
            self.assertEqual(loaded["part_fragments"].get(2009), 2)
            self.assertEqual(loaded["part_levels"][2009], backend.part_acquisition_level(2009))
            part_sync = backend.make_player_data_sync(
                app.player_id, part_fragments=loaded["part_fragments"],
                part_levels=loaded["part_levels"],
            )
            parts = backend.protobuf_length_delimited_values(
                base_info_from_sync(part_sync), 17,
            )
            part_row = next(
                row for row in parts
                if backend.protobuf_varint_values(row).get(1) == 2009
            )
            part_data = backend.protobuf_length_delimited_values(part_row, 2)[0]
            self.assertEqual(backend.protobuf_varint_values(part_data), {
                1: backend.part_acquisition_level(2009), 2: 2, 3: 3,
            })
            # Client GetItems animates from post-sync item count minus reward
            # quantity. The acquired base part keeps that baseline >= 1.
            part_values = backend.protobuf_varint_values(part_data)
            self.assertEqual(part_values[2] + 1 - 2, 1)
            self.assertIn(2009, backend.owned_part_levels({}, loaded["part_levels"]))
            accepted, _, _, reason = backend.apply_lineup_part_updates(
                app.lineups, app.inventory_items, app.part_levels, 1,
                [{"toy_index": 1, "parts": {"bot": 2009}}],
            )
            self.assertTrue(accepted, reason)

    def test_soprano_first_fragment_unlocks_like_other_parts_and_shuriken_guarantee_remains(self):
        # Soprano (2021) follows the same first-fragment ownership rule;
        # Shuriken's separate first-chest guarantee remains intact.
        self.assertEqual(backend.UPGRADEABLE_PART_LOCATIONS[2021], 2)
        self.assertEqual(backend.UPGRADEABLE_PART_LOCATIONS[2011], 1)

        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_988)
            backend.credit_box_reward(app, 2021, 1)
            app.part_levels[2011] = 3
            backend.save_profile_state(app)

            self.assertEqual(app.part_fragments[2021], 1)
            self.assertEqual(app.part_levels[2021], backend.part_acquisition_level(2021))
            self.assertIn(2021, backend.owned_part_levels(app.inventory_items, app.part_levels))
            self.assertEqual(backend.owned_part_levels(
                app.inventory_items, app.part_levels,
            )[2011], 3)

            sync = backend.make_player_data_sync(
                app.player_id,
                part_fragments=app.part_fragments,
                part_levels=app.part_levels,
            )
            base_info = base_info_from_sync(sync)
            soprano_row = next(
                row for row in backend.protobuf_length_delimited_values(
                    base_info, 17,
                ) if backend.protobuf_varint_values(row).get(1) == 2021
            )
            soprano = backend.protobuf_varint_values(
                backend.protobuf_length_delimited_values(soprano_row, 2)[0],
            )
            self.assertEqual(soprano, {
                1: backend.part_acquisition_level(2021), 2: 1, 3: 3,
            })

            accepted, _, _, reason = backend.apply_lineup_part_updates(
                app.lineups, app.inventory_items, app.part_levels, 1,
                [{"toy_index": 1, "parts": {"bot": 2021}}],
            )
            self.assertTrue(accepted, reason)

            accepted, _, _, reason = backend.apply_lineup_part_updates(
                app.lineups, app.inventory_items, app.part_levels, 1,
                [{"toy_index": 1, "parts": {"mid": 2011}}],
            )
            self.assertTrue(accepted, reason)

    def test_zero_or_negative_soprano_fragment_counts_do_not_create_ownership(self):
        for count in (0, -1, -3):
            with self.subTest(count=count):
                app = SimpleNamespace(
                    inventory_items={}, part_fragments={2021: count},
                    part_levels={},
                )
                self.assertNotIn(2021, backend.owned_part_levels(
                    app.inventory_items, app.part_levels,
                ))
                sync = backend.make_player_data_sync(
                    100_000_989,
                    part_fragments=app.part_fragments,
                    part_levels=app.part_levels,
                )
                base_info = base_info_from_sync(sync)
                serialized = [
                    backend.protobuf_varint_values(row).get(1)
                    for field in (15, 16, 17)
                    for row in backend.protobuf_length_delimited_values(
                        base_info, field,
                    )
                ]
                self.assertNotIn(2021, serialized)

    def test_repeated_part_rewards_add_fragments_without_duplicate_part_rows(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_991)
            backend.credit_box_reward(app, 2021, 2)
            first_level = app.part_levels[2021]
            backend.save_profile_state(app)
            app = profile_app(path, player_id=100_000_991)
            backend.credit_box_reward(app, 2021, 3)
            self.assertEqual(app.part_fragments[2021], 5)
            self.assertEqual(app.part_levels[2021], first_level)
            backend.save_profile_state(app)
            restored = backend.load_profile_state(path, app.player_id)
            self.assertEqual(restored["part_fragments"][2021], 5)
            self.assertEqual(restored["part_levels"][2021], first_level)
            sync = backend.make_player_data_sync(
                app.player_id, part_fragments=restored["part_fragments"],
                part_levels=restored["part_levels"],
            )
            rows = [row for row in backend.protobuf_length_delimited_values(
                base_info_from_sync(sync), 17,
            ) if backend.protobuf_varint_values(row).get(1) == 2021]
            self.assertEqual(len(rows), 1)
            part = backend.protobuf_varint_values(
                backend.protobuf_length_delimited_values(rows[0], 2)[0],
            )
            self.assertEqual(part[1], first_level)
            self.assertEqual(part[2] + 1, 6)

    def test_existing_positive_fragment_profile_migrates_to_owned_without_losing_progress(self):
        with temporary_profile() as path:
            path.write_text(json.dumps({
                "player_id": 100_000_992,
                "part_fragments": {"2021": 4},
                "part_levels": {},
            }), encoding="utf-8")
            restored = backend.load_profile_state(path, 100_000_992)
            self.assertEqual(restored["part_fragments"][2021], 4)
            self.assertEqual(restored["part_levels"][2021],
                             backend.part_acquisition_level(2021))

    def test_owned_parts_and_first_fragment_counts_match_client_item_count(self):
        # The client reads each part's count as Piece + 1. Both part records
        # are now owned; an unowned component is absent until first reward.
        sync = backend.make_player_data_sync(
            100_000_990,
            part_levels={2004: 1},
            part_fragments={2004: 3, 2021: 3},
        )
        base_info = base_info_from_sync(sync)
        by_id = {}
        for field in (15, 16, 17):
            for row in backend.protobuf_length_delimited_values(base_info, field):
                item_id = backend.protobuf_varint_values(row)[1]
                by_id[item_id] = backend.protobuf_varint_values(
                    backend.protobuf_length_delimited_values(row, 2)[0],
                )
        self.assertEqual(by_id[2004], {1: 1, 2: 3, 3: 1})
        self.assertEqual(by_id[2021], {
            1: backend.part_acquisition_level(2021), 2: 3, 3: 3,
        })
        self.assertEqual(by_id[2004][2] + 1, 4)  # owned copy + 3 fragments
        self.assertEqual(by_id[2021][2] + 1, 4)  # owned copy + 3 fragments
        owned = backend.owned_part_levels({}, {
            2004: 1, 2021: backend.part_acquisition_level(2021),
        })
        self.assertEqual(owned[2004], 1)
        self.assertEqual(owned[2021], backend.part_acquisition_level(2021))

    def test_first_chest_fixes_only_shuriken_and_other_drops_remain_random(self):
        def open_rewards(draw):
            app = SimpleNamespace(
                inventory_items={}, part_fragments={}, part_levels={},
                championship_state={"race_score": 0},
                first_chest_shuriken_claimed=False,
            )
            random_rewards = backend.resolve_configured_box_rewards(
                app, 1228025907, draw=draw,
            )
            self.assertIsNotNone(random_rewards)
            guaranteed, newly_owned = backend.ensure_first_chest_shuriken(
                app, random_rewards,
            )
            self.assertTrue(newly_owned)
            self.assertEqual(app.part_levels[2011], 3)
            self.assertEqual(
                [r for r in guaranteed if r["item_id"] == 2011],
                [{"item_id": 2011, "num": 1, "item_type": 1}],
            )
            self.assertIn({"item_id": backend.GOLD_ITEM_ID,
                           "num": 82, "item_type": 3}, guaranteed)
            return guaranteed

        first = open_rewards(lambda total: 0)
        second = open_rewards(lambda total: total - 1)
        first_random = [row["item_id"] for row in first if row["item_id"] != 2011]
        second_random = [row["item_id"] for row in second if row["item_id"] != 2011]
        self.assertNotEqual(first_random, second_random)
        # The guarantee is the only fixed part identity; the configured pools
        # still select the other parts, and the I203 table also includes Gold.
        self.assertEqual(first_random, [2001, 2001, 2009, backend.GOLD_ITEM_ID])
        self.assertEqual(second_random, [4020, 4020, 2021, backend.GOLD_ITEM_ID])

    def test_recovered_chest_part_fragments_can_be_upgraded_into_usable_parts(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_984)
            part_id = 2009
            upgrade = backend.ITEM_UPGRADE_ROWS[
                backend.PART_UPGRADE_KEYS[part_id]
            ]
            init_level = int(upgrade["initLevel"])
            level_row = upgrade[f"Level{init_level}"]
            app.part_fragments[part_id] = int(level_row["piece"])
            app.inventory_items[backend.GOLD_ITEM_ID] = 1_000_000
            self.assertNotIn(part_id, backend.owned_part_levels({}, app.part_levels))

            result = backend.apply_part_level_upgrade(app, part_id)
            self.assertTrue(result["changed"])
            self.assertEqual(result["item_opr_ret"], backend.ITEM_OPR_RET_UPGRADE_OK)
            self.assertGreaterEqual(app.part_levels[part_id], 1)
            self.assertIn(part_id, backend.owned_part_levels({}, app.part_levels))
            accepted, changed_lineups, _, reason = backend.apply_lineup_part_updates(
                app.lineups, app.inventory_items, app.part_levels, 1,
                [{"toy_index": 1, "parts": {"bot": part_id}}],
            )
            self.assertTrue(accepted, reason)
            self.assertEqual(changed_lineups["1"]["1"]["bot"], part_id)

            backend.save_profile_state(app)
            reloaded = backend.load_profile_state(path, app.player_id)
            self.assertEqual(reloaded["part_levels"][part_id], app.part_levels[part_id])
            sync = backend.make_player_data_sync(
                app.player_id, part_fragments=reloaded["part_fragments"],
                part_levels=reloaded["part_levels"],
            )
            part_row = next(
                row for row in backend.protobuf_length_delimited_values(
                    base_info_from_sync(sync), 17,
                ) if backend.protobuf_varint_values(row).get(1) == part_id
            )
            self.assertGreaterEqual(
                backend.protobuf_varint_values(
                    backend.protobuf_length_delimited_values(part_row, 2)[0],
                )[1], 1,
            )

    def test_recovered_upgrade_catalog_covers_shuriken_and_box_reward_parts(self):
        self.assertEqual(backend.PART_ITEM_LOCATIONS.get(2011), 1)
        key = backend.PART_UPGRADE_KEYS[2011]
        self.assertEqual(key, 5011)
        self.assertEqual(backend.ITEM_UPGRADE_ROWS[key]["initLevel"], 3)
        self.assertEqual(backend.PART_ITEM_LOCATIONS.get(2009), None)
        self.assertEqual(backend.UPGRADEABLE_PART_LOCATIONS.get(2009), 2)

    def test_first_successful_timed_box_open_grants_usable_shuriken_once(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_985)
            app.championship_state["race_score"] = 0
            put_box(app, 0, 1228025907, can_open=999)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertTrue(result["changed"])
            shuriken = [row for row in result["rewards"]
                        if row["item_id"] == backend.FIRST_CHEST_SHURIKEN_PART_ID]
            self.assertEqual(shuriken, [{"item_id": 2011, "num": 1, "item_type": 1}])
            self.assertEqual(app.part_levels[2011], 3)
            self.assertNotIn(2011, app.part_fragments)
            self.assertTrue(app.first_chest_shuriken_claimed)
            accepted, changed_lineups, _, reason = backend.apply_lineup_part_updates(
                app.lineups, app.inventory_items, app.part_levels, 1,
                [{"toy_index": 1, "parts": {"mid": 2011}}],
            )
            self.assertTrue(accepted, reason)
            self.assertEqual(changed_lineups["1"]["1"]["mid"], 2011)

            backend.save_profile_state(app)
            loaded = backend.load_profile_state(path, app.player_id)
            self.assertTrue(loaded["first_chest_shuriken_claimed"])
            self.assertEqual(loaded["part_levels"][2011], 3)
            sync = backend.make_player_data_sync(
                app.player_id, part_levels=loaded["part_levels"],
                part_fragments=loaded["part_fragments"],
            )
            row = next(
                row for row in backend.protobuf_length_delimited_values(
                    base_info_from_sync(sync), 16,
                ) if backend.protobuf_varint_values(row).get(1) == 2011
            )
            part = backend.protobuf_varint_values(
                backend.protobuf_length_delimited_values(row, 2)[0],
            )
            self.assertEqual(part[1], 3)  # Positive Level means client-owned/selectable.
            self.assertEqual(part[3], 2)  # Middle-slot PartAs.

            retry = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=1001, draw=lambda _: 0,
            )
            self.assertFalse(retry["changed"])
            self.assertEqual(app.part_levels[2011], 3)

    def test_first_successful_immediate_box_open_grants_same_tutorial_part(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_986)
            app.inventory_items[1228025906] = 1
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025906, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(app.part_levels[2011], 3)
            self.assertTrue(app.first_chest_shuriken_claimed)
            self.assertEqual(sum(row["num"] for row in result["rewards"]
                                 if row["item_id"] == 2011), 1)
            loaded = backend.load_profile_state(path, app.player_id)
            self.assertEqual(loaded["inventory_items"].get(1228025906, 0), 0)
            self.assertEqual(loaded["part_levels"][2011], 3)

    def test_failed_open_does_not_consume_first_shuriken_guarantee(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_987)
            put_box(app, 0, 1228025907, can_open=2000)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertFalse(result["changed"])
            self.assertFalse(app.first_chest_shuriken_claimed)
            self.assertNotIn(2011, app.part_levels)
            self.assertIn("0", app.save_boxes)

    def test_first_chest_guarantee_is_persistent_and_player_specific(self):
        with temporary_profile() as first_path, temporary_profile() as second_path:
            first = profile_app(first_path, player_id=100_000_988)
            put_box(first, 0, 1228025906)
            first.inventory_items[1228025906] = 1
            result = backend.apply_box_item_operation(
                first, backend.ITEM_OPR_USE_ITEM, 1228025906, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertTrue(result["changed"])
            first_after_restart = backend.load_profile_state(first_path, first.player_id)
            self.assertTrue(first_after_restart["first_chest_shuriken_claimed"])

            second = profile_app(second_path, player_id=100_000_989)
            self.assertFalse(second.first_chest_shuriken_claimed)
            self.assertNotIn(2011, second.part_levels)

    def test_owned_shuriken_does_not_receive_a_second_guarantee(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_990)
            app.part_levels[2011] = 5
            put_box(app, 0, 1228025906)
            app.inventory_items[1228025906] = 1
            expected = backend.resolve_configured_box_rewards(
                app, 1228025906, draw=lambda _: 0,
            )
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025906, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertEqual(result["rewards"], expected)
            self.assertTrue(app.first_chest_shuriken_claimed)
            self.assertEqual(app.part_levels[2011], 5)

    def test_legacy_inventory_owned_shuriken_does_not_receive_second_part(self):
        app = SimpleNamespace(
            inventory_items={2011: 1}, part_fragments={}, part_levels={},
            first_chest_shuriken_claimed=False,
        )
        random_fragment = [{"item_id": 2011, "num": 2, "item_type": 1}]
        rewards, newly_owned = backend.ensure_first_chest_shuriken(
            app, random_fragment,
        )
        self.assertFalse(newly_owned)
        self.assertEqual(rewards, random_fragment)
        self.assertTrue(app.first_chest_shuriken_claimed)
        self.assertEqual(app.inventory_items[2011], 1)
        self.assertEqual(app.part_levels, {})

    def test_subsequent_chests_keep_configured_reward_selection(self):
        with temporary_profile() as path:
            app = profile_app(path, player_id=100_000_991)
            app.first_chest_shuriken_claimed = True
            app.championship_state["race_score"] = 400
            put_box(app, 0, 1228025907, can_open=999)
            expected = backend.resolve_configured_box_rewards(
                app, 1228025907, draw=lambda _: 0,
            )
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 1,
                now=1000, draw=lambda _: 0,
            )
            self.assertEqual(result["rewards"], expected)
            self.assertNotIn(2011, app.part_levels)
            self.assertTrue(app.first_chest_shuriken_claimed)

    def test_start_uses_each_box_configured_delay_and_persists(self):
        with temporary_profile() as path:
            app = profile_app(path)
            put_box(app, 0, 1228025907)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_BOX_COUNTDOWN, 1228025907, 0, now=1000,
            )
            self.assertEqual(result["item_opr_ret"], backend.ITEM_OPR_RET_SUCCEED)
            self.assertEqual(app.save_boxes["0"]["can_open_time"], 8200)
            backend.save_profile_state(app)
            reloaded = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(reloaded["save_boxes"]["0"]["can_open_time"], 8200)

            put_box(app, 1, 1228025908)
            second = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_BOX_COUNTDOWN, 1228025908, 1, now=1000,
            )
            self.assertEqual(second["item_opr_ret"], backend.ITEM_OPR_RET_COUNTING_DOWN)
            # Once the first timer has finished, the second box receives its
            # own 4-hour Item.DelayOpenTime rather than reusing I203's delay.
            second = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_BOX_COUNTDOWN, 1228025908, 1, now=8200,
            )
            self.assertEqual(second["item_opr_ret"], backend.ITEM_OPR_RET_SUCCEED)
            self.assertEqual(app.save_boxes["1"]["can_open_time"], 22600)

    def test_ready_queue_promotes_to_its_own_configured_timer(self):
        with temporary_profile() as path:
            app = profile_app(path)
            put_box(app, 0, 1228025907, can_open=8200)
            put_box(app, 1, 1228025908)
            queued = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_BOX_READY_OPEN, 1228025908, 1, now=1000,
            )
            self.assertEqual(queued["item_opr_ret"], backend.ITEM_OPR_RET_BOX_READY_OK)
            self.assertEqual(app.save_boxes["1"]["ready_countdown_time"], 8200)
            backend.save_profile_state(app)
            queued_reload = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(queued_reload["save_boxes"]["1"]["ready_countdown_time"], 8200)
            self.assertFalse(backend.refresh_save_box_timers(app, now=8199))
            self.assertTrue(backend.refresh_save_box_timers(app, now=8200))
            self.assertEqual(app.save_boxes["1"]["can_open_time"], 22600)
            backend.save_profile_state(app)
            saved = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(saved["save_boxes"]["1"]["can_open_time"], 22600)

    def test_paid_speedup_uses_remaining_time_and_grants_once(self):
        with temporary_profile() as path:
            app = profile_app(path)
            app.inventory_items[backend.DIAMOND_ITEM_ID] = 100
            app.championship_state["race_score"] = 400
            put_box(app, 0, 1228025907, can_open=2000)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 0,
                now=1100, draw=lambda total: 0,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(result["diamond_cost"], 1)
            self.assertEqual(app.inventory_items[backend.DIAMOND_ITEM_ID], 99)
            self.assertNotIn("0", app.save_boxes)
            before = dict(app.inventory_items)
            duplicate = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025907, 0, 0,
                now=1101, draw=lambda total: 0,
            )
            self.assertFalse(duplicate["changed"])
            self.assertEqual(app.inventory_items, before)
            reloaded = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(reloaded["inventory_items"], before)

    def test_ready_claim_delivers_client_receipt_and_persists_rewards(self):
        with temporary_profile() as path:
            app = profile_app(path)
            app.championship_state["race_score"] = 400
            put_box(app, 0, 1228025908, can_open=999)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025908, 0, 1,
                now=1000, draw=lambda total: 0,
            )
            self.assertTrue(result["changed"])
            self.assertTrue(result["rewards"])
            frame = backend.make_box_open_get_items(1228025908, result["rewards"])
            marker = frame[1] & 0x7F
            frame_offset = 2 if marker < 126 else (4 if marker == 126 else 10)
            self.assertEqual(frame[frame_offset + 4:frame_offset + 6],
                             (143).to_bytes(2, "big"))
            self.assertNotIn("0", app.save_boxes)
            stored = backend.load_profile_state(app.state_file, app.player_id)
            for reward in result["rewards"]:
                reward_id = reward["item_id"]
                if reward["item_type"] in (0, 1, 2):
                    if reward_id == backend.FIRST_CHEST_SHURIKEN_PART_ID:
                        self.assertGreaterEqual(stored["part_levels"].get(reward_id, 0), 1)
                    else:
                        self.assertGreaterEqual(stored["part_fragments"].get(reward_id, 0),
                                                reward["num"])
                else:
                    self.assertGreaterEqual(stored["inventory_items"].get(reward_id, 0),
                                            reward["num"])
            retry = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_USE_ITEM, 1228025908, 0, 1,
                now=1001, draw=lambda total: 0,
            )
            self.assertFalse(retry["changed"])
            self.assertEqual(backend.load_profile_state(
                app.state_file, app.player_id,
            )["inventory_items"], stored["inventory_items"])

    def test_ad_speedup_is_not_fabricated_when_reduction_rule_is_unavailable(self):
        with temporary_profile() as path:
            app = profile_app(path)
            put_box(app, 0, 1228025907, can_open=8200)
            before = backend.normalize_save_boxes(app.save_boxes)
            result = backend.apply_box_item_operation(
                app, backend.ITEM_OPR_REDUCE_BOX_COUNTDOWN,
                1228025907, 0, now=1000,
            )
            self.assertFalse(result["changed"])
            self.assertEqual(app.save_boxes, before)

    def test_invalid_slot_item_and_operation_do_not_mutate_inventory_or_boxes(self):
        with temporary_profile() as path:
            app = profile_app(path)
            put_box(app, 0, 1228025907)
            before = backend.normalize_save_boxes(app.save_boxes)
            inventory_before = dict(app.inventory_items)
            cases = [
                (backend.ITEM_OPR_BOX_COUNTDOWN, 1228025907, 9, 0),
                (backend.ITEM_OPR_BOX_COUNTDOWN, 1228025908, 0, 0),
                (999, 1228025907, 0, 0),
                (backend.ITEM_OPR_USE_ITEM, 1228025907, 9, 1),
            ]
            for operation, item_id, slot, use_case in cases:
                result = backend.apply_box_item_operation(
                    app, operation, item_id, slot, use_case,
                    now=1000, draw=lambda _: 0,
                )
                self.assertFalse(result["changed"])
            self.assertEqual(app.save_boxes, before)
            self.assertEqual(app.inventory_items, inventory_before)


if __name__ == "__main__":
    unittest.main()
