"""Queue consumer: an accepted order produces a notification."""

QUEUE = "orders.accepted"


def handle(message: dict) -> str:
    order_id = str(message.get("order_id") or "")
    return f"notify:{order_id}" if order_id else ""
