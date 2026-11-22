"""Prepare a frozen local answer sample, or run it once and create an unfilled review."""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unity_rag.answer_sample import prepare_sample, run_sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('manifest')
    prepare.add_argument('--workspace', required=True)
    prepare.add_argument('--output', required=True)
    run = commands.add_parser('run')
    run.add_argument('lock')
    run.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            lock = prepare_sample(args.manifest, args.workspace, args.output)
            print(json.dumps({'output': args.output, 'kind': lock['kind']}))
            return 0
        result = run_sample(args.lock, args.output)
        print(json.dumps(result, indent=2))
        return 0 if result['status'] == 'completed' and result['valid_outputs'] == result['questions'] else 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
