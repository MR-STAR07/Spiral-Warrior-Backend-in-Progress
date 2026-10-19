"""State for the single synthetic Mail item used by the local game clone."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "state" / "mail_state.json"
TEST_MAIL_ID = "9000001"
MAIL_STATE_NEW = 0
MAIL_STATE_READ = 1
MAIL_STATE_RECEIVED = 2
MAIL_STATE_DELETED = 3
MAIL_TYPE_SHOW = 3
# The client clock in the no-backend profile starts at 2018-01-01 00:00 China
# time (1514736000 UTC seconds); anchoring this test mail there avoids a false
# multi-year future timestamp against that synthetic clock.
MAIL_TIME_GAME_EPOCH = 1514736000


def default_mail_state() -> dict[str, Any]:
    return {
        "mails": [
            {
                "id": TEST_MAIL_ID,
                "title": "Preservation test mail",
                "body": "This message is served by the local Spiral Warrior test backend.",
                "from": "Local Test Server",
                "state": MAIL_STATE_NEW,
                "time": MAIL_TIME_GAME_EPOCH,
                "type": MAIL_TYPE_SHOW,
                "attachment": "[]",
                "deleted": False,
            }
        ]
    }


def load_mail_state(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_STATE_FILE
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_mail_state()
    if not isinstance(value, dict) or not isinstance(value.get("mails"), list):
        raise ValueError(f"invalid synthetic mail state file: {target}")
    return value


def save_mail_state(state: dict[str, Any], path: Path | str | None = None) -> None:
    target = Path(path) if path is not None else DEFAULT_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def update_mail_state(
    ids: list[str],
    *,
    state_value: int | None = None,
    delete: bool = False,
    path: Path | str | None = None,
) -> int:
    state = load_mail_state(path)
    wanted = {str(value) for value in ids}
    changed = 0
    for mail in state["mails"]:
        if str(mail.get("id")) not in wanted:
            continue
        if delete:
            mail["state"] = MAIL_STATE_DELETED
            mail["deleted"] = True
        elif state_value is not None:
            mail["state"] = int(state_value)
        else:
            continue
        changed += 1
    save_mail_state(state, path)
    return changed


def visible_mails(*, state_filter: int = -1, path: Path | str | None = None) -> list[dict[str, Any]]:
    state = load_mail_state(path)
    result = []
    for mail in state["mails"]:
        if mail.get("deleted") or int(mail.get("state", MAIL_STATE_NEW)) == MAIL_STATE_DELETED:
            continue
        if state_filter not in (-1, None) and int(mail.get("state", MAIL_STATE_NEW)) != int(state_filter):
            continue
        result.append({key: value for key, value in mail.items() if key != "deleted"})
    return result
