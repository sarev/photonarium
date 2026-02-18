"""Extract translatable strings from script.json.

Reads script.json and writes a plain-text file with one line per
``title`` or ``caption`` value, in the order they appear in the JSON.
The output is intended as a starting point for translation work.

Usage:
    python extract_strings.py                  # writes strings.txt
    python extract_strings.py -o french.txt    # custom output path
"""

import argparse
import json
from pathlib import Path

SCRIPT_JSON = Path(__file__).parent / 'script.json'


def extract_strings(data):
    """Walk *data* and yield every ``title`` or ``caption`` value in order."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ('title', 'caption') and isinstance(value, str):
                yield value
            else:
                yield from extract_strings(value)
    elif isinstance(data, list):
        for item in data:
            yield from extract_strings(item)


def main():
    parser = argparse.ArgumentParser(
        description='Extract title/caption strings from script.json',
    )
    parser.add_argument(
        '-o',
        '--output',
        default='strings.txt',
        help='output file path (default: strings.txt)',
    )
    args = parser.parse_args()

    with open(SCRIPT_JSON, encoding='utf-8') as f:
        data = json.load(f)

    lines = list(extract_strings(data))

    output = Path(args.output)
    output.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{len(lines)} strings written to {output}')


if __name__ == '__main__':
    main()
