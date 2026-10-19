"""Regression checks for starter part ownership in PlayerData sync."""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
import unittest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402


@contextmanager
def temporary_profile():
    path = BACKEND_DIR / f".starter-test-{uuid.uuid4().hex}.json"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def player_data_proto(sync: bytes) -> bytes:
    frame_type = sync[1] & 0x7F
    ws_header_size = 2 if frame_type < 126 else (4 if frame_type == 126 else 10)
    message = sync[ws_header_size:]
    return message[6:]


def owned_parts_by_slot(sync: bytes) -> dict[int, tuple[int, int]]:
    player_data = player_data_proto(sync)
    base_info_rows = backend.protobuf_length_delimited_values(player_data, 1)
    if len(base_info_rows) != 1:
        raise AssertionError(f"expected one BaseInfo row, found {len(base_info_rows)}")
    base_info = base_info_rows[0]
    result = {}
    for slot_field in (15, 16, 17):  # TopPartList, MidPartList, BotPartList.
        for row in backend.protobuf_length_delimited_values(base_info, slot_field):
            item_id = backend.protobuf_varint_values(row).get(1)
            part_rows = backend.protobuf_length_delimited_values(row, 2)
            if item_id is None or len(part_rows) != 1:
                raise AssertionError("malformed owned-part row")
            if int(item_id) in result:
                raise AssertionError(f"duplicate owned-part row for {item_id}")
            part = backend.protobuf_varint_values(part_rows[0])
            result[int(item_id)] = (slot_field, int(part.get(3, 0)))
    return result


def lineup_parts_by_team(sync: bytes) -> dict[int, dict[int, dict[str, int]]]:
    player_data = player_data_proto(sync)
    base_info = backend.protobuf_length_delimited_values(player_data, 1)[0]
    result = {}
    for team_row in backend.protobuf_length_delimited_values(base_info, 23):
        team_fields = backend.protobuf_varint_values(team_row)
        if 1 not in team_fields:
            continue  # BaseInfo retains the client-required empty index-0 row.
        team_index = team_fields[1]
        lineup_rows = backend.protobuf_length_delimited_values(team_row, 2)
        if len(lineup_rows) != 1:
            raise AssertionError("malformed lineup container")
        lineup = lineup_rows[0]
        toys = {}
        for toy_row in backend.protobuf_length_delimited_values(lineup, 1):
            toy_index = backend.protobuf_varint_values(toy_row)[1]
            toy_values = backend.protobuf_length_delimited_values(toy_row, 2)
            if len(toy_values) != 1:
                raise AssertionError("malformed toy-top container")
            values = backend.protobuf_varint_values(toy_values[0])
            toys[int(toy_index)] = {
                "top": int(values[1]),
                "mid": int(values[2]),
                "bot": int(values[3]),
            }
        result[int(team_index)] = toys
    return result


class StarterBladeStateTests(unittest.TestCase):
    def test_initial_player_sync_marks_each_seeded_starter_part_owned_once(self):
        sync = backend.make_player_data_sync(100_000_123, inventory_items={})

        parts = owned_parts_by_slot(sync)
        self.assertEqual(parts, {
            2001: (15, 1),  # top
            2002: (16, 2),  # middle
            2003: (17, 3),  # bottom
            2004: (15, 1), 2005: (16, 2), 2006: (17, 3),
            2028: (15, 1), 2029: (16, 2), 2030: (17, 3),
        })
        self.assertEqual(
            set(backend.owned_part_levels()),
            {2001, 2002, 2003, 2004, 2005, 2006, 2028, 2029, 2030},
        )

    def test_later_guide_part_is_not_owned_until_profile_records_acquisition(self):
        initial_sync = backend.make_player_data_sync(100_000_123, inventory_items={})
        self.assertNotIn(2011, owned_parts_by_slot(initial_sync))
        self.assertNotIn(2011, backend.owned_part_levels())

        acquired_sync = backend.make_player_data_sync(
            100_000_123, inventory_items={2011: 1},
        )
        self.assertEqual(owned_parts_by_slot(acquired_sync)[2011], (16, 2))
        self.assertEqual(backend.owned_part_levels({2011: 1})[2011], 1)

    def test_default_lineup_has_three_distinct_client_configured_assemblies(self):
        sync = backend.make_player_data_sync(100_000_123, inventory_items={})
        expected_toys = {
            1: {"top": 2001, "mid": 2002, "bot": 2003},
            2: {"top": 2004, "mid": 2005, "bot": 2006},
            3: {"top": 2028, "mid": 2029, "bot": 2030},
        }

        self.assertEqual(backend.default_lineups()["1"], {
            str(index): parts for index, parts in expected_toys.items()
        })
        self.assertEqual(lineup_parts_by_team(sync), {
            team: expected_toys for team in (1, 2, 3)
        })

    def test_other_unowned_parts_are_not_added_to_owned_lists(self):
        sync = backend.make_player_data_sync(100_000_123, inventory_items={})

        parts = owned_parts_by_slot(sync)
        self.assertNotIn(2034, parts)
        self.assertNotIn(2035, parts)
        self.assertNotIn(2036, parts)
        self.assertNotIn(2070, parts)

    def test_repeated_player_sync_does_not_duplicate_starter_ownership(self):
        inventory = {"2009": 5, "2027": 5, "1194340405": 5}
        first = backend.make_player_data_sync(100_000_123, inventory_items=inventory)
        second = backend.make_player_data_sync(100_000_123, inventory_items=inventory)

        first_parts = owned_parts_by_slot(first)
        second_parts = owned_parts_by_slot(second)
        self.assertEqual(first_parts, second_parts)
        self.assertEqual(len(first_parts), 9)

    def test_existing_profile_inventory_survives_legacy_lineup_correction(self):
        with temporary_profile() as state_file:
            player_id = 100_000_123
            original = {
                "schema_version": 8,
                "player_id": player_id,
                "inventory_items": {"2009": 5, "2027": 5, "1194340405": 5},
                "lineups": {
                    str(team): {
                        str(toy): {"top": 2001, "mid": 2002, "bot": 2003}
                        for toy in range(1, 4)
                    }
                    for team in range(1, 4)
                },
            }
            state_file.write_text(json.dumps(original), encoding="utf-8")
            before = state_file.read_bytes()
            loaded = backend.load_profile_state(state_file, player_id)
            loaded_again = backend.load_profile_state(state_file, player_id)
            sync = backend.make_player_data_sync(
                player_id,
                inventory_items=loaded["inventory_items"],
                lineups=loaded["lineups"],
                part_levels=loaded["part_levels"],
            )

            self.assertEqual(state_file.read_bytes(), before)
            self.assertEqual(loaded["inventory_items"], {
                2009: 5, 2027: 5, 1194340405: 5,
            })
            self.assertEqual([loaded["part_levels"][item_id] for item_id in (
                2001, 2002, 2003, 2004, 2005, 2006, 2028, 2029, 2030,
            )], [10] * 9)
            self.assertEqual(loaded["lineups"], backend.default_lineups())
            self.assertEqual(loaded_again["lineups"], loaded["lineups"])
            self.assertEqual(state_file.read_bytes(), before)
            self.assertEqual(set(owned_parts_by_slot(sync)), {
                2001, 2002, 2003, 2004, 2005, 2006, 2028, 2029, 2030,
                # Legacy generic inventory was already migrated into valid
                # part-fragment counts; those positive counts now unlock the
                # corresponding part without discarding the counts.
                2009, 2027,
            })
            self.assertEqual(loaded["part_fragments"][2009], 5)
            self.assertEqual(loaded["part_fragments"][2027], 5)
            self.assertGreaterEqual(loaded["part_levels"][2009], 1)
            self.assertGreaterEqual(loaded["part_levels"][2027], 1)

    def test_custom_lineup_parts_are_preserved_during_default_correction(self):
        legacy = backend.normalize_lineups(None)
        for team in legacy.values():
            for toy in team.values():
                toy.update(top=2001, mid=2002, bot=2003)
        legacy["1"]["1"]["top"] = 2034

        corrected = backend.normalize_lineups(legacy)

        self.assertEqual(corrected["1"]["1"], {
            "top": 2034, "mid": 2002, "bot": 2003,
        })
        self.assertEqual(corrected["1"]["2"], {
            "top": 2001, "mid": 2002, "bot": 2003,
        })

    def test_upgrade_ownership_source_includes_starters_but_not_other_parts(self):
        owned = backend.owned_part_levels(inventory_items={}, part_levels={})

        starter_ids = (2001, 2002, 2003, 2004, 2005, 2006, 2028, 2029, 2030)
        self.assertEqual([owned[item_id] for item_id in starter_ids], [1] * 9)
        self.assertNotIn(2034, owned)
        self.assertNotIn(2035, owned)
        self.assertNotIn(2036, owned)


if __name__ == "__main__":
    unittest.main()
