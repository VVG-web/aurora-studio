package com.example.refunds;

import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class RefundService {

    public static final int RETENTION_DAYS = 3;

    @Disabled
    @DeleteMapping("/api/refunds/{id}")
    public void cancel(@PathVariable String id) {
        // returns the money when an order is cancelled
    }
}
