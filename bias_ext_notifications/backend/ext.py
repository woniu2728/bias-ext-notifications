from bias_ext_notifications.backend import tasks as notification_tasks  # noqa: F401
from bias_ext_notifications.backend.extenders import (
    event_extenders,
    frontend_extenders,
    model_extenders,
    notification_extenders,
    optional_integration_extenders,
    resource_extenders,
    service_extenders,
    signal_extenders,
)


def extend():
    return [
        *frontend_extenders(),
        *notification_extenders(),
        *model_extenders(),
        *resource_extenders(),
        *event_extenders(),
        *optional_integration_extenders(),
        *service_extenders(),
        *signal_extenders(),
    ]
