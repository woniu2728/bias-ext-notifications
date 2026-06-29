import json
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from ninja_jwt.tokens import RefreshToken
from unittest.mock import patch
from types import SimpleNamespace

from bias_core.extensions.notifications import NotificationBlueprint
from bias_core.extensions import ResourceEndpointDefinition
from bias_core.extensions.testing import (
    ExtensionRuntimeTestMixin,
    ResourceRegistry,
    Setting,
    build_extension_test_host,
    clear_runtime_setting_caches,
    get_forum_registry,
    get_resource_registry,
)
from bias_ext_notifications.backend.services import NotificationService
from bias_ext_notifications.backend.resource_contracts import notification_resource_endpoints


def _runtime_facade(name: str):
    from importlib import import_module

    return getattr(import_module("bias_core.extensions.runtime"), name)


def create_runtime_discussion(*args, **kwargs):
    return _runtime_facade("create_runtime_discussion")(*args, **kwargs)


def get_runtime_discussion_state_model(*args, **kwargs):
    return _runtime_facade("get_runtime_discussion_state_model")(*args, **kwargs)


def get_runtime_notification_model(*args, **kwargs):
    return _runtime_facade("get_runtime_notification_model")(*args, **kwargs)


def create_runtime_notification(*args, **kwargs):
    return _runtime_facade("create_runtime_notification")(*args, **kwargs)


def create_runtime_post(*args, **kwargs):
    return _runtime_facade("create_runtime_post")(*args, **kwargs)


def delete_runtime_notifications(*args, **kwargs):
    return _runtime_facade("delete_runtime_notifications")(*args, **kwargs)


def delete_runtime_post(*args, **kwargs):
    return _runtime_facade("delete_runtime_post")(*args, **kwargs)


def like_runtime_post(*args, **kwargs):
    return _runtime_facade("like_runtime_post")(*args, **kwargs)


def set_runtime_post_hidden_state(*args, **kwargs):
    return _runtime_facade("set_runtime_post_hidden_state")(*args, **kwargs)


def sync_runtime_notifications(*args, **kwargs):
    return _runtime_facade("sync_runtime_notifications")(*args, **kwargs)


def unlike_runtime_post(*args, **kwargs):
    return _runtime_facade("unlike_runtime_post")(*args, **kwargs)


def get_runtime_user_model(*args, **kwargs):
    return _runtime_facade("get_runtime_user_model")(*args, **kwargs)


class RuntimeModelProxy:
    def __init__(self, resolver):
        self._resolver = resolver

    def __getattr__(self, name):
        return getattr(self._resolver(), name)


User = RuntimeModelProxy(get_runtime_user_model)


def discussion_state_model():
    return get_runtime_discussion_state_model()


def notification_model():
    return get_runtime_notification_model()


class RuntimeNotificationModel:
    @property
    def objects(self):
        return notification_model().objects

    def __call__(self, *args, **kwargs):
        return notification_model()(*args, **kwargs)


Notification = RuntimeNotificationModel()


class NotificationServiceTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="author",
            email="author@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.replier = User.objects.create_user(
            username="replier",
            email="replier@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.participant = User.objects.create_user(
            username="participant",
            email="participant@example.com",
            password="password123",
            is_email_confirmed=True,
        )

        self.mentioned = User.objects.create_user(
            username="mentioned",
            email="mentioned@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        self.admin = User.objects.create_superuser(
            username="notification-admin",
            email="notification-admin@example.com",
            password="password123",
        )

        self.discussion = create_runtime_discussion(
            title="Notification discussion",
            content="Initial post",
            user=self.author,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.initial_reply = create_runtime_post(
                discussion_id=self.discussion.id,
                content="First reply",
                user=self.participant,
            )

    def tearDown(self):
        clear_runtime_setting_caches()
        super().tearDown()

    def test_extension_detail_api_surfaces_frontend_for_notifications_extension(self):
        response = self.client.get(
            "/api/admin/extensions/notifications",
            **self.auth_header(self.admin),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()["extension"]
        self.assertEqual(payload["frontend_forum_entry"], "extensions/notifications/frontend/forum/index.js")
        self.assertTrue(
            any(
                route["path"] == "/notifications"
                and route["name"] == "notifications"
                and route["component"] == "./NotificationView.vue"
                and route["requires_auth"]
                for route in payload["frontend_routes"]
            )
        )
        self.assertTrue(any(item["module_id"] == "notifications" for item in payload["resource_endpoints"]))

    def test_reply_to_post_creates_post_reply_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_runtime_post(
                discussion_id=self.discussion.id,
                content="@author Thanks for the update",
                user=self.replier,
                reply_to_post_id=self.initial_reply.id,
            )

        notification = Notification.objects.filter(
            user=self.participant,
            type="postReply",
        ).latest("id")

        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["reply_to_post_id"], self.initial_reply.id)
        self.assertEqual(notification.data["reply_to_post_number"], self.initial_reply.number)
        self.assertIn("post_number", notification.data)

    def test_post_reply_notification_from_event_does_not_query_post_context(self):
        event = SimpleNamespace(
            post_id=9001,
            post_number=7,
            discussion_id=self.discussion.id,
            discussion_title=self.discussion.title,
            actor_user_id=self.replier.id,
            discussion_user_id=self.author.id,
            reply_to_post_id=self.initial_reply.id,
            reply_to_post_user_id=self.participant.id,
            reply_to_post_number=self.initial_reply.number,
            is_approved=True,
        )

        with patch(
            "bias_ext_notifications.backend.services.NotificationService._get_post_reply_notification_context",
            side_effect=AssertionError("event notification path should not query posts.service reply context"),
        ):
            NotificationService.notify_post_reply_from_event(event, self.replier)

        notification = Notification.objects.get(
            user=self.participant,
            type="postReply",
            subject_id=self.initial_reply.id,
        )
        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["discussion_title"], self.discussion.title)
        self.assertEqual(notification.data["post_id"], 9001)
        self.assertEqual(notification.data["post_number"], 7)
        self.assertEqual(notification.data["reply_to_post_id"], self.initial_reply.id)
        self.assertEqual(notification.data["reply_to_post_number"], self.initial_reply.number)

    def test_hidden_post_soft_deletes_post_reply_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = create_runtime_post(
                discussion_id=self.discussion.id,
                content="Direct reply to hide",
                user=self.replier,
                reply_to_post_id=self.initial_reply.id,
            )
        notification = Notification.objects.get(
            user=self.participant,
            type="postReply",
            data__post_id=reply.id,
        )

        with self.captureOnCommitCallbacks(execute=True):
            set_runtime_post_hidden_state(reply, self.admin, True)

        notification.refresh_from_db()
        self.assertTrue(notification.is_deleted)

    def test_deleted_post_soft_deletes_post_reply_notification(self):
        with self.captureOnCommitCallbacks(execute=True):
            reply = create_runtime_post(
                discussion_id=self.discussion.id,
                content="Direct reply to delete",
                user=self.replier,
                reply_to_post_id=self.initial_reply.id,
            )
        notification = Notification.objects.get(
            user=self.participant,
            type="postReply",
            data__post_id=reply.id,
        )

        with self.captureOnCommitCallbacks(execute=True):
            delete_runtime_post(reply.id, self.replier)

        notification.refresh_from_db()
        self.assertTrue(notification.is_deleted)

    def test_like_notification_contains_post_number(self):
        with self.captureOnCommitCallbacks(execute=True):
            like_runtime_post(self.initial_reply.id, self.replier)

        notification = Notification.objects.get(
            user=self.participant,
            type="postLiked",
            subject_id=self.initial_reply.id,
        )

        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["post_id"], self.initial_reply.id)
        self.assertEqual(notification.data["post_number"], self.initial_reply.number)
        self.assertEqual(notification.data["from_user_id"], self.replier.id)

    def test_like_notification_from_event_does_not_query_post_context(self):
        event = SimpleNamespace(
            post_id=self.initial_reply.id,
            discussion_id=self.discussion.id,
            actor_user_id=self.replier.id,
            post_user_id=self.participant.id,
            post_number=self.initial_reply.number,
            discussion_title=self.discussion.title,
        )

        with patch(
            "bias_ext_notifications.backend.services.NotificationService._get_post_notification_context",
            side_effect=AssertionError("event notification path should not query posts.service context"),
        ):
            NotificationService.notify_post_liked_from_event(event, self.replier)

        notification = Notification.objects.get(
            user=self.participant,
            type="postLiked",
            subject_id=self.initial_reply.id,
        )
        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["discussion_title"], self.discussion.title)
        self.assertEqual(notification.data["post_number"], self.initial_reply.number)
        self.assertEqual(notification.data["from_user_id"], self.replier.id)

    def test_like_notification_is_synced_for_like_and_unlike(self):
        with self.captureOnCommitCallbacks(execute=True):
            like_runtime_post(self.initial_reply.id, self.replier)

        notification = Notification.objects.get(
            user=self.participant,
            type="postLiked",
            subject_id=self.initial_reply.id,
        )
        with self.captureOnCommitCallbacks(execute=True):
            unlike_runtime_post(self.initial_reply.id, self.replier)

        notification.refresh_from_db()
        self.assertTrue(notification.is_deleted)
        self.assertIsNone(NotificationService.get_notification_by_id(notification.id, self.participant))

    def test_like_notification_sync_is_idempotent_for_same_actor_and_post(self):
        with self.captureOnCommitCallbacks(execute=True):
            like_runtime_post(self.initial_reply.id, self.replier)

        notification = Notification.objects.get(
            user=self.participant,
            type="postLiked",
            subject_id=self.initial_reply.id,
        )
        NotificationService.notify_post_liked(self.initial_reply.id, self.replier)

        self.assertEqual(
            Notification.objects.filter(
                user=self.participant,
                type="postLiked",
                subject_id=self.initial_reply.id,
                data__from_user_id=self.replier.id,
            ).count(),
            1,
        )
        notification.refresh_from_db()
        self.assertFalse(notification.is_deleted)

    def test_unlike_notification_cleanup_handles_legacy_payload_without_actor_id(self):
        notification = Notification.objects.create(
            user=self.participant,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={
                "discussion_id": self.discussion.id,
                "post_id": self.initial_reply.id,
                "post_number": self.initial_reply.number,
            },
        )

        deleted_count = NotificationService.delete_post_liked_for_post_user(self.initial_reply.id, self.replier)

        notification.refresh_from_db()
        self.assertEqual(deleted_count, 1)
        self.assertTrue(notification.is_deleted)

    def test_blueprint_sync_is_idempotent_and_soft_deletes_removed_recipients(self):
        blueprint = NotificationBlueprint(
            type="postReply",
            from_user=self.replier,
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={
                "post_id": 99901,
                "discussion_id": self.discussion.id,
                "reply_to_post_id": self.initial_reply.id,
            },
            match_data={"post_id": 99901},
        )

        first = sync_runtime_notifications(
            blueprint=blueprint,
            recipients=[self.participant],
        )
        second = sync_runtime_notifications(
            blueprint=blueprint,
            recipients=[self.participant],
        )
        removed = sync_runtime_notifications(
            blueprint=blueprint,
            recipients=[],
        )

        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(removed["deleted_count"], 1)
        notification = Notification.objects.get(id=first["created"][0].id)
        self.assertTrue(notification.is_deleted)

    def test_blueprint_delete_matching_notifications(self):
        notification = Notification.objects.create(
            user=self.participant,
            from_user=self.replier,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={
                "post_id": 99902,
                "discussion_id": self.discussion.id,
                "reply_to_post_id": self.initial_reply.id,
            },
        )

        deleted_count = delete_runtime_notifications(
            blueprint=NotificationBlueprint(
                type="postReply",
                subject_type="post",
                subject_id=self.initial_reply.id,
                match_data={"post_id": 99902},
            ),
        )

        notification.refresh_from_db()
        self.assertEqual(deleted_count, 1)
        self.assertTrue(notification.is_deleted)

    def test_create_from_blueprint_respects_preferences_and_self_notification_guard(self):
        self.participant.preferences = {"notify_post_reply": False}
        self.participant.save(update_fields=["preferences"])
        muted = create_runtime_notification(
            recipient=self.participant,
            blueprint=NotificationBlueprint(
                type="postReply",
                from_user=self.replier,
                subject_type="post",
                subject_id=self.initial_reply.id,
                data={"post_id": 99903},
            ),
        )
        self_notification = create_runtime_notification(
            recipient=self.replier,
            blueprint=NotificationBlueprint(
                type="postReply",
                from_user=self.replier,
                subject_type="post",
                subject_id=self.initial_reply.id,
                data={"post_id": 99904},
            ),
        )

        self.assertIsNone(muted)
        self.assertIsNone(self_notification)
        self.assertFalse(Notification.objects.filter(data__post_id__in=[99903, 99904]).exists())

    def test_mention_notification_contains_post_number(self):
        with self.captureOnCommitCallbacks(execute=True):
            post = create_runtime_post(
                discussion_id=self.discussion.id,
                content=f"Hello @{self.mentioned.username}",
                user=self.replier,
            )

        notification = Notification.objects.get(
            user=self.mentioned,
            type="userMentioned",
            subject_id=post.id,
        )

        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["post_id"], post.id)
        self.assertEqual(notification.data["post_number"], post.number)
        self.assertEqual(notification.data["mentioned_user_id"], self.mentioned.id)

    def test_mention_notification_from_event_does_not_query_post_context(self):
        event = SimpleNamespace(
            post_id=9101,
            discussion_id=self.discussion.id,
            actor_user_id=self.replier.id,
            mentioned_user_id=self.mentioned.id,
            post_number=8,
            discussion_title=self.discussion.title,
        )

        with patch(
            "bias_ext_notifications.backend.services.NotificationService._get_post_notification_context",
            side_effect=AssertionError("event mention path should not query posts.service context"),
        ):
            NotificationService.notify_user_mentioned_from_event(event, self.mentioned, self.replier)

        notification = Notification.objects.get(
            user=self.mentioned,
            type="userMentioned",
            subject_id=9101,
        )
        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["discussion_title"], self.discussion.title)
        self.assertEqual(notification.data["post_id"], 9101)
        self.assertEqual(notification.data["post_number"], 8)
        self.assertEqual(notification.data["mentioned_user_id"], self.mentioned.id)

    def test_post_approval_notification_from_event_does_not_query_post_context(self):
        event = SimpleNamespace(
            post_id=9201,
            discussion_id=self.discussion.id,
            actor_user_id=self.participant.id,
            admin_user_id=self.admin.id,
            post_number=9,
            discussion_title=self.discussion.title,
        )

        with patch(
            "bias_ext_notifications.backend.services.NotificationService._get_post_notification_context",
            side_effect=AssertionError("event approval path should not query posts.service context"),
        ):
            NotificationService.notify_post_approved_from_event(event, self.admin, note="ok")

        notification = Notification.objects.get(
            user=self.participant,
            type="postApproved",
            subject_id=9201,
        )
        self.assertEqual(notification.data["discussion_id"], self.discussion.id)
        self.assertEqual(notification.data["discussion_title"], self.discussion.title)
        self.assertEqual(notification.data["post_id"], 9201)
        self.assertEqual(notification.data["post_number"], 9)
        self.assertEqual(notification.data["approval_note"], "ok")

    def test_discussion_approval_notification_from_event_uses_event_context(self):
        event = SimpleNamespace(
            discussion_id=9301,
            actor_user_id=self.author.id,
            admin_user_id=self.admin.id,
            discussion_title="Event discussion approval",
        )

        NotificationService.notify_discussion_approved_from_event(event, self.admin, note="ok")

        notification = Notification.objects.get(
            user=self.author,
            type="discussionApproved",
            subject_id=9301,
        )
        self.assertEqual(notification.data["discussion_id"], 9301)
        self.assertEqual(notification.data["discussion_title"], "Event discussion approval")
        self.assertEqual(notification.data["approval_note"], "ok")

    def test_delete_user_mention_notification_for_post_soft_deletes_one_recipient(self):
        other_mentioned = User.objects.create_user(
            username="other-mentioned",
            email="other-mentioned@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        with self.captureOnCommitCallbacks(execute=True):
            post = create_runtime_post(
                discussion_id=self.discussion.id,
                content=f"Hello @{self.mentioned.username} @{other_mentioned.username}",
                user=self.replier,
            )
        matching = Notification.objects.get(
            user=self.mentioned,
            type="userMentioned",
            subject_id=post.id,
        )
        retained = Notification.objects.get(
            user=other_mentioned,
            type="userMentioned",
            subject_id=post.id,
        )

        deleted_count = NotificationService.delete_user_mentioned_for_post(post.id, mentioned_user=self.mentioned)

        matching.refresh_from_db()
        retained.refresh_from_db()
        self.assertEqual(deleted_count, 1)
        self.assertTrue(matching.is_deleted)
        self.assertFalse(retained.is_deleted)

    def test_reply_notification_respects_post_reply_preference(self):
        self.participant.preferences = {"notify_post_reply": False}
        self.participant.save(update_fields=["preferences"])

        with self.captureOnCommitCallbacks(execute=True):
            create_runtime_post(
                discussion_id=self.discussion.id,
                content="Reply without notify",
                user=self.replier,
                reply_to_post_id=self.initial_reply.id,
            )

        self.assertFalse(
            Notification.objects.filter(
                user=self.participant,
                type="postReply",
            ).exists()
        )

    def test_like_notification_respects_post_liked_preference(self):
        self.participant.preferences = {"notify_post_liked": False}
        self.participant.save(update_fields=["preferences"])

        with self.captureOnCommitCallbacks(execute=True):
            like_runtime_post(self.initial_reply.id, self.replier)

        self.assertFalse(
            Notification.objects.filter(
                user=self.participant,
                type="postLiked",
                subject_id=self.initial_reply.id,
            ).exists()
        )

    def test_mention_notification_respects_user_preference(self):
        self.mentioned.preferences = {"notify_user_mentioned": False}
        self.mentioned.save(update_fields=["preferences"])

        with self.captureOnCommitCallbacks(execute=True):
            create_runtime_post(
                discussion_id=self.discussion.id,
                content=f"Hello again @{self.mentioned.username}",
                user=self.replier,
            )

        self.assertFalse(
            Notification.objects.filter(
                user=self.mentioned,
                type="userMentioned",
            ).exists()
        )

    def test_multiple_replies_in_same_discussion_create_multiple_notifications(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_runtime_post(
                discussion_id=self.discussion.id,
                content="Second reply",
                user=self.replier,
            )
        with self.captureOnCommitCallbacks(execute=True):
            create_runtime_post(
                discussion_id=self.discussion.id,
                content="Third reply",
                user=self.replier,
            )

        notifications = Notification.objects.filter(
            user=self.author,
            type="discussionReply",
            subject_id=self.discussion.id,
            is_read=False,
        ).order_by("id")

        self.assertEqual(notifications.count(), 3)
        self.assertEqual(
            [item.data["post_number"] for item in notifications],
            [2, 3, 4],
        )

    def test_discussion_reply_notifications_dispatch_created_event(self):
        subscriber = User.objects.create_user(
            username="subscriber",
            email="subscriber@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        discussion_state_model().objects.create(discussion_id=self.discussion.id, user=subscriber, is_subscribed=True)
        with patch("bias_ext_subscriptions.backend.listeners._notify_discussion_reply"):
            with self.captureOnCommitCallbacks(execute=True):
                new_post = create_runtime_post(
                    discussion_id=self.discussion.id,
                    content="Sync reply",
                    user=self.replier,
                )
        existing_notification_ids = set(Notification.objects.values_list("id", flat=True))

        with patch("bias_ext_notifications.backend.tasks.dispatch_notification_batch.delay") as delay:
            with patch("bias_ext_notifications.backend.services.dispatch_forum_event_after_commit") as dispatch_event:
                with self.captureOnCommitCallbacks(execute=True):
                    NotificationService.notify_discussion_reply(
                        discussion_id=self.discussion.id,
                        post_id=new_post.id,
                        from_user=self.replier,
                    )

        created_notifications = Notification.objects.filter(
            type="discussionReply",
            subject_id=self.discussion.id,
            data__post_id=new_post.id,
        ).exclude(id__in=existing_notification_ids)
        self.assertEqual({item.user_id for item in created_notifications}, {self.author.id, subscriber.id})
        delay.assert_not_called()
        dispatched_ids = set()
        for call in dispatch_event.call_args_list:
            dispatched_ids.update(getattr(call.args[0], "notification_ids", ()))
        self.assertTrue(set(created_notifications.values_list("id", flat=True)).issubset(dispatched_ids))

    @override_settings(CELERY_BROKER_URL="redis://localhost:6379/1")
    def test_discussion_reply_notifications_dispatch_created_event_when_queue_enabled(self):
        Setting.objects.update_or_create(
            key="advanced.queue_enabled",
            defaults={"value": "true"},
        )
        Setting.objects.update_or_create(
            key="advanced.queue_driver",
            defaults={"value": '"redis"'},
        )
        clear_runtime_setting_caches()

        subscriber = User.objects.create_user(
            username="subscriber",
            email="subscriber@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        muted = User.objects.create_user(
            username="muted",
            email="muted@example.com",
            password="password123",
            is_email_confirmed=True,
            preferences={"notify_new_post": False},
        )
        discussion_state_model().objects.create(discussion_id=self.discussion.id, user=subscriber, is_subscribed=True)
        discussion_state_model().objects.create(discussion_id=self.discussion.id, user=muted, is_subscribed=True)
        with patch("bias_ext_subscriptions.backend.listeners._notify_discussion_reply"):
            with self.captureOnCommitCallbacks(execute=True):
                new_post = create_runtime_post(
                    discussion_id=self.discussion.id,
                    content="Bulk reply",
                    user=self.replier,
                )
        existing_notification_ids = set(Notification.objects.values_list("id", flat=True))

        with patch("bias_ext_notifications.backend.services.dispatch_forum_event_after_commit") as dispatch_event:
            with self.captureOnCommitCallbacks(execute=True):
                NotificationService.notify_discussion_reply(
                    discussion_id=self.discussion.id,
                    post_id=new_post.id,
                    from_user=self.replier,
                )

        created_notifications = Notification.objects.filter(
            type="discussionReply",
            subject_id=self.discussion.id,
            data__post_id=new_post.id,
        ).exclude(id__in=existing_notification_ids)
        self.assertEqual({item.user_id for item in created_notifications}, {self.author.id, subscriber.id})
        dispatched_ids = set()
        for call in dispatch_event.call_args_list:
            dispatched_ids.update(getattr(call.args[0], "notification_ids", ()))
        self.assertTrue(set(created_notifications.values_list("id", flat=True)).issubset(dispatched_ids))

    @override_settings(CELERY_BROKER_URL="redis://localhost:6379/1")
    def test_bulk_notifications_expose_realtime_batch_loader_when_task_enqueue_fails(self):
        Setting.objects.update_or_create(
            key="advanced.queue_enabled",
            defaults={"value": "true"},
        )
        Setting.objects.update_or_create(
            key="advanced.queue_driver",
            defaults={"value": '"redis"'},
        )
        clear_runtime_setting_caches()

        NotificationModel = notification_model()
        first = NotificationModel(
            user=self.author,
            from_user=self.replier,
            type="discussionReply",
            subject_type="discussion",
            subject_id=self.discussion.id,
            data={"discussion_id": self.discussion.id},
        )
        second = NotificationModel(
            user=self.participant,
            from_user=self.replier,
            type="discussionReply",
            subject_type="discussion",
            subject_id=self.discussion.id,
            data={"discussion_id": self.discussion.id},
        )

        with patch("bias_ext_notifications.backend.tasks.dispatch_notification_batch.delay", side_effect=RuntimeError("queue down")):
            with self.captureOnCommitCallbacks(execute=True):
                created = NotificationService.create_notifications_bulk([first, second])

        self.assertEqual(len(created), 2)
        loaded = NotificationService.load_notifications_for_realtime([item.id for item in created])
        self.assertEqual([item.id for item in loaded], [item.id for item in created])

    def auth_header(self, user):
        token = RefreshToken.for_user(user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_delete_all_read_endpoint_uses_clear_route(self):
        unread_before = Notification.objects.filter(user=self.author, is_read=False).count()
        Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=True,
            data={"post_id": self.initial_reply.id},
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=False,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.delete(
            "/api/notifications/read/clear",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Notification.objects.filter(user=self.author, is_read=True, is_deleted=False).count(), 0)
        self.assertEqual(
            Notification.objects.filter(user=self.author, is_read=False, is_deleted=False).count(),
            unread_before + 1,
        )

    def test_notification_list_returns_type_count_metadata(self):
        Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=True,
            data={"post_id": self.initial_reply.id},
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            "/api/notifications",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["type_counts"]["postLiked"], 2)
        self.assertEqual(payload["type_counts"]["postReply"], 1)
        self.assertEqual(payload["unread_type_counts"]["postLiked"], 1)
        self.assertEqual(payload["unread_type_counts"]["postReply"], 1)

    def test_mark_filtered_as_read_endpoint_supports_type_and_discussion_filters(self):
        other_discussion = create_runtime_discussion(
            title="Another discussion",
            content="Seed content",
            user=self.author,
        )
        matching = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={
                "post_id": self.initial_reply.id,
                "discussion_id": self.discussion.id,
                "discussion_title": self.discussion.title,
            },
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postLiked",
            subject_type="discussion",
            subject_id=other_discussion.id,
            data={
                "discussion_id": other_discussion.id,
                "discussion_title": other_discussion.title,
            },
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={
                "post_id": self.initial_reply.id,
                "discussion_id": self.discussion.id,
                "discussion_title": self.discussion.title,
            },
        )

        response = self.client.post(
            f"/api/notifications/read-filtered?type=postLiked&discussion_id={self.discussion.id}",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["type_counts"]["postLiked"], 1)
        matching.refresh_from_db()
        self.assertTrue(matching.is_read)
        self.assertEqual(Notification.objects.filter(user=self.author, is_read=False, type="postLiked").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.author, is_read=False, type="postReply").count(), 1)

    def test_clear_filtered_read_endpoint_supports_type_and_discussion_filters(self):
        matching = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=True,
            data={
                "post_id": self.initial_reply.id,
                "discussion_id": self.discussion.id,
                "discussion_title": self.discussion.title,
            },
        )
        Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=True,
            data={
                "post_id": self.initial_reply.id,
                "discussion_id": self.discussion.id,
                "discussion_title": self.discussion.title,
            },
        )
        retained = Notification.objects.create(
            user=self.author,
            from_user=self.participant,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            is_read=True,
            data={
                "post_id": self.initial_reply.id,
                "discussion_id": self.discussion.id,
                "discussion_title": self.discussion.title,
            },
        )

        response = self.client.delete(
            f"/api/notifications/read/clear-filtered?type=postReply&discussion_id={self.discussion.id}",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["type_counts"]["postReply"], 2)
        matching.refresh_from_db()
        self.assertTrue(matching.is_deleted)
        self.assertTrue(Notification.objects.filter(id=retained.id).exists())
        self.assertFalse(Notification.objects.get(id=retained.id).is_deleted)

    def test_notification_detail_exposes_registered_from_user_summary(self):
        group = self.replier.user_groups.create(name="Notifier", color="#1abc9c", icon="fas fa-bell")
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            f"/api/notifications/{notification.id}",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["from_user"]["username"], self.replier.username)
        self.assertEqual(payload["from_user"]["primary_group"]["name"], group.name)

    def test_notification_list_exposes_registered_from_user_summary(self):
        group = self.replier.user_groups.create(name="NotifyList", color="#9b59b6", icon="fas fa-star")
        Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            "/api/notifications",
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertGreaterEqual(payload["total"], 1)
        self.assertEqual(payload["data"][0]["from_user"]["username"], self.replier.username)
        self.assertEqual(payload["data"][0]["from_user"]["primary_group"]["name"], group.name)

    def test_notification_detail_supports_resource_field_selection(self):
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            f"/api/notifications/{notification.id}",
            {"fields[notification]": "from_user"},
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["id"], notification.id)
        self.assertEqual(payload["type"], "postLiked")
        self.assertEqual(payload["subject_id"], self.initial_reply.id)
        self.assertIn("from_user", payload)

    def test_notification_detail_omits_registered_fields_when_not_selected(self):
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            f"/api/notifications/{notification.id}",
            {"fields[notification]": "unknown_field"},
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertNotIn("from_user", payload)

    def test_notification_detail_supports_resource_include_for_from_user(self):
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        response = self.client.get(
            f"/api/notifications/{notification.id}",
            {"fields[notification]": "unknown_field", "include": "from_user"},
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn("from_user", payload)
        self.assertEqual(payload["from_user"]["username"], self.replier.username)

    def test_notification_detail_static_route_uses_resource_endpoint_mutator(self):
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        def mutate_endpoint(endpoint):
            def handler(context):
                payload = endpoint.handler(context)
                payload["mutated_by_resource_endpoint"] = True
                return payload

            return ResourceEndpointDefinition(
                resource=endpoint.resource,
                endpoint=endpoint.endpoint,
                module_id="test",
                handler=handler,
                methods=endpoint.methods,
            )

        registry = ResourceRegistry()
        for endpoint in notification_resource_endpoints():
            registry.register_endpoint(endpoint)
        registry.register_endpoint(
            ResourceEndpointDefinition(
                resource="notification",
                endpoint="show",
                module_id="test",
                operation="mutate",
                mutator=mutate_endpoint,
            )
        )

        with patch("bias_ext_notifications.backend.handlers.get_runtime_resource_registry", return_value=registry):
            with patch("bias_core.resource_dispatcher.get_runtime_resource_registry", return_value=registry):
                response = self.client.get(
                    f"/api/notifications/{notification.id}",
                    **self.auth_header(self.author),
                )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["mutated_by_resource_endpoint"])

    def test_notification_list_avoids_n_plus_one_for_registered_from_user_summary(self):
        for index in range(3):
            Notification.objects.create(
                user=self.author,
                from_user=self.replier,
                type="postReply",
                subject_type="post",
                subject_id=self.initial_reply.id,
                data={"post_id": self.initial_reply.id, "index": index},
                is_read=bool(index % 2),
            )

        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                "/api/notifications",
                **self.auth_header(self.author),
            )

        self.assertEqual(response.status_code, 200, response.content)
        select_group_queries = [
            query["sql"]
            for query in context.captured_queries
            if "user_groups" in query["sql"].lower()
        ]
        self.assertLessEqual(len(select_group_queries), 2)

    def test_notification_list_normalizes_page_and_limit(self):
        response = self.client.get(
            "/api/notifications",
            {"page": 0, "limit": 999},
            **self.auth_header(self.author),
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 100)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "notification-cache-test"}})
    def test_notification_stats_reuses_cached_unread_count(self):
        cache.clear()
        unread_before = Notification.objects.filter(user=self.author, is_read=False).count()
        Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        with self.assertNumQueries(1):
            stats = NotificationService.get_stats(self.author)

        self.assertEqual(stats["total"], Notification.objects.filter(user=self.author).count())
        self.assertEqual(stats["unread_count"], unread_before + 1)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "notification-invalidate-test"}})
    def test_unread_count_cache_is_invalidated_after_writes(self):
        cache.clear()
        unread_before = Notification.objects.filter(user=self.author, is_read=False).count()
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        NotificationService.mark_as_read(notification.id, self.author)
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before)

        NotificationService.create_notification(
            user=self.author,
            from_user=self.replier,
            type="postReply",
            subject_type="post",
            subject_id=self.initial_reply.id,
            allow_merge=False,
            data={"post_id": self.initial_reply.id},
        )
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        new_notification = Notification.objects.filter(user=self.author, is_read=False).latest("id")
        NotificationService.delete_notification(new_notification.id, self.author)
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before)

    def test_delete_notification_soft_deletes_and_hides_from_user_queries(self):
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )

        self.assertTrue(NotificationService.delete_notification(notification.id, self.author))

        notification.refresh_from_db()
        self.assertTrue(notification.is_deleted)
        self.assertIsNone(NotificationService.get_notification_by_id(notification.id, self.author))

    def test_discussion_reply_sync_soft_deletes_removed_recipients(self):
        subscriber = User.objects.create_user(
            username="sync-subscriber",
            email="sync-subscriber@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        discussion_state_model().objects.create(discussion_id=self.discussion.id, user=subscriber, is_subscribed=True)
        with self.captureOnCommitCallbacks(execute=True):
            post = create_runtime_post(
                discussion_id=self.discussion.id,
                content="Sync recipient change",
                user=self.replier,
            )

        subscriber_notification = Notification.objects.get(
            user=subscriber,
            type="discussionReply",
            data__post_id=post.id,
        )
        discussion_state_model().objects.filter(discussion_id=self.discussion.id, user=subscriber).update(is_subscribed=False)

        NotificationService.notify_discussion_reply(
            discussion_id=self.discussion.id,
            post_id=post.id,
            from_user=self.replier,
        )

        subscriber_notification.refresh_from_db()
        self.assertTrue(subscriber_notification.is_deleted)
        self.assertTrue(
            Notification.objects.filter(
                user=self.author,
                type="discussionReply",
                data__post_id=post.id,
                is_deleted=False,
            ).exists()
        )

    def test_discussion_reply_sync_restores_existing_notification(self):
        subscriber = User.objects.create_user(
            username="restore-subscriber",
            email="restore-subscriber@example.com",
            password="password123",
            is_email_confirmed=True,
        )
        discussion_state_model().objects.create(discussion_id=self.discussion.id, user=subscriber, is_subscribed=True)
        with self.captureOnCommitCallbacks(execute=True):
            post = create_runtime_post(
                discussion_id=self.discussion.id,
                content="Restore recipient",
                user=self.replier,
            )

        notification = Notification.objects.get(
            user=subscriber,
            type="discussionReply",
            data__post_id=post.id,
        )
        notification.is_deleted = True
        notification.save(update_fields=["is_deleted"])

        NotificationService.notify_discussion_reply(
            discussion_id=self.discussion.id,
            post_id=post.id,
            from_user=self.replier,
        )

        notification.refresh_from_db()
        self.assertFalse(notification.is_deleted)
        self.assertEqual(
            Notification.objects.filter(
                user=subscriber,
                type="discussionReply",
                data__post_id=post.id,
            ).count(),
            1,
        )

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "notification-signal-test"}})
    def test_unread_count_cache_is_invalidated_by_model_signals(self):
        cache.clear()
        unread_before = Notification.objects.filter(user=self.author, is_read=False).count()

        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        notification.is_read = True
        notification.read_at = None
        notification.save(update_fields=["is_read", "read_at"])
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before)

        notification.is_read = False
        notification.save(update_fields=["is_read"])
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        notification.delete()
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "notification-admin-test"}})
    def test_notification_mark_read_handler_invalidates_unread_count_cache(self):
        from bias_ext_notifications.backend.handlers import dispatch_notification_mark_read

        cache.clear()
        notification = Notification.objects.create(
            user=self.author,
            from_user=self.replier,
            type="postLiked",
            subject_type="post",
            subject_id=self.initial_reply.id,
            data={"post_id": self.initial_reply.id},
        )
        unread_before = Notification.objects.filter(user=self.author, is_read=False).exclude(id=notification.id).count()
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before + 1)

        dispatch_notification_mark_read({
            "object_id": notification.id,
            "user": self.author,
        })
        self.assertEqual(NotificationService.get_unread_count(self.author), unread_before)


class NotificationExtensionDiagnosticsTests(ExtensionRuntimeTestMixin, TestCase):
    def test_notifications_extension_registers_runtime_service_provider(self):
        application = self.bootstrap_extensions("notifications")
        service = application.get_service("notifications.service")

        self.assertIn("notifications.service", application.get_service_provider_keys(extension_id="notifications"))
        self.assertEqual(service["model"].__name__, "Notification")
        for key in (
            "notify_discussion_reply",
            "notify_discussion_approved",
            "notify_discussion_approved_from_event",
            "notify_discussion_rejected",
            "notify_discussion_rejected_from_event",
            "notify_post_approved",
            "notify_post_approved_from_event",
            "notify_post_rejected",
            "notify_post_rejected_from_event",
            "notify_post_reply_from_event",
            "notify_post_liked",
            "notify_post_liked_from_event",
            "delete_post_liked_for_post_user",
            "notify_user_mentioned",
            "notify_user_mentioned_from_event",
            "delete_post_reply_for_post",
            "delete_user_mentioned_for_post",
            "dispatch_batch",
            "send_batch",
            "load_realtime_notifications",
            "serialize_realtime_notification",
            "delete_discussion_reply_for_post",
        ):
            self.assertTrue(callable(service[key]), key)

    def test_content_post_event_integration_registers_without_posts_service(self):
        application = build_extension_test_host("notifications")
        service = application.get_service("notifications.service")
        listener_names = {
            listener.handler.__name__
            for listener in application.events.get_listeners(extension_id="notifications")
        }
        notification_type_codes = {
            item.code
            for item in application.forum_registry.get_notification_types()
            if item.module_id == "notifications"
        }

        self.assertIsNone(application.get_service("posts.service"))
        self.assertIsNotNone(application.get_service("content.posts"))
        self.assertIn("handle_post_created_direct_reply_notification", listener_names)
        self.assertIn("handle_post_hidden_direct_reply_notification_cleanup", listener_names)
        self.assertIn("handle_post_deleted_direct_reply_notification_cleanup", listener_names)
        self.assertIn("postReply", notification_type_codes)
        self.assertIsNone(service["notify_post_liked"](1, None))
        self.assertIsNone(service["notify_user_mentioned"](1, None, None))

    def test_posts_extension_is_not_required_for_post_event_integration(self):
        application = build_extension_test_host("content", "notifications")
        listener_names = {
            listener.handler.__name__
            for listener in application.events.get_listeners(extension_id="notifications")
        }
        notification_type_codes = {
            item.code
            for item in application.forum_registry.get_notification_types()
            if item.module_id == "notifications"
        }

        self.assertIsNotNone(application.get_service("content.posts"))
        self.assertIsNone(application.get_service("posts.service"))
        self.assertIn("handle_post_created_direct_reply_notification", listener_names)
        self.assertIn("handle_post_hidden_direct_reply_notification_cleanup", listener_names)
        self.assertIn("handle_post_deleted_direct_reply_notification_cleanup", listener_names)
        self.assertIn("postReply", notification_type_codes)

    def test_notifications_capabilities_are_filtered_when_extension_disabled(self):
        self.disable_extension_for_test("notifications")

        resource_registry = get_resource_registry()
        forum_registry = get_forum_registry()

        self.assertIsNone(resource_registry.get_dispatch_endpoint("notification", "read", "POST", {}))
        self.assertIsNone(resource_registry.get_dispatch_endpoint("notification", "index", "GET", {}))
        self.assertFalse(any(item.module_id == "notifications" for item in forum_registry.get_notification_types()))

    def test_inspect_reports_notification_model_as_extension_native(self):
        stdout = StringIO()
        call_command(
            "inspect_extensions",
            "--extension-id",
            "notifications",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        extension = payload["extensions"][0]
        audit = extension["model_ownership_audit"]
        item = audit["items"][0]

        self.assertEqual(extension["id"], "notifications")
        migration_files = {
            *extension["migration_plan"]["pending_files"],
            *extension["migration_plan"]["applied_files"],
        }
        self.assertIn("0001_initial.py", migration_files)
        self.assertEqual(audit["owned_model_count"], 1)
        self.assertEqual(audit["extension_native_count"], 1)
        self.assertEqual(audit["django_app_count"], 0)
        self.assertEqual(audit["package_migration_required_count"], 0)
        self.assertEqual(audit["app_label_migration_required_count"], 0)
        self.assertEqual(item["model"], "Notification")
        self.assertEqual(item["model_module"], "bias_ext_notifications.backend.models")
        self.assertEqual(item["current_app_label"], "notifications")
        self.assertEqual(item["target_app_label"], "notifications")
        self.assertEqual(item["migration_risk"], "none")
