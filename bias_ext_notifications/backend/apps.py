from django.apps import AppConfig


class NotificationsExtensionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    label = "notifications"
    name = "bias_ext_notifications.backend"
    verbose_name = "Bias Notifications Extension"

