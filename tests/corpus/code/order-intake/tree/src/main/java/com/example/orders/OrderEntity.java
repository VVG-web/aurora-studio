package com.example.orders;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

@Entity
@Table(name = "orders")
public class OrderEntity {

    @Id
    public Long id;

    @Column(name = "external_id", unique = true)
    public String externalId;

    public String supplier;

    public String status;
}
