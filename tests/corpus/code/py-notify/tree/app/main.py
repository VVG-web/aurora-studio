"""HTTP routes of the notification service (decorators in the style of a web framework)."""
from app.models import NotificationIn, NotificationRecord

ROUTES = {}


def post(path):
    def mark(fn):
        ROUTES[("POST", path)] = fn
        return fn
    return mark


@post("/api/notifications")
def create(body: NotificationIn) -> dict:
    record = NotificationRecord(body.order_id, body.channel)
    return {"order_id": record.order_id, "queued": True}


if __name__ == "__main__":
    print(sorted(ROUTES))
