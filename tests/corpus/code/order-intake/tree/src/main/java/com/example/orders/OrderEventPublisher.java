package com.example.orders;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

@Component
public class OrderEventPublisher {

    public static final String TOPIC_ACCEPTED = "orders.accepted";

    private final KafkaTemplate<String, String> kafka;

    public OrderEventPublisher(KafkaTemplate<String, String> kafka) {
        this.kafka = kafka;
    }

    public void accepted(OrderEntity order) {
        kafka.send(TOPIC_ACCEPTED, order.externalId);
    }
}
