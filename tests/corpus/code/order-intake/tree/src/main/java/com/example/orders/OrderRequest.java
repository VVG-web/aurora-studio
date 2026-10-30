package com.example.orders;

import java.util.ArrayList;
import java.util.List;

public class OrderRequest {

    public String externalId;
    public String supplier;
    public String warehouse;
    public int quantity;

    public List<String> invalidFields() {
        List<String> out = new ArrayList<>();
        if (externalId == null || externalId.isBlank()) out.add("externalId");
        if (supplier == null || supplier.isBlank()) out.add("supplier");
        if (quantity <= 0) out.add("quantity");
        return out;
    }
}
