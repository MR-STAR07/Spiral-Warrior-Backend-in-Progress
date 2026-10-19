"""Protocol and persistence tests for the local low-league robot Championship."""

from __future__ import annotations

import sys
import shutil
import socket
import struct
import threading
import base64
import os
import uuid
import json
from urllib.request import Request, urlopen
from contextlib import contextmanager
import unittest
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import ws_probe_server as backend  # noqa: E402
import server as http_backend  # noqa: E402
from account_store import AccountStore  # noqa: E402


@contextmanager
def temporary_profile_path():
    path = BACKEND_DIR / f".championship-test-{uuid.uuid4().hex}.json"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + f".{threading.get_ident()}.tmp").unlink(missing_ok=True)


def frame_payload(frame: bytes) -> tuple[int, bytes]:
    marker = frame[1] & 0x7F
    if marker < 126:
        offset = 2
    elif marker == 126:
        offset = 4
    else:
        offset = 10
    payload = frame[offset:]
    return int.from_bytes(payload[4:6], "big"), payload[6:]


def app_for(path: Path, player_id: int = 100_000_321):
    state = backend.load_profile_state(path, player_id)
    app = SimpleNamespace(state_file=path, player_id=player_id,
                          roguelike_lock=threading.RLock())
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
    return app


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


class ChampionshipModeTests(unittest.TestCase):
    def test_high_score_query_with_score_returns_client_shaped_local_roster(self):
        player_id = 100_000_322
        with temporary_profile_path() as state_file:
            state_file.write_text(json.dumps({
                "schema_version": 1,
                "player_id": player_id,
                "championship_state": {"race_score": 3, "league_index": 1,
                                       "win_mask": 3},
            }), encoding="utf-8")
            app = SimpleNamespace(account_store=None, player_id=player_id,
                                  profile_state_file=state_file, log_file=None)
            server = ThreadingHTTPServer(("127.0.0.1", 0), http_backend.Handler)
            server.app = app
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/RankingService.QueryWithScore",
                    data=json.dumps({"rank": "Rank_1_3", "min_score": "0",
                                     "max_score": "100000"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["code"], 0)
                rows = result["data"]["datas"]
                self.assertEqual(len(rows), 15)
                self.assertEqual(rows[0], {"member": str(player_id), "score": "2",
                                            "metadata": ""})
                self.assertEqual([int(row["member"]) for row in rows[1:]],
                                 list(range(-46, -60, -1)))
                self.assertTrue(all(set(row) >= {"member", "score", "metadata"}
                                    for row in rows))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_top_champion_rank_query_returns_stable_synthetic_season_rows(self):
        player_id = 100_000_324
        with temporary_profile_path() as state_file:
            state_file.write_text(json.dumps({
                "schema_version": 1, "player_id": player_id,
                "championship_state": {"race_score": 31, "league_index": 1,
                                        "top_champion_cups": 200},
            }), encoding="utf-8")
            app = SimpleNamespace(account_store=None, player_id=player_id,
                                  profile_state_file=state_file, log_file=None)
            server = ThreadingHTTPServer(("127.0.0.1", 0), http_backend.Handler)
            server.app = app
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def query():
                    request = Request(
                        f"http://127.0.0.1:{server.server_address[1]}/RankingService.QueryRank",
                        data=json.dumps({"rank": "zone-TopChampionRank.3", "page": 1,
                                         "page_num": 15, "member": ""}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    with urlopen(request, timeout=3) as response:
                        return json.loads(response.read().decode("utf-8"))

                result = query()
                self.assertEqual(result["code"], 0)
                data = result["data"]
                self.assertEqual(len(data["members"]), 15)
                self.assertEqual(data["total_page"], 1)
                self.assertEqual(data["member_score"], "200")
                self.assertEqual(sum(row["member"] == str(player_id)
                                     for row in data["members"]), 1)
                robot_ids = [int(row["member"]) for row in data["members"]
                             if int(row["member"]) < 0]
                self.assertEqual(len(robot_ids), 14)
                self.assertEqual(robot_ids, [int(row["member"]) for row in
                                             http_backend.local_top_champion_members(
                                                 "TopChampionRank.3", player_id,
                                                 state_file)["members"] if int(row["member"]) < 0])
                member_request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/RankingService.QueryWithMember",
                    data=json.dumps({"rank": "zone-TopChampionRank.3",
                                     "members": [str(player_id)]}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(member_request, timeout=3) as response:
                    member_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(member_result["code"], 0)
                self.assertEqual(member_result["data"]["datas"][0]["member"], str(player_id))
                self.assertEqual(member_result["data"]["datas"][0]["rank"], data["member_rank"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_top_champion_rank_item_response_matches_client_displayitem_schema(self):
        player_id = 100_000_325
        ids = [-9_000_301, -9_000_302, player_id, -7]
        request = backend.protobuf_int(1, 100) + b"".join(
            backend.protobuf_int(2, item_id) for item_id in ids
        )
        response = backend.make_champion_rank_item_response(
            request, player_id, "beta-user", player_score=200,
        )
        message_id, payload = frame_payload(response)
        fields = backend.protobuf_varint_values(payload)
        self.assertEqual(message_id, 162)
        self.assertEqual(fields[1], 100)
        self.assertEqual(backend.parse_repeated_int64(payload, 3), ids)
        items = backend.protobuf_length_delimited_values(payload, 4)
        self.assertEqual(len(items), 3)  # The unrelated negative ID is rejected.
        self.assertIn(b"Local Champion 01", items[0])
        self.assertIn(b"UI/TouXiang/TouXiang_010.png", items[0])
        self.assertIn(b"beta-user", items[2])
        self.assertEqual(backend.protobuf_varint_values(items[2])[7], 200)

    def test_top_champion_cups_and_init_date_survive_profile_normalization_and_sync(self):
        state = backend.normalize_championship_state({
            "race_score": 31, "top_champion_cups": 200,
            "top_champion_init_date": 123456,
        })
        self.assertEqual(state["top_champion_cups"], 200)
        self.assertEqual(state["top_champion_init_date"], 123456)
        _message_id, player_data = frame_payload(backend.make_player_data_sync(
            100_000_327, championship_state=state,
        ))
        base_info = backend.protobuf_length_delimited_values(player_data, 1)[0]
        fields = backend.protobuf_varint_values(base_info)
        self.assertEqual(fields[42], 200)  # Client BaseInfo.TopChampionCups.
        self.assertEqual(fields[44], 123456)  # Client BaseInfo.TopChampionInitDate.

    def test_promotion_crosses_robot_cutoff_and_unblocks_public_roster(self):
        started = 50_000
        state = backend.normalize_championship_state({
            "race_score": 2, "win_mask": 0b1111, "wins": 4,
            "period_started_at": started, "period_id": started,
        })
        promoted, changed = backend.champion_period_rollover(
            state, now=started + backend.COMMUNITY_CHAMPION_PERIOD_SECONDS,
        )
        self.assertTrue(changed)
        self.assertEqual(promoted["race_score"], 3)
        self.assertEqual(len(backend.champion_available_robot_ids(100_000_321, promoted)), 14)

    def test_high_score_robot_details_and_match_entry_follow_client_join_index(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file, player_id=100_000_323)
            app.championship_state = backend.normalize_championship_state({
                "race_score": 3, "league_index": 1, "vouchers": 7,
            })
            ids = backend.champion_available_robot_ids(app.player_id,
                                                       app.championship_state)
            self.assertEqual(ids, list(range(-46, -60, -1)))
            query = backend.protobuf_int(1, 6) + backend.protobuf_int(2, ids[0])
            response = backend.make_champion_display_response(
                query, app.player_id, app.championship_state,
            )
            message_id, payload = frame_payload(response)
            self.assertEqual(message_id, 162)
            self.assertEqual(len(backend.protobuf_length_delimited_values(payload, 2)), 1)
            join_index = -ids[0] - 3 * backend.CHAMPION_MAX_RANK_ROWS
            self.assertEqual(join_index, 1)
            entered, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id, enemy_id=ids[0],
                join_index=join_index, league_index=1, now=100,
            )
            self.assertEqual(code, 44)
            self.assertEqual(entered["active_match"]["enemy_id"], ids[0])

    def test_high_score_http_ranking_through_websocket_win_is_playable(self):
        player_id = 100_000_326
        with temporary_profile_path() as state_file:
            state_file.write_text(json.dumps({
                "schema_version": 1, "player_id": player_id,
                "inventory_items": {str(backend.GOLD_ITEM_ID): 1500},
                "championship_state": {"race_score": 3, "league_index": 1,
                                       "vouchers": 7, "win_mask": 0,
                                       "period_started_at": 100},
            }), encoding="utf-8")
            http_app = SimpleNamespace(account_store=None, player_id=player_id,
                                       profile_state_file=state_file, log_file=None)
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), http_backend.Handler)
            http_server.app = http_app
            http_worker = threading.Thread(target=http_server.serve_forever, daemon=True)
            http_worker.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{http_server.server_address[1]}/RankingService.QueryWithScore",
                    data=json.dumps({"rank": "Rank_1_3", "min_score": "0",
                                     "max_score": "15", "limit": 15}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request, timeout=3) as response:
                    ranking = json.loads(response.read().decode("utf-8"))["data"]["datas"]
            finally:
                http_server.shutdown()
                http_server.server_close()
                http_worker.join(timeout=3)

            enemy_id = next(int(row["member"]) for row in ranking
                            if int(row["member"]) < 0)
            join_index = -enemy_id - 3 * backend.CHAMPION_MAX_RANK_ROWS
            self.assertTrue(1 <= join_index < backend.CHAMPION_MAX_RANK_ROWS)
            app = app_for(state_file, player_id)
            app.account = {}
            app.account_store = None
            app.log_file = None
            ws_server = backend.Server(("127.0.0.1", 0), backend.Handler)
            ws_server.app = app
            ws_worker = threading.Thread(target=ws_server.serve_forever, daemon=True)
            ws_worker.start()
            client = socket.create_connection(ws_server.server_address, timeout=3)
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
                self.assertIn(b"101 Switching Protocols", headers)

                detail_request = backend.protobuf_int(1, 6) + backend.protobuf_int(2, enemy_id)
                client.sendall(masked_client_frame(161, detail_request))
                detail_id, detail_payload = receive_server_message(client)
                self.assertEqual(detail_id, 162)
                detail_rows = backend.protobuf_length_delimited_values(detail_payload, 2)
                self.assertEqual(len(detail_rows), 1)
                component_rows = backend.protobuf_length_delimited_values(detail_rows[0], 9)
                self.assertEqual(len(component_rows), 9)

                entry = b"".join((backend.protobuf_int(1, 44),
                                  backend.protobuf_int(2, 5),
                                  backend.protobuf_int(3, join_index),
                                  backend.protobuf_int(5, 1),
                                  backend.protobuf_int(7, enemy_id)))
                client.sendall(masked_client_frame(139, entry))
                self.assertEqual(receive_server_message(client)[0], 1)
                entry_id, entry_payload = receive_server_message(client)
                self.assertEqual(entry_id, 140)
                self.assertEqual(backend.protobuf_varint_values(entry_payload)[1], 44)
                self.assertEqual(app.championship_state["vouchers"], 6)

                result = b"".join((backend.protobuf_int(1, 43),
                                   backend.protobuf_int(2, 5),
                                   backend.protobuf_int(3, join_index),
                                   backend.protobuf_int(4, 1),
                                   backend.protobuf_int(5, 1),
                                   backend.protobuf_int(7, enemy_id)))
                client.sendall(masked_client_frame(139, result))
                self.assertEqual(receive_server_message(client)[0], 1)
                result_id, result_payload = receive_server_message(client)
                self.assertEqual(result_id, 140)
                self.assertEqual(backend.protobuf_varint_values(result_payload)[1], 43)
                self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], 2000)
                self.assertEqual(app.inventory_items[backend.CHAMPION_RESULT_CHEST_ID], 1)
                backend.save_profile_state(app)
                restarted = app_for(state_file, player_id)
                self.assertEqual(restarted.championship_state["wins"], 1)
                self.assertEqual(restarted.championship_state["vouchers"], 6)
                self.assertEqual(restarted.inventory_items[backend.GOLD_ITEM_ID], 2000)
            finally:
                client.close()
                ws_server.shutdown()
                ws_server.server_close()
                ws_worker.join(timeout=3)

    def test_local_websocket_protocol_serves_details_entry_and_result(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file)
            app.account = {}
            app.account_store = None
            app.log_file = None
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
                self.assertIn(b"101 Switching Protocols", headers)

                season_item_ids = [-9_000_001, -9_000_002, app.player_id]
                season_item_request = backend.protobuf_int(1, 100) + b"".join(
                    backend.protobuf_int(2, item_id) for item_id in season_item_ids
                )
                client.sendall(masked_client_frame(161, season_item_request))
                display_id, display_payload = receive_server_message(client)
                self.assertEqual(display_id, 162)
                self.assertEqual(backend.protobuf_varint_values(display_payload)[1], 100)
                display_items = backend.protobuf_length_delimited_values(display_payload, 4)
                self.assertEqual(len(display_items), 3)
                self.assertIn(b"Local Champion 01", display_items[0])

                enemy_id = backend.champion_robot_ids(app.player_id, 0)[0]
                gold_before = int(app.inventory_items.get(backend.GOLD_ITEM_ID, 0))
                query = backend.protobuf_int(1, 6) + backend.protobuf_int(2, enemy_id)
                client.sendall(masked_client_frame(161, query))
                query_id, query_payload = receive_server_message(client)
                self.assertEqual(query_id, 162)
                self.assertEqual(len(backend.protobuf_length_delimited_values(query_payload, 2)), 1)

                join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
                entry = b"".join((backend.protobuf_int(1, 44), backend.protobuf_int(2, 5),
                                  backend.protobuf_int(3, join_index), backend.protobuf_int(5, 1),
                                  backend.protobuf_int(7, enemy_id)))
                client.sendall(masked_client_frame(139, entry))
                sync_id, _ = receive_server_message(client)
                entry_id, entry_payload = receive_server_message(client)
                self.assertEqual(sync_id, 1)
                self.assertEqual(entry_id, 140)
                self.assertEqual(backend.protobuf_varint_values(entry_payload)[1], 44)

                result = b"".join((backend.protobuf_int(1, 43), backend.protobuf_int(2, 5),
                                   backend.protobuf_int(3, join_index), backend.protobuf_int(4, 0),
                                   backend.protobuf_int(5, 1), backend.protobuf_int(7, enemy_id)))
                client.sendall(masked_client_frame(139, result))
                sync_id, _ = receive_server_message(client)
                result_id, result_payload = receive_server_message(client)
                self.assertEqual(sync_id, 1)
                self.assertEqual(result_id, 140)
                self.assertEqual(backend.protobuf_varint_values(result_payload)[1], 43)
                result_fields = backend.protobuf_varint_values(result_payload)
                self.assertEqual(result_fields[2], 0)
                self.assertEqual(result_fields[3], 0)
                self.assertEqual(backend.protobuf_length_delimited_values(result_payload, 7), [])
                self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], gold_before)
                self.assertEqual(app.inventory_items.get(backend.CHAMPION_RESULT_CHEST_ID, 0), 0)
                self.assertEqual(app.championship_state["losses"], 1)

                # Replaying the same completed result without a new entry is
                # rejected and cannot change the wallet or inventory.
                client.sendall(masked_client_frame(139, result))
                replay_id, replay_payload = receive_server_message(client)
                self.assertEqual(replay_id, 140)
                self.assertEqual(backend.protobuf_varint_values(replay_payload)[1], 46)
                self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], gold_before)

                # A new attempt is a distinct eligible match. Retry cost is
                # taken from the client's LoseTimes-based ticket rule.
                client.sendall(masked_client_frame(139, entry))
                sync_id, _ = receive_server_message(client)
                entry_id, entry_payload = receive_server_message(client)
                self.assertEqual((sync_id, entry_id), (1, 140))
                self.assertEqual(backend.protobuf_varint_values(entry_payload)[1], 44)
                self.assertEqual(app.championship_state["vouchers"], 7)

                winning_result = (backend.protobuf_int(1, 43) + backend.protobuf_int(2, 5)
                                  + backend.protobuf_int(3, join_index)
                                  + backend.protobuf_int(4, 1)
                                  + backend.protobuf_int(5, 1)
                                  + backend.protobuf_int(7, enemy_id))
                client.sendall(masked_client_frame(139, winning_result))
                sync_id, _ = receive_server_message(client)
                result_id, result_payload = receive_server_message(client)
                self.assertEqual((sync_id, result_id), (1, 140))
                result_fields = backend.protobuf_varint_values(result_payload)
                self.assertEqual(result_fields[1], 43)
                self.assertEqual(result_fields[2], backend.CHAMPION_RESULT_CHEST_ID)
                self.assertEqual(result_fields[3], 1)
                self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], gold_before + 500)
                self.assertEqual(app.inventory_items[backend.CHAMPION_RESULT_CHEST_ID], 1)
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_display_query_returns_only_valid_stable_robot_profiles(self):
        player_id = 100_000_321
        ids = backend.champion_robot_ids(player_id, 0)
        self.assertEqual(len(backend.champion_available_robot_ids(
            player_id, backend.normalize_championship_state({"win_mask": 0}),
        )), 9)  # Ten initial client rows include the local player.
        self.assertEqual(len(backend.champion_available_robot_ids(
            player_id, backend.normalize_championship_state({"win_mask": (1 << 9) - 1}),
        )), 10)  # RobotRankData grows its row array after the ninth win.
        request = backend.protobuf_int(1, 6) + b"".join(
            backend.protobuf_int(2, enemy_id) for enemy_id in (ids[0], ids[1], 25)
        )
        response = backend.make_champion_display_response(
            request, player_id, backend.normalize_championship_state({"vouchers": 7}),
        )
        message_id, payload = frame_payload(response)
        self.assertEqual(message_id, 162)
        self.assertEqual(backend.protobuf_varint_values(payload)[1], 6)
        details = backend.protobuf_length_delimited_values(payload, 2)
        self.assertEqual(len(details), 2)
        first = backend.protobuf_varint_values(details[0])
        self.assertEqual(first[1] - (1 << 64), ids[0])
        parts = backend.protobuf_length_delimited_values(details[0], 9)
        self.assertEqual(len(parts), 9)
        part_ids = [backend.protobuf_varint_values(part).get(10, 0) for part in parts]
        self.assertEqual(set(part_ids), {part for assembly in backend.STARTER_BLADE_ASSEMBLIES for part in assembly})
        for blade_slot, assembly in enumerate(backend.STARTER_BLADE_ASSEMBLIES, start=1):
            grouped_ids = {
                backend.protobuf_varint_values(part).get(10, 0)
                for part in parts
                if backend.protobuf_varint_values(part).get(3, 0) == blade_slot
            }
            self.assertEqual(grouped_ids, set(assembly))
        display_fields = backend.protobuf_varint_values(details[0])
        robot_name = backend.protobuf_length_delimited_values(details[0], 2)[0].decode()
        self.assertIn(robot_name, backend.CHAMPION_ROBOT_DISPLAY_NAMES)
        self.assertIn(b"UI/TouXiang/TouXiang_010.png",
                      backend.protobuf_length_delimited_values(details[0], 4)[0])
        self.assertEqual(display_fields[7], 9)
        self.assertEqual(display_fields[8], 1)
        returned_ids = backend.parse_repeated_int64(payload, 3)
        self.assertEqual(returned_ids, ids[:2])
        self.assertEqual(backend.champion_robot_ids(player_id, 0), ids)

    def test_display_query_includes_real_player_and_stable_robot_names(self):
        player_id = 100_000_329
        profile = SimpleNamespace(
            account={"username": "hauaahaha"}, level=2,
            lineups=backend.normalize_lineups(None), part_levels={2001: 2},
        )
        state = backend.normalize_championship_state({"race_score": 0})
        robot_ids = backend.champion_robot_ids(player_id, 0)
        request_ids = [robot_ids[0], player_id, robot_ids[1]]
        request = backend.protobuf_int(1, 6) + b"".join(
            backend.protobuf_int(2, value) for value in request_ids
        )
        response = backend.make_champion_display_response(
            request, player_id, state, player_profile=profile,
        )
        payload = frame_payload(response)[1]
        rows = backend.protobuf_length_delimited_values(payload, 2)
        self.assertEqual(len(rows), 3)
        profiles = {
            (backend.protobuf_varint_values(row)[1] - (1 << 64)
             if backend.protobuf_varint_values(row)[1] >= (1 << 63)
             else backend.protobuf_varint_values(row)[1]): row
            for row in rows
        }
        self.assertEqual(set(profiles), set(request_ids))
        own_fields = backend.protobuf_varint_values(profiles[player_id])
        own_name = backend.protobuf_length_delimited_values(profiles[player_id], 2)[0]
        self.assertEqual(own_name, b"hauaahaha")
        self.assertEqual(own_fields[8], 0)  # Local player, not a robot.
        own_parts = backend.protobuf_length_delimited_values(profiles[player_id], 9)
        self.assertEqual(len(own_parts), 9)
        self.assertEqual(backend.protobuf_varint_values(own_parts[0])[1], 2)

        names = []
        for robot_id in robot_ids[:9]:
            one = backend.make_champion_display_response(
                backend.protobuf_int(1, 6) + backend.protobuf_int(2, robot_id),
                player_id, state,
            )
            robot = backend.protobuf_length_delimited_values(frame_payload(one)[1], 2)[0]
            name = backend.protobuf_length_delimited_values(robot, 2)[0].decode()
            names.append(name)
            repeated = backend.make_champion_display_response(
                backend.protobuf_int(1, 6) + backend.protobuf_int(2, robot_id),
                player_id, state,
            )
            repeated_robot = backend.protobuf_length_delimited_values(
                frame_payload(repeated)[1], 2,
            )[0]
            self.assertEqual(
                name, backend.protobuf_length_delimited_values(repeated_robot, 2)[0].decode(),
            )
        self.assertEqual(len(set(names)), 9)
        self.assertFalse(any(name.startswith("Arena Robot") for name in names))

    def test_win_sync_persists_daily_gold_and_chest_slot(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file, player_id=100_000_330)
            app.account = {"username": "beta-player"}
            starting_gold = app.inventory_items[backend.GOLD_ITEM_ID]
            enemy_id = backend.champion_robot_ids(app.player_id)[0]
            join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
            app.championship_state, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id,
                enemy_id=enemy_id, join_index=join_index, league_index=1,
                now=int(backend.time.time()),
            )
            self.assertEqual(code, 44)
            code, _, rewards = backend.complete_champion_match(
                app, enemy_id=enemy_id, join_index=join_index,
                league_index=1, won=True, now=int(backend.time.time()),
            )
            self.assertEqual(code, 43)
            self.assertEqual(rewards, ((backend.GOLD_ITEM_ID, 500),
                                       (backend.CHAMPION_RESULT_CHEST_ID, 1)))
            self.assertEqual(app.inventory_items[backend.GOLD_ITEM_ID], starting_gold + 500)
            self.assertEqual(app.daily_gold_earned, 500)
            self.assertEqual(len(app.save_boxes), 1)
            backend.save_profile_state(app)

            restarted = app_for(state_file, app.player_id)
            self.assertEqual(restarted.daily_gold_earned, 500)
            self.assertEqual(restarted.inventory_items[backend.GOLD_ITEM_ID], starting_gold + 500)
            sync = backend.make_app_player_data_sync(restarted)
            payload = frame_payload(sync)[1]
            player_data = payload
            daily = backend.protobuf_length_delimited_values(player_data, 2)[0]
            daily_gold = backend.protobuf_length_delimited_values(daily, 9)
            self.assertEqual(len(daily_gold), 1)
            gold_fields = backend.protobuf_varint_values(daily_gold[0])
            self.assertEqual(gold_fields[1], backend.GOLD_ITEM_ID)
            self.assertEqual(gold_fields[2], 500)

            base_info = backend.protobuf_length_delimited_values(player_data, 1)[0]
            save_box_rows = backend.protobuf_length_delimited_values(base_info, 9)
            self.assertEqual(len(save_box_rows), 1)
            slot_fields = backend.protobuf_varint_values(save_box_rows[0])
            self.assertEqual(slot_fields[1], 0)
            box_fields = backend.protobuf_varint_values(
                backend.protobuf_length_delimited_values(save_box_rows[0], 2)[0],
            )
            self.assertEqual(box_fields[1], backend.CHAMPION_RESULT_CHEST_ID)
            self.assertEqual(box_fields[5], 1)

            current_day = backend.game_local_day_index()
            next_day_ms = backend.GAME_TIME_EPOCH_MS + (current_day + 1) * 86_400_000 + 1
            self.assertEqual(backend.record_daily_gold_earned(
                restarted, 0, now_ms=next_day_ms,
            ), 0)
            self.assertEqual(restarted.daily_gold_day, current_day + 1)
            self.assertEqual(restarted.inventory_items[backend.GOLD_ITEM_ID], starting_gold + 500)

    def test_championship_race_over_task_advances_on_win_and_survives_reload(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file, player_id=100_000_340)
            app.level = 2  # Tier 3 is active after claiming the Entrance Examination.
            app.account = {"username": "existing-beta"}
            app.inventory_items[5000] = 9
            app.level_task_state["1278226993"] = {"process": 4, "status": 1}
            app.task_exchange_claims = [backend.LEVEL_FIRST_REWARD_ID]
            app.persisted_inventory_items = dict(app.inventory_items)
            backend.save_profile_state(app)

            tier = backend.LEVEL_PROGRESSION_ROWS[3]
            self.assertEqual(tier["lvUp_TaskNum"], 2)
            self.assertEqual(tier["LevelTasks"], [1278226993, 1278226994, 1278226995])
            task = backend.LEVEL_PROGRESSION_TASKS[1278226994]
            self.assertEqual((task["condition"], task["RaceMarks"], task["maxProcess"]),
                             (0, 2, 1))

            enemy_id = backend.champion_robot_ids(app.player_id)[0]
            join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
            app.championship_state, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id, enemy_id=enemy_id,
                join_index=join_index, league_index=1, now=100,
            )
            self.assertEqual(code, 44)
            code, _, rewards = backend.complete_champion_match(
                app, enemy_id=enemy_id, join_index=join_index, league_index=1,
                won=True, now=101,
            )
            self.assertEqual(code, 43)
            self.assertEqual(rewards[-1], (backend.CHAMPION_RESULT_CHEST_ID, 1))
            self.assertEqual(app.level_task_state["1278226994"], {
                "process": 1, "status": backend.TASK_STATUS_COMPLETE,
            })
            backend.save_profile_state(app)

            restarted = app_for(state_file, app.player_id)
            self.assertEqual(restarted.level_task_state["1278226994"], {
                "process": 1, "status": backend.TASK_STATUS_COMPLETE,
            })
            self.assertEqual(restarted.level_task_state["1278226993"], {
                "process": 4, "status": 1,
            })
            self.assertEqual(restarted.task_exchange_claims,
                             [backend.LEVEL_FIRST_REWARD_ID])
            self.assertEqual(restarted.inventory_items[5000], 9)

            # The normal player synchronization carries the task ID/progress/state.
            task_payload = backend.make_level_task_container(
                restarted.level, restarted.inventory_items, restarted.level_task_state,
                restarted.progression_counters, restarted.part_levels,
            )
            level_task_list = backend.protobuf_length_delimited_values(task_payload, 2)[0]
            entries = backend.protobuf_length_delimited_values(level_task_list, 1)
            task_entry = next(
                entry for entry in entries
                if backend.protobuf_varint_values(entry).get(1) == 1278226994
            )
            state_payload = backend.protobuf_length_delimited_values(task_entry, 2)[0]
            state_fields = backend.protobuf_varint_values(state_payload)
            self.assertEqual(state_fields[1], 1)
            self.assertEqual(state_fields[2], backend.TASK_STATUS_COMPLETE)

            # A replay has no active entry, so neither mission nor rewards advance again.
            gold_after = restarted.inventory_items[backend.GOLD_ITEM_ID]
            chest_after = restarted.inventory_items[backend.CHAMPION_RESULT_CHEST_ID]
            code, _, rewards = backend.complete_champion_match(
                restarted, enemy_id=enemy_id, join_index=join_index,
                league_index=1, won=True, now=102,
            )
            self.assertEqual((code, rewards), (46, ()))
            self.assertEqual(restarted.level_task_state["1278226994"]["process"], 1)
            self.assertEqual(restarted.inventory_items[backend.GOLD_ITEM_ID], gold_after)
            self.assertEqual(restarted.inventory_items[backend.CHAMPION_RESULT_CHEST_ID],
                             chest_after)

    def test_championship_race_over_task_counts_loss_without_rewards(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file, player_id=100_000_341)
            app.level = 2
            app.account = {"username": "loss-test"}
            before_inventory = dict(app.inventory_items)
            enemy_id = backend.champion_robot_ids(app.player_id)[0]
            join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
            app.championship_state, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id, enemy_id=enemy_id,
                join_index=join_index, league_index=1, now=200,
            )
            self.assertEqual(code, 44)
            code, _, rewards = backend.complete_champion_match(
                app, enemy_id=enemy_id, join_index=join_index, league_index=1,
                won=False, now=201,
            )
            self.assertEqual(code, 43)
            self.assertEqual(rewards, ())
            self.assertEqual(app.inventory_items, before_inventory)
            self.assertEqual(app.level_task_state["1278226994"], {
                "process": 1, "status": backend.TASK_STATUS_COMPLETE,
            })
            backend.save_profile_state(app)
            restarted = app_for(state_file, app.player_id)
            self.assertEqual(restarted.level_task_state["1278226994"]["process"], 1)
            self.assertEqual(restarted.inventory_items, before_inventory)

    def test_zero_race_score_uses_client_rank_floor_without_changing_saved_state(self):
        player_id = 100_000_011
        state = backend.normalize_championship_state({"race_score": 0})
        history = backend.make_champion_history(state)
        history_fields = backend.protobuf_varint_values(
            backend.protobuf_length_delimited_values(history, 2)[0],
        )

        # The client's getRankofScore(0) floors to rank key 1, but robot
        # profile initialization directly requests LeagueRank[RaceScore].
        self.assertEqual(history_fields[6], 1)
        self.assertEqual(state["race_score"], 0)

        expected_ids = backend.champion_robot_ids(player_id, 1)
        self.assertEqual(backend.champion_robot_ids(player_id, 0), expected_ids)
        available_ids = backend.champion_available_robot_ids(player_id, state)
        self.assertEqual(available_ids, expected_ids[:9])
        self.assertTrue(all((-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS in range(1, 10)
                            for enemy_id in available_ids))

    def test_new_account_receives_seven_tickets_once(self):
        directory = BACKEND_DIR / f".championship-store-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            store = AccountStore(directory / "accounts.sqlite")
            store._ensure_profile(1, 100_000_001)
            profile_path = store.profile_path(1)
            self.assertEqual(backend.load_profile_state(profile_path, 100_000_001)
                             ["championship_state"]["vouchers"], 7)
            store._ensure_profile(1, 100_000_001)
            self.assertEqual(backend.load_profile_state(profile_path, 100_000_001)
                             ["championship_state"]["vouchers"], 7)
        finally:
            shutil.rmtree(directory)

    def test_entry_validates_identity_and_consumes_client_ticket_cost_once(self):
        player_id = 100_000_321
        enemy_id = backend.champion_robot_ids(player_id)[0]
        join_index = (-enemy_id) % backend.CHAMPION_MAX_RANK_ROWS
        initial = backend.normalize_championship_state({"vouchers": 7})
        entered, code, reason = backend.apply_champion_entry(
            initial, player_id=player_id, enemy_id=enemy_id,
            join_index=join_index, league_index=1, now=100,
        )
        self.assertEqual((code, reason), (44, "match_entered"))
        self.assertEqual(entered["vouchers"], 6)
        duplicate, code, reason = backend.apply_champion_entry(
            entered, player_id=player_id, enemy_id=enemy_id,
            join_index=join_index, league_index=1, now=101,
        )
        self.assertEqual((code, reason), (44, "duplicate_entry_reused"))
        self.assertEqual(duplicate["vouchers"], 6)
        self.assertEqual(backend.apply_champion_entry(
            initial, player_id=player_id, enemy_id=25,
            join_index=0, league_index=1, now=100,
        )[1], 45)
        self.assertEqual(backend.apply_champion_entry(
            initial, player_id=player_id, enemy_id=enemy_id,
            join_index=(join_index + 1) % 15, league_index=1, now=100,
        )[1], 45)
        self.assertEqual(backend.apply_champion_entry(
            initial, player_id=player_id, enemy_id=enemy_id,
            join_index=join_index, league_index=2, now=100,
        )[1], 45)
        self.assertEqual(backend.apply_champion_entry(
            backend.normalize_championship_state({"vouchers": 0}),
            player_id=player_id, enemy_id=enemy_id, join_index=join_index,
            league_index=1, now=100,
        )[1], 47)

    def test_win_and_loss_rewards_are_exactly_once_and_persist(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file)
            app.championship_state = backend.normalize_championship_state({"vouchers": 7})
            app.inventory_items[5000] = 9
            app.persisted_inventory_items[5000] = 9
            enemy_id = backend.champion_robot_ids(app.player_id)[0]
            join_index = (-enemy_id) % 15
            state, code, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id,
                enemy_id=enemy_id, join_index=join_index, league_index=1, now=100,
            )
            app.championship_state = state
            backend.save_profile_state(app)
            restarted = app_for(state_file, app.player_id)
            self.assertEqual(restarted.championship_state["vouchers"], 6)
            code, _, rewards = backend.complete_champion_match(
                restarted, enemy_id=enemy_id, join_index=join_index,
                league_index=1, won=True, now=101,
            )
            self.assertEqual(code, 43)
            self.assertEqual(rewards, ((backend.GOLD_ITEM_ID, 500),
                                       (backend.CHAMPION_RESULT_CHEST_ID, 1)))
            backend.save_profile_state(restarted)
            after = app_for(state_file, app.player_id)
            gold = after.inventory_items[backend.GOLD_ITEM_ID]
            chest = after.inventory_items[backend.CHAMPION_RESULT_CHEST_ID]
            self.assertEqual(after.championship_state["wins"], 1)
            self.assertEqual(after.inventory_items[5000], 9)
            self.assertTrue(after.championship_state["win_mask"] & (1 << join_index))
            code, _, rewards = backend.complete_champion_match(
                after, enemy_id=enemy_id, join_index=join_index,
                league_index=1, won=True, now=102,
            )
            self.assertEqual(code, 46)
            self.assertEqual(rewards, ())
            self.assertEqual(after.inventory_items[backend.GOLD_ITEM_ID], gold)
            self.assertEqual(after.inventory_items[backend.CHAMPION_RESULT_CHEST_ID], chest)

    def test_loss_grants_nothing_and_retry_cost_matches_client(self):
        app = SimpleNamespace(championship_state=backend.normalize_championship_state({"vouchers": 7}),
                              inventory_items={backend.GOLD_ITEM_ID: 1500})
        enemy_id = backend.champion_robot_ids(100_000_321)[0]
        join_index = (-enemy_id) % 15
        app.championship_state, _, _ = backend.apply_champion_entry(
            app.championship_state, player_id=100_000_321, enemy_id=enemy_id,
            join_index=join_index, league_index=1, now=100,
        )
        code, _, rewards = backend.complete_champion_match(
            app, enemy_id=enemy_id, join_index=join_index, league_index=1,
            won=False, now=101,
        )
        self.assertEqual(code, 43)
        self.assertEqual(rewards, ())
        self.assertEqual(app.inventory_items, {backend.GOLD_ITEM_ID: 1500})
        self.assertEqual(app.championship_state["lose_times"][str(enemy_id)], 1)
        self.assertEqual(app.championship_state["losses"], 1)
        self.assertIsNone(app.championship_state["active_match"])
        app.championship_state, code, _ = backend.apply_champion_entry(
            app.championship_state, player_id=100_000_321, enemy_id=enemy_id,
            join_index=join_index, league_index=1, now=102,
        )
        self.assertEqual(code, 44)
        self.assertEqual(app.championship_state["vouchers"], 4)

    def test_result_response_contains_the_client_reward_envelope(self):
        reward_items = ((backend.GOLD_ITEM_ID, 500),)
        response = backend.make_bout_response(
            43, param1=backend.CHAMPION_RESULT_CHEST_ID, param2=1,
            reward_items=reward_items,
        )
        message_id, payload = frame_payload(response)
        self.assertEqual(message_id, 140)
        fields = backend.protobuf_varint_values(payload)
        self.assertEqual(fields[1], 43)
        self.assertEqual(fields[2], backend.CHAMPION_RESULT_CHEST_ID)
        self.assertEqual(fields[3], 1)
        receipt = backend.protobuf_length_delimited_values(payload, 7)[0]
        self.assertEqual(backend.protobuf_varint_values(receipt)[1], 7)  # GIF_GAME.
        items = backend.protobuf_length_delimited_values(receipt, 2)[0]
        item_infos = backend.protobuf_length_delimited_values(items, 1)
        decoded = [backend.protobuf_varint_values(item) for item in item_infos]
        self.assertEqual([(row[1], row[2]) for row in decoded], list(reward_items))

    def test_low_league_state_sync_and_24_hour_top_five_rollover(self):
        started = 10_000
        state = backend.normalize_championship_state({
            "vouchers": 3, "win_mask": 0b1111, "wins": 4,
            "period_started_at": started, "period_id": started,
        })
        rolled, changed = backend.champion_period_rollover(
            state, now=started + backend.COMMUNITY_CHAMPION_PERIOD_SECONDS,
        )
        self.assertTrue(changed)
        self.assertEqual(rolled["league_index"], 1)
        self.assertEqual(rolled["race_score"], 1)  # Community top-five score-tier fallback.
        self.assertEqual(rolled["last_period_rank"], 5)
        self.assertEqual(rolled["vouchers"], 3)
        self.assertEqual(rolled["win_mask"], 0)
        not_yet, not_yet_changed = backend.champion_period_rollover(
            state, now=started + backend.COMMUNITY_CHAMPION_PERIOD_SECONDS - 1,
        )
        self.assertFalse(not_yet_changed)
        self.assertEqual(not_yet["win_mask"], 0b1111)
        sync = backend.make_player_data_sync(
            100_000_321, championship_state=rolled,
        )
        payload = frame_payload(sync)[1]
        history = next(
            backend.protobuf_length_delimited_values(row, 2)[0]
            for row in backend.protobuf_length_delimited_values(payload, 3)
            if backend.protobuf_varint_values(row).get(1) == backend.CHAMPION_RACE_TYPE
        )
        values = backend.protobuf_varint_values(history)
        self.assertEqual(values[6], 1)  # Remains in the supported all-robot bracket.
        self.assertEqual(values[9], 1)
        self.assertEqual(values[13], 3)
        again, changed_again = backend.champion_period_rollover(rolled, now=started + 1)
        self.assertFalse(changed_again)
        self.assertEqual(again["race_score"], 1)

    def test_existing_profile_and_active_match_survive_restart(self):
        with temporary_profile_path() as state_file:
            app = app_for(state_file)
            app.championship_state = backend.normalize_championship_state({"vouchers": 2})
            enemy_id = backend.champion_robot_ids(app.player_id)[1]
            join_index = (-enemy_id) % 15
            app.championship_state, _, _ = backend.apply_champion_entry(
                app.championship_state, player_id=app.player_id, enemy_id=enemy_id,
                join_index=join_index, league_index=1, now=500,
            )
            backend.save_profile_state(app)
            restarted = app_for(state_file, app.player_id)
            self.assertEqual(restarted.championship_state["vouchers"], 1)
            self.assertEqual(restarted.championship_state["active_match"]["enemy_id"], enemy_id)
            # No login initialization or restart refills the persisted balance.
            backend.save_profile_state(restarted)
            self.assertEqual(backend.load_profile_state(state_file, app.player_id)
                             ["championship_state"]["vouchers"], 1)


if __name__ == "__main__":
    unittest.main()
