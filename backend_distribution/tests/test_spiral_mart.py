"""Client-protocol regression tests for the local Spiral Mart implementation."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402


def masked_client_frame(message_id: int, protobuf: bytes) -> bytes:
    payload = struct.pack(">IH", 6 + len(protobuf), message_id) + protobuf
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    if len(payload) < 126:
        header = bytes((0x82, 0x80 | len(payload)))
    else:
        header = bytes((0x82, 0xFE)) + struct.pack(">H", len(payload))
    return header + mask + masked


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = sock.recv(size - len(chunks))
        if not block:
            raise EOFError("backend closed before completing a WebSocket frame")
        chunks.extend(block)
    return bytes(chunks)


def receive_server_message(sock: socket.socket) -> tuple[int, bytes]:
    first = receive_exact(sock, 2)
    marker = first[1] & 0x7F
    if marker == 126:
        size = struct.unpack(">H", receive_exact(sock, 2))[0]
    elif marker == 127:
        size = struct.unpack(">Q", receive_exact(sock, 8))[0]
    else:
        size = marker
    payload = receive_exact(sock, size)
    return int.from_bytes(payload[4:6], "big"), payload[6:]


def backend_frame_payload(frame: bytes) -> tuple[int, bytes]:
    marker = frame[1] & 0x7F
    offset = 2 if marker < 126 else 4 if marker == 126 else 10
    payload = frame[offset:]
    return int.from_bytes(payload[4:6], "big"), payload[6:]


@contextmanager
def lottery_socket(app):
    server = backend.Server(("127.0.0.1", 0), backend.Handler)
    server.app = app
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = socket.create_connection(server.server_address, timeout=3)
    client.settimeout(3)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        client.sendall((
            "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        headers = bytearray()
        while not headers.endswith(b"\r\n\r\n"):
            headers.extend(client.recv(1))
        if b"101 Switching Protocols" not in headers:
            raise AssertionError(f"WebSocket handshake failed: {headers!r}")
        yield client
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@contextmanager
def lottery_profile(inventory: dict[int, int] | None = None):
    state_file = BACKEND_DIR / f".spiral-mart-test-{uuid.uuid4().hex}.json"
    try:
        player_id = 1_009_000_001
        state_file.write_text(json.dumps({
            "schema_version": backend.PROFILE_SCHEMA_VERSION,
            "player_id": player_id,
            "inventory_items": {str(key): value for key, value in (inventory or {}).items()},
            "lottery_info": {},
        }), encoding="utf-8")
        state = backend.load_profile_state(state_file, player_id)
        app = SimpleNamespace(
            state_file=state_file, player_id=player_id,
            roguelike_lock=threading.RLock(), account={}, account_store=None,
            log_file=None,
        )
        for name, value in state.items():
            setattr(app, name, value)
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
        app.persisted_save_boxes = dict(app.save_boxes)
        yield app
    finally:
        state_file.unlink(missing_ok=True)
        for temporary in BACKEND_DIR.glob(state_file.name + ".*.tmp"):
            temporary.unlink(missing_ok=True)


def request_draw(sock: socket.socket, lottery_id: int, times: int,
                 has_cost: int | None = None) -> tuple[int, list[tuple[int, bytes]]]:
    request = backend.protobuf_int(1, lottery_id) + backend.protobuf_int(2, times)
    if has_cost is not None:
        request += backend.protobuf_int(3, has_cost)
    sock.sendall(masked_client_frame(227, request))
    messages: list[tuple[int, bytes]] = []
    while True:
        message = receive_server_message(sock)
        messages.append(message)
        if message[0] == 228:
            return backend.protobuf_varint_values(message[1]).get(1, 0), messages


def response_box_items(payload: bytes) -> list[tuple[int, int, int]]:
    rewards = backend.protobuf_length_delimited_values(payload, 2)
    if not rewards:
        return []
    result: list[tuple[int, int, int]] = []
    for detail in backend.protobuf_length_delimited_values(rewards[0], 3):
        fields = backend.protobuf_varint_values(detail)
        box_id = fields.get(2, 0)
        for item_data in backend.protobuf_length_delimited_values(detail, 1):
            item = backend.protobuf_varint_values(item_data)
            result.append((item.get(1, 0), item.get(2, 0), box_id))
    return result


class SpiralMartTests(unittest.TestCase):
    def test_recovered_pool_ids_costs_chance_tables_and_blank_schedule(self):
        self.assertEqual(set(backend.LOTTERY_CONFIGS), {1, 2, 3})
        standard = backend.LOTTERY_CONFIGS[1]
        newbie_limited = backend.LOTTERY_CONFIGS[2]
        basic = backend.LOTTERY_CONFIGS[3]
        self.assertEqual((standard["PoolBoxId"], standard["Cost"]),
                         (1228419120, {"ID": 5000, "Num": 1}))
        self.assertEqual((newbie_limited["PoolBoxId"], newbie_limited["Cost"]),
                         (1228419122, {"ID": 5000, "Num": 1}))
        self.assertEqual((basic["PoolBoxId"], basic["Cost"]),
                         (1228419121, {"ID": backend.GOLD_ITEM_ID, "Num": 2000}))
        self.assertTrue(backend.lottery_pool_is_open(standard))
        self.assertEqual([len(backend.LOTTERY_CHANCE_ROWS[key]) for key in
                          (1228419120, 1228419121, 1228419122)], [4, 3, 3])
        self.assertEqual(sum(row["Pvalue"] for row in
                             backend.LOTTERY_CHANCE_ROWS[1228419121]), 228)

    def test_schedule_bounds_are_respected_and_unknown_formats_fail_closed(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        self.assertTrue(backend.lottery_pool_is_open({
            "Begin": "2026-10-19T11:00:00Z", "End": "2026-10-19T13:00:00Z",
        }, now))
        self.assertFalse(backend.lottery_pool_is_open({
            "Begin": "2026-10-19T13:00:00Z", "End": "",
        }, now))
        self.assertFalse(backend.lottery_pool_is_open({"Begin": "unknown"}, now))

    def test_initial_player_sync_seeds_ticket_and_gold_cost_records(self):
        frame = backend.make_player_data_sync(1_009_000_001, lottery_info={})
        message_id, player_data = backend_frame_payload(frame)
        self.assertEqual(message_id, 1)
        lottery_info = backend.protobuf_length_delimited_values(player_data, 25)[0]
        rows = backend.protobuf_length_delimited_values(lottery_info, 2)
        costs = {
            (fields := backend.protobuf_varint_values(row))[1]: fields.get(2, 0)
            for row in rows
        }
        self.assertEqual(costs[5000], 0)
        self.assertEqual(costs[backend.GOLD_ITEM_ID], 0)

    def test_standard_ticket_draw_returns_valid_reward_and_persists(self):
        with lottery_profile({5000: 1}) as app:
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                code, messages = request_draw(client, lottery_id=1, times=1, has_cost=0)
            self.assertEqual(code, 1)
            self.assertEqual([message_id for message_id, _ in messages], [1, 228])
            items = response_box_items(messages[-1][1])
            self.assertEqual(len(items), 1)
            item_id, quantity, box_id = items[0]
            self.assertEqual((item_id, quantity, box_id), (2007, 25, 5002))
            self.assertIn(item_id, backend.UPGRADEABLE_PART_LOCATIONS)
            self.assertEqual(app.inventory_items.get(5000, 0), 0)
            self.assertEqual(app.part_fragments[item_id], 25)
            self.assertGreaterEqual(app.part_levels[item_id], 1)
            restored = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(restored["inventory_items"].get(5000, 0), 0)
            self.assertEqual(restored["part_fragments"][item_id], 25)
            self.assertEqual(restored["part_levels"][item_id], app.part_levels[item_id])
            self.assertEqual(restored["lottery_info"]["cost_record"]["5000"], 1)

    def test_stale_ticket_draw_replay_does_not_deduct_or_grant_twice(self):
        with lottery_profile({5000: 2}) as app:
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                first_code, first_messages = request_draw(client, 1, 1, 0)
                replay_code, replay_messages = request_draw(client, 1, 1, 0)
            self.assertEqual(first_code, 1)
            self.assertEqual(replay_code, 3)
            self.assertEqual([message_id for message_id, _ in first_messages], [1, 228])
            self.assertEqual([message_id for message_id, _ in replay_messages], [228])
            self.assertEqual(app.inventory_items[5000], 1)
            self.assertEqual(app.part_fragments[2007], 25)
            self.assertEqual(app.lottery_info["cost_record"]["5000"], 1)

    def test_ten_draw_request_uses_client_limit_cost_and_recovered_pity(self):
        with lottery_profile({5000: 10}) as app:
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                code, messages = request_draw(client, 1, 10, 0)
            self.assertEqual(code, 1)
            self.assertEqual(app.inventory_items.get(5000, 0), 0)
            self.assertEqual(app.lottery_info["cost_record"]["5000"], 10)
            rewards = response_box_items(messages[-1][1])
            self.assertEqual(len(rewards), 10)
            self.assertEqual(rewards[-1][2], 5003)
            self.assertEqual(app.lottery_info["lotteries"]["1"]["rarity_times"]["3"], 0)

    def test_basic_pool_uses_gold_cost_and_synchronizes_inventory_and_balance(self):
        with lottery_profile({backend.GOLD_ITEM_ID: 4000}) as app:
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                code, messages = request_draw(client, 3, 1, 0)
            self.assertEqual(code, 1)
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 2000)
            self.assertEqual(app.lottery_info["cost_record"][str(backend.GOLD_ITEM_ID)], 2000)
            self.assertEqual(len(response_box_items(messages[-1][1])), 1)
            # The client-facing PlayerData sync precedes ESC_Lottery and carries
            # the authoritative post-draw wallet and inventory state.
            sync_id, sync_payload = messages[0]
            self.assertEqual(sync_id, 1)
            self.assertTrue(backend.protobuf_length_delimited_values(sync_payload, 2))
            restored = backend.load_profile_state(app.state_file, app.player_id)
            self.assertEqual(restored["inventory_items"][backend.GOLD_ITEM_ID], 2000)

    def test_basic_pool_replay_is_rejected_using_synchronized_gold_cost_record(self):
        with lottery_profile({backend.GOLD_ITEM_ID: 4000}) as app:
            self.assertIn(str(backend.GOLD_ITEM_ID), app.lottery_info["cost_record"])
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                first, _ = request_draw(client, 3, 1, 0)
                replay, _ = request_draw(client, 3, 1, 0)
            self.assertEqual((first, replay), (1, 3))
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 2000)
            self.assertEqual(app.lottery_info["cost_record"][str(backend.GOLD_ITEM_ID)], 2000)

    def test_insufficient_cost_invalid_pool_and_closed_pool_do_not_mutate(self):
        with lottery_profile({5000: 0, backend.GOLD_ITEM_ID: 1999}) as app:
            with lottery_socket(app) as client:
                invalid, invalid_messages = request_draw(client, 999, 1, 0)
                poor, poor_messages = request_draw(client, 3, 1, 0)
            self.assertEqual(invalid, 2)  # Client maps LotteryIdInvalid to DC009.
            self.assertEqual(poor, 5)
            self.assertEqual([mid for mid, _ in invalid_messages], [228])
            self.assertEqual([mid for mid, _ in poor_messages], [228])
            self.assertEqual(app.inventory_items.get(5000, 0), 0)
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 1999)
            self.assertEqual(app.part_fragments, {})
            self.assertEqual(app.lottery_info["cost_record"][str(backend.GOLD_ITEM_ID)], 0)

        with lottery_profile({5000: 1}) as app:
            closed = dict(backend.LOTTERY_CONFIGS[1])
            closed["Begin"] = "2999-01-01T00:00:00Z"
            with patch.dict(backend.LOTTERY_CONFIGS, {1: closed}):
                with lottery_socket(app) as client:
                    closed_code, closed_messages = request_draw(client, 1, 1, 0)
            self.assertEqual(closed_code, 2)
            self.assertEqual([mid for mid, _ in closed_messages], [228])
            self.assertEqual(app.inventory_items.get(5000, 0), 1)

    def test_recovered_single_use_newbie_pool_is_limited_after_one_draw(self):
        with lottery_profile({5000: 2}) as app:
            with lottery_socket(app) as client, patch.object(backend.secrets, "randbelow", return_value=0):
                success, _ = request_draw(client, 2, 1, 0)
                over_limit, _ = request_draw(client, 2, 1, 1)
            self.assertEqual(success, 1)
            self.assertEqual(over_limit, 7)
            self.assertEqual(app.inventory_items[5000], 1)
            self.assertEqual(app.lottery_info["lotteries"]["2"]["allowance"], 1)

    def test_no_configuration_reward_is_granted_when_selection_cannot_resolve(self):
        with lottery_profile({5000: 1}) as app:
            with patch.dict(backend.LOTTERY_CHANCE_ROWS, {1228419120: []}):
                with lottery_socket(app) as client:
                    code, messages = request_draw(client, 1, 1, 0)
            self.assertEqual(code, 4)
            self.assertEqual([message_id for message_id, _ in messages], [228])
            self.assertEqual(app.inventory_items[5000], 1)
            self.assertEqual(app.part_fragments, {})
            self.assertEqual(app.lottery_info["cost_record"]["5000"], 0)


if __name__ == "__main__":
    unittest.main()
