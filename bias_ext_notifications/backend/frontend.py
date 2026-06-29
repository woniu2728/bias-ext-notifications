from __future__ import annotations

from bias_core.extensions import FrontendExtender


def frontend_extender():
    return FrontendExtender(
        forum_entry="extensions/notifications/frontend/forum/index.js",
    ).route(
        "/notifications",
        "notifications",
        "./NotificationView.vue",
        title="通知",
        description="查看你的论坛通知、回复提醒和系统消息。",
        order=40,
        requires_auth=True,
    )
