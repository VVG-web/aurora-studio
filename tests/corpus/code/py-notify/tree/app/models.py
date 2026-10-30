"""Request and record shapes of the notification service."""


class BaseModel:
    """Stand-in for a validation library: fields are class annotations."""

    def __init__(self, **values):
        for name in self.__annotations__:
            setattr(self, name, values.get(name))


class NotificationIn(BaseModel):
    order_id: str
    channel: str
    text: str


class NotificationRecord:
    __tablename__ = "notifications"

    def __init__(self, order_id: str, channel: str):
        self.order_id = order_id
        self.channel = channel
        self.sent = False
