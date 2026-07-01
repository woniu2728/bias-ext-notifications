"""
通知系统业务逻辑层
"""
from typing import Any, Optional, List, Tuple
from django.db.models import Q, Count
from django.core.cache import cache
from django.utils import timezone
from bias_core.extensions.notifications import NotificationBlueprint
from bias_core.extensions.platform import dispatch_forum_event_after_commit
from bias_ext_notifications.backend.events import NotificationCreatedEvent
from bias_ext_notifications.backend.models import Notification


def _get_runtime_service(service_key: str, default=None):
    from bias_core.extensions.runtime import get_runtime_service

    return get_runtime_service(service_key, default)


def _runtime_service_method(service_key: str, name: str):
    service = _get_runtime_service(service_key)
    if isinstance(service, dict):
        method = service.get(name)
    else:
        method = getattr(service, name, None)
    if not callable(method):
        raise RuntimeError(f"Notifications 扩展运行时服务缺少方法: {service_key}.{name}")
    return method


def _get_user_by_id(user_id: int):
    return _runtime_service_method("users.service", "get_by_id")(user_id)


def _get_user_preference(user, key: str, *, fallback=False):
    get_preference = _runtime_service_method("users.service", "get_preference")
    try:
        return get_preference(user, key, fallback=fallback)
    except TypeError:
        value = get_preference(user, key)
        return fallback if value is None else value


def _discussion_reply_context(discussion_id: int, post_id: int, from_user: Any):
    return _runtime_service_method("content.discussions", "reply_notification_context")(
        discussion_id,
        post_id,
        from_user,
    )


def _post_reply_context(reply_to_post_id: int, post_id: int, from_user: Any):
    return _runtime_service_method("content.posts", "reply_notification_context")(
        reply_to_post_id,
        post_id,
        from_user,
    )


def _post_context(post_id: int):
    return _runtime_service_method("content.posts", "notification_context")(post_id)


UNREAD_COUNT_CACHE_KEY = "notifications.unread_count.{user_id}"
UNREAD_COUNT_CACHE_TIMEOUT = 60 * 5


class NotificationService:
    """通知服务"""

    # 通知类型常量
    TYPE_DISCUSSION_REPLY = 'discussionReply'
    TYPE_POST_LIKED = 'postLiked'
    TYPE_USER_MENTIONED = 'userMentioned'
    TYPE_POST_REPLY = 'postReply'
    TYPE_DISCUSSION_APPROVED = 'discussionApproved'
    TYPE_DISCUSSION_REJECTED = 'discussionRejected'
    TYPE_POST_APPROVED = 'postApproved'
    TYPE_POST_REJECTED = 'postRejected'
    TYPE_USER_SUSPENDED = 'userSuspended'
    TYPE_USER_UNSUSPENDED = 'userUnsuspended'

    @staticmethod
    def is_notification_enabled(user: Any | None, type_code: str) -> bool:
        if not user:
            return False

        from bias_core.extensions.platform import get_forum_registry

        definition = get_forum_registry().get_notification_type(type_code)
        if not definition or not definition.preference_key:
            return True

        return _get_user_preference(
            user,
            definition.preference_key,
            fallback=definition.preference_default_enabled,
        )

    @staticmethod
    def _unread_count_cache_key(user_id: int) -> str:
        return UNREAD_COUNT_CACHE_KEY.format(user_id=user_id)

    @staticmethod
    def invalidate_unread_count(user_id: int) -> None:
        if not user_id:
            return
        try:
            cache.delete(NotificationService._unread_count_cache_key(user_id))
        except Exception:
            return None

    @staticmethod
    def invalidate_unread_counts(user_ids: List[int]) -> None:
        for user_id in set(user_ids or []):
            NotificationService.invalidate_unread_count(user_id)

    @staticmethod
    def create_notification(
        user: Any,
        type: str,
        from_user: Optional[Any] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[int] = None,
        data: Optional[dict] = None,
        allow_merge: bool = True,
    ) -> Notification:
        """
        创建通知

        Args:
            user: 接收通知的用户
            type: 通知类型
            from_user: 触发通知的用户
            subject_type: 主体类型
            subject_id: 主体ID
            data: 额外数据

        Returns:
            Notification: 创建的通知对象
        """
        # 不给自己发通知
        if from_user and from_user.id == user.id:
            return None

        if not NotificationService.is_notification_enabled(user, type):
            return None

        # 检查是否已存在相同通知（防止重复）
        existing = None
        if allow_merge:
            existing = Notification.objects.filter(
                user=user,
                type=type,
                subject_type=subject_type,
                subject_id=subject_id,
                is_read=False,
                is_deleted=False,
            ).first()

        if existing:
            # 更新现有通知
            existing.from_user = from_user
            existing.data = data or {}
            existing.created_at = timezone.now()
            existing.save()
            return existing

        notification = Notification.objects.create(
            user=user,
            from_user=from_user,
            type=type,
            subject_type=subject_type,
            subject_id=subject_id,
            data=data or {},
        )

        NotificationService.invalidate_unread_count(user.id)
        NotificationService._dispatch_notifications_after_commit([notification.id])

        return notification

    @staticmethod
    def create_from_blueprint(
        *,
        blueprint: NotificationBlueprint,
        recipient: Any,
        allow_merge: bool = True,
    ) -> Notification:
        return NotificationService.create_notification(
            user=recipient,
            type=blueprint.type,
            from_user=blueprint.from_user,
            subject_type=blueprint.subject_type,
            subject_id=blueprint.subject_id,
            data=dict(blueprint.data or {}),
            allow_merge=allow_merge,
        )

    @staticmethod
    def create_notifications_bulk(notifications: List[Notification]) -> List[Notification]:
        if not notifications:
            return []

        created = Notification.objects.bulk_create(notifications)
        NotificationService.invalidate_unread_counts([item.user_id for item in created])
        NotificationService._dispatch_notifications_after_commit([item.id for item in created if item.id])
        return created

    @staticmethod
    def sync_notifications(
        *,
        recipients: List[Any],
        blueprint: NotificationBlueprint | None = None,
        type: str | None = None,
        from_user: Optional[Any] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[int] = None,
        data: Optional[dict] = None,
        match_data: Optional[dict] = None,
    ) -> dict:
        blueprint = NotificationService._resolve_blueprint(
            blueprint,
            type=type,
            from_user=from_user,
            subject_type=subject_type,
            subject_id=subject_id,
            data=data,
            match_data=match_data,
        )
        payload = dict(blueprint.data or {})
        recipient_map = {}
        from_user_id = getattr(blueprint.from_user, "id", None)
        for recipient in recipients or []:
            recipient_id = getattr(recipient, "id", None)
            if not recipient_id or recipient_id == from_user_id:
                continue
            if not NotificationService.is_notification_enabled(recipient, blueprint.type):
                continue
            recipient_map[int(recipient_id)] = recipient

        queryset = Notification.objects.filter(
            type=blueprint.type,
            subject_type=blueprint.subject_type,
            subject_id=blueprint.subject_id,
        )
        for key, value in dict(blueprint.match_data or {}).items():
            queryset = queryset.filter(**{f"data__{key}": value})

        existing = list(queryset.select_related("user"))
        existing_by_user_id = {item.user_id: item for item in existing}
        recipient_ids = set(recipient_map)
        visible_existing_ids = {
            item.user_id
            for item in existing
            if not item.is_deleted
        }

        to_delete_ids = [
            item.id
            for item in existing
            if item.user_id not in recipient_ids and not item.is_deleted
        ]
        if to_delete_ids:
            Notification.objects.filter(id__in=to_delete_ids).update(is_deleted=True)

        restored = []
        touched_user_ids = [item.user_id for item in existing if item.id in to_delete_ids]
        for user_id, recipient in recipient_map.items():
            existing_notification = existing_by_user_id.get(user_id)
            if existing_notification is None:
                continue

            update_fields = []
            if existing_notification.is_deleted:
                existing_notification.is_deleted = False
                update_fields.append("is_deleted")
            if existing_notification.from_user_id != from_user_id:
                existing_notification.from_user = blueprint.from_user
                update_fields.append("from_user")
            if existing_notification.data != payload:
                existing_notification.data = payload
                update_fields.append("data")
            if update_fields:
                existing_notification.save(update_fields=update_fields)
                restored.append(existing_notification)
                touched_user_ids.append(user_id)

        new_notifications = [
            Notification(
                user=recipient,
                from_user=blueprint.from_user,
                type=blueprint.type,
                subject_type=blueprint.subject_type,
                subject_id=blueprint.subject_id,
                data=payload,
            )
            for user_id, recipient in recipient_map.items()
            if user_id not in existing_by_user_id
        ]
        created = NotificationService.create_notifications_bulk(new_notifications)

        changed_user_ids = set(touched_user_ids)
        changed_user_ids.update(item.user_id for item in created)
        NotificationService.invalidate_unread_counts(list(changed_user_ids))

        return {
            "created": created,
            "restored": restored,
            "deleted_count": len(to_delete_ids),
            "visible_recipient_ids_before": visible_existing_ids,
            "visible_recipient_ids_after": recipient_ids,
        }

    @staticmethod
    def delete_matching_notifications(
        *,
        blueprint: NotificationBlueprint | None = None,
        type: str | None = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[int] = None,
        match_data: Optional[dict] = None,
    ) -> int:
        blueprint = NotificationService._resolve_blueprint(
            blueprint,
            type=type,
            subject_type=subject_type,
            subject_id=subject_id,
            match_data=match_data,
        )
        queryset = Notification.objects.filter(
            type=blueprint.type,
            is_deleted=False,
        )
        if blueprint.subject_type is not None:
            queryset = queryset.filter(subject_type=blueprint.subject_type)
        if blueprint.subject_id is not None:
            queryset = queryset.filter(subject_id=blueprint.subject_id)
        for key, value in dict(blueprint.match_data or {}).items():
            queryset = queryset.filter(**{f"data__{key}": value})

        user_ids = list(queryset.values_list("user_id", flat=True).distinct())
        count = queryset.update(is_deleted=True)
        if count:
            NotificationService.invalidate_unread_counts(user_ids)
        return count

    @staticmethod
    def _resolve_blueprint(
        blueprint: NotificationBlueprint | None = None,
        *,
        type: str | None = None,
        from_user: Optional[Any] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[int] = None,
        data: Optional[dict] = None,
        match_data: Optional[dict] = None,
    ) -> NotificationBlueprint:
        if blueprint is not None:
            return blueprint
        if not type:
            raise ValueError("通知同步需要 type 或 NotificationBlueprint")
        return NotificationBlueprint(
            type=type,
            from_user=from_user,
            subject_type=subject_type,
            subject_id=subject_id,
            data=dict(data or {}),
            match_data=dict(match_data or {}),
        )

    @staticmethod
    def _dispatch_notifications_after_commit(notification_ids: List[int]):
        if not notification_ids:
            return

        dispatch_forum_event_after_commit(
            NotificationCreatedEvent(notification_ids=tuple(int(item) for item in notification_ids if item)),
        )

    @staticmethod
    def _get_post_reply_notification_context(reply_to_post_id: int, post_id: int, from_user: Any):
        try:
            return _post_reply_context(reply_to_post_id, post_id, from_user)
        except Exception:
            return None

    @staticmethod
    def _get_post_notification_context(post_id: int):
        try:
            return _post_context(post_id)
        except Exception:
            return None

    @staticmethod
    def _collect_type_counts(queryset) -> dict:
        return {
            item["type"]: item["count"]
            for item in queryset.values("type").annotate(count=Count("id"))
        }

    @staticmethod
    def _build_filtered_queryset(
        user: Any,
        is_read: Optional[bool] = None,
        type: Optional[str] = None,
        discussion_id: Optional[int] = None,
    ):
        queryset = Notification.objects.filter(user=user, is_deleted=False)

        if is_read is not None:
            queryset = queryset.filter(is_read=is_read)

        if type:
            queryset = queryset.filter(type=type)

        if discussion_id is not None:
            queryset = queryset.filter(
                Q(subject_type='discussion', subject_id=discussion_id)
                | Q(data__discussion_id=discussion_id)
            )

        return queryset

    @staticmethod
    def get_notification_list(
        user: Any,
        is_read: Optional[bool] = None,
        type: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
        preload=None,
    ) -> Tuple[List[Notification], int, int, dict, dict]:
        """
        获取通知列表

        Args:
            user: 用户
            is_read: 是否已读（None表示全部）
            type: 通知类型
            page: 页码
            limit: 每页数量

        Returns:
            Tuple[List[Notification], int, int, dict, dict]: (通知列表, 总数, 未读数, 各类型总数, 各类型未读数)
        """
        base_queryset = Notification.objects.filter(user=user, is_deleted=False)
        queryset = NotificationService._build_filtered_queryset(
            user=user,
            is_read=is_read,
            type=type,
        )
        if preload is not None:
            queryset = preload(queryset)

        type_counts = NotificationService._collect_type_counts(base_queryset)
        unread_type_counts = NotificationService._collect_type_counts(base_queryset.filter(is_read=False))

        # 排序
        queryset = queryset.order_by('-created_at', '-id')

        # 统计
        total = queryset.count()
        unread_count = NotificationService.get_unread_count(user)

        # 分页
        offset = (page - 1) * limit
        notifications = list(queryset[offset:offset + limit])

        return notifications, total, unread_count, type_counts, unread_type_counts

    @staticmethod
    def get_notification_by_id(notification_id: int, user: Any, preload=None) -> Optional[Notification]:
        """
        获取通知详情

        Args:
            notification_id: 通知ID
            user: 用户（用于权限检查）

        Returns:
            Optional[Notification]: 通知对象
        """
        try:
            queryset = Notification.objects.filter(user=user, is_deleted=False)
            if preload is not None:
                queryset = preload(queryset)
            notification = queryset.get(id=notification_id)
            return notification
        except Notification.DoesNotExist:
            return None

    @staticmethod
    def mark_as_read(notification_id: int, user: Any) -> bool:
        """
        标记通知为已读

        Args:
            notification_id: 通知ID
            user: 用户

        Returns:
            bool: 是否成功
        """
        try:
            notification = Notification.objects.get(id=notification_id, user=user, is_deleted=False)
            was_unread = not notification.is_read
            notification.mark_as_read()
            if was_unread:
                NotificationService.invalidate_unread_count(user.id)
            return True
        except Notification.DoesNotExist:
            return False

    @staticmethod
    def mark_all_as_read(user: Any) -> int:
        """
        标记所有通知为已读

        Args:
            user: 用户

        Returns:
            int: 标记的数量
        """
        count, _ = NotificationService.mark_filtered_as_read(user)
        return count

    @staticmethod
    def mark_filtered_as_read(
        user: Any,
        type: Optional[str] = None,
        discussion_id: Optional[int] = None,
    ) -> Tuple[int, dict]:
        queryset = NotificationService._build_filtered_queryset(
            user=user,
            is_read=False,
            type=type,
            discussion_id=discussion_id,
        )
        type_counts = NotificationService._collect_type_counts(queryset)

        if not type_counts:
            return 0, {}

        count = queryset.update(
            is_read=True,
            read_at=timezone.now()
        )
        if count:
            NotificationService.invalidate_unread_count(user.id)
        return count, type_counts

    @staticmethod
    def delete_notification(notification_id: int, user: Any) -> bool:
        """
        删除通知

        Args:
            notification_id: 通知ID
            user: 用户

        Returns:
            bool: 是否成功
        """
        try:
            notification = Notification.objects.get(id=notification_id, user=user, is_deleted=False)
            was_unread = not notification.is_read
            notification.is_deleted = True
            notification.save(update_fields=["is_deleted"])
            if was_unread:
                NotificationService.invalidate_unread_count(user.id)
            return True
        except Notification.DoesNotExist:
            return False

    @staticmethod
    def delete_all_read(user: Any) -> int:
        """
        删除所有已读通知

        Args:
            user: 用户

        Returns:
            int: 删除的数量
        """
        count, _ = NotificationService.delete_filtered_read(user)
        return count

    @staticmethod
    def delete_filtered_read(
        user: Any,
        type: Optional[str] = None,
        discussion_id: Optional[int] = None,
    ) -> Tuple[int, dict]:
        queryset = NotificationService._build_filtered_queryset(
            user=user,
            is_read=True,
            type=type,
            discussion_id=discussion_id,
        )
        type_counts = NotificationService._collect_type_counts(queryset)

        if not type_counts:
            return 0, {}

        count = queryset.update(is_deleted=True)
        if count:
            NotificationService.invalidate_unread_count(user.id)
        return count, type_counts

    @staticmethod
    def get_unread_count(user: Any) -> int:
        """
        获取未读通知数量

        Args:
            user: 用户

        Returns:
            int: 未读数量
        """
        cache_key = NotificationService._unread_count_cache_key(user.id)
        try:
            cached = cache.get(cache_key)
        except Exception:
            cached = None

        if cached is not None:
            return int(cached)

        unread_count = Notification.objects.filter(user=user, is_read=False, is_deleted=False).count()
        try:
            cache.set(cache_key, unread_count, UNREAD_COUNT_CACHE_TIMEOUT)
        except Exception:
            pass
        return unread_count

    @staticmethod
    def get_stats(user: Any) -> dict:
        """
        获取通知统计

        Args:
            user: 用户

        Returns:
            dict: 统计数据
        """
        total = Notification.objects.filter(user=user, is_deleted=False).count()
        unread_count = NotificationService.get_unread_count(user)
        read_count = total - unread_count

        return {
            'total': total,
            'unread_count': unread_count,
            'read_count': read_count,
        }

    @staticmethod
    def notify_discussion_reply(discussion_id: int, post_id: int, from_user: Any):
        """
        通知讨论有新回复

        Args:
            discussion_id: 讨论ID
            post_id: 帖子ID
            from_user: 回复者
        """
        context = _discussion_reply_context(discussion_id, post_id, from_user)
        if not context:
            return

        payload = dict(context.get("payload") or {})
        recipients = []
        discussion_author = context.get("discussion_author")
        if discussion_author and discussion_author.id != getattr(from_user, "id", None):
            recipients.append(discussion_author)

        for subscriber in context.get("subscribers") or ():
            recipients.append(subscriber)

        NotificationService.sync_notifications(
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_DISCUSSION_REPLY,
                from_user=from_user,
                subject_type='discussion',
                subject_id=discussion_id,
                data=payload,
                match_data={"post_id": post_id},
            ),
            recipients=recipients,
        )

    @staticmethod
    def notify_post_reply(reply_to_post_id: int, post_id: int, from_user: Any):
        """
        通知某条帖子被回复

        Args:
            reply_to_post_id: 被回复帖子ID
            post_id: 新回复帖子ID
            from_user: 回复者
        """
        context = NotificationService._get_post_reply_notification_context(reply_to_post_id, post_id, from_user)
        if not context:
            return

        NotificationService.create_from_blueprint(
            recipient=context["recipient"],
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_REPLY,
                from_user=from_user,
                subject_type='post',
                subject_id=reply_to_post_id,
                data=dict(context.get("payload") or {}),
            ),
            allow_merge=False,
        )

    @staticmethod
    def notify_post_reply_from_event(event: Any, from_user: Any):
        recipient_id = int(getattr(event, "reply_to_post_user_id", 0) or 0)
        from_user_id = int(getattr(from_user, "id", 0) or 0)
        discussion_user_id = int(getattr(event, "discussion_user_id", 0) or 0)
        if not recipient_id or recipient_id in {from_user_id, discussion_user_id}:
            return None

        try:
            recipient = _get_user_by_id(recipient_id)
        except Exception:
            return None
        if recipient is None:
            return None

        reply_to_post_id = int(getattr(event, "reply_to_post_id", 0) or 0)
        payload = {
            "post_id": int(getattr(event, "post_id", 0) or 0),
            "post_number": getattr(event, "post_number", None),
            "discussion_id": getattr(event, "discussion_id", None),
            "discussion_title": getattr(event, "discussion_title", "") or "",
            "reply_to_post_id": reply_to_post_id,
            "reply_to_post_number": getattr(event, "reply_to_post_number", None),
        }
        return NotificationService.create_from_blueprint(
            recipient=recipient,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_REPLY,
                from_user=from_user,
                subject_type='post',
                subject_id=reply_to_post_id,
                data=payload,
            ),
            allow_merge=False,
        )

    @staticmethod
    def delete_post_reply_for_post(post_id: int) -> int:
        return NotificationService.delete_matching_notifications(
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_REPLY,
                match_data={"post_id": post_id},
            ),
        )

    @staticmethod
    def notify_post_liked(post_id: int, from_user: Any):
        """
        通知帖子被点赞

        Args:
            post_id: 帖子ID
            from_user: 点赞者
        """
        context = NotificationService._get_post_notification_context(post_id)
        if not context:
            return

        author = context.get("author")
        if author and author.id != getattr(from_user, "id", None):
            payload = dict(context.get("payload") or {})
            payload["from_user_id"] = getattr(from_user, "id", None)
            NotificationService.sync_notifications(
                blueprint=NotificationBlueprint(
                    type=NotificationService.TYPE_POST_LIKED,
                    from_user=from_user,
                    subject_type='post',
                    subject_id=post_id,
                    data=payload,
                    match_data={"post_id": post_id, "from_user_id": getattr(from_user, "id", None)},
                ),
                recipients=[author],
            )

    @staticmethod
    def notify_post_liked_from_event(event: Any, from_user: Any):
        post_user_id = int(getattr(event, "post_user_id", 0) or 0)
        from_user_id = int(getattr(from_user, "id", 0) or 0)
        if not post_user_id or post_user_id == from_user_id:
            return None

        try:
            author = _get_user_by_id(post_user_id)
        except Exception:
            return None
        if author is None:
            return None

        post_id = int(getattr(event, "post_id", 0) or 0)
        payload = {
            "post_id": post_id,
            "post_number": getattr(event, "post_number", None),
            "discussion_id": getattr(event, "discussion_id", None),
            "discussion_title": getattr(event, "discussion_title", "") or "",
            "from_user_id": from_user_id,
        }
        return NotificationService.sync_notifications(
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_LIKED,
                from_user=from_user,
                subject_type='post',
                subject_id=post_id,
                data=payload,
                match_data={"post_id": post_id, "from_user_id": from_user_id},
            ),
            recipients=[author],
        )

    @staticmethod
    def delete_post_liked_for_post_user(post_id: int, from_user: Any) -> int:
        from_user_id = getattr(from_user, "id", None)
        deleted_count = NotificationService.delete_matching_notifications(
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_LIKED,
                subject_type='post',
                subject_id=post_id,
                match_data={"post_id": post_id, "from_user_id": from_user_id},
            ),
        )
        legacy_count = Notification.objects.filter(
            type=NotificationService.TYPE_POST_LIKED,
            subject_type='post',
            subject_id=post_id,
            from_user_id=from_user_id,
            is_deleted=False,
        ).exclude(data__has_key="from_user_id").update(is_deleted=True)
        if legacy_count:
            user_ids = list(Notification.objects.filter(
                type=NotificationService.TYPE_POST_LIKED,
                subject_type='post',
                subject_id=post_id,
                from_user_id=from_user_id,
            ).values_list("user_id", flat=True).distinct())
            NotificationService.invalidate_unread_counts(user_ids)
        return int(deleted_count or 0) + int(legacy_count or 0)

    @staticmethod
    def notify_user_mentioned(post_id: int, mentioned_user: Any, from_user: Any):
        """
        通知用户被@提及

        Args:
            post_id: 帖子ID
            mentioned_user: 被提及的用户
            from_user: 提及者
        """
        context = NotificationService._get_post_notification_context(post_id)
        if not context:
            return

        payload = dict(context.get("payload") or {})
        payload["mentioned_user_id"] = getattr(mentioned_user, "id", None)
        NotificationService.create_from_blueprint(
            recipient=mentioned_user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_USER_MENTIONED,
                from_user=from_user,
                subject_type='post',
                subject_id=post_id,
                data=payload,
            ),
        )

    @staticmethod
    def notify_user_mentioned_from_event(event: Any, mentioned_user: Any, from_user: Any):
        payload = {
            "post_id": int(getattr(event, "post_id", 0) or 0),
            "post_number": getattr(event, "post_number", None),
            "discussion_id": getattr(event, "discussion_id", None),
            "discussion_title": getattr(event, "discussion_title", "") or "",
            "mentioned_user_id": getattr(mentioned_user, "id", None),
        }
        return NotificationService.create_from_blueprint(
            recipient=mentioned_user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_USER_MENTIONED,
                from_user=from_user,
                subject_type='post',
                subject_id=payload["post_id"],
                data=payload,
            ),
        )

    @staticmethod
    def delete_user_mentioned_for_post(
        post_id: int,
        mentioned_user: Any | None = None,
        mentioned_user_id: int | None = None,
    ) -> int:
        resolved_user_id = mentioned_user_id or getattr(mentioned_user, "id", None)
        if resolved_user_id is None:
            return NotificationService.delete_matching_notifications(
                blueprint=NotificationBlueprint(
                    type=NotificationService.TYPE_USER_MENTIONED,
                    subject_type='post',
                    subject_id=post_id,
                    match_data={"post_id": post_id},
                ),
            )

        deleted_count = NotificationService.delete_matching_notifications(
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_USER_MENTIONED,
                subject_type='post',
                subject_id=post_id,
                match_data={"post_id": post_id, "mentioned_user_id": resolved_user_id},
            ),
        )
        legacy_queryset = Notification.objects.filter(
            user_id=resolved_user_id,
            type=NotificationService.TYPE_USER_MENTIONED,
            subject_type='post',
            subject_id=post_id,
            is_deleted=False,
        ).exclude(data__has_key="mentioned_user_id")
        user_ids = list(legacy_queryset.values_list("user_id", flat=True).distinct())
        legacy_count = legacy_queryset.update(is_deleted=True)
        if legacy_count:
            NotificationService.invalidate_unread_counts(user_ids)
        return int(deleted_count or 0) + int(legacy_count or 0)

    @staticmethod
    def notify_discussion_approved(discussion, admin_user: Any, note: str = ""):
        if not getattr(discussion, "user", None):
            return

        NotificationService.create_from_blueprint(
            recipient=discussion.user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_DISCUSSION_APPROVED,
                from_user=admin_user,
                subject_type='discussion',
                subject_id=discussion.id,
                data={
                    'discussion_id': discussion.id,
                    'discussion_title': discussion.title,
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_discussion_approved_from_event(event: Any, admin_user: Any, note: str = ""):
        return NotificationService._notify_discussion_approval_from_event(
            event,
            admin_user,
            type_code=NotificationService.TYPE_DISCUSSION_APPROVED,
            note=note,
        )

    @staticmethod
    def notify_discussion_rejected(discussion, admin_user: Any, note: str = ""):
        if not getattr(discussion, "user", None):
            return

        NotificationService.create_from_blueprint(
            recipient=discussion.user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_DISCUSSION_REJECTED,
                from_user=admin_user,
                subject_type='discussion',
                subject_id=discussion.id,
                data={
                    'discussion_id': discussion.id,
                    'discussion_title': discussion.title,
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_discussion_rejected_from_event(event: Any, admin_user: Any, note: str = ""):
        return NotificationService._notify_discussion_approval_from_event(
            event,
            admin_user,
            type_code=NotificationService.TYPE_DISCUSSION_REJECTED,
            note=note,
        )

    @staticmethod
    def _notify_discussion_approval_from_event(event: Any, admin_user: Any, *, type_code: str, note: str = ""):
        author_id = int(getattr(event, "actor_user_id", 0) or 0)
        if not author_id:
            return None

        try:
            author = _get_user_by_id(author_id)
        except Exception:
            return None
        if author is None:
            return None

        discussion_id = int(getattr(event, "discussion_id", 0) or 0)
        return NotificationService.create_from_blueprint(
            recipient=author,
            blueprint=NotificationBlueprint(
                type=type_code,
                from_user=admin_user,
                subject_type='discussion',
                subject_id=discussion_id,
                data={
                    'discussion_id': discussion_id,
                    'discussion_title': getattr(event, "discussion_title", "") or "",
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_post_approved(post, admin_user: Any, note: str = ""):
        if not getattr(post, "user", None):
            return

        NotificationService.create_from_blueprint(
            recipient=post.user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_APPROVED,
                from_user=admin_user,
                subject_type='post',
                subject_id=post.id,
                data={
                    'discussion_id': post.discussion_id,
                    'discussion_title': post.discussion.title if getattr(post, "discussion", None) else "",
                    'post_id': post.id,
                    'post_number': post.number,
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_post_approved_from_event(event: Any, admin_user: Any, note: str = ""):
        return NotificationService._notify_post_approval_from_event(
            event,
            admin_user,
            type_code=NotificationService.TYPE_POST_APPROVED,
            note=note,
        )

    @staticmethod
    def notify_post_rejected(post, admin_user: Any, note: str = ""):
        if not getattr(post, "user", None):
            return

        NotificationService.create_from_blueprint(
            recipient=post.user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_POST_REJECTED,
                from_user=admin_user,
                subject_type='post',
                subject_id=post.id,
                data={
                    'discussion_id': post.discussion_id,
                    'discussion_title': post.discussion.title if getattr(post, "discussion", None) else "",
                    'post_id': post.id,
                    'post_number': post.number,
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_post_rejected_from_event(event: Any, admin_user: Any, note: str = ""):
        return NotificationService._notify_post_approval_from_event(
            event,
            admin_user,
            type_code=NotificationService.TYPE_POST_REJECTED,
            note=note,
        )

    @staticmethod
    def _notify_post_approval_from_event(event: Any, admin_user: Any, *, type_code: str, note: str = ""):
        author_id = int(getattr(event, "actor_user_id", 0) or 0)
        if not author_id:
            return None

        try:
            author = _get_user_by_id(author_id)
        except Exception:
            return None
        if author is None:
            return None

        post_id = int(getattr(event, "post_id", 0) or 0)
        return NotificationService.create_from_blueprint(
            recipient=author,
            blueprint=NotificationBlueprint(
                type=type_code,
                from_user=admin_user,
                subject_type='post',
                subject_id=post_id,
                data={
                    'discussion_id': getattr(event, "discussion_id", None),
                    'discussion_title': getattr(event, "discussion_title", "") or "",
                    'post_id': post_id,
                    'post_number': getattr(event, "post_number", None),
                    'approval_note': note or "",
                },
            ),
        )

    @staticmethod
    def notify_user_suspended(user: Any, admin_user: Optional[Any] = None):
        NotificationService.create_from_blueprint(
            recipient=user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_USER_SUSPENDED,
                from_user=admin_user,
                subject_type='user',
                subject_id=user.id,
                data={
                    'suspended_until': user.suspended_until.isoformat() if user.suspended_until else None,
                    'suspend_reason': user.suspend_reason or "",
                    'suspend_message': user.suspend_message or "",
                },
            ),
        )

    @staticmethod
    def notify_user_unsuspended(user: Any, admin_user: Optional[Any] = None):
        NotificationService.create_from_blueprint(
            recipient=user,
            blueprint=NotificationBlueprint(
                type=NotificationService.TYPE_USER_UNSUSPENDED,
                from_user=admin_user,
                subject_type='user',
                subject_id=user.id,
                data={},
            ),
        )

    @staticmethod
    def load_notifications_for_realtime(notification_ids: List[int]):
        if not notification_ids:
            return []

        notifications = list(
            Notification.objects.filter(id__in=notification_ids, is_deleted=False).select_related('from_user')
        )
        notification_map = {notification.id: notification for notification in notifications}
        return [
            notification_map[notification_id]
            for notification_id in notification_ids
            if notification_id in notification_map
        ]
