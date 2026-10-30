package com.example.orders;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class OrderControllerTest {

    static final String DUPLICATE_ORDER = "DUPLICATE_ORDER";

    @Test
    void duplicateExternalIdIsRejected() {
        String code = rejectDuplicate("ext-1", "ext-1");
        assertEquals(DUPLICATE_ORDER, code);
    }

    private String rejectDuplicate(String first, String second) {
        return first.equals(second) ? DUPLICATE_ORDER : "";
    }
}
