"""Equivalence and conservative boundaries for simple declared assembly groups."""
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from unity_rag import code_map
from unity_rag.asset_map import asset_report
from unity_rag.workspace import refresh, resolve_workspace


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AsmdefReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.project = self.root/'Project'; self.workspace = self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project', self.project)
        self.alpha=self.project/'Assets'/'Alpha'; self.beta=self.project/'Assets'/'Beta'
        self.alpha.mkdir();self.beta.mkdir()
        (self.alpha/'Alpha.asmdef').write_text('{"name":"Alpha"}',encoding='utf-8')
        (self.beta/'Beta.asmdef').write_text('{"name":"Beta","references":[]}',encoding='utf-8')
        self.base=self.alpha/'Worker.cs';self.base.write_text('namespace A { public class Worker { public int Value() => 1; } }\n',encoding='utf-8')
        (self.alpha/'Use.cs').write_text('namespace A { public class Use { public int Get(Worker w) => w.Value(); } }\n',encoding='utf-8')
        (self.beta/'Other.cs').write_text('namespace B { public class Other { public int Get() => 2; } }\n',encoding='utf-8')
        refresh(self.workspace,self.project,semantic=False)

    def captured_refresh(self):
        calls=[];original=code_map._run
        def run(command, **kwargs):
            result=original(command,**kwargs)
            if 'input' in kwargs:
                calls.append(json.loads(kwargs['input']))
            return result
        with patch.object(code_map,'_run',side_effect=run):
            result=refresh(self.workspace,semantic=False)
        return result,calls

    def evidence(self):
        with sqlite3.connect(resolve_workspace(self.workspace)/'inventory.sqlite3') as db:
            return db.execute('SELECT * FROM evidence ORDER BY id').fetchall()

    def assert_clean_equivalent(self):
        code,assets,evidence=code_map.code_report(self.workspace),asset_report(self.workspace),self.evidence()
        refresh(self.workspace,semantic=False,rebuild=True)
        full_code,full_assets=code_map.code_report(self.workspace),asset_report(self.workspace)
        for left,right in ((code,full_code),(assets,full_assets)):
            for key in ('discovery_scanned_at','code_fingerprint'):
                left.pop(key,None);right.pop(key,None)
            self.assertEqual(left,right)
        self.assertEqual(evidence,self.evidence())

    def test_edit_add_delete_and_cross_file_invalidation_match_clean(self):
        added=self.alpha/'Probe.cs'
        for operation in ('edit','add','delete'):
            if operation == 'edit':
                self.base.write_text(self.base.read_text(encoding='utf-8').replace('Value()', 'Changed()'),encoding='utf-8')
            elif operation == 'add':
                added.write_text('namespace A { public class Probe { public Worker W; } }\n',encoding='utf-8')
            else:
                added.unlink()
            _,calls=self.captured_refresh()
            self.assertEqual(len(calls),1)
            self.assertEqual({f['assembly'] for f in calls[0]['files']},{'Alpha'})
            if operation == 'edit':
                self.assertTrue(any(d.get('path')=='Assets/Alpha/Use.cs' and d['severity']=='Error'
                                    for d in code_map.code_report(self.workspace)['diagnostics']))
            self.assert_clean_equivalent()

    def test_unsupported_declared_dependency_and_boundary_change_fall_back(self):
        definition=self.alpha/'Alpha.asmdef'
        definition.write_text('{"name":"Alpha","references":["Beta"]}',encoding='utf-8')
        result,_=self.captured_refresh()
        self.assertEqual(result['refresh_mode'],'full')
        self.base.write_text(self.base.read_text(encoding='utf-8')+'\n// change\n',encoding='utf-8')
        _,calls=self.captured_refresh()
        self.assertEqual(len(calls),1)
        self.assertEqual({f['assembly'] for f in calls[0]['files']},{'Alpha','Beta','Assembly-CSharp'})
        self.assert_clean_equivalent()

    def test_assembly_allowlist_rejects_ambiguous_or_nondefault_inputs(self):
        row=lambda path, value: {'path':path,'declaration':value}
        valid=[row('Assets/A/A.asmdef',{'name':'A'}),row('Assets/B/B.asmdef',{'name':'B','references':[]})]
        self.assertEqual(code_map._supported_assembly_groups(valid),{'A','B'})
        for invalid in ([row('Assets/A/A.asmref',{'reference':'A'})],
                        [row('Assets/A/A.asmdef',{'name':'A','references':['B']})],
                        [row('Assets/A/A.asmdef',{'name':'A','defineConstraints':['TEST']})],
                        [row('Assets/A/A.asmdef',{'name':'A'}),row('Assets/B/B.asmdef',{'name':'A'})],
                        [row('Assets/A/A.asmdef',{'name':'A'}),row('Assets/B/B.asmdef',{'name':'a'})],
                        [row('Assets/A/A.asmdef',{'name':'Assembly-CSharp'})],
                        [row('Assets/A/A.asmdef',{'name':'A','unknown':'value'})]):
            self.assertIsNone(code_map._supported_assembly_groups(invalid))
