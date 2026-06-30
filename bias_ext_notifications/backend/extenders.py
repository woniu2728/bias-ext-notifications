from __future__ import annotations

from bias_core.extensions import (
    ApiResourceExtender,
    ConditionalExtender,
    EventListenersExtender,
    LifecycleExtender,
    ModelExtender,
    RuntimeServiceContractExtender,
    ServiceProviderExtender,
    SignalExtender,
)

from bias_ext_notifications.backend.frontend import frontend_extender
from bias_ext_notifications.backend.listener_contracts import (
    notification_event_listener_definitions,
    post_notification_event_listener_definitions,
)
from bias_ext_notifications.backend.model_contracts import owned_models
from bias_ext_notifications.backend.notification_contracts import notification_extender, post_notification_extender
from bias_ext_notifications.backend.resource_contracts import notification_resource_endpoints
from bias_ext_notifications.backend.resources import (
    notification_resource_definition,
    notification_resource_field_definitions,
    notification_resource_relationship_definitions,
)
from bias_ext_notifications.backend.runtime import notification_service_provider
from bias_ext_notifications.backend.signal_contracts import signal_connections


def frontend_extenders():
    return (frontend_extender(),)


def notification_extenders():
    return (notification_extender(),)


def model_extenders():
    extender = ModelExtender()
    for model, description in owned_models():
        extender = extender.owns(model, description=description)
    return (extender,)


def resource_extenders():
    return (
        ApiResourceExtender(notification_resource_definition())
        .fields(notification_resource_field_definitions)
        .relationships(notification_resource_relationship_definitions)
        .endpoints(notification_resource_endpoints),
    )


def event_extenders():
    return (
        EventListenersExtender(
            listeners=notification_event_listener_definitions(),
        ),
    )


def post_integration_extenders():
    return (
        post_notification_extender(),
        EventListenersExtender(
            listeners=post_notification_event_listener_definitions(),
        ),
    )


def optional_integration_extenders():
    return (
        ConditionalExtender().when_extension_enabled("content", post_integration_extenders),
    )


def signal_extenders():
    extender = SignalExtender()
    for connection in signal_connections():
        extender = extender.connect(
            connection["signal"],
            connection["receiver"],
            sender=connection["sender"],
            dispatch_uid=connection["dispatch_uid"],
            description=connection["description"],
        )
    return (extender,)


def service_extenders():
    return (
        ServiceProviderExtender(
            key="notifications.service",
            provider=notification_service_provider,
        ),
        RuntimeServiceContractExtender().service(
            "notifications.service",
            required_values=("model",),
            required_methods=(
                "create_from_blueprint",
                "delete_discussion_reply_for_post",
                "delete_matching_notifications",
                "delete_user_mentioned_for_post",
                "sync_notifications",
            ),
            optional_methods=(
                "delete_post_liked_for_post_user",
                "dispatch_batch",
                "load_realtime_notifications",
                "notify_discussion_approved",
                "notify_discussion_rejected",
                "notify_discussion_reply",
                "notify_post_approved",
                "notify_post_liked",
                "notify_post_rejected",
                "notify_user_mentioned",
                "serialize_realtime_notification",
            ),
        ),
        LifecycleExtender(),
    )
