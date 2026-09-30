import os, sys, tempfile, subprocess, json
from memorygraf.store import Store
from memorygraf.indexer import Indexer
from memorygraf import analyze as an
print("python", sys.version, "cwd", os.getcwd(), "TMPDIR", tempfile.gettempdir())
print(subprocess.run(["git", "--version"], capture_output=True, text=True).stdout)
t = tempfile.mkdtemp(prefix="mg_test_"); p = os.path.join(t, "proj"); os.makedirs(p)
open(os.path.join(p, "mod.py"), "w").write("def work():\n    return 1\n")
for i in range(4):
    open(os.path.join(p, f"c{i}.py"), "w").write(f"from mod import work\n\ndef f{i}():\n    return work()\n")
print("walk", list(os.walk(p)))
print("inside git?", subprocess.run(["git", "-C", p, "rev-parse", "--is-inside-work-tree", "--show-toplevel"], capture_output=True, text=True))
st = Store(os.path.join(t, "g.db"))
print("counters", Indexer(st, {"projects": [{"name": "proj", "root": p}]}).index_all())
for n in st.all_nodes(): print("NODE", n["id"], n["type"])
for e in st.all_edges(): print("EDGE", e["source"], "->", e["target"], e["type"], e.get("provenance"))
r = an.analyze(st); print("ANALYZE", json.dumps({k: r[k] for k in ("totals", "thresholds", "god_nodes")}, default=str))
c = os.path.join("tests", "fixtures", "crossfile", "c")
print("walk C", list(os.walk(c)))
