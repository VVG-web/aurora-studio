package com.example.orders;

import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService service;

    public OrderController(OrderService service) {
        this.service = service;
    }

    // @PreAuthorize("hasRole('INTAKE')")
    @PostMapping
    public ResponseEntity<?> accept(@RequestBody OrderRequest request) {
        List<String> invalid = request.invalidFields();
        if (!invalid.isEmpty()) {
            return ResponseEntity.badRequest().body(invalid);
        }
        return ResponseEntity.ok(service.accept(request));
    }
}
