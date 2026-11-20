"""Ollama JSON generation adapter; local endpoint by default."""
import ipaddress
import json
from urllib.error import URLError, HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaProvider:
    def __init__(self, model, endpoint='http://127.0.0.1:11434', allow_remote=False, timeout=120,
                 thinking=None, max_output_tokens=2048, seed=42):
        parts = urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(parts.hostname or '').is_loopback
        except ValueError:
            local = parts.hostname == 'localhost'
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
            raise ValueError('Use an Ollama HTTP(S) server origin without credentials, query, or path.')
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError('Specify an installed Ollama model name.')
        remote = not local or 'cloud' in model.lower()
        if remote and not allow_remote:
            raise ValueError('Remote models receive project evidence. Explicitly set --allow-remote to permit this.')
        if not local and parts.scheme != 'https':
            raise ValueError('Remote Ollama endpoints must use HTTPS.')
        if not 1 <= timeout <= 300:
            raise ValueError('Model timeout must be 1–300 seconds.')
        if thinking is not None and type(thinking) is not bool:
            raise ValueError('Thinking must be true, false, or unspecified.')
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 16384:
            raise ValueError('Output budget must be 256-16384 tokens.')
        if type(seed) is not int or not 0 <= seed <= 2147483647:
            raise ValueError('Seed must be an integer from 0 to 2147483647.')
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout
        self.config = {'provider': 'ollama', 'model': model, 'endpoint': self.endpoint,
                       'remote_transfer_allowed': bool(allow_remote), 'temperature': 0,
                       'max_output_tokens': max_output_tokens, 'thinking': thinking,
                       'context_tokens': 16384, 'seed': seed}

    def generate(self, messages, schema):
        body = {'model': self.config['model'], 'messages': messages, 'format': schema,
                'stream': False, 'options': {'temperature': 0, 'num_predict': self.config['max_output_tokens'],
                                           'num_ctx': self.config['context_tokens'], 'seed': self.config['seed']}}
        if self.config['thinking'] is not None:
            body['think'] = self.config['thinking']
        request = Request(self.endpoint+'/api/chat', data=json.dumps(body).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            # Do not leak repository evidence through environment proxies or HTTP redirects.
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('Model response exceeded the size limit.')
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError('Ollama returned an invalid response envelope.')
            if envelope.get('done_reason') == 'length':
                raise ValueError('Model output was truncated; increase --max-output-tokens within the context limit or choose another model. Thinking can consume this budget.')
            return json.loads(envelope['message']['content'])
        except HTTPError as exc:
            raise ValueError(f'Ollama returned HTTP {exc.code}. Check the server and installed model name.') from exc
        except (URLError, TimeoutError) as exc:
            raise ValueError('Cannot reach Ollama or the request timed out. Start the server and check the endpoint.') from exc
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError('Ollama returned malformed JSON output.') from exc
