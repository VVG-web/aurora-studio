// Operator portal: the order list as the intake operator sees it.

export interface OrderRow {
  externalId: string;
  supplier: string;
  status: string;
}

export class OrdersController {
  constructor(private readonly rows: OrderRow[]) {}

  // GET /portal/orders
  list(status?: string): OrderRow[] {
    return status ? this.rows.filter(r => r.status === status) : this.rows;
  }
}
