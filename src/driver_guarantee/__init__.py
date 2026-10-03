"""司机履约与权益保障服务。

在综合交通协同基础能力之上，构建驾驶员工时台账、派单原子校验、
合同与结算规则版本化、扣款证据、争议托管、申诉时限与账期复算等能力。
"""

from .service import GuaranteeService

__all__ = ["GuaranteeService"]
