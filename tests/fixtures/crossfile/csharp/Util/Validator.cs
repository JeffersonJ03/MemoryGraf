namespace App.Util {
    public class Validator {
        public static bool IsValid(string s) {
            return s != null && s.Length > 0;
        }
    }
}
