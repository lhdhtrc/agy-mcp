"""额度与模型选型：读 /quota、后台刷新、快用完时告警、model=auto 按余量挑模型。

额度报告由 CLI 自身回答（不起 turn、不扣额度），结果缓存在内存；刷新放在后台线程，
因此告警与选型都不会给轮次增加延迟。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core.agy import run_agy
from core.config import (
    METADATA_TIMEOUT_SEC,  # noqa: F401
    DEFAULT_MODEL_ID,
    MODEL_PREFERENCE,
    QUOTA_REFRESH_SEC,
    QUOTA_WARN_PERCENT,
    _env_float,
    log,
)
from core.protocol import join_streams, parse_json_output


QUOTA_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}


QUOTA_CACHE_TTL = _env_float("AGY_MCP_QUOTA_CACHE_SEC", 60.0)


MODELS_CACHE: Dict[str, Any] = {"ts": 0.0, "output": None, "code": 1}


MODELS_CACHE_TTL = _env_float("AGY_MCP_MODELS_CACHE_SEC", 300.0)


_QUOTA_REFRESHING = threading.Event()


_QUOTA_WARNED_AT = 0.0


EFFORT_LEVELS = ("low", "medium", "high")


def cached_models() -> Tuple[int, str]:
    """`agy models` 每次都要一次网络往返（约 4 秒），所以缓存给 status 之类的调用用。"""
    if MODELS_CACHE.get("output") is not None and (
        time.time() - float(MODELS_CACHE.get("ts") or 0)
    ) < MODELS_CACHE_TTL:
        return int(MODELS_CACHE.get("code") or 0), str(MODELS_CACHE["output"])
    code, out, err = run_agy(["models"], timeout=METADATA_TIMEOUT_SEC)
    MODELS_CACHE.update({"ts": time.time(), "output": (out or err).strip(), "code": code})
    return code, str(MODELS_CACHE["output"])


def parse_models(text: str) -> List[Dict[str, str]]:
    """`agy models` 每行输出一条 `id<TAB>标签` 记录。"""
    models: List[Dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        identifier, _, label = line.partition("\t")
        if identifier.strip():
            models.append({"id": identifier.strip(), "label": label.strip()})
    return models


def model_group(model_id: str) -> str:
    """这个模型 id 属于哪个额度组。"""
    lowered = model_id.lower()
    if lowered.startswith("claude") or "gpt" in lowered:
        return "Claude and GPT models"
    return "Gemini Models"


def model_for_effort(model_id: str, effort: str) -> Optional[str]:
    """模型 id 里带着思考强度（如 `...-high`）；返回同系列里 `effort` 档位的那个 id。"""
    for suffix in EFFORT_LEVELS:
        if model_id.endswith("-" + suffix):
            return model_id[: -len(suffix)] + effort
    return None


def reconcile_model_and_effort(
    model_id: Optional[str], effort: Optional[str], model_was_explicit: bool
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """CLI 不接受 `--model <x>-high` 配 `--effort low`，所以两者必须保持一致。

    返回 (模型, 强度, 说明)。id 里带强度的会被改写成请求的档位；不带强度的保留模型、
    放弃 effort 并给出说明。
    """
    if not effort:
        return model_id, None, []
    effort = str(effort).strip().lower()
    if effort not in EFFORT_LEVELS:
        return model_id, None, [f"ignored unknown effort {effort!r} (expected low|medium|high)"]
    if not model_id:
        return None, effort, []

    mapped = model_for_effort(model_id, effort)
    if mapped is None:
        return model_id, None, [
            f"{model_id} does not encode a reasoning effort, so effort={effort} was ignored"
        ]
    if mapped == model_id:
        return model_id, effort, []
    notes = [
        f"effort={effort}: using {mapped} instead of {model_id}"
        + ("" if model_was_explicit else " (default model)")
    ]
    return mapped, effort, notes


def resolve_model(requested: Any) -> Tuple[Optional[str], List[str]]:
    """解析 `model` 参数：具体 id、`auto`（按额度余量挑）或默认值。"""
    requested_text = str(requested).strip() if requested else ""
    if not requested_text:
        return (DEFAULT_MODEL_ID or None), []
    if requested_text.lower() != "auto":
        return requested_text, []

    available = [model["id"] for model in parse_models(cached_models()[1])]
    if not available:
        return (DEFAULT_MODEL_ID or None), ["auto: could not list models, used the default"]
    headroom = quota_headroom()
    candidates = [model for model in MODEL_PREFERENCE if model in available] or available

    def left(model_id: str) -> float:
        return headroom.get(model_group(model_id), 100.0)

    roomy = [model for model in candidates if left(model) > QUOTA_WARN_PERCENT]
    chosen = roomy[0] if roomy else max(candidates, key=left)
    reason = f"auto: {chosen}"
    if headroom:
        reason += f" (headroom {left(chosen):.0f}% in {model_group(chosen)})"
    else:
        reason += " (quota unknown)"
    return chosen, [reason]


def read_quota() -> Dict[str, Any]:
    """`-p "/quota"` 由 CLI 自己回答：不起轮次、不扣额度、不产生会话。"""
    if QUOTA_CACHE.get("payload") and (time.time() - float(QUOTA_CACHE.get("ts") or 0)) < QUOTA_CACHE_TTL:
        return QUOTA_CACHE["payload"]
    code, out, err = run_agy(["-p", "/quota", "--output-format", "json"], timeout=90)
    payload = parse_json_output(out)
    if payload:
        QUOTA_CACHE["ts"] = time.time()
        QUOTA_CACHE["payload"] = payload
    else:
        payload = {"status": "ERROR", "error": join_streams(code, out, err)}
    return payload


def summarize_quota(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 CLI 的额度报告压平成 [{group, buckets:[{id,name,remaining_percent}]}]。"""
    command = payload.get("command")
    groups: List[Dict[str, Any]] = []
    if isinstance(command, dict):
        data = command.get("data")
        if isinstance(data, dict) and isinstance(data.get("groups"), list):
            for group in data["groups"]:
                if not isinstance(group, dict):
                    continue
                buckets = []
                for bucket in group.get("buckets") or []:
                    if not isinstance(bucket, dict):
                        continue
                    fraction = bucket.get("remaining_fraction")
                    buckets.append(
                        {
                            "id": bucket.get("id"),
                            "name": bucket.get("name"),
                            "remaining_percent": round(float(fraction) * 100, 1)
                            if isinstance(fraction, (int, float))
                            else None,
                            "reset_time": bucket.get("reset_time"),
                        }
                    )
                groups.append({"group": group.get("name"), "buckets": buckets})
    return groups


def quota_headroom() -> Dict[str, float]:
    """每个模型组最紧的那个窗口（5 小时 / 周）还剩多少百分比。"""
    payload = QUOTA_CACHE.get("payload")
    headroom: Dict[str, float] = {}
    if not payload:
        return headroom
    for group in summarize_quota(payload):
        values = [
            bucket["remaining_percent"]
            for bucket in group.get("buckets", [])
            if isinstance(bucket.get("remaining_percent"), (int, float))
        ]
        if values:
            headroom[str(group.get("group"))] = min(values)
    return headroom


def refresh_quota_in_background() -> None:
    """在后台把额度缓存焐热，这样告警永远不给轮次增加延迟。"""
    age = time.time() - float(QUOTA_CACHE.get("ts") or 0)
    if QUOTA_CACHE.get("payload") and age < QUOTA_REFRESH_SEC:
        return
    if _QUOTA_REFRESHING.is_set():
        return

    def run() -> None:
        _QUOTA_REFRESHING.set()
        try:
            read_quota()
        except Exception as exc:  # noqa: BLE001 - a background refresh must stay quiet
            log(f"background quota refresh failed: {exc!r}")
        finally:
            _QUOTA_REFRESHING.clear()

    threading.Thread(target=run, daemon=True).start()


def quota_warning() -> Optional[str]:
    """某个组的 5 小时或周窗口快用完时提醒一下（带冷却，不会每次都唠叨）。"""
    global _QUOTA_WARNED_AT
    if QUOTA_WARN_PERCENT <= 0:
        return None
    headroom = quota_headroom()
    if not headroom:
        return None
    low = {group: left for group, left in headroom.items() if left <= QUOTA_WARN_PERCENT}
    if not low:
        return None
    if time.time() - _QUOTA_WARNED_AT < QUOTA_REFRESH_SEC:
        return None
    _QUOTA_WARNED_AT = time.time()
    detail = ", ".join(f"{group} {left:.0f}%" for group, left in sorted(low.items()))
    return f"quota is nearly used up (5-hour or weekly window): {detail}"
