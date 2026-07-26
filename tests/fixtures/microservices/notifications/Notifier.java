// Notifications service (Java) — declara /api/notify.
public class Notifier {
    static final String NOTIFY_ROUTE = "/api/notify";

    private boolean valid(String to) {
        return to != null && to.length() > 0;
    }

    public String send(String to) {
        if (valid(to)) {
            return "sent to " + to + " via " + NOTIFY_ROUTE;
        }
        return "skipped";
    }
}
