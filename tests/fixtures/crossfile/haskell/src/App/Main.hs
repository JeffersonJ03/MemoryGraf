module App.Main (run) where
import qualified App.Util as U
import App.Fmt (pretty)
import App.Open
run :: Int -> Int
run x = U.check x + pretty x + opened x + local x
  where local y = y
