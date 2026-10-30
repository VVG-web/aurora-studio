package com.example.orders;

import org.springframework.stereotype.Service;

@Service
public class OrderService {

    public static final String STATUS_ACCEPTED = "ACCEPTED";

    private final OrderRepository repository;
    private final OrderEventPublisher events;

    public OrderService(OrderRepository repository, OrderEventPublisher events) {
        this.repository = repository;
        this.events = events;
    }

    public OrderEntity accept(OrderRequest request) {
        OrderEntity order = new OrderEntity();
        order.externalId = request.externalId;
        order.supplier = request.supplier;
        order.status = STATUS_ACCEPTED;
        OrderEntity saved = repository.save(order);
        events.accepted(saved);
        return saved;
    }
}
