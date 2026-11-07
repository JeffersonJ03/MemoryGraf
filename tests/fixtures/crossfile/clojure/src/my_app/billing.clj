(ns my-app.billing
  (:require [my-app.util :as u]
            [my-app.fmt :refer [pretty]]
            [my-app.plat :as p]
            [clojure.string :as str]))

(defn- local [x] x)

(defn run [x]
  (u/check x)
  (u/area x)
  (pretty x)
  (p/go)
  (local x)
  (str/join "," [x]))
