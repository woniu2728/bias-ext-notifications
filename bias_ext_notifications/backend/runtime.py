from __future__ import annotations

from typing import Any

from bias_core.extensions.notifications import NotificationBlueprint


def delete_runtime_discussion_reply_notifications_for_post(*args, **kwargs):
    from bias_core.extensions.runtime import (
        delete_runtime_discussion_reply_notifications_for_post as runtime_delete_discussion_reply_notifications_for_post,
    )

    return runtime_delete_discussion_reply_notifications_for_post(*args, **kwargs)


def delete_runtime_notifications(*args, **kwargs):
    from bias_core.extensions.runtime import delete_runtime_notifications as runtime_delete_notifications

    return runtime_delete_notifications(*args, **kwargs)


def get_runtime_notification_model(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_notification_model as runtime_get_notification_model

    return runtime_get_notification_model(*args, **kwargs)


def get_runtime_notification_service(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_notification_service as runtime_get_notification_service

    return runtime_get_notification_service(*args, **kwargs)


def notify_runtime_notification(*args, **kwargs):
    from bias_core.extensions.runtime import notify_runtime_notification as runtime_notify_notification

    return runtime_notify_notification(*args, **kwargs)


def _runtime_service_method(service: Any, name: str):
    if isinstance(service, dict):
        method = service.get(name)
    else:
        method = getattr(service, name, None)
    if not callable(method):
        raise RuntimeError(f"Notifications 扩展运行时服务缺少方法: {name}")
    return method


def notification_service_provider() -> dict:
    from bias_ext_notifications.backend.models import Notification
    from bias_ext_notifications.backend.services import NotificationService

    return {
        "model": Notification,
        "event_types": notification_event_type_aliases(),
        "notify_discussion_reply": NotificationService.notify_discussion_reply,
        "notify_discussion_approved": NotificationService.notify_discussion_approved,
        "notify_discussion_approved_from_event": NotificationService.notify_discussion_approved_from_event,
        "notify_discussion_rejected": NotificationService.notify_discussion_rejected,
        "notify_discussion_rejected_from_event": NotificationService.notify_discussion_rejected_from_event,
        "notify_post_approved": NotificationService.notify_post_approved,
        "notify_post_approved_from_event": NotificationService.notify_post_approved_from_event,
        "notify_post_rejected": NotificationService.notify_post_rejected,
        "notify_post_rejected_from_event": NotificationService.notify_post_rejected_from_event,
        "notify_post_reply_from_event": NotificationService.notify_post_reply_from_event,
        "notify_post_liked": NotificationService.notify_post_liked,
        "notify_post_liked_from_event": NotificationService.notify_post_liked_from_event,
        "delete_post_liked_for_post_user": NotificationService.delete_post_liked_for_post_user,
        "notify_user_mentioned": NotificationService.notify_user_mentioned,
        "notify_user_mentioned_from_event": NotificationService.notify_user_mentioned_from_event,
        "create_from_blueprint": NotificationService.create_from_blueprint,
        "sync_notifications": NotificationService.sync_notifications,
        "delete_matching_notifications": NotificationService.delete_matching_notifications,
        "delete_post_reply_for_post": NotificationService.delete_post_reply_for_post,
        "delete_user_mentioned_for_post": NotificationService.delete_user_mentioned_for_post,
        "dispatch_batch": dispatch_notification_batch,
        "send_batch": _send_notification_batch_now,
        "load_realtime_notifications": NotificationService.load_notifications_for_realtime,
        "serialize_realtime_notification": serialize_realtime_notification,
        "delete_discussion_reply_for_post": _delete_discussion_reply_for_post,
    }


def notification_event_type_aliases() -> dict[str, type]:
    from bias_ext_notifications.backend.events import NotificationCreatedEvent

    return {
        "notifications.notification.created": NotificationCreatedEvent,
    }


notification_service_provider.event_types = notification_event_type_aliases


def get_notification_service(default: Any = None):
    return get_runtime_notification_service(default)


def get_notification_model():
    return get_runtime_notification_model()


def notify(method_name: str, *args, **kwargs):
    return notify_runtime_notification(method_name, *args, **kwargs)


def dispatch_runtime_notification_batch(notification_ids) -> Any:
    return notify("dispatch_batch", list(notification_ids or ()))


def delete_discussion_reply_notifications_for_post(post_id: int) -> int:
    return delete_runtime_discussion_reply_notifications_for_post(post_id)


def dispatch_notification_batch(notification_ids):
    from bias_core.extensions.platform import QueueService
    from bias_ext_notifications.backend.tasks import dispatch_notification_batch as dispatch_task

    normalized_ids = [int(item) for item in notification_ids or () if item]
    if not normalized_ids:
        return None

    return QueueService.dispatch_celery_task(
        dispatch_task,
        normalized_ids,
        fallback=lambda: _send_notification_batch_now(normalized_ids),
    )


def _send_notification_batch_now(notification_ids):
    return deliver_notification_batch(notification_ids or [])


def deliver_notification_batch(notification_ids):
    from bias_ext_notifications.backend.services import NotificationService
    from bias_ext_notifications.backend.mail import send_notification_batch_email

    loaded = NotificationService.load_notifications_for_realtime(notification_ids or [])
    sent_email_ids = send_notification_batch_email([notification.id for notification in loaded])
    return {
        "notification_ids": [notification.id for notification in loaded],
        "email_notification_ids": sent_email_ids,
        "realtime_count": len(loaded),
        "email_count": len(sent_email_ids),
    }


def serialize_realtime_notification(notification) -> dict:
    from bias_core.extensions.runtime import get_runtime_resource_registry

    user = getattr(notification, "from_user", None)

    return {
        "id": notification.id,
        "type": notification.type,
        "from_user": get_runtime_resource_registry().serialize("user_summary", user, {}) if user else None,
        "data": notification.data,
        "is_read": notification.is_read,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
    }


def _delete_discussion_reply_for_post(post_id: int) -> int:
    from bias_ext_notifications.backend.services import NotificationService

    return delete_runtime_notifications(
        blueprint=NotificationBlueprint(
            type=NotificationService.TYPE_DISCUSSION_REPLY,
            match_data={"post_id": post_id},
        ),
    )
