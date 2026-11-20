"""Publish a completed editor probe into a fresh local SQLite sidecar."""
import hashlib,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
def digest(p):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda:f.read(1048576),b""): h.update(block)
    return h.hexdigest()
def build(scan,project,dest):
    rows=[json.loads(line) for line in scan.read_text(encoding="utf-8-sig").splitlines()]
    summary=rows.pop()
    if not summary.get("complete") or sum("error" not in r for r in rows)!=summary["saved"]: raise ValueError("Incomplete probe")
    tmp=dest.with_suffix(".building.sqlite3")
    if dest.exists() or tmp.exists():raise FileExistsError("Use a fresh index destination")
    doc_rel="Docs/Catalog/RigEvidence-20261118.md"
    doc=project/doc_rel
    doc.parent.mkdir(parents=True,exist_ok=True)
    if doc.exists():raise FileExistsError(doc)
    records=[]; lines=["# Unity imported rig evidence","Editor snapshot. Mesh bindings and Avatar validity are not animation playback tests."]
    db=sqlite3.connect(tmp)
    try:
        db.execute("create table rigs(guid text primary key,path text unique not null,data text not null)")
        db.execute("create table info(data text not null)")
        db.execute("insert into info values(?)",(json.dumps(summary),))
        for r in rows:
            if "error" in r:continue
            # A default Generic Avatar on a static box is not a rig.
            if not (r["bones"] or r["binds"] or r["validHuman"] or r["importer"]=="Human"):continue
            source=(project/r["path"]).resolve()
            if not source.is_relative_to((project/"Assets").resolve()):raise ValueError("Invalid source scope")
            r["source_sha256"]=digest(source);r["meta_sha256"]=digest(Path(str(source)+".meta"))
            from unity_rag.rig_catalog import describe
            lines.append(describe(r));r["line"]=len(lines);records.append(r)
        with doc.open("x",encoding="utf8",newline="\n") as f:f.write("\n".join(lines)+"\n")
        signature=dict(path=doc_rel,sha256=digest(doc),size=doc.stat().st_size)
        for r in records:
            r["report"]=signature
            db.execute("insert into rigs values(?,?,?)",(r["guid"],r["path"],json.dumps(r)))
        db.commit()
        if db.execute("pragma integrity_check").fetchone()!=("ok",):raise ValueError("Invalid database")
    finally:db.close()
    tmp.rename(dest);print(summary)
if __name__=="__main__":build(*map(Path,sys.argv[1:]))
