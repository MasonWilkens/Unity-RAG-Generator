import unittest
from unity_rag.question_guidance import question_guidance,UNIT_GUIDANCE

class QuestionGuidanceTests(unittest.TestCase):
 def test_measurement_questions_receive_unit_constraint(self):
  for question in ['When does Enemy.Tick update nextAttackTime, and by how much?', 'What units does movement speed use?', 'How far does the object move?']:
   self.assertEqual(question_guidance(question),UNIT_GUIDANCE)
 def test_other_question_types_do_not_change_the_general_prompt(self):
  for question in ['Explain how sortTickets orders battle actions.', 'Where are the six combative references assigned in PlayerParty.prefab?', 'Which move slot does MBAI actually select?', 'What server endpoint does the game use?']:
   self.assertEqual(question_guidance(question),'')
