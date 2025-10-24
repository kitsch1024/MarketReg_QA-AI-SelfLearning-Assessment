"""SessionState 的 JSON 持久化工具。

应用仅持久化会话字段的子集。本模块负责 Python 类型与 JSON 字典的互转
（例如 set <-> list、dataclass <-> dict）。
"""

import json
from typing import Any
from .models import SessionState, AnswerRecord, ReviewEntry


def load_state(path: str) -> SessionState:
    """从 JSON 文件加载 SessionState。

    若文件缺失或格式错误，返回默认初始化的状态。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return SessionState(ability=3.0, answers_by_item={}, answered_items=set(), review_schedule={}, kp_mastery={})

    answers_by_item = {}
    for k, v in (raw.get("answers_by_item") or {}).items():
        answers_by_item[k] = AnswerRecord(is_correct=v.get("is_correct"), ts_ms=int(v.get("ts_ms", 0)))

    review_schedule = {}
    for k, v in (raw.get("review_schedule") or {}).items():
        review_schedule[k] = ReviewEntry(bucket=int(v.get("bucket", 0)), next_ts_ms=int(v.get("next_ts_ms", 0)))

    answered_items = set(raw.get("answered_items") or [])
    kp_mastery = {k: int(v) for k, v in (raw.get("kp_mastery") or {}).items()}

    return SessionState(
        ability=float(raw.get("ability", 3.0)),
        answers_by_item=answers_by_item,
        answered_items=answered_items,
        review_schedule=review_schedule,
        kp_mastery=kp_mastery,
    )


def save_state(path: str, state: SessionState) -> None:
    """将 SessionState 保存到 JSON 文件（UTF-8，带缩进）。"""
    data: dict[str, Any] = {
        "ability": state.ability,
        "answers_by_item": {k: {"is_correct": v.is_correct, "ts_ms": v.ts_ms} for k, v in state.answers_by_item.items()},
        "answered_items": list(state.answered_items),
        "review_schedule": {k: {"bucket": v.bucket, "next_ts_ms": v.next_ts_ms} for k, v in state.review_schedule.items()},
        "kp_mastery": state.kp_mastery,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


