(ns my-app.util)

(defprotocol Shape
  (area [this]))

(defn check [x] x)
