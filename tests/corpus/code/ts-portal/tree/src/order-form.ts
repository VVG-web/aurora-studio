// Order form: fields and their validators.

export const required = (v: string): boolean => v.trim().length > 0;
export const positive = (v: number): boolean => v > 0;

export const ORDER_FORM = {
  externalId: [required],
  supplier: [required],
  quantity: [positive],
};
