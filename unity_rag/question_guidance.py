"""Small, explicit question-specific constraints; never insert project facts."""
import re

UNIT_GUIDANCE = '''Preserve unspecified measurement units. A numeric constant added to a parameter is in
that parameter's units. Call it seconds, meters, etc. only when the supplied code or caller
explicitly establishes the units. If units are not established, state the expression without
inventing units (for example, an increment in the input parameter's units).'''


def question_guidance(question):
    if re.search(r'\b(tick|cooldown|timer|interval|duration|distance|units?|speed|velocity)\b|\bhow (much|far|long)\b', question, re.I):
        return UNIT_GUIDANCE
    return ''
