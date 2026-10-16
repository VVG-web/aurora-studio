/* Быстрый старт — раздел-модуль.

   Маршруты обслуживания базы. Сам список рисует общий с «Продуктивностью» файл
   `routes.js`: экран один, разница только в группе сценариев. */

import {renderRoutes} from "./routes.js";

export function mount(ctx){
  ctx.root.dataset.module = "quickstart";
}

export async function refresh(ctx){
  await renderRoutes(ctx, "база", ctx.$("#quickBody"));   // данные движка
}

// Список маршрутов перерисовывается при открытии; у скрытого раздела фоновых обязанностей нет.
export const lifecycle = 1;
export default {mount, refresh};
