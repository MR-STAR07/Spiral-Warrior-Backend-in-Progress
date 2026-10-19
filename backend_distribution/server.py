#!/usr/bin/env python3
"""Small, loopback-only HTTP harness for the Spiral Warrior test clone.

Only runtime-observed paths are implemented. Area-list and auth payloads are
loaded from caller-selected files because original server responses are not
known. No credentials, request bodies, or arbitrary paths are stored.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import mimetypes
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from account_store import AccountError, AccountStore
from mail_fixture import DEFAULT_STATE_FILE, MAIL_STATE_READ, MAIL_STATE_RECEIVED, update_mail_state, visible_mails

ROOT = Path(__file__).resolve().parent
DEFAULT_NOTICE = ROOT / "notices" / "NoticeEng.txt"
DEFAULT_PROFILE_STATE = ROOT / "data" / "local_profile_state.json"
DEFAULT_FREE_PURCHASE_CATALOG = ROOT / "data" / "local_free_purchase_catalog.json"
CHAMPION_RANK_NAME = re.compile(r"(?:^|_)Rank_(\d+)_(\d+)$")
CHAMPION_RANK_ROWS = 15
TOP_CHAMPION_RANK_NAME = re.compile(r"TopChampionRank\.(\d+)$")


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(fallback)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def local_champion_score_rows(rank_name: str, player_id: int,
                              profile_state_file: Path) -> list[dict[str, str]]:
    """Build the client-shaped local robot roster for the public-rank branch.

    The Android client uses RobotRankData itself through RaceScore 2. Above
    that threshold it calls QueryWithScore and expects ``data.datas`` entries
    with ``member`` and ``score``. Negative member IDs are synthetic robots;
    their encoded slot matches ChampDataMgr.robotId2JoinIdx in the recovered
    client. This is a local compatibility roster, not original ranking data.
    """
    match = CHAMPION_RANK_NAME.search(str(rank_name))
    if not match:
        raise ValueError("unsupported local Championship rank name")
    _league_index, race_score = (int(value) for value in match.groups())
    if race_score <= 2:
        raise ValueError("QueryWithScore is only used above the all-robot client threshold")

    profile = _read_json(profile_state_file, {})
    championship = profile.get("championship_state", {})
    if not isinstance(championship, dict):
        championship = {}
    saved_score = int(championship.get("race_score", race_score))
    if saved_score != race_score:
        raise ValueError("requested Championship score does not match the saved profile")

    # Current local profiles use JoinIndex 0. Retain it as the player's row;
    # every robot is a distinct negative ID whose decoded slot is stable.
    win_mask = max(0, int(championship.get("win_mask", 0)))
    player_wins = win_mask.bit_count()
    rows = [{
        "member": str(int(player_id)),
        "score": str(player_wins),
        "metadata": "",
    }]
    for slot in range(1, CHAMPION_RANK_ROWS):
        robot_id = -(race_score * CHAMPION_RANK_ROWS + slot)
        # Deterministic spread gives the client rank order and per-row win
        # counters to render without representing real competitive results.
        wins = (CHAMPION_RANK_ROWS - slot - 1) % 8
        rows.append({"member": str(robot_id), "score": str(wins), "metadata": ""})
    return rows


def local_top_champion_members(rank_name: str, player_id: int,
                               profile_state_file: Path,
                               page: int = 1, page_num: int = 15) -> dict:
    """Return a deterministic local-only Top Champion board.

    The client explicitly requests TopChampionRank.<season-index> and then
    fetches each member through QueryUserRankItem. The original ranking
    service is unavailable, so these rows are synthetic local display data,
    not real player accounts or original-server ranking results.
    """
    match = TOP_CHAMPION_RANK_NAME.search(str(rank_name))
    if not match:
        raise ValueError("unsupported local Top Championship rank name")
    season_index = int(match.group(1))
    profile = _read_json(profile_state_file, {})
    championship = profile.get("championship_state", {})
    if not isinstance(championship, dict):
        championship = {}
    self_score = max(0, int(championship.get("top_champion_cups", 0)))
    rows = [{
        "member": str(-9_000_000 - season_index * 100 - slot),
        "score": str(max(1, 300 - slot * 10)),
        "metadata": "",
    } for slot in range(1, CHAMPION_RANK_ROWS)]
    rows.append({"member": str(int(player_id)), "score": str(self_score), "metadata": ""})
    rows.sort(key=lambda row: (-int(row["score"]), int(row["member"])))
    size = min(100, max(1, int(page_num)))
    start = (max(1, int(page)) - 1) * size
    members = [dict(row, rank=index + 1)
               for index, row in enumerate(rows[start:start + size], start)]
    self_rank = next((index + 1 for index, row in enumerate(rows)
                      if int(row["member"]) == int(player_id)), 0)
    return {"members": members, "total_page": (len(rows) + size - 1) // size,
            "member_score": str(self_score), "member_rank": self_rank}


def _utc_bucket(limit_type: int, now: datetime) -> str | None:
    if limit_type == 0:
        return None
    if limit_type == 1:
        return "day:" + now.strftime("%Y-%m-%d")
    if limit_type == 2:
        year, week, _ = now.isocalendar()
        return f"week:{year}-{week:02d}"
    if limit_type == 3:
        return "month:" + now.strftime("%Y-%m")
    return "lifetime"


def claim_local_free_purchase(app: argparse.Namespace, product_id: int, nonce: str) -> tuple[int, dict]:
    with app.free_purchase_lock:
        catalog_row = app.free_purchase_catalog.get(int(product_id))
        if catalog_row is None:
            return 404, {"code": 404, "error": "unknown_local_product", "data": None}

        profile = _read_json(app.profile_state_file, {
            "schema_version": 1, "player_id": app.player_id, "inventory_items": {},
            "roguelike_keys": 4, "roguelike_map_state": None,
        })
        local = profile.setdefault("local_purchases", {})
        limits = local.setdefault("limits", {})
        processed = local.setdefault("processed_nonces", {})
        marks = set(int(x) for x in local.get("recharge_marks", []) if str(x).lstrip("-").isdigit())

        if nonce in processed:
            previous = processed[nonce]
            return 200, {"code": 0, "error": "", "data": {**previous, "replayed": True}}

        limit_type = int(catalog_row.get("limit_type", 0))
        limit_times = int(catalog_row.get("limit_times", 0))
        bucket = _utc_bucket(limit_type, datetime.now(timezone.utc))
        limit_key = f"{product_id}:{bucket}" if bucket else None
        count = int(limits.get(limit_key, 0)) if limit_key else 0
        cap = max(1, limit_times) if bucket and limit_times > 0 else None
        if cap is not None and count >= cap:
            return 409, {"code": 409, "error": "already_claimed", "data": {
                "product_id": product_id, "already_claimed": True,
                "inventory_items": profile.get("inventory_items", {}),
                **_purchase_status_data(app.free_purchase_catalog, local),
            }}

        mark = int(catalog_row.get("mark", 0))
        first_purchase = mark > 0 and mark not in marks
        reward_rows = list(catalog_row.get("rewards", []))
        bonus_key = "first_give" if first_purchase else "not_first_give"
        reward_rows.extend(catalog_row.get(bonus_key, []))
        delivery: dict[int, int] = {}
        for row in reward_rows:
            item_id, quantity = int(row.get("item_id", 0)), int(row.get("num", 0))
            if item_id > 0 and quantity > 0:
                delivery[item_id] = delivery.get(item_id, 0) + quantity

        inventory = {str(int(key)): max(0, int(value))
                     for key, value in profile.get("inventory_items", {}).items()
                     if str(key).lstrip("-").isdigit() and int(value) > 0}
        for item_id, quantity in delivery.items():
            inventory[str(item_id)] = inventory.get(str(item_id), 0) + quantity
        profile["inventory_items"] = inventory

        if limit_key:
            limits[limit_key] = count + 1
        if mark > 0:
            marks.add(mark)
            local["recharge_marks"] = sorted(marks)
        if limit_key or mark > 0:
            # Keep a compact view of currently claimed limited entries for the client UI.
            local["limits"] = limits
        data = {
            "product_id": product_id,
            "granted_items": [{"item_id": item_id, "num": quantity}
                              for item_id, quantity in sorted(delivery.items())],
            "inventory_items": inventory,
            "mark": mark,
            "limit_type": limit_type,
            "limit_count": count + 1 if limit_key else None,
            "already_claimed": False,
            "replayed": False,
            **_purchase_status_data(app.free_purchase_catalog, local),
        }
        processed[nonce] = data
        if len(processed) > 512:
            for old_nonce in list(processed)[:-512]:
                processed.pop(old_nonce, None)
        profile["local_purchases"] = local
        _write_json_atomic(app.profile_state_file, profile)
        return 200, {"code": 0, "error": "", "data": data}


def _claimed_product_ids(catalog: dict[int, dict], limits: dict[str, int]) -> list[int]:
    claimed = []
    for product_id, row in catalog.items():
        limit_type = int(row.get("limit_type", 0))
        limit_times = int(row.get("limit_times", 0))
        if limit_type == 0 or limit_times <= 0:
            continue
        bucket = _utc_bucket(limit_type, datetime.now(timezone.utc))
        count = int(limits.get(f"{product_id}:{bucket}", 0))
        if count >= max(1, limit_times):
            claimed.append(product_id)
    return claimed


def _purchase_status_data(catalog: dict[int, dict], local: dict) -> dict:
    limits = local.get("limits", {}) if isinstance(local.get("limits", {}), dict) else {}
    recharge_counts = []
    for product_id, row in catalog.items():
        limit_type = int(row.get("limit_type", 0))
        bucket = _utc_bucket(limit_type, datetime.now(timezone.utc))
        if not bucket:
            continue
        count = int(limits.get(f"{product_id}:{bucket}", 0))
        if count:
            recharge_counts.append({"product_id": product_id, "limit_type": limit_type, "count": count})
    return {
        "claimed_product_ids": _claimed_product_ids(catalog, limits),
        "recharge_marks": local.get("recharge_marks", []),
        "recharge_counts": recharge_counts,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SpiralWarriorLocal/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Requests are written through the bounded JSONL logger below.
        return

    @property
    def app(self) -> argparse.Namespace:
        return getattr(self, "request_app", self.server.app)  # type: ignore[attr-defined]

    def _session_token(self) -> str | None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        token = query.get("local_session", [""])[-1]
        if token:
            return token
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    def _prepare_account(self, *, required: bool) -> dict | None:
        store = getattr(self.server.app, "account_store", None)  # type: ignore[attr-defined]
        if not store:
            return None
        account = store.resolve(self._session_token())
        if not account:
            if required:
                return None
            return None
        self.account = account
        user_app = copy.copy(self.server.app)  # type: ignore[attr-defined]
        user_app.player_id = account["player_id"]
        user_app.profile_state_file = store.profile_path(account["id"])
        user_app.mail_state_file = store.mail_path(account["id"])
        user_app.free_purchase_lock = store.lock_for(account["id"])
        self.request_app = user_app
        return account

    def _account_required(self) -> bool:
        if not getattr(self.server.app, "account_store", None):  # type: ignore[attr-defined]
            return True
        if self._prepare_account(required=True):
            return True
        body = b'{"ok":false,"error":"authentication_required","message":"Inicia sesion o crea una cuenta local."}'
        self.emit(401, "application/json; charset=utf-8", body, send_body=True)
        self.record(401, len(body))
        return False

    @staticmethod
    def _auth_game_token(account: dict, session_token: str, ws_url: str) -> bytes:
        def encoded(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        header = encoded(b'{"alg":"none","typ":"JWT"}')
        entry = ws_url.rstrip("/") + "/?session=" + quote(session_token, safe="")
        claims = json.dumps(
            {"code": 1, "playerid": int(account["player_id"]), "entry": entry},
            separators=(",", ":"),
        ).encode("utf-8")
        return f"{header}.{encoded(claims)}.local-test".encode("ascii")

    def emit(self, status: int, content_type: str, body: bytes, *, send_body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)

    def record(self, status: int, response_bytes: int, request_shape: dict | None = None) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        event = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "remote": self.client_address[0],
            "method": self.command,
            "path": parsed.path,
            "query": {key: query[key][-1] for key in ("area_id", "plt", "ver") if key in query},
            "status": status,
            "response_bytes": response_bytes,
        }
        if request_shape is not None:
            event["request_shape"] = request_shape
        line = json.dumps(event, ensure_ascii=False)
        print(line, flush=True)
        if self.app.log_file:
            with self.app.log_file.open("a", encoding="utf-8") as log:
                log.write(line + "\n")

    def do_GET(self) -> None:
        self.route(send_body=True)

    def do_HEAD(self) -> None:
        self.route(send_body=False)

    def route(self, *, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz" and self.command == "GET":
            body = json.dumps({
                "status": "ok", "scope": "local beta server",
                "accounts_enabled": bool(getattr(self.server.app, "account_store", None)),  # type: ignore[attr-defined]
            }).encode("utf-8")
            status, content_type = 200, "application/json; charset=utf-8"
        elif path == "/oversea/NoticeEng.txt":
            try:
                body = read_bytes(self.app.notice_file)
                status, content_type = 200, "text/plain; charset=utf-8"
            except OSError as exc:
                body = json.dumps({"error": "notice_file_unavailable", "detail": str(exc)}).encode()
                status, content_type = 500, "application/json; charset=utf-8"
        elif path == "/auth/es":
            store = getattr(self.server.app, "account_store", None)  # type: ignore[attr-defined]
            if store:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                session_token = query.get("local_session", [""])[-1]
                account = store.resolve(session_token)
                if not account:
                    body = b'{"error":"authentication_required"}'
                    status, content_type = 401, "application/json; charset=utf-8"
                else:
                    body = self._auth_game_token(account, session_token, self.app.ws_url)
                    status, content_type = 200, "text/plain; charset=utf-8"
            elif self.app.auth_response:
                try:
                    body = read_bytes(self.app.auth_response)
                    status, content_type = 200, self.app.auth_content_type
                except OSError as exc:
                    body = json.dumps({"error": "auth_response_file_unavailable", "detail": str(exc)}).encode()
                    status, content_type = 500, "application/json; charset=utf-8"
            else:
                body = b'{"error":"auth_response_not_configured","detail":"The /auth/es success schema is not known."}'
                status, content_type = 501, "application/json; charset=utf-8"
        elif path == "/local/free-purchase/status" and self.command == "GET":
            if not self._account_required():
                return
            try:
                with self.app.free_purchase_lock:
                    profile = _read_json(self.app.profile_state_file, {})
                    local = profile.get("local_purchases", {})
                    body = json.dumps({
                        "code": 0,
                        "data": _purchase_status_data(
                            self.app.free_purchase_catalog,
                            local if isinstance(local, dict) else {},
                        ),
                    }).encode("utf-8")
                status, content_type = 200, "application/json; charset=utf-8"
            except (OSError, TypeError, ValueError) as exc:
                body = json.dumps({"code": 500, "error": "local_purchase_state_error", "detail": str(exc)[:120]}).encode()
                status, content_type = 500, "application/json; charset=utf-8"
        elif path in ("/area/listV2", "/area/dataV2"):
            if self.app.area_response:
                try:
                    body = read_bytes(self.app.area_response)
                    status, content_type = 200, self.app.area_content_type
                except OSError as exc:
                    body = json.dumps({"error": "area_response_file_unavailable", "detail": str(exc)}).encode()
                    status, content_type = 500, "application/json; charset=utf-8"
            else:
                body = (
                    b'{"error":"area_response_not_configured",'
                    b'"detail":"The original /area/listV2 response schema is not known."}'
                )
                status, content_type = 501, "application/json; charset=utf-8"
        else:
            body = b'{"error":"not_found"}'
            status, content_type = 404, "application/json; charset=utf-8"
        self.emit(status, content_type, body, send_body=send_body)
        self.record(status, len(body))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        supported = {
            "/local/register",
            "/local/login",
            "/local/session",
            "/MailService.GetMailPage",
            "/MailService.DelMail",
            "/MailService.MarkRead",
            "/MailService.MarkReceived",
            "/RankingService.QueryRank",
            "/RankingService.QueryWithRank",
            "/RankingService.QueryWithScore",
            "/RankingService.QueryWithMember",
            "/local/free-purchase",
        }
        if path not in supported:
            body = b'{"error":"method_not_observed"}'
            self.emit(405, "application/json; charset=utf-8", body, send_body=True)
            self.record(405, len(body))
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0 or content_length > 1024 * 1024:
                raise ValueError("request body exceeds 1 MiB limit")
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            body = json.dumps({"code": 400, "error": "invalid_request", "data": None}).encode("utf-8")
            self.emit(400, "application/json; charset=utf-8", body, send_body=True)
            self.record(400, len(body), {"error": str(exc)[:120]})
            return

        request_shape = {
            "keys": sorted(payload.keys()),
            "types": {key: type(value).__name__ for key, value in payload.items()},
            "page": payload.get("page"),
            "page_num": payload.get("page_num"),
            "state": payload.get("state"),
            "ids_count": len(payload.get("ids", [])) if isinstance(payload.get("ids", []), list) else None,
            "rank": payload.get("rank") if isinstance(payload.get("rank"), str) else None,
            "ranks_count": len(payload.get("ranks", [])) if isinstance(payload.get("ranks", []), list) else None,
            "members_count": len(payload.get("members", [])) if isinstance(payload.get("members", []), list) else None,
            "member_present": isinstance(payload.get("member"), (str, int)),
        }
        if path in ("/local/register", "/local/login"):
            try:
                store = getattr(self.server.app, "account_store", None)  # type: ignore[attr-defined]
                if not store:
                    raise AccountError("accounts_disabled", "El backend no tiene cuentas habilitadas.", 503)
                identifier = payload.get("identifier", "")
                password = payload.get("password", "")
                if path == "/local/register":
                    token, account = store.register(identifier, password)
                else:
                    token, account = store.login(identifier, password)
                result = {"ok": True, "token": token, "account": account}
                body = json.dumps(result, separators=(",", ":")).encode("utf-8")
                self.emit(200, "application/json; charset=utf-8", body, send_body=True)
                self.record(200, len(body), {"keys": ["identifier", "password"], "operation": path.rsplit("/", 1)[-1]})
            except AccountError as exc:
                body = json.dumps({"ok": False, "error": exc.code, "message": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.emit(exc.status, "application/json; charset=utf-8", body, send_body=True)
                self.record(exc.status, len(body), {"operation": path.rsplit("/", 1)[-1], "error": exc.code})
            except (OSError, sqlite3.Error) as exc:
                body = b'{"ok":false,"error":"account_store_error","message":"No se pudo guardar la cuenta."}'
                self.emit(500, "application/json; charset=utf-8", body, send_body=True)
                self.record(500, len(body), {"operation": path.rsplit("/", 1)[-1], "error": type(exc).__name__})
            return
        if path == "/local/session":
            account = self._prepare_account(required=True)
            if not account:
                body = b'{"ok":false,"error":"session_expired"}'
                self.emit(401, "application/json; charset=utf-8", body, send_body=True)
                self.record(401, len(body), {"operation": "session"})
            else:
                body = json.dumps({"ok": True, "account": account}, separators=(",", ":")).encode("utf-8")
                self.emit(200, "application/json; charset=utf-8", body, send_body=True)
                self.record(200, len(body), {"operation": "session"})
            return
        if path.startswith("/local/") or path.startswith("/MailService.") or path.startswith("/RankingService."):
            if not self._account_required():
                return
        if path == "/local/free-purchase":
            try:
                product_id = int(payload.get("product_id", 0))
                nonce = str(payload.get("nonce", ""))
                if product_id <= 0 or not nonce or len(nonce) > 96:
                    raise ValueError("product_id and a bounded purchase nonce are required")
                status, result = claim_local_free_purchase(self.app, product_id, nonce)
                body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.emit(status, "application/json; charset=utf-8", body, send_body=True)
                self.record(status, len(body), {
                    "product_id": product_id,
                    "catalogued": product_id in self.app.free_purchase_catalog,
                    "nonce_present": True,
                })
            except (OSError, TypeError, ValueError, KeyError) as exc:
                body = json.dumps({"code": 400, "error": "invalid_local_purchase", "data": None}).encode("utf-8")
                self.emit(400, "application/json; charset=utf-8", body, send_body=True)
                self.record(400, len(body), {"error": type(exc).__name__})
            return
        try:
            if path == "/MailService.GetMailPage":
                state_filter = int(payload.get("state", -1))
                page = max(1, int(payload.get("page", 1)))
                page_num = min(99, max(1, int(payload.get("page_num", 99))))
                with self.app.free_purchase_lock:
                    all_mails = visible_mails(state_filter=state_filter, path=self.app.mail_state_file)
                start = (page - 1) * page_num
                data = {"total": len(all_mails), "mails": all_mails[start:start + page_num]}
            elif path == "/MailService.DelMail":
                ids = payload.get("ids", [])
                if not isinstance(ids, list):
                    raise ValueError("ids must be a JSON array")
                with self.app.free_purchase_lock:
                    changed = update_mail_state(ids, delete=True, path=self.app.mail_state_file)
                data = {"deleted": changed}
            elif path == "/MailService.MarkRead":
                ids = payload.get("ids", [])
                if not isinstance(ids, list):
                    raise ValueError("ids must be a JSON array")
                with self.app.free_purchase_lock:
                    changed = update_mail_state(ids, state_value=MAIL_STATE_READ, path=self.app.mail_state_file)
                data = {"updated": changed}
            elif path == "/MailService.MarkReceived":
                ids = payload.get("ids", [])
                if not isinstance(ids, list):
                    raise ValueError("ids must be a JSON array")
                with self.app.free_purchase_lock:
                    changed = update_mail_state(ids, state_value=MAIL_STATE_RECEIVED, path=self.app.mail_state_file)
                data = {"updated": changed}
            elif path == "/RankingService.QueryWithScore":
                rank_name = payload.get("rank")
                if not isinstance(rank_name, str):
                    raise ValueError("rank must be a string")
                data = {"datas": local_champion_score_rows(
                    rank_name, int(self.app.player_id), Path(self.app.profile_state_file),
                )}
            elif path == "/RankingService.QueryWithMember":
                rank_name = payload.get("rank", "")
                members = payload.get("members", [])
                if not isinstance(rank_name, str) or not isinstance(members, list):
                    raise ValueError("rank and members have invalid types")
                if TOP_CHAMPION_RANK_NAME.search(rank_name):
                    board = local_top_champion_members(
                        rank_name, int(self.app.player_id),
                        Path(self.app.profile_state_file), 1, CHAMPION_RANK_ROWS,
                    )["members"]
                    rank_by_member = {str(row["member"]): row for row in board}
                    data = {"datas": [dict(rank_by_member[str(member)])
                                      for member in members
                                      if str(member) in rank_by_member]}
                else:
                    data = {"datas": []}
            elif path == "/RankingService.QueryRank":
                page = max(1, int(payload.get("page", 1)))
                page_num = min(100, max(1, int(payload.get("page_num", 15))))
                member = str(payload.get("member", self.app.player_id))
                rank_name = payload.get("rank", "")
                if isinstance(rank_name, str) and TOP_CHAMPION_RANK_NAME.search(rank_name):
                    data = local_top_champion_members(
                        rank_name, int(self.app.player_id),
                        Path(self.app.profile_state_file), page, page_num,
                    )
                else:
                    # Preserve the existing generic test-profile behavior for
                    # unrelated ranking services.
                    data = {
                        "members": [{"member": str(self.app.player_id), "score": "10", "metadata": "", "rank": 1}],
                        "total_page": 1 if page == 1 else 0,
                        "member_score": "10" if member == str(self.app.player_id) else "0",
                        "member_rank": 1 if member == str(self.app.player_id) else 0,
                    }
            else:  # /RankingService.QueryWithRank
                ranks = payload.get("ranks", [1])
                if not isinstance(ranks, list):
                    raise ValueError("ranks must be a JSON array")
                data = {
                    "datas": [
                        {"rank": int(rank), "member": str(self.app.player_id), "score": "10", "metadata": ""}
                        for rank in ranks[:15]
                    ]
                }
            body = json.dumps({"code": 0, "error": "", "data": data}, ensure_ascii=False).encode("utf-8")
            self.emit(200, "application/json; charset=utf-8", body, send_body=True)
            self.record(200, len(body), request_shape)
        except (OSError, TypeError, ValueError) as exc:
            body = json.dumps({"code": 500, "error": "local_mail_state_error", "data": None}).encode("utf-8")
            self.emit(500, "application/json; charset=utf-8", body, send_body=True)
            self.record(500, len(body), {**request_shape, "error": type(exc).__name__})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 only with --allow-lan.")
    parser.add_argument("--allow-lan", action="store_true", help="Allow binding beyond loopback for an explicitly LAN-scoped test.")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--notice-file", type=Path, default=DEFAULT_NOTICE)
    parser.add_argument("--area-response", type=Path, help="Raw response file for GET /area/listV2; omitted means HTTP 501.")
    parser.add_argument("--area-content-type", default="application/json; charset=utf-8")
    parser.add_argument("--auth-response", type=Path, help="Candidate JSON response file for GET /auth/es; omitted means HTTP 501.")
    parser.add_argument("--auth-content-type", default="application/json; charset=utf-8")
    parser.add_argument("--mail-state-file", type=Path, default=DEFAULT_STATE_FILE,
                        help="Shared JSON state for the single synthetic local mail item.")
    parser.add_argument("--profile-state-file", type=Path, default=DEFAULT_PROFILE_STATE,
                        help="Shared state file used by the local WebSocket profile.")
    parser.add_argument("--accounts-db", type=Path,
                        help="Enable username/email accounts and isolated profiles in this SQLite database.")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:18766",
                        help="Loopback WebSocket URL placed in account login tokens.")
    parser.add_argument("--free-purchase-catalog", type=Path,
                        help="Optional local product and reward catalog extracted from a client copy.")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args()
    args.notice_file = args.notice_file.resolve()
    args.area_response = args.area_response.resolve() if args.area_response else None
    args.auth_response = args.auth_response.resolve() if args.auth_response else None
    args.mail_state_file = args.mail_state_file.resolve()
    args.profile_state_file = args.profile_state_file.resolve()
    args.accounts_db = args.accounts_db.resolve() if args.accounts_db else None
    args.log_file = args.log_file.resolve() if args.log_file else None

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_lan:
        parser.error("Non-loopback binding requires --allow-lan.")
    if not args.notice_file.is_file():
        parser.error(f"notice file does not exist: {args.notice_file}")

    catalog_path = (args.free_purchase_catalog or DEFAULT_FREE_PURCHASE_CATALOG).resolve()
    if not catalog_path.is_file():
        if args.free_purchase_catalog is not None:
            parser.error(f"free purchase catalog does not exist: {catalog_path}")
        print(
            "WARNING: Optional local free-purchase catalog is missing at "
            f"{catalog_path}. Purchase claims will return unknown_local_product "
            "until a catalog is supplied.",
            flush=True,
        )
        catalog_data = {}
    else:
        catalog_data = _read_json(catalog_path, {})
    args.free_purchase_catalog = {
        int(row["id"]): row for row in catalog_data.get("products", [])
        if isinstance(row, dict) and int(row.get("id", 0)) > 0
    }
    args.free_purchase_lock = threading.RLock()
    args.account_store = AccountStore(args.accounts_db) if args.accounts_db else None
    args.ws_url = args.ws_url

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.app = args  # type: ignore[attr-defined]
    print(f"Listening on http://{args.host}:{args.port}; accounts={'enabled' if args.account_store else 'disabled'}; routes: /healthz, /local/register, /local/login, /local/session, /area/listV2, /area/dataV2, /auth/es, /oversea/NoticeEng.txt, /RankingService.QueryRank, /RankingService.QueryWithRank", flush=True)
    if args.area_response:
        print(f"/area/listV2 loads response bytes from {args.area_response} ({args.area_content_type}).", flush=True)
    else:
        print("/area/listV2 returns HTTP 501 until --area-response is supplied.", flush=True)
    if args.account_store:
        print("/auth/es validates the account session and returns a synthetic per-account game token.", flush=True)
    elif args.auth_response:
        print(f"/auth/es loads candidate response bytes from {args.auth_response} ({args.auth_content_type}).", flush=True)
    else:
        print("/auth/es returns HTTP 501 until --auth-response is supplied.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

