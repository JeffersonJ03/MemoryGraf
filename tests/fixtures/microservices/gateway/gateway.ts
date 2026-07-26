// API Gateway — enruta a los servicios internos por HTTP.
const ORDERS_URL = "http://orders:8080/api/orders";
const INVENTORY_URL = "http://inventory:8080/api/inventory";
const HEALTH_PATH = "/api/health";

function buildRequest(path: string): string {
  return `GET ${path}`;
}

export async function routeOrder(id: string): Promise<string> {
  const req = buildRequest(ORDERS_URL);
  return req + " " + id;
}

export function checkInventory(sku: string): string {
  return buildRequest(INVENTORY_URL) + " " + sku;
}

export function health(): string {
  return buildRequest(HEALTH_PATH);
}
