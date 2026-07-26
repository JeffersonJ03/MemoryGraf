using App.Util;

namespace App {
    public class Service {
        public bool Run(string s) {
            return Validator.IsValid(s);
        }
    }
}
