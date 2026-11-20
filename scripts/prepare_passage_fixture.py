"""Freeze source-backed synthetic passage controls; never overwrite labels."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    project = root/'tests/fixtures/passage_project'
    code = 'Assets/ThermalController.cs'
    def passage(path, quote):
        text = (project/path).read_text(encoding='utf-8')
        assert text.count(quote) == 1
        first = text.count('\n', 0, text.index(quote)) + 1
        return {'path': path, 'quote': quote, 'start_line': first, 'end_line': first + quote.count('\n')}
    def case(identity, question, expected, negative=()):
        labels = [passage(p, q) for p, q in expected]
        negatives = [passage(p, q) for p, q in negative]
        paths = {p['path'] for p in labels + negatives}
        return {'id': identity, 'question': question, 'category': 'synthetic-development',
                'passage_groups': [[p] for p in labels], 'negative_passages': negatives,
                'reviewed_source_sha256': {p: hashlib.sha256((project/p).read_bytes()).hexdigest() for p in sorted(paths)}}
    cases = [
        case('cooling-tail', 'How does ThermalController AdvanceHeat reduce heat using CoolingPerSecond?',
             [(code, 'heat = System.Math.Max(0, heat - CoolingPerSecond * elapsed);')],
             [(code, 'return "Old documentation says cooling increases heat; this is a string, not the calculation.";')]),
        case('venting-tail', 'How does ThermalController AdvanceHeat stop venting when heat reaches zero?',
             [(code, 'if (heat <= 0f)'), (code, 'venting = false;')]),
        case('cooldown', 'How does ThermalController GetCooldown scale baseDelay with heat?',
             [(code, 'return baseDelay * (1f + heat / 100f);')]),
        case('compute', 'How does CoolBuffer subtract CoolingStep from Heat?',
             [('Assets/Effects/Cooling.compute', 'Heat[id.x] = max(0.0, Heat[id.x] - CoolingStep);')]),
        case('panel', 'Where does venting-status show Cooling in HeatPanel.uxml?',
             [('Assets/UI/HeatPanel.uxml', '<ui:Label name="venting-status" text="Cooling" />')]),
        case('style', 'What font-size is assigned to venting-status?',
             [('Assets/UI/HeatPanel.uss', 'font-size: 18px;')]),
        case('settings', 'What is the configured maximumHeat?',
             [('Assets/Config/Heat.json', '"maximumHeat": 120')]),
    ]
    data = {'schema_version': 2, 'split': 'synthetic-development',
            'scope': 'Authored mechanism controls, not independent real-project acceptance. Frozen before the paired evaluation.',
            'cases': cases}
    destination = root/'tests/evaluation/passage-development-v1.json'
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
    print(destination)


if __name__ == '__main__':
    main()
