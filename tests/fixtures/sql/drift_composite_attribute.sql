-- The composite type gained a field, so anything constructing or destructuring it changes shape.
ALTER TYPE "cumo-invoicing".money_split ADD ATTRIBUTE gross numeric(12,2);
