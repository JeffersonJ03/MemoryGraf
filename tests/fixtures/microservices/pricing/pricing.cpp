// Pricing service (C++) — declara /api/pricing.
#include <string>

const std::string PRICING_ROUTE = "/api/pricing";

static double base_price(int units) {
    return units * 9.99;
}

double quote(int units) {
    double p = base_price(units);
    return p * 1.18;  // + IVA
}
