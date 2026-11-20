"""Budget answer evidence using an explicitly selected token counter."""
import json


def counter(encoding):
    if encoding == 'utf8-bytes':
        return lambda text: len(text.encode('utf-8'))
    try:
        import tiktoken
    except ImportError as exc:
        raise ValueError('Install unity-rag[tokens] to use a tiktoken encoding.') from exc
    try:
        codec = tiktoken.get_encoding(encoding)
    except ValueError as exc:
        raise ValueError('Unknown tiktoken encoding: ' + encoding) from exc
    return lambda text: len(codec.encode(text, disallowed_special=()))


def count_evidence(evidence, count):
    return count(json.dumps(evidence, ensure_ascii=False))


def fit_record(evidence, key, record, max_tokens, count, *, query='', focus=None, extra_fits=None):
    """Fit evidence using the selected counter and query-relevant source lines."""
    from .passage_selection import fit_source_record
    def fits(candidate):
        combined = {**evidence, key: candidate}
        return (count_evidence(combined, count) <= max_tokens
                and (extra_fits is None or extra_fits(combined)))
    return fit_source_record(record, fits, query=query, focus=focus)


class InputBudget:
    """Selected-tokenizer estimate including prompt/schema and an output reserve.

    The provider may use different framing/tokenization. This is a conservative
    local planning check, not an assertion about server-side token counts.
    """
    def __init__(self, limit, output_tokens, count, framing_tokens=256):
        if (type(limit) is not int or not 1 <= limit <= 2000000
                or type(output_tokens) is not int or output_tokens < 1):
            raise ValueError('Model context and output reserve must be positive integer token counts.')
        self.limit, self.output_tokens, self.count = limit, output_tokens, count
        self.framing_tokens = framing_tokens
        self.peak_input = 0

    def estimate(self, messages, schema):
        return self.count(json.dumps({'messages': messages, 'schema': schema}, ensure_ascii=False)) + self.framing_tokens

    def fits(self, messages, schema):
        return self.estimate(messages, schema) + self.output_tokens <= self.limit

    def generate(self, provider, messages, schema):
        estimate = self.estimate(messages, schema)
        if estimate + self.output_tokens > self.limit:
            raise ValueError('Estimated prompt plus reserved output exceeds the configured model context; no request sent.')
        self.peak_input = max(self.peak_input, estimate)
        return provider.generate(messages, schema)

    def report(self):
        return {'model_context_tokens': self.limit, 'reserved_output_tokens': self.output_tokens,
                'framing_reserve_tokens': self.framing_tokens, 'peak_estimated_input_tokens': self.peak_input,
                'scope': 'Selected tokenizer over serialized messages/schema plus framing reserve; provider tokenization may differ.'}
