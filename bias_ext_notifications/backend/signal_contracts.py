from __future__ import annotations

from django.db.models.signals import post_delete, post_save

from bias_ext_notifications.backend.models import Notification
from bias_ext_notifications.backend.signals import (
    invalidate_unread_count_on_delete,
    invalidate_unread_count_on_save,
)


def signal_connections():
    return (
        {
            "signal": post_save,
            "receiver": invalidate_unread_count_on_save,
            "sender": Notification,
            "dispatch_uid": "notifications.invalidate_unread_count_on_save",
            "description": "通知写入后清除用户未读数缓存。",
        },
        {
            "signal": post_delete,
            "receiver": invalidate_unread_count_on_delete,
            "sender": Notification,
            "dispatch_uid": "notifications.invalidate_unread_count_on_delete",
            "description": "通知删除后清除用户未读数缓存。",
        },
    )
