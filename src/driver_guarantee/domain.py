"""工时区段类别、事件类别与状态枚举。"""

from __future__ import annotations

# 工时日志事件类别（可控时钟记录）
LOG_EVENTS = frozenset({
    "driving",        # 驾驶
    "loading",        # 装卸
    "waiting",        # 等待
    "rest",           # 场地休息
    "berth_rest",     # 卧铺休息（双驾随车）
    "shift_change",   # 双驾换班
    "rescue",         # 途中救援
    "cancellation",   # 任务取消
    "backfill",       # 补录
    "freeze",         # 台账冻结
})

# 会计入工时的区段
WORK_KINDS = frozenset({"driving", "loading", "waiting", "rescue"})

# 计为有效休息的区段
REST_KINDS = frozenset({"rest", "berth_rest"})

# 只能影响尚未冻结区段的事后事件
MUTATING_EVENTS = frozenset({"shift_change", "rescue", "cancellation", "backfill"})

# 任务状态
TRIP_OPEN = "open"
TRIP_DISPATCHED = "dispatched"
TRIP_IN_PROGRESS = "in_progress"
TRIP_COMPLETED = "completed"
TRIP_CANCELLED = "cancelled"

# 账期状态
PERIOD_OPEN = "open"
PERIOD_CLOSED = "closed"

# 结算行状态
LINE_PAYABLE = "payable"
LINE_HELD = "held"
LINE_RELEASED = "released"
LINE_VOID = "void"

# 申诉状态
APPEAL_OPEN = "open"
APPEAL_UPHELD = "upheld"
APPEAL_REJECTED = "rejected"
APPEAL_WITHDRAWN = "withdrawn"

# 结算行类别
LINE_FREIGHT = "freight"
LINE_SUBSIDY = "subsidy"
LINE_DEDUCTION = "deduction"

# 访问角色
ROLE_DRIVER = "driver"
ROLE_CARRIER = "carrier"
ROLE_REGULATOR = "regulator"
ROLE_ADMIN = "admin"
ROLES = frozenset({ROLE_DRIVER, ROLE_CARRIER, ROLE_REGULATOR, ROLE_ADMIN})
