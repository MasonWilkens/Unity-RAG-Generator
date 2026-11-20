"""Opt-in OpenAI Responses adapter with persistent, conservative spend reservations."""
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

from .generation import NoRedirect

MODEL = 'gpt-6-luna'
ENDPOINT = 'https://api.openai.com/v1/responses'
PRICING_URL = 'https://developers.openai.com/api/docs/models/gpt-6-luna'
PRICING_CHECKED = '2026-11-13'
PRICING_VALID_THROUGH = '2026-11-20'
# Standard rates: $0.10 input, $0.125 30m cache writes, $0.50 output / million.
# Flex is 50% of Standard. Reserve the FULL model context at long-context
# cache-write pricing (highest input rate), plus capped output at long rates.
# This deliberately does not rely on a local tokenizer or byte/token estimate.
INPUT_NANODOLLARS = 125
OUTPUT_NANODOLLARS = 375
# Explicit 30m caching rules out the more expensive 12h write rate.
# https://developers.openai.com/api/docs/guides/prompt-caching#cache-lifetime
SERVICE_RATES = {'flex': (INPUT_NANODOLLARS, OUTPUT_NANODOLLARS),
                 'default': (250, 750)}
MAX_REQUEST_BYTES = 65536
MAX_INPUT_TOKENS = 1_050_000
KEY_PATTERN = re.compile(r'(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])')


def _nanodollars(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not Decimal('0') < amount <= Decimal('56'):
            raise ValueError
        scaled = amount * 1_000_000_000
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (ValueError, InvalidOperation):
        raise ValueError('Budget must be greater than zero and at most $56, with at most 9 decimal places.') from None


def load_credential(path):
    """Read only the explicitly selected local file; never modify or echo it."""
    try:
        target = Path(path).absolute()
        for component in (target, *target.parents):
            info = component.lstat()
            if component.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError
        with target.open('r', encoding='utf-8-sig') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError
        candidates = set(KEY_PATTERN.findall(raw))
        if len(candidates) != 1:
            raise ValueError
        return candidates.pop()
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ValueError('Cannot load exactly one API key from the selected credential file; content suppressed.') from None


class SpendLedger:
    """Reserve before network I/O, never refund uncertain outcomes, share across processes."""
    def __init__(self, path, budget_usd):
        self.path = Path(path).absolute()
        cap = _nanodollars(budget_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), cap INTEGER NOT NULL, reserved INTEGER NOT NULL, blocked INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, reserved INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER)')
            db.execute('INSERT OR IGNORE INTO budget VALUES (1, ?, 0, 0)', (cap,))
            if db.execute('SELECT cap FROM budget WHERE id=1').fetchone()[0] != cap:
                raise ValueError('Ledger budget is fixed; requested budget differs. Do not reset a ledger to bypass its cap.')

    def reserve(self, amount):
        if type(amount) is not int or amount <= 0:
            raise ValueError('Reservation must be a positive integer number of nanodollars.')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            cap, used, blocked = db.execute('SELECT cap,reserved,blocked FROM budget WHERE id=1').fetchone()
            pending = db.execute('SELECT COUNT(*) FROM attempts WHERE input_tokens IS NULL OR output_tokens IS NULL').fetchone()[0]
            if blocked or pending or used + amount > cap:
                raise ValueError('Local OpenAI spending limit reached or ledger blocked; no request sent.')
            db.execute('UPDATE budget SET reserved=reserved+? WHERE id=1', (amount,))
            return db.execute('INSERT INTO attempts(reserved) VALUES (?)', (amount,)).lastrowid

    def record(self, attempt, input_tokens, output_tokens):
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise ValueError('Measured token counts must be nonnegative integers.')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            updated = db.execute('UPDATE attempts SET input_tokens=?,output_tokens=? WHERE id=? AND input_tokens IS NULL AND output_tokens IS NULL',
                                 (input_tokens, output_tokens, attempt))
            if updated.rowcount != 1:
                raise ValueError('Usage can only be recorded once for an existing pending attempt.')

    def block(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('UPDATE budget SET blocked=1 WHERE id=1')

    def summary(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            cap, reserved, blocked = db.execute('SELECT cap,reserved,blocked FROM budget WHERE id=1').fetchone()
            attempts, measured, inputs, outputs = db.execute('SELECT COUNT(*),COUNT(input_tokens),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM attempts').fetchone()
        return {'budget_usd': cap / 1e9, 'reserved_upper_usd': reserved / 1e9,
                'attempts': attempts, 'measured_responses': measured,
                'input_tokens': inputs, 'output_tokens': outputs,
                'unmeasured_attempts': attempts - measured,
                'blocked': bool(blocked or attempts != measured)}


class OpenAIProvider:
    def __init__(self, model=MODEL, *, api_key_file, ledger_path, budget_usd,
                 allow_remote=False, timeout=60, max_output_tokens=2048, max_calls=3,
                 service_tier='flex'):
        if not allow_remote:
            raise ValueError('OpenAI receives retrieved project evidence; explicitly set --allow-remote.')
        if model != MODEL:
            raise ValueError('Only the price-verified gpt-6-luna model is enabled; no automatic fallback.')
        if not isinstance(service_tier, str) or service_tier not in SERVICE_RATES:
            raise ValueError('Only explicitly priced default (Standard) and flex service tiers are enabled.')
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('OpenAI timeout must be 1-120 seconds.')
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 8192:
            raise ValueError('OpenAI output budget must be 256-8192 tokens, including reasoning.')
        if type(max_calls) is not int or not 1 <= max_calls <= 100:
            raise ValueError('OpenAI call limit must be 1-100.')
        if not api_key_file or not ledger_path:
            raise ValueError('Explicit credential file and persistent spend ledger are required.')
        self._credential_file = Path(api_key_file)
        self.ledger = SpendLedger(ledger_path, budget_usd)
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.max_calls = max_calls
        self.service_tier = service_tier
        self.calls = 0
        self.last_usage = None

    @property
    def config(self):
        return {'provider': 'openai', 'model': MODEL, 'endpoint': ENDPOINT,
                'remote_transfer_allowed': True, 'store': False, 'service_tier': self.service_tier,
                'prompt_cache_ttl': '30m',
                'reasoning_effort': 'none', 'max_output_tokens': self.max_output_tokens,
                'max_calls': self.max_calls, 'automatic_retries': 0,
                'pricing_checked': PRICING_CHECKED, 'pricing_url': PRICING_URL,
                'spend': self.ledger.summary(), 'last_usage': self.last_usage}

    def generate(self, messages, schema):
        if not date.fromisoformat(PRICING_CHECKED) <= date.today() <= date.fromisoformat(PRICING_VALID_THROUGH):
            raise ValueError('Reverify official OpenAI pricing before refreshing the price-validity window.')
        if self.calls >= self.max_calls:
            raise ValueError('OpenAI call limit reached; no request sent.')
        if not isinstance(messages, list) or not 1 <= len(messages) <= 8:
            raise ValueError('OpenAI accepts 1-8 text messages only.')
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                    or message['role'] not in ('system', 'user', 'assistant', 'developer')
                    or not isinstance(message['content'], str)):
                raise ValueError('OpenAI accepts plain text messages without files, tools, or implicit context.')
        body = {'model': MODEL, 'input': messages, 'store': False,
                'service_tier': self.service_tier, 'reasoning': {'effort': 'none'},
                'prompt_cache_options': {'mode': 'implicit', 'ttl': '30m'},
                'max_output_tokens': self.max_output_tokens,
                'text': {'format': {'type': 'json_schema', 'name': 'unity_rag_answer',
                                    'schema': schema, 'strict': True}}}
        raw_body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        if len(raw_body) > MAX_REQUEST_BYTES:
            raise ValueError('OpenAI request exceeds 64 KiB; reduce the retrieval context budget.')
        if KEY_PATTERN.search(raw_body.decode('utf-8')) or b'PRIVATE KEY-----' in raw_body:
            raise ValueError('Potential credential material in request; upload refused.')
        key = load_credential(self._credential_file)
        input_rate, output_rate = SERVICE_RATES[self.service_tier]
        reserve = MAX_INPUT_TOKENS * input_rate + self.max_output_tokens * output_rate
        attempt = self.ledger.reserve(reserve)
        self.calls += 1
        self.last_usage = None
        request = Request(ENDPOINT, data=raw_body, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        key = None
        try:
            # HTTPS verification stays enabled. No proxy, redirect, retry, or alternate host.
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('OpenAI response exceeded the size limit.')
            envelope = json.loads(raw)
            if envelope.get('service_tier') != self.service_tier:
                self.ledger.block()
                raise ValueError('OpenAI returned an unexpected service tier; ledger blocked, no fallback.')
            returned_model = envelope.get('model')
            if (not isinstance(returned_model, str) or
                    not re.fullmatch(re.escape(MODEL) + r'(?:-\d{4}-\d{2}-\d{2})?', returned_model)):
                self.ledger.block()
                raise ValueError('OpenAI returned an unverified model; ledger blocked, no fallback.')
            usage = envelope.get('usage', {})
            inputs, outputs = usage.get('input_tokens'), usage.get('output_tokens')
            if (type(inputs) is not int or type(outputs) is not int
                    or not 0 <= inputs <= MAX_INPUT_TOKENS
                    or not 0 <= outputs <= self.max_output_tokens):
                self.ledger.block()
                raise ValueError('OpenAI usage missing or outside the reserved bound; ledger blocked.')
            self.ledger.record(attempt, inputs, outputs)
            self.last_usage = {'attempt_id': attempt, 'model': returned_model,
                               'service_tier': self.service_tier,
                               'input_tokens': inputs, 'output_tokens': outputs,
                               'estimated_cost_upper_usd': (inputs * input_rate + outputs * output_rate) / 1e9,
                               'estimate_basis': 'All measured input at 30m cache-write long-context rate; output at long-context rate.'}
            if self.service_tier == 'flex':
                self.last_usage['estimated_flex_cost_upper_usd'] = self.last_usage['estimated_cost_upper_usd']
            if envelope.get('status') != 'completed':
                raise ValueError('OpenAI response incomplete; no automatic retry.')
            pieces = []
            for item in envelope.get('output', []):
                if item.get('type') == 'message':
                    for content in item.get('content', []):
                        if content.get('type') == 'refusal':
                            raise ValueError('OpenAI declined this request.')
                        if content.get('type') == 'output_text':
                            pieces.append(content['text'])
            result = json.loads(''.join(pieces))
            if not isinstance(result, dict):
                raise ValueError('OpenAI returned a non-object result.')
            return result
        except HTTPError as exc:
            self.ledger.block()
            raise ValueError(f'OpenAI returned HTTP {exc.code}; response body suppressed, no retry. Reservation retained.') from None
        except (URLError, TimeoutError, OSError):
            self.ledger.block()
            raise ValueError('OpenAI request failed or timed out; outcome uncertain, no retry. Reservation retained.') from None
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeError):
            self.ledger.block()
            raise ValueError('OpenAI returned malformed output; reservation retained.') from None
        except ValueError:
            self.ledger.block()
            raise


def provider_from_args(args):
    """One provider factory shared by ask and evaluate-answers."""
    if args.style != 'explain':
        return None
    if args.provider == 'openai':
        if args.endpoint is not None or args.thinking != 'auto':
            raise ValueError('--endpoint and --thinking are Ollama controls; OpenAI uses the fixed official endpoint.')
        return OpenAIProvider(args.model or MODEL, api_key_file=args.api_key_file,
                              ledger_path=args.spend_ledger, budget_usd=args.budget_usd,
                              allow_remote=args.allow_remote, timeout=min(args.timeout, 120),
                              max_output_tokens=args.max_output_tokens, max_calls=args.max_api_calls)
    from .generation import OllamaProvider
    return OllamaProvider(args.model, args.endpoint or 'http://127.0.0.1:11434',
                          args.allow_remote, args.timeout,
                          thinking={'auto': None, 'on': True, 'off': False}[args.thinking],
                          max_output_tokens=args.max_output_tokens, seed=args.seed)
