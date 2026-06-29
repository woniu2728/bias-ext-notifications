from __future__ import annotations

from bias_core.extensions import NotificationsExtender


def notification_extender():
    return (
        NotificationsExtender()
        .type(
            "userSuspended",
            label="账号封禁通知",
            description="通知用户账号已被管理员封禁。",
            icon="fas fa-user-lock",
            navigation_scope="profile",
            preference_key="notify_account_status",
            preference_label="账号状态通知",
            preference_description="当你的账号被封禁或解除封禁时通知你。",
        )
        .type(
            "userUnsuspended",
            label="账号解除封禁",
            description="通知用户账号已恢复正常。",
            icon="fas fa-user-check",
            navigation_scope="profile",
            preference_key="notify_account_status",
            preference_label="账号状态通知",
            preference_description="当你的账号被封禁或解除封禁时通知你。",
        )
    )


def post_notification_extender():
    return (
        NotificationsExtender()
        .type(
            "postReply",
            label="回复被回应",
            description="通知被回复的楼层作者。",
            icon="fas fa-comment-dots",
            navigation_scope="post",
            preference_key="notify_post_reply",
            preference_label="回复被回应通知",
            preference_description="当其他用户直接回复你的某条帖子时通知你。",
        )
    )
