import json
from pathlib import Path
import tempfile
import unittest
from unity_rag.answer_review import review_template, review_answers

class AnswerReviewTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
  self.report=self.root/'report.json'; self.judgments=self.root/'review.json'
  self.report.write_text(json.dumps({'results':[{'id':'case','checks':{'valid_output_and_citations':True}}]}))
 def tearDown(self): self.temp.cleanup()
 def completed(self):
  value=review_template(self.report); value.update(reviewer='Test reviewer',reviewer_kind='agent')
  value['reviews'][0].update(correctness=True,support=True,completeness=True,abstention=True,rationale='Verified against fixture lines.')
  return value
 def check(self,value):
  self.judgments.write_text(json.dumps(value)); return review_answers(self.report,self.judgments)
 def test_complete_review_passes(self): self.assertTrue(self.check(self.completed())['accepted'])
 def test_incomplete_or_negative_review_cannot_pass(self):
  value=self.completed(); value['reviews'][0]['support']=False
  self.assertEqual(self.check(value)['results'][0]['failed_criteria'],['support'])
  value['reviews'][0]['support']=None
  with self.assertRaisesRegex(ValueError,'explicitly'): self.check(value)
 def test_changed_report_invalidates_review(self):
  value=self.completed(); self.report.write_text(self.report.read_text()+'\n')
  with self.assertRaisesRegex(ValueError,'exact'): self.check(value)
 def test_failed_contract_or_runtime_expectation_cannot_be_overridden(self):
  for checks in ({'valid_output_and_citations':False}, {'valid_output_and_citations':True,'expected_runtime_flag':False}):
   self.report.write_text(json.dumps({'results':[{'id':'case','checks':checks}]}))
   self.assertFalse(self.check(self.completed())['accepted'])
 def test_wrong_or_missing_case_rejected(self):
  value=self.completed(); value['reviews'][0]['id']='different'
  with self.assertRaisesRegex(ValueError,'IDs differ'): self.check(value)
  value['reviews']=[]
  with self.assertRaisesRegex(ValueError,'every'): self.check(value)
 def test_requires_named_reviewer_and_rationale(self):
  value=self.completed(); value['reviewer']=' '
  with self.assertRaisesRegex(ValueError,'reviewer'): self.check(value)
  value=self.completed(); value['reviews'][0]['rationale']=''
  with self.assertRaisesRegex(ValueError,'rationale'): self.check(value)

 def test_cli_failure_sets_exit_code(self):
  import subprocess,sys
  value=self.completed();value['reviews'][0]['correctness']=False
  self.judgments.write_text(json.dumps(value))
  result=subprocess.run([sys.executable,'-m','unity_rag.cli','review-answers',str(self.report),str(self.judgments)],capture_output=True,text=True)
  self.assertEqual(result.returncode,1)
  self.assertFalse(json.loads(result.stdout)['accepted'])
