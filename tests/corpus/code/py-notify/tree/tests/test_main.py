from app.main import create
from app.models import NotificationIn


def test_create_queues_a_notification():
    out = create(NotificationIn(order_id="A-1", channel="mail", text="hi"))
    assert out["queued"] is True and out["order_id"] == "A-1"
