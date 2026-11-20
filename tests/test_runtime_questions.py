import unittest
from unity_rag.runtime_questions import runtime_requirement

class RuntimeQuestionTests(unittest.TestCase):
 def test_explicit_live_questions_require_observation(self):
  for text in ['What is the current health of the Goblin instance in my running game?',
               'How many milliseconds does this battle simulation take per frame on my machine right now?',
               'What is the live position of the player in the live game?']:
   self.assertIsNotNone(runtime_requirement(text))
 def test_static_and_hypothetical_questions_are_not_routed(self):
  for text in ['If health is 30 and damage is 10, what is the result?',
               'What is the configured target FPS?', 'How does the current health field work in source code?',
               'What is the server endpoint?', 'Summarize the notes about a 1000 FPS claim.',
               'Assuming the current health is 30 in my running game, subtract 10.']:
   self.assertIsNone(runtime_requirement(text))
