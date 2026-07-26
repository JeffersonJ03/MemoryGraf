"""Orders service (Python) — declara /api/orders y llama a otros servicios."""

PAYMENTS_URL = "http://payments:8080/api/payments"
NOTIFY_URL = "http://notifications:8080/api/notify"
PRICING_URL = "http://pricing:8080/api/pricing"
ORDERS_ROUTE = "/api/orders"


def _post(url, body):
    return {"url": url, "body": body}


def create_order(item):
    _post(PAYMENTS_URL, {"item": item})
    _post(NOTIFY_URL, {"item": item})
    return _post(PRICING_URL, {"item": item})


def register(app):
    app.add_url_rule(ORDERS_ROUTE, "orders", create_order)
