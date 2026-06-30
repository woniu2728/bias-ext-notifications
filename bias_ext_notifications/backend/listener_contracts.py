from __future__ import annotations

from bias_core.extensions import ExtensionEventListenerDefinition

from bias_ext_notifications.backend.listeners import (
    handle_notification_created_delivery,
    handle_post_created_direct_reply_notification,
    handle_post_deleted_direct_reply_notification_cleanup,
    handle_post_hidden_direct_reply_notification_cleanup,
    handle_user_suspended_notification,
    handle_user_unsuspended_notification,
)


def notification_event_listener_definitions():
    return (
        ExtensionEventListenerDefinition(
            event_type="notifications.notification.created",
            handler=handle_notification_created_delivery,
            description="通知创建后派发实时与邮件投递批次。",
        ),
        ExtensionEventListenerDefinition(
            event_type="users.user.suspended",
            handler=handle_user_suspended_notification,
            description="账号封禁后通知用户。",
        ),
        ExtensionEventListenerDefinition(
            event_type="users.user.unsuspended",
            handler=handle_user_unsuspended_notification,
            description="账号解除封禁后通知用户。",
        ),
    )


def post_notification_event_listener_definitions():
    return (
        ExtensionEventListenerDefinition(
            event_type="posts.post.created",
            handler=handle_post_created_direct_reply_notification,
            description="回复发布后通知被回复楼层作者。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.hidden",
            handler=handle_post_hidden_direct_reply_notification_cleanup,
            description="回复隐藏后同步清理直接回复通知。",
        ),
        ExtensionEventListenerDefinition(
            event_type="posts.post.deleted",
            handler=handle_post_deleted_direct_reply_notification_cleanup,
            description="回复删除后同步清理直接回复通知。",
        ),
    )
