import json,sqlite3,tempfile,unittest
from pathlib import Path
from unity_rag.rig_catalog import lookup,label,family
class RigPriority(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.gen=root/"generations"/"one";self.gen.mkdir(parents=True)
        rows=[]
        for i,name,human,bones,imp in [(1,"Knight",True,30,"Human"),(2,"Crawler",False,12,"Generic"),(3,"RunClip",True,0,"Human"),(4,"KnightIncomplete",False,30,"Human")]:
            rows.append(dict(guid=str(i),path="Assets/"+name+".fbx",kind="model",bones=bones,binds=bones,meshes=int(bones>0),validHuman=human,assignedHuman=human,validGeneric=imp=="Generic",invalidAvatar=False,importer=imp,source_sha256="0"*64,line=i,report=dict(path="Docs/Catalog/RigEvidence-20261118.md",sha256="a"*64,size=90)))
        self.rows=rows
        with sqlite3.connect(root/"rig-catalog-v1.sqlite3") as db:
            db.execute("create table rigs(data)");db.executemany("insert into rigs values(?)",[(json.dumps(r),) for r in rows])
    def names(self,q):return [r["title"] for r in lookup(self.gen,q,"search",None,5)]
    def test_compat(self):
        self.assertEqual(self.names("humanoid rigged models")[0],"Knight")
        self.assertEqual(self.names("generic rigged models"),["Crawler"])
        self.assertEqual(label(self.rows[-1]),"configured_humanoid_unverified")
        self.assertEqual(family(self.rows[2]),"skeleton_or_animation_only")
    def test_scope(self):
        self.assertEqual(self.names("find Knight character models")[0],"Knight")
        for query in ["procedural ocean shader","how does the avatar script work","models","tower damage"]:
            self.assertEqual(self.names(query),[])
        self.assertFalse(lookup(self.gen,"rigged models","exact",None,5))
