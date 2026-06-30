from celery import shared_task

from bias_ext_notifications.backend.runtime import deliver_notification_batch as deliver_notification_batch_now


@shared_task(name="bias_ext_notifications.backend.tasks.dispatch_notification_batch", ignore_result=True)
def dispatch_notification_batch(notification_ids):
    return deliver_notification_batch_now(notification_ids or [])
