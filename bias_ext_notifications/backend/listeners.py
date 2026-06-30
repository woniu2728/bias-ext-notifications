def get_runtime_user_by_id(*args, **kwargs):
    from bias_core.extensions.runtime import get_runtime_user_by_id as runtime_get_user_by_id

    return runtime_get_user_by_id(*args, **kwargs)


def handle_post_created_direct_reply_notification(event) -> None:
    if not event.is_approved:
        return

    from bias_ext_notifications.backend.services import NotificationService

    if not event.reply_to_post_id:
        return

    from_user = _resolve_user_or_none(event.actor_user_id)
    if from_user is None:
        return

    NotificationService.notify_post_reply_from_event(event, from_user)


def handle_post_hidden_direct_reply_notification_cleanup(event) -> None:
    if not getattr(event, "is_hidden", False):
        return

    from bias_ext_notifications.backend.services import NotificationService

    NotificationService.delete_post_reply_for_post(event.post_id)


def handle_post_deleted_direct_reply_notification_cleanup(event) -> None:
    from bias_ext_notifications.backend.services import NotificationService

    NotificationService.delete_post_reply_for_post(event.post_id)


def handle_notification_created_delivery(event) -> None:
    from bias_ext_notifications.backend.runtime import dispatch_runtime_notification_batch

    dispatch_runtime_notification_batch(getattr(event, "notification_ids", ()))


def handle_user_suspended_notification(event) -> None:
    from bias_ext_notifications.backend.services import NotificationService

    user = _resolve_user_or_none(event.user_id)
    if user is None:
        return

    admin_user = None
    if event.actor_user_id:
        admin_user = _resolve_user_or_none(event.actor_user_id)

    NotificationService.notify_user_suspended(user, admin_user)


def handle_user_unsuspended_notification(event) -> None:
    from bias_ext_notifications.backend.services import NotificationService

    user = _resolve_user_or_none(event.user_id)
    if user is None:
        return

    admin_user = None
    if event.actor_user_id:
        admin_user = _resolve_user_or_none(event.actor_user_id)

    NotificationService.notify_user_unsuspended(user, admin_user)


def _resolve_user_or_none(user_id: int):
    try:
        return get_runtime_user_by_id(user_id)
    except Exception:
        return None
