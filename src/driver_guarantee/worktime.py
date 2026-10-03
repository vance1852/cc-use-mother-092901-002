"""工时与休息窗口规则引擎（纯函数，便于测试与复算）。

所有时间均为带时区的 ISO8601 UTC 字符串（以 Z 结尾）。
规则参数来自可版本化的 RuleSet.limits，单位均为分钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .domain import REST_KINDS, WORK_KINDS


WINDOW_24H = 24 * 60
DEFAULT_LIMITS: dict[str, int] = {
    "max_continuous_driving_min": 240,      # 连续驾驶上限 4 小时
    "min_rest_after_drive_min": 20,         # 连续驾驶后的最短休息
    "max_driving_24h_min": 480,             # 24 小时累计驾驶 8 小时
    "max_work_24h_min": 780,                # 24 小时累计工时 13 小时
}


def parse_ts(value: str) -> datetime:
    """解析服务统一格式的 UTC 时间字符串。"""

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    """格式化为以 Z 结尾的 UTC 字符串。"""

    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def merge_limits(limits: dict[str, int] | None) -> dict[str, int]:
    merged = dict(DEFAULT_LIMITS)
    if limits:
        merged.update({k: int(v) for k, v in limits.items()})
    return merged


def add_minutes(value: str, minutes: int) -> str:
    return format_ts(parse_ts(value) + timedelta(minutes=minutes))


def _duration_min(seg: dict[str, Any]) -> int:
    return int(round((parse_ts(seg["end_at"]) - parse_ts(seg["start_at"])).total_seconds() / 60))


def _overlap(seg: dict[str, Any], window_start: datetime, window_end: datetime) -> int:
    s = max(parse_ts(seg["start_at"]), window_start)
    e = min(parse_ts(seg["end_at"]), window_end)
    return max(0, int(round((e - s).total_seconds() / 60)))


def _segments_ending_before(segments: Iterable[dict[str, Any]], point: datetime) -> list[dict[str, Any]]:
    return [g for g in segments if parse_ts(g["end_at"]) <= point]


def totals_in_window(segments: Iterable[dict[str, Any]], at: str, window_min: int,
                     kinds: frozenset[str]) -> int:
    """计算截止 at 的过去 window_min 分钟内，指定类别区段的重叠分钟数。"""

    point = parse_ts(at)
    window_start = point - timedelta(minutes=window_min)
    return sum(_overlap(g, window_start, point) for g in segments if g["kind"] in kinds)


def continuous_driving(segments: Iterable[dict[str, Any]], at: str,
                       required_rest_min: int, rest_kinds: frozenset[str] = REST_KINDS) -> int:
    """从 at 向前回溯未被达标休息打断的连续驾驶累计分钟数。

    - 达标休息（时长不少于 required_rest_min 的休息类区段）打断连续链；
    - 装卸、等待等其他工作不打断驾驶累计；
    - 时间轴上出现无记录空隙时连续链终止（缺乏连续驾驶证据）。
    """

    point = parse_ts(at)
    cursor = point
    total = 0
    ordered = sorted(_segments_ending_before(segments, point),
                     key=lambda g: parse_ts(g["end_at"]), reverse=True)
    for seg in ordered:
        s = parse_ts(seg["start_at"])
        e = parse_ts(seg["end_at"])
        if e < cursor:
            break  # 无记录空隙，连续链终止
        if seg["kind"] in rest_kinds:
            if _duration_min(seg) >= required_rest_min:
                break
            cursor = s
            continue
        if seg["kind"] == "driving":
            total += int(round((min(e, cursor) - s).total_seconds() / 60))
        cursor = s
    return total


def evaluate_availability(segments: list[dict[str, Any]], at: str,
                          limits: dict[str, int] | None = None,
                          trip_driving_min: int = 0, trip_work_min: int | None = None,
                          rest_kinds: frozenset[str] = REST_KINDS,
                          work_kinds: frozenset[str] = WORK_KINDS) -> dict[str, Any]:
    """在给定时刻原子评估司机能否承接新任务。

    同时校验剩余工时与“任务结束后能否落实后续休息窗口”，
    返回判定原因、连续/累计工时和休息窗口边界，供 service 层决策。
    """

    lim = merge_limits(limits)
    trip_work_min = trip_work_min if trip_work_min is not None else trip_driving_min
    point = parse_ts(at)

    driving_24h = totals_in_window(segments, at, WINDOW_24H, frozenset({"driving"}))
    work_24h = totals_in_window(segments, at, WINDOW_24H, work_kinds)
    continuous = continuous_driving(segments, at, lim["min_rest_after_drive_min"], rest_kinds)

    rests: list[dict[str, Any]] = []
    day_start = point - timedelta(minutes=WINDOW_24H)
    for seg in sorted(_segments_ending_before(segments, point), key=lambda g: g["start_at"]):
        if seg["kind"] in rest_kinds and parse_ts(seg["end_at"]) > day_start:
            rests.append({"kind": seg["kind"], "start_at": seg["start_at"],
                          "end_at": seg["end_at"], "duration_min": _duration_min(seg)})

    reasons: list[str] = []

    # 1. 连续驾驶已达上限，必须先完成达标休息
    if continuous >= lim["max_continuous_driving_min"]:
        reasons.append("continuous_driving_limit_reached")
    elif continuous + trip_driving_min > lim["max_continuous_driving_min"]:
        # 剩余连续驾驶余量不足以完成本趟，途中必然超时
        reasons.append("continuous_driving_capacity_would_exceed")

    # 2. 接下本趟后 24 小时累计驾驶将超限
    if driving_24h + trip_driving_min > lim["max_driving_24h_min"]:
        reasons.append("driving_24h_limit_would_exceed")

    # 3. 接下本趟后 24 小时累计工时将超限
    if work_24h + trip_work_min > lim["max_work_24h_min"]:
        reasons.append("work_24h_limit_would_exceed")

    # 4. 后续休息窗口：任务结束后必须能在不突破 24 小时工时上限的前提下
    #    完成最短休息。窗口起点 = 任务结束，终点 = 结束 + 最短休息。
    window_start = point + timedelta(minutes=trip_work_min)
    window_end = window_start + timedelta(minutes=lim["min_rest_after_drive_min"])
    pre_work = sum(_overlap(g, window_end - timedelta(minutes=WINDOW_24H), point)
                   for g in segments if g["kind"] in work_kinds)
    if pre_work + trip_work_min > lim["max_work_24h_min"]:
        reasons.append("rest_window_not_feasible")

    return {
        "at": at,
        "eligible": not reasons,
        "reasons": reasons,
        "window_start_at": format_ts(window_start),
        "window_end_at": format_ts(window_end),
        "continuous_driving_min": continuous,
        "driving_last_24h_min": driving_24h,
        "work_last_24h_min": work_24h,
        "rests_last_24h": rests,
    }
