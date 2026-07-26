// Billing service (C#) — declara /api/billing.
namespace Billing {
    public class Invoice {
        const string BillingRoute = "/api/billing";

        private decimal Tax(decimal amount) {
            return amount * 0.18m;
        }

        public string Total(decimal amount) {
            decimal t = amount + Tax(amount);
            return t + " via " + BillingRoute;
        }
    }
}
