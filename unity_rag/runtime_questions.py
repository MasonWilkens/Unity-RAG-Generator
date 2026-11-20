"""Conservative routing for explicit live-state questions, not general intent classification."""
import re


def runtime_requirement(question):
    text = ' '.join(question.casefold().split())
    # Require explicit observation context. Static/hypothetical questions stay with retrieval.
    if re.search(r'\b(hypothetical|assume|assuming|if|configured|configuration|source code|implementation)\b', text):
        return None
    live = re.search(r'\b(right now|currently running|running game|live game|on my machine)\b', text)
    state = re.search(r'\b(current|live)\s+(health|mana|position|rotation|velocity|score|inventory|scene|inspector values?)\b', text)
    timing = re.search(r'\b(how many|how long|how fast|what is)\b', text) and re.search(
        r'\b(milliseconds|frame time|frames per second|fps|per frame)\b', text)
    if live and (state or timing):
        return 'Current game state or performance requires a live Editor/runtime observation; a static repository snapshot cannot provide that observation.'
    return None
