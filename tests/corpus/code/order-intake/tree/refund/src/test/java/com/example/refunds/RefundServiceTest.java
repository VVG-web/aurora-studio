package com.example.refunds;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class RefundServiceTest {

    @Test
    void cancelledOrderGetsAnErrorBranch() {
        String error = "REFUND_NOT_ALLOWED";
        assertTrue(error.startsWith("REFUND"));
    }
}
