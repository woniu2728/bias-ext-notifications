from __future__ import annotations

from html import escape
from typing import Any

from bias_core.extensions.platform import EmailService, get_frontend_url

from bias_ext_notifications.backend.models import Notification


TYPE_LABELS = {
    "discussionReply": "讨论有新回复",
    "postLiked": "帖子被点赞",
    "userMentioned": "你被提及",
    "postReply": "你的回复收到回应",
    "discussionApproved": "讨论已通过审核",
    "discussionRejected": "讨论未通过审核",
    "postApproved": "帖子已通过审核",
    "postRejected": "帖子未通过审核",
    "userSuspended": "账号已被封禁",
    "userUnsuspended": "账号已恢复",
}


def send_notification_batch_email(notification_ids) -> list[int]:
    notifications = _load_notifications(notification_ids)
    sent_ids: list[int] = []
    for notification in notifications:
        recipient_email = str(getattr(notification.user, "email", "") or "").strip()
        if not recipient_email:
            continue

        message = build_notification_email(notification)
        if EmailService.send_email(
            subject=message["subject"],
            text_content=message["text_content"],
            html_content=message["html_content"],
            to_email=recipient_email,
            source="notifications.notification_batch",
        ):
            sent_ids.append(notification.id)
    return sent_ids


def build_notification_email(notification: Notification) -> dict[str, str]:
    site_name = EmailService.get_site_name()
    label = TYPE_LABELS.get(notification.type, "你有一条新通知")
    actor = _actor_name(notification)
    data = dict(notification.data or {})
    title = _subject_title(data)
    url = _notification_url(data)
    summary = _notification_summary(notification.type, actor=actor, title=title, data=data)
    subject = f"[{site_name}] {label}"

    text_lines = [
        summary,
        "",
        f"查看通知：{url}",
    ]
    html_content = (
        f"<p>{escape(summary)}</p>"
        f'<p><a href="{escape(url, quote=True)}">查看通知</a></p>'
    )
    return {
        "subject": subject,
        "text_content": "\n".join(text_lines),
        "html_content": html_content,
    }


def _load_notifications(notification_ids) -> list[Notification]:
    normalized_ids = [int(item) for item in notification_ids or () if item]
    if not normalized_ids:
        return []
    notifications = list(
        Notification.objects.filter(id__in=normalized_ids, is_deleted=False)
        .select_related("user", "from_user")
    )
    notification_map = {notification.id: notification for notification in notifications}
    return [
        notification_map[notification_id]
        for notification_id in normalized_ids
        if notification_id in notification_map
    ]


def _actor_name(notification: Notification) -> str:
    from_user = getattr(notification, "from_user", None)
    return str(
        getattr(from_user, "display_name", "")
        or getattr(from_user, "username", "")
        or "系统"
    ).strip()


def _subject_title(data: dict[str, Any]) -> str:
    return str(data.get("discussion_title") or data.get("title") or "").strip()


def _notification_url(data: dict[str, Any]) -> str:
    frontend_url = get_frontend_url()
    discussion_id = data.get("discussion_id")
    post_number = data.get("post_number")
    if discussion_id:
        suffix = f"/d/{discussion_id}"
        if post_number:
            suffix = f"{suffix}/{post_number}"
        return f"{frontend_url}{suffix}"
    return f"{frontend_url}/notifications"


def _notification_summary(type_code: str, *, actor: str, title: str, data: dict[str, Any]) -> str:
    title_text = f"《{title}》" if title else "相关内容"
    if type_code == "discussionReply":
        return f"{actor} 回复了讨论 {title_text}。"
    if type_code == "postReply":
        return f"{actor} 回复了你在 {title_text} 中的帖子。"
    if type_code == "postLiked":
        return f"{actor} 赞了你在 {title_text} 中的帖子。"
    if type_code == "userMentioned":
        return f"{actor} 在 {title_text} 中提到了你。"
    if type_code == "discussionApproved":
        return f"你的讨论 {title_text} 已通过审核。"
    if type_code == "discussionRejected":
        note = str(data.get("approval_note") or "").strip()
        return f"你的讨论 {title_text} 未通过审核。{note}".strip()
    if type_code == "postApproved":
        return f"你在 {title_text} 中的帖子已通过审核。"
    if type_code == "postRejected":
        note = str(data.get("approval_note") or "").strip()
        return f"你在 {title_text} 中的帖子未通过审核。{note}".strip()
    if type_code == "userSuspended":
        message = str(data.get("suspend_message") or data.get("suspend_reason") or "").strip()
        return f"你的账号已被封禁。{message}".strip()
    if type_code == "userUnsuspended":
        return "你的账号已恢复正常。"
    return f"{actor} 给你发送了一条新通知。"
