"""Coverage for fresh local-account starter equipment and currencies."""

from __future__ import annotations

import json
import sys
import shutil
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from account_store import AccountStore  # noqa: E402
import ws_probe_server as backend  # noqa: E402


@contextmanager
def temporary_root(prefix: str):
    root = BACKEND_DIR / f".{prefix}-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def app_for_profile(path: Path, player_id: int):
    app = SimpleNamespace(
        state_file=path,
        player_id=player_id,
        roguelike_lock=threading.RLock(),
    )
    backend.refresh_shared_profile_state(app)
    return app


def player_data_proto(sync: bytes) -> bytes:
    frame_type = sync[1] & 0x7F
    header_size = 2 if frame_type < 126 else (4 if frame_type == 126 else 10)
    return sync[header_size + 6:]


def wallet_and_part_levels(sync: bytes) -> tuple[dict[int, int], dict[int, int]]:
    player_data = player_data_proto(sync)
    base_info = backend.protobuf_length_delimited_values(player_data, 1)[0]
    wallet = backend.protobuf_varint_values(base_info)
    levels = {}
    for slot_field in (15, 16, 17):
        for row in backend.protobuf_length_delimited_values(base_info, slot_field):
            item_id = backend.protobuf_varint_values(row)[1]
            part = backend.protobuf_length_delimited_values(row, 2)[0]
            levels[int(item_id)] = backend.protobuf_varint_values(part).get(1, 0)
    return {4: wallet.get(4, 0), 5: wallet.get(5, 0)}, levels


class NewAccountInitializationTests(unittest.TestCase):
    def test_registration_creates_level_one_starters_and_fallback_wallet_once(self):
        with temporary_root("new-account-test") as root:
            db = root / "accounts.sqlite3"
            store = AccountStore(db)
            _token, account = store.register("fresh_tester", "test-password-123")
            profile_path = store.profile_path(account["id"])

            raw = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], backend.PROFILE_SCHEMA_VERSION)
            self.assertEqual(raw["inventory_items"][str(backend.GOLD_ITEM_ID)], 1500)
            self.assertEqual(raw["inventory_items"][str(backend.DIAMOND_ITEM_ID)], 100)
            self.assertEqual(
                {int(part_id): level for part_id, level in raw["part_levels"].items()},
                {part_id: 1 for part_id, _ in backend.SYNTHETIC_STARTER_PARTS},
            )

            # The normal profile-load path adds only the one-time tutorial
            # upgrade materials; the cash fallback remains exactly 1,500 Gold.
            loaded = backend.load_profile_state(profile_path, account["player_id"])
            self.assertEqual(loaded["inventory_items"][backend.GOLD_ITEM_ID], 1500)
            self.assertEqual(loaded["inventory_items"][backend.DIAMOND_ITEM_ID], 100)
            self.assertEqual(loaded["part_fragments"][backend.TUTORIAL_UPGRADE_PART_ID], 2)
            self.assertTrue(loaded["tutorial_upgrade_resources_granted"])
            self.assertEqual([loaded["part_levels"][part_id]
                              for part_id, _ in backend.SYNTHETIC_STARTER_PARTS], [1] * 9)

            sync = backend.make_player_data_sync(
                account["player_id"], inventory_items=loaded["inventory_items"],
                part_fragments=loaded["part_fragments"],
                part_levels=loaded["part_levels"],
            )
            wallet, synced_levels = wallet_and_part_levels(sync)
            self.assertEqual(wallet, {4: 100, 5: 1500})
            self.assertEqual(synced_levels, {part_id: 1
                                             for part_id, _ in backend.SYNTHETIC_STARTER_PARTS})

            app = app_for_profile(profile_path, account["player_id"])
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 1500)
            self.assertEqual(app.inventory_items[backend.DIAMOND_ITEM_ID], 100)
            persisted = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["tutorial_upgrade_resources_granted"])

            # A new store instance represents backend restart; login and profile
            # refresh must not regrant wallet balances or tutorial materials.
            restarted_store = AccountStore(db)
            _token2, logged_in = restarted_store.login(
                "fresh_tester", "test-password-123",
            )
            self.assertEqual(logged_in["id"], account["id"])
            restarted_app = app_for_profile(profile_path, account["player_id"])
            self.assertEqual(restarted_app.inventory_items[backend.GOLD_ITEM_ID], 1500)
            self.assertEqual(restarted_app.inventory_items[backend.DIAMOND_ITEM_ID], 100)
            self.assertEqual(restarted_app.part_fragments[backend.TUTORIAL_UPGRADE_PART_ID], 2)
            self.assertEqual([restarted_app.part_levels[part_id]
                              for part_id, _ in backend.SYNTHETIC_STARTER_PARTS], [1] * 9)
            self.assertGreaterEqual(restarted_app.inventory_items[backend.GOLD_ITEM_ID], 0)

    def test_existing_account_profile_and_balances_are_not_reinitialized(self):
        with temporary_root("existing-account-test") as root:
            store = AccountStore(root / "accounts.sqlite3")
            _token, account = store.register("existing_tester", "test-password-123")
            path = store.profile_path(account["id"])
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile["inventory_items"][str(backend.GOLD_ITEM_ID)] = 2345
            profile["inventory_items"][str(backend.DIAMOND_ITEM_ID)] = 76
            profile["part_levels"]["2001"] = 7
            path.write_text(json.dumps(profile), encoding="utf-8")

            store._ensure_profile(account["id"], account["player_id"])
            unchanged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["inventory_items"][str(backend.GOLD_ITEM_ID)], 2345)
            self.assertEqual(unchanged["inventory_items"][str(backend.DIAMOND_ITEM_ID)], 76)
            self.assertEqual(unchanged["part_levels"]["2001"], 7)
            loaded = backend.load_profile_state(path, account["player_id"])
            self.assertEqual(loaded["inventory_items"][backend.GOLD_ITEM_ID], 2345)
            self.assertEqual(loaded["inventory_items"][backend.DIAMOND_ITEM_ID], 76)
            self.assertEqual(loaded["part_levels"][2001], 7)


if __name__ == "__main__":
    unittest.main()
