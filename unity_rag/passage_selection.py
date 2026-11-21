"""Choose contiguous source lines without changing entity ranking or source text."""
from collections import Counter


def _query_line(lines, query, first=0, last=None, title=''):
    from .retrieval import terms
    last = len(lines) - 1 if last is None else last
    wanted = set(terms(query)) - {'and', 'or', 'with', 'from', 'this', 'that', 'it', 'when', 'can', 'be'}
    line_terms = [set(terms(line)) for line in lines]
    behavior_terms = wanted - set(terms(title))
    # Naming a method scopes the search; its repeated owner/signature should
    # not defeat a distinct requested behavior in the body of that same chunk.
    if behavior_terms and any(tokens & behavior_terms for tokens in line_terms[first:last + 1]):
        wanted = behavior_terms
    matches = [tokens & wanted for tokens in line_terms]
    frequencies = Counter(term for match in matches for term in match)
    scores = [sum(1 / frequencies[t] for t in match) for match in matches]
    candidates = range(first, last + 1)
    best = max(candidates, key=lambda i: (scores[i], -i), default=None)
    return best if best is not None and scores[best] > 0 else None


def source_lines(record):
    """Return the sole text source; dependency citations are not text sources."""
    sources = [s for s in record.get('sources', []) if s.get('role') != 'reference_dependency']
    if record.get('evidence_type') != 'original_source' or len(sources) != 1:
        return None
    source = sources[0]
    start, end = source.get('start_line'), source.get('end_line')
    lines = record.get('text', '').splitlines()
    if type(start) is not int or type(end) is not int or start < 1 or end - start + 1 != len(lines):
        return None
    return source, lines


def focus_lines(record, query=''):
    """Return a zero-based inclusive line range and the reason for selecting it.

    Parsed behavior spans take priority, then the winning semantic window when
    that channel supplied the passage, then local lexical evidence. No lines
    from another chunk, partial declaration or source file can be borrowed.
    """
    source_and_lines = source_lines(record)
    if source_and_lines is None:
        return None
    source, lines = source_and_lines
    diagnostics = record.get('ranking_diagnostics', {})
    spans = (diagnostics.get('type_comparison', {}).get('reference_spans')
             or diagnostics.get('cross_file_call', {}).get('reference_spans')
             or diagnostics.get('multipart_code', {}).get('reference_spans')
             or diagnostics.get('behavior_anchor', {}).get('reference_spans', []))
    valid = [s for s in spans if s.get('path') == source['path']
             and type(s.get('start_line')) is int and type(s.get('end_line')) is int
             and source['start_line'] <= s['start_line'] <= s['end_line'] <= source['end_line']]
    if valid:
        return (min(s['start_line'] for s in valid) - source['start_line'],
                max(s['end_line'] for s in valid) - source['start_line'], 'parsed_references')
    passage = diagnostics.get('semantic_passage') or {}
    if diagnostics.get('selected_passage') == 'semantic':
        start, end = passage.get('start_char'), passage.get('end_char')
        body = record['text']
        if type(start) is int and type(end) is int and 0 <= start < end <= len(body):
            return (body.count('\n', 0, start), body.count('\n', 0, end - 1), 'semantic_window')
    if query:
        index = _query_line(lines, query, title=record.get('title', ''))
        if index is not None:
            return index, index, 'query_terms'
    return None


def clip_source(record, first, last, reason):
    """Slice whole source lines and preserve all freshness dependencies."""
    source, lines = source_lines(record)
    candidate = {**record, 'text': '\n'.join(lines[first:last + 1]),
                 'sources': [{**s, 'start_line': source['start_line'] + first,
                              'end_line': source['start_line'] + last} if s is source else dict(s)
                             for s in record['sources']]}
    candidate['packing'] = {'truncated': True, 'original_start_line': source['start_line'],
                            'original_end_line': source['end_line'], 'selection': reason}
    if reason == 'prefix':
        candidate['packing'] = {'truncated': True, 'original_end_line': source['end_line']}
    if record.get('id'):
        candidate['packing']['original_chunk_id'] = record['id']
    return candidate


def fit_source_record(record, fits, *, query='', focus=None):
    """Fit a full record or a contiguous window measured with the actual counter.

    Examine every window size rather than assuming tokenizer counts are
    monotonic. Windows grow around the selected focus, never stitch passages.
    Parsed facts and records with ambiguous text provenance are not shortened.
    """
    if fits(record):
        return record, False
    value = source_lines(record)
    if value is None:
        return None, False
    _, lines = value
    focus = focus or focus_lines(record, query)
    if focus is None:
        first, last, reason = 0, 0, 'prefix'
    else:
        first, last, reason = focus
        if not (0 <= first <= last < len(lines)):
            return None, False
    center = (first + last) // 2
    if query and focus:
        match = _query_line(lines, query, first, last, record.get('title', ''))
        if match is not None:
            center = match
    best = None
    for size in range(1, len(lines)):
        start = max(0, min(len(lines) - size, center - (size - 1) // 2)) if focus else 0
        if focus and size >= last - first + 1:
            start = min(first, max(last - size + 1, start))
        candidate = clip_source(record, start, start + size - 1, reason)
        if fits(candidate):
            best = candidate
    return best, best is not None
