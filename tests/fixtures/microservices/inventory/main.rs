// Inventory service (Rust) — declara /api/inventory.
const INVENTORY_ROUTE: &str = "/api/inventory";

fn in_stock(qty: i32) -> bool {
    qty > 0
}

pub fn reserve(qty: i32) -> String {
    if in_stock(qty) {
        format!("reserved {} at {}", qty, INVENTORY_ROUTE)
    } else {
        String::from("out of stock")
    }
}
