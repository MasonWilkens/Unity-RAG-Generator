import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from unity_rag import code_map
from unity_rag.asset_map import asset_report
from unity_rag.workspace import refresh, resolve_workspace


@unittest.skipUnless(shutil.which('dotnet'), 'Requires .NET SDK')
class AssemblyReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.project = self.root/'Project'; self.workspace = self.root/'Index'
        shutil.copytree(Path(__file__).parent/'fixtures'/'retrieval_project', self.project)
        self.editor = self.project/'Assets'/'Editor'/'Inspector.cs'; self.editor.parent.mkdir()
        self.editor.write_text('namespace Tooling { public class Inspector { public int Count() => 4; } }\n', encoding='utf-8')
        self.consumer = self.project/'Assets'/'Consumer.cs'
        self.consumer.write_text('namespace Game { public class Consumer { public int Use(Targeting t) => t.FindTarget(4); } }\n', encoding='utf-8')
        refresh(self.workspace,self.project,semantic=False)

    def capture_refresh(self, *, alter_output=None, alter_previous=None):
        calls = []; original_run = code_map._run; original_reuse = code_map._reuse_assembly_groups
        def run(command, **kwargs):
            value = original_run(command, **kwargs)
            if 'input' in kwargs:
                calls.append(json.loads(kwargs['input']))
                if alter_output and len(calls) == 1:
                    data = json.loads(value); data['environment_fingerprint'] = alter_output
                    return json.dumps(data)
            return value
        def reuse(helper, files, previous, *args):
            previous = copy.deepcopy(previous)
            if alter_previous:
                alter_previous(previous)
            return original_reuse(helper,files,previous,*args)
        with patch.object(code_map,'_run',side_effect=run), patch.object(code_map,'_reuse_assembly_groups',side_effect=reuse):
            result = refresh(self.workspace,semantic=False)
        return result,calls

    def assert_clean_equivalent(self):
        left_code = code_map.code_report(self.workspace); left_assets = asset_report(self.workspace)
        refresh(self.workspace,semantic=False,rebuild=True)
        right_code = code_map.code_report(self.workspace); right_assets = asset_report(self.workspace)
        for left,right in ((left_code,right_code),(left_assets,right_assets)):
            for key in ('discovery_scanned_at','code_fingerprint'):
                left.pop(key,None);right.pop(key,None)
            self.assertEqual(left,right)

    def test_baseline_reuse_cross_group_edits_additions_and_deletions(self):
        baseline = code_map.code_report(self.workspace)
        self.assertRegex(baseline['environment_fingerprint'], r'^[0-9a-f]{64}$')
        self.assertEqual(set(baseline['group_fingerprints']), {'Assembly-CSharp','Assembly-CSharp-Editor'})
        changed = self.project/'Assets'/'Targeting.cs'; added = self.project/'Assets'/'Extra.cs'
        for kind in ('edit','add','delete','editor','order_flip','remove_group','restore_group'):
            if kind == 'edit':
                changed.write_text(changed.read_text(encoding='utf-8').replace('FindTarget(int id)', 'FindById(int id)'),encoding='utf-8')
            elif kind == 'add':
                added.write_text('namespace Game { public class Extra { public bool Check(Enemy e) => e.CanAttack(1); } }\n',encoding='utf-8')
            elif kind == 'delete':
                added.unlink()
            elif kind == 'editor':
                self.editor.write_text(self.editor.read_text(encoding='utf-8').replace('=> 4','=> 5'),encoding='utf-8')
            elif kind == 'order_flip':
                self.consumer.unlink()
            elif kind == 'remove_group':
                self.editor.unlink()
            else:
                self.editor.write_text('namespace Tooling { public class Inspector { public int Count() => 6; } }\n',encoding='utf-8')
            _, calls = self.capture_refresh()
            self.assertEqual(len(calls),1)
            group = 'Assembly-CSharp-Editor' if kind in ('editor','restore_group') else 'Assembly-CSharp'
            expected = set() if kind == 'remove_group' else {group}
            self.assertEqual({f['assembly'] for f in calls[0]['files']},expected)
            if kind == 'edit':
                report = code_map.code_report(self.workspace)
                self.assertTrue(any(d.get('path') == 'Assets/Consumer.cs' and d['severity'] == 'Error'
                                    for d in report['diagnostics']))
            if kind == 'order_flip':
                report = code_map.code_report(self.workspace)
                self.assertEqual(report['files'][0]['assembly'], 'Assembly-CSharp-Editor')
                self.assertEqual(report['declarations'][0]['assembly'], 'Assembly-CSharp-Editor')
            self.assert_clean_equivalent()

    def test_missing_provenance_and_group_tampering_reanalyze_every_group(self):
        path = self.project/'Assets'/'Targeting.cs'
        for field in ('environment_fingerprint','group_fingerprints'):
            path.write_text(path.read_text(encoding='utf-8')+'\n// change\n',encoding='utf-8')
            _, calls = self.capture_refresh(alter_previous=lambda p: p.pop(field,None))
            self.assertEqual(len(calls),1)
            self.assertEqual({f['assembly'] for f in calls[0]['files']}, {'Assembly-CSharp','Assembly-CSharp-Editor'})
            self.assert_clean_equivalent()

    def test_changed_runtime_analyzer_or_reference_fingerprint_falls_back(self):
        path = self.project/'Assets'/'Targeting.cs'
        for reason in ('runtime','analyzer','reference'):
            path.write_text(path.read_text(encoding='utf-8')+f'\n// {reason}\n',encoding='utf-8')
            _, calls = self.capture_refresh(alter_output='0'*64)
            self.assertEqual(len(calls),2)
            self.assertEqual({f['assembly'] for f in calls[0]['files']},{'Assembly-CSharp'})
            self.assertEqual({f['assembly'] for f in calls[1]['files']},{'Assembly-CSharp','Assembly-CSharp-Editor'})
            self.assert_clean_equivalent()

    def test_actual_reference_and_analyzer_bytes_change_fingerprint(self):
        analyzer = self.workspace/'analyzer'/'UnityRag.Analyzer.dll'
        copied = self.root/'copy-analyzer'
        shutil.copytree(analyzer.parent,copied)
        helper = copied/analyzer.name
        reference = self.root/'Reference.dll'
        shutil.copyfile(helper,reference)
        def signature():
            request={'files':[],'defines':[],'references':[str(reference)],'language_version':'10'}
            result=json.loads(code_map._run(['dotnet',str(helper)],input=json.dumps(request)))
            self.assertRegex(result['environment_fingerprint'],r'^[0-9a-f]{64}$')
            return result['environment_fingerprint']
        before=signature()
        with reference.open('ab') as stream:
            stream.write(b'\0')
        after_reference=signature()
        self.assertNotEqual(before,after_reference)
        with helper.open('ab') as stream:
            stream.write(b'\0')
        self.assertNotEqual(after_reference,signature())

    def test_reference_configuration_change_rejects_reuse_before_analyzing(self):
        report=code_map.code_report(self.workspace)
        files=[{**f,'text':''} for f in report['files']]
        helper=self.workspace/'analyzer'/'UnityRag.Analyzer.dll'
        with patch.object(code_map,'_run',side_effect=AssertionError('unexpected analysis')):
            self.assertIsNone(code_map._reuse_assembly_groups(helper,files,report,[],['different.dll'],'10',[],[]))

    def test_declared_or_unresolved_assembly_boundaries_reject_reuse(self):
        report=code_map.code_report(self.workspace)
        files=[{**f,'text':''} for f in report['files']]
        helper=self.workspace/'analyzer'/'UnityRag.Analyzer.dll'
        with patch.object(code_map,'_run',side_effect=AssertionError('unexpected analysis')):
            self.assertIsNone(code_map._reuse_assembly_groups(helper,files,report,[],[],'10',[{'path':'Assets/X.asmdef'}],[]))
            self.assertIsNone(code_map._reuse_assembly_groups(helper,files,report,[],[],'10',[],[{'reason':'unresolved_assembly_boundary'}]))
