"""Opt-in, project-scoped Unity rig evidence overlay; no model or network calls."""
import json,re,sqlite3
from pathlib import Path

def label(r):
    if r["validHuman"]: return "valid_humanoid_avatar"
    if r["importer"]=="Human": return "configured_humanoid_unverified"
    if r["validGeneric"]: return "valid_generic_avatar"
    if r["invalidAvatar"]: return "invalid_avatar"
    return "skin_binding_only_humanoid_unverified"

def family(r):
    if r["meshes"]==0: return "skeleton_or_animation_only"
    return "skinned_mesh" if r["bones"] or r["binds"] else "avatar_with_mesh_unconfirmed_binding"

def words(q):
    return set(re.findall(r"[a-z0-9]+",re.sub(r"([a-z])([A-Z])",r"\1 \2",q).lower()))

def lookup(workspace,query,mode,kind,limit):
    if mode not in ("search","hybrid","semantic") or kind not in (None,"rig_asset"):return []
    tokens=words(query)
    character_intent=bool(tokens & {"character","characters","creature","creatures","npc","npcs"}) and bool(tokens & {"model","models","prefab","prefabs","mesh","meshes","asset","assets","recommend","find","choose","choosing"})
    animation_intent=bool(tokens & {"animation","animations"}) and bool(tokens & {"compatible","compatibility","retarget","retargeting"})
    if not character_intent and not animation_intent and not tokens & set("rig rigs rigged humanoid humanoids retarget retargetable retargeting skinned avatar avatars skeletal skeleton".split()):return []
    if tokens & set("method methods class classes code script scripts function functions bug exceptions implement implementation shader".split()):return []
    w=Path(workspace)
    if w.parent.name!="generations":return []
    index=w.parent.parent/"rig-catalog-v1.sqlite3"
    if not index.is_file():return []
    c=sqlite3.connect(index.resolve().as_uri()+"?mode=ro",uri=True)
    try:rows=[json.loads(s) for s, in c.execute("select data from rigs")]
    finally:c.close()
    generic="generic" in tokens or "nonhumanoid" in tokens or {"non","humanoid"}<=tokens
    human=not generic and bool(tokens & {"humanoid","humanoids","retarget","retargetable","retargeting"})
    specific=tokens-set("a an are as assets asset avatar avatars can character characters compatible compatibility do does evidence find for from game give has have humanoid humanoids i in is it list me meshes mesh model models my of on only or our prefab prefabs project recommend retarget retargetable retargeting rig rigs rigged rigging show skeletal skeleton skinned some source supported supports that the their these to unity use used using valid verified what which with animation animations available and generic non nonhumanoid priority please choose choosing could would work playable also give options".split())
    hits=[]
    for r in rows:
        lbl=label(r);fam=family(r);path=r["path"]; pt=words(path);name=Path(path).stem
        if generic and lbl!="valid_generic_avatar":continue
        if character_intent and fam!="skinned_mesh":continue
        if tokens & {"prefab","prefabs"} and r["kind"]!="prefab":continue
        if specific and not (specific & pt):continue
        if human and not r["validHuman"] and not specific and lbl!="configured_humanoid_unverified":continue
        score=(len(specific&words(name)),len(specific&pt),int(not human or r["validHuman"]),int(fam=="skinned_mesh"),int("@" not in name),int("backup" not in path.lower()),int(r["assignedHuman"]),int(r["kind"]=="model"))
        hits.append((score,path,r))
    hits.sort(key=lambda t:(tuple(-n for n in t[0]),t[1].casefold()))
    out=[]
    for _,path,r in hits[:limit]:
        ident="rig:"+r["guid"]; desc=describe(r)
        out.append(dict(id=ident,entity_id=ident,kind="rig_asset",evidence_type="unity_editor_rig_snapshot",title=Path(path).stem,text=desc,sources=[dict(path=r["report"]["path"],start_line=r["line"],end_line=r["line"],sha256=r["report"]["sha256"])],parent=None,detail=r))
    return out


def check_rig_snapshot(project,item):
    import hashlib
    r=item["detail"]; path=(Path(project)/r["path"]).resolve()
    if not path.is_relative_to((Path(project)/"Assets").resolve()):return dict(path=r["path"],status="excluded")
    try:
        for p,key in [(path,"source_sha256"),(Path(str(path)+".meta"),"meta_sha256")]:
            h=hashlib.sha256()
            with p.open("rb") as f:
                for block in iter(lambda:f.read(1048576),b""):h.update(block)
            if h.hexdigest()!=r[key]:return dict(path=r["path"],status="stale")
    except OSError:return dict(path=r["path"],status="unreadable")
    return dict(path=r["path"],status="current_source_and_meta",sha256=r["source_sha256"],note="Transitive dependencies and avatar validity reflect the Unity scan, not a new import.")

def describe(r):
    path=r["path"];return f"{path} | {family(r)} | {label(r)}. Unity importer: {r['importer'] or 'not applicable'}; {r['meshes']} meshes, {r['bones']} bound transforms, maximum {r['binds']} bind poses per renderer. Valid human Avatar assigned to an Animator in this hierarchy: {r['assignedHuman']}. The valid-human flag is evidence of a Unity Humanoid Avatar, not proof every mesh uses it. No retargeted playback test; a non-Humanoid setup does not rule out future conversion."

def report_entry(row):
    v=row["detail"]["report"]
    if v["path"]!="Docs/Catalog/RigEvidence-20261118.md" or type(v["size"]) is not int or not 0<v["size"]<16000000 or not re.fullmatch("[0-9a-f]{64}",v["sha256"]):raise ValueError("Invalid catalog citation")
    return dict(v,status="supported")
