import { OrdersController } from "./orders.controller";

const controller = new OrdersController([]);
console.log(controller.list().length);
