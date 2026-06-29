from __future__ import annotations

from bias_ext_notifications.backend.models import Notification


def owned_models():
    return (
        (
            Notification,
            "通知记录由 notifications 扩展拥有。",
        ),
    )
