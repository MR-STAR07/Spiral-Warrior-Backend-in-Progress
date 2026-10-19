"""Regression coverage for the locally simulated opening tutorial flow."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from contextlib import contextmanager
import unittest
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402


@contextmanager
def temporary_profile():
    path = BACKEND_DIR / f".tutorial-test-{uuid.uuid4().hex}.json"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def profile_app(state_file: Path, player_id: int = 100_000_123):
    """Create an isolated app state object matching the server startup path."""
    state = backend.load_profile_state(state_file, player_id)
    app = SimpleNamespace(
        state_file=state_file,
        player_id=player_id,
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


class TutorialProgressionTests(unittest.TestCase):
    def test_first_local_tutorial_win_does_not_mark_full_guide_complete(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)

            changed = backend.persist_local_tutorial_battle(app)

            saved = backend.load_profile_state(state_file, app.player_id)
            self.assertTrue(changed)
            self.assertEqual(saved["guide_step"] & backend.GUIDE_MARK_OVER_MASK, 0)
            self.assertTrue(saved["tutorial_battle_complete"])
            self.assertEqual(saved["task_exchange_claims"], [])
            self.assertEqual(saved["inventory_items"].get(1228156978, 0), 0)
            self.assertEqual(saved["chapter_battle_results"], {})
            self.assertNotIn(2011, backend.owned_part_levels(
                saved["inventory_items"], saved["part_levels"],
            ))

            # E_RACE_GUIDE_WIN is only the client-simulated Quick Race result;
            # it does not manufacture a Championship win or reward box.
            sync = backend.make_app_player_data_sync(app)
            frame_size = 2 if (sync[1] & 0x7F) < 126 else (
                4 if (sync[1] & 0x7F) == 126 else 10
            )
            player_data = sync[frame_size + 6:]
            histories = backend.protobuf_length_delimited_values(player_data, 3)
            champion = next(
                backend.protobuf_length_delimited_values(row, 2)[0]
                for row in histories
                if backend.protobuf_varint_values(row).get(1)
                == backend.CHAMPION_RACE_TYPE
            )
            champion_fields = backend.protobuf_varint_values(champion)
            self.assertEqual(champion_fields.get(1, 0), 0)  # no championship wins
            self.assertEqual(champion_fields.get(6, 0), 1)  # client rank-key floor only
            self.assertEqual(app.championship_state["race_score"], 0)  # no earned score

    def test_relogin_sync_skips_local_battle_without_marking_guide_over(self):
        with temporary_profile() as state_file:
            first_session = profile_app(state_file)
            self.assertTrue(backend.persist_local_tutorial_battle(first_session))

            next_session = profile_app(state_file)
            sync = backend.make_app_player_data_sync(next_session)
            ws_header_size = 2 if (sync[1] & 0x7F) < 126 else (
                4 if (sync[1] & 0x7F) == 126 else 10
            )
            player_data = sync[ws_header_size + 6:]
            self.assertEqual(backend.protobuf_varint_values(player_data).get(9), 0)
            histories = backend.protobuf_length_delimited_values(player_data, 3)
            quick_history = next(
                backend.protobuf_length_delimited_values(row, 2)[0]
                for row in histories
                if backend.protobuf_varint_values(row).get(1) == 1
            )
            self.assertEqual(backend.protobuf_varint_values(quick_history).get(1), 1)
            self.assertEqual(backend.protobuf_varint_values(quick_history).get(2, 0), 0)
            self.assertFalse(backend.persist_local_tutorial_battle(next_session))
            saved = backend.load_profile_state(state_file, next_session.player_id)
            self.assertEqual(saved["guide_step"] & backend.GUIDE_MARK_OVER_MASK, 0)
            self.assertTrue(saved["tutorial_battle_complete"])

    def test_later_client_guide_report_persists_and_survives_relogin(self):
        with temporary_profile() as state_file:
            app = profile_app(state_file)
            self.assertFalse(backend.persist_client_guide_step(app, 0))
            self.assertTrue(backend.persist_client_guide_step(
                app, backend.GUIDE_MARK_OVER_MASK,
            ))
            self.assertFalse(backend.persist_client_guide_step(
                app, backend.GUIDE_MARK_OVER_MASK,
            ))
            restored = backend.load_profile_state(state_file, app.player_id)
            self.assertEqual(restored["guide_step"] & backend.GUIDE_MARK_OVER_MASK,
                             backend.GUIDE_MARK_OVER_MASK)
            self.assertEqual(profile_app(state_file).guide_step, backend.GUIDE_MARK_OVER_MASK)

    def test_legacy_premature_over_recovers_to_post_battle_without_data_loss(self):
        with temporary_profile() as state_file:
            player_id = 100_000_123
            original = {
                "schema_version": 8, "player_id": player_id,
                "guide_step": backend.GUIDE_MARK_OVER_MASK | 1,
                "level": 1, "exp": 7, "power": 0,
                "part_levels": {"2001": 1},
                "inventory_items": {"1227894833": 91, "2009": 5},
                "lineups": backend.default_lineups(),
                "task_exchange_claims": [],
                "level_task_state": {str(backend.FIRST_LEVEL_UPGRADE_TASK_ID):
                                      {"process": 1, "status": 2}},
            }
            state_file.write_text(json.dumps(original), encoding="utf-8")
            app = profile_app(state_file, player_id)
            backend.refresh_shared_profile_state(app)
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], backend.PROFILE_SCHEMA_VERSION)
            self.assertEqual(migrated["guide_step"], 1)
            self.assertTrue(migrated["tutorial_battle_complete"])
            self.assertEqual(migrated["inventory_items"]["1227894833"], 91)
            self.assertEqual(migrated["inventory_items"]["2009"], 5)
            self.assertEqual(migrated["inventory_items"]["1227894833"],
                             max(91, backend.TUTORIAL_UPGRADE_GOLD))
            self.assertEqual(migrated["part_fragments"]["2001"], 2)
            self.assertEqual(migrated["level"], 1)
            self.assertEqual(migrated["exp"], 7)
            self.assertEqual(migrated["power"], 0)
            self.assertEqual(migrated["task_exchange_claims"], [])
            self.assertEqual(migrated["level_task_state"][str(backend.FIRST_LEVEL_UPGRADE_TASK_ID)],
                             {"process": 0, "status": backend.TASK_STATUS_DOING})
            self.assertEqual(migrated["inventory_items"].get(1228156978, 0), 0)

    def test_legacy_claimed_lv01_profile_stays_complete_without_reward_regrant(self):
        with temporary_profile() as state_file:
            player_id = 100_000_123
            original = {
                "schema_version": 8, "player_id": player_id,
                "guide_step": backend.GUIDE_MARK_OVER_MASK,
                "level": 2, "power": 12,
                "inventory_items": {"1228156978": 1, "2009": 5},
                "task_exchange_claims": [backend.LEVEL_FIRST_REWARD_ID],
                "lineups": backend.default_lineups(),
            }
            state_file.write_text(json.dumps(original), encoding="utf-8")
            app = profile_app(state_file, player_id)
            backend.refresh_shared_profile_state(app)
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(migrated["guide_step"] & backend.GUIDE_MARK_OVER_MASK,
                             backend.GUIDE_MARK_OVER_MASK)
            self.assertTrue(migrated["tutorial_battle_complete"])
            self.assertEqual(migrated["power"], 12)
            self.assertEqual(migrated["inventory_items"]["1228156978"], 1)
            self.assertEqual(migrated["task_exchange_claims"], [backend.LEVEL_FIRST_REWARD_ID])

    def test_legacy_level10_profile_is_not_downgraded_or_sent_to_impossible_upgrade_step(self):
        with temporary_profile() as state_file:
            player_id = 100_000_123
            original = {
                "schema_version": 8, "player_id": player_id,
                "guide_step": backend.GUIDE_MARK_OVER_MASK,
                "level": 1, "power": 0,
                "inventory_items": {"2009": 5},
                "part_levels": {}, "task_exchange_claims": [],
                "lineups": backend.default_lineups(),
            }
            state_file.write_text(json.dumps(original), encoding="utf-8")
            app = profile_app(state_file, player_id)
            backend.refresh_shared_profile_state(app)
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(migrated["part_levels"]["2001"], 10)
            self.assertEqual(migrated["guide_step"] & backend.GUIDE_MARK_OVER_MASK,
                             backend.GUIDE_MARK_OVER_MASK)
            self.assertTrue(migrated["tutorial_battle_complete"])
            self.assertEqual(migrated["inventory_items"]["2009"], 5)
            self.assertEqual(migrated["task_exchange_claims"], [])

    def test_auth_server_sync_keeps_lan_http_and_websocket_urls_consistent(self):
        sync = backend.make_public_svr_sync(
            server_time=1,
            session_token="test-session",
            public_http_url="http://192.0.2.10:18765",
            public_ws_url="ws://192.0.2.10:18766",
        )
        ws_header_size = 2 if (sync[1] & 0x7F) < 126 else (
            4 if (sync[1] & 0x7F) == 126 else 10
        )
        message = sync[ws_header_size:]
        public_server = backend.protobuf_length_delimited_values(message[6:], 12)[0]
        addresses = {
            value.decode("utf-8")
            for field in (1, 4)
            for value in backend.protobuf_length_delimited_values(public_server, field)
        }
        self.assertIn("http://192.0.2.10:18765", addresses)
        self.assertIn("ws://192.0.2.10:18766/?session=test-session", addresses)

        # The client defaults missing counters to zero. The community server
        # advertises a finite local policy rather than an int32-max placeholder.
        gold_limit_entries = backend.protobuf_length_delimited_values(message[6:], 7)
        self.assertEqual(len(gold_limit_entries), 1)
        gold_limit = backend.protobuf_varint_values(gold_limit_entries[0])
        self.assertEqual(gold_limit[1], backend.GOLD_ITEM_ID)
        self.assertEqual(gold_limit[2], backend.COMMUNITY_GOLD_DAILY_LIMIT)
        self.assertEqual(gold_limit[2], 10_000)

    def test_championship_query_stays_separate_from_robot_match_entry(self):
        self.assertEqual(backend.response_bout_opr_for_request(40, 5), 40)
        self.assertNotEqual(backend.response_bout_opr_for_request(40, 5), 44)
        self.assertEqual(backend.response_bout_opr_for_request(44, 5), 44)

    def test_tutorial_lv01_fallback_requires_completion_and_preserves_claim_guards(self):
        arguments = {
            "exchange_id": backend.LEVEL_FIRST_REWARD_ID,
            "sub_ids": [],
            "already_claimed": False,
            "requirements_met": False,
            "guide_step": 0,
            "has_known_reward": True,
        }
        self.assertFalse(backend.can_claim_level_first_reward(**arguments))
        self.assertTrue(backend.can_claim_level_first_reward(
            **{**arguments, "guide_step": backend.GUIDE_MARK_OVER_MASK},
        ))
        self.assertTrue(backend.can_claim_level_first_reward(
            **{**arguments, "requirements_met": True},
        ))
        for changed in (
            {"already_claimed": True},
            {"sub_ids": [1]},
            {"has_known_reward": False},
            {"exchange_id": backend.LEVEL_FIRST_REWARD_ID + 1},
        ):
            self.assertFalse(backend.can_claim_level_first_reward(
                **{**arguments, "guide_step": backend.GUIDE_MARK_OVER_MASK, **changed},
            ))

        with temporary_profile() as state_file:
            app = profile_app(state_file)
            backend.persist_local_tutorial_battle(app)
            restored = backend.load_profile_state(state_file, app.player_id)

            # Completion records eligibility; it does not grant anything until
            # the client makes the one allowed LV01 exchange request.
            self.assertEqual(restored["task_exchange_claims"], [])
            self.assertEqual(restored["level"], 1)
            self.assertEqual(restored["inventory_items"].get(1228156978, 0), 0)
            self.assertEqual(restored["guide_step"] & backend.GUIDE_MARK_OVER_MASK, 0)


if __name__ == "__main__":
    unittest.main()

