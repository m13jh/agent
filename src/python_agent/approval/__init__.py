"""高风险工具的审批服务。

该包只提供审批请求模型和服务协议；是否弹窗、通过 UI 操作还是自动拒绝，由应用层
注入具体实现，避免审批逻辑耦合到工具业务代码。
"""

from python_agent.approval.service import (
    ApprovalRequest,
    ApprovalService,
    CallbackApprovalService,
    DenyApprovalService,
)

__all__ = [
    "ApprovalRequest",
    "ApprovalService",
    "CallbackApprovalService",
    "DenyApprovalService",
]
