"""Web 层：FastAPI app + SSE + REST（Phase 9/10 精简版）。

对外暴露 create_app()——FastAPI 应用工厂。路由：
    GET  /api/sessions                  列历史 session
    GET  /api/sessions/{id}/events      读历史事件（刷新后重建视图用）
    POST /api/sessions                  起新 session + 跑任务（流式 SSE 响应）
    GET  /api/health                    健康检查
    POST /api/sessions/{id}/approve     审批决策（V1 seam：auto-approve 为主）

设计原则（spec 11 §4）：
- SSE 只是传输 surface，**不持有 Runtime 状态**。
- 前端直接消费 AgentEvent 流，不在后端做投影（不变量 #22）。
- 断连时清理 generator，不泄漏 producer task。
"""

from agent_harness.web.app import create_app

__all__ = ["create_app"]
