"""Line-preserving evidence budgets with an explicit, reproducible counter."""
from .context_packing import counter


DEFAULT_CHUNK_BUDGET = 4096
DEFAULT_CHUNK_ENCODING = 'utf8-bytes'


class ChunkPolicy:
    def __init__(self, budget=DEFAULT_CHUNK_BUDGET, encoding=DEFAULT_CHUNK_ENCODING):
        if type(budget) is not int or not 128 <= budget <= 65536:
            raise ValueError('Chunk budget must be an integer from 128 to 65536.')
        self.count = counter(encoding)
        self.config = {'version': 1, 'budget': budget, 'encoding': encoding,
                       'unit': 'bytes' if encoding == 'utf8-bytes' else 'tokens',
                       'max_source_lines': 120, 'overlap_lines': 20}
        self.oversized_lines = 0

    def windows(self, lines, *, max_lines=120, overlap=20, heading=''):
        """Yield [start,end) line windows; never truncate a long source line.

        A line that cannot fit alone remains intact and is reported. Splitting
        such a line would require column citations, which the format lacks.
        """
        first = 0
        while first < len(lines):
            last = first
            for candidate in range(first + 1, min(len(lines), first + max_lines) + 1):
                text = '\n'.join(([heading] if heading else []) + lines[first:candidate])
                if self.count(text) > self.config['budget']:
                    break
                last = candidate
            if last == first:
                last = first + 1
                self.oversized_lines += 1
            yield first, last
            if last == len(lines):
                break
            # A short token-limited chunk must still make forward progress.
            first = max(first + 1, last - min(overlap, (last - first) // 2))
