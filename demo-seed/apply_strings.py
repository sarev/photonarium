"""Apply translated strings back into a script.json template.

The inverse of ``extract_strings.py``: reads a plain-text file (one
translated string per line) and meshes them back into the JSON
structure of the template, replacing each ``title`` and ``caption``
value in the order they appear.

Usage:
    python apply_strings.py -t script.json -i strings.fr.txt -o script.fr.json
"""

import argparse
import json
from pathlib import Path


def apply_strings(data, lines, index):
    """Walk *data* in the same order as ``extract_strings`` and replace
    each ``title`` / ``caption`` value with the next line from *lines*.

    Returns the updated index so recursive calls stay in sync.
    """
    if isinstance(data, dict):
        for key in data:
            if key in ('title', 'caption') and isinstance(data[key], str):
                if index >= len(lines):
                    raise ValueError(f'Not enough lines in input: expected more than {len(lines)}')
                data[key] = lines[index]
                index += 1
            else:
                index = apply_strings(data[key], lines, index)
    elif isinstance(data, list):
        for item in data:
            index = apply_strings(item, lines, index)
    return index


def main():
    parser = argparse.ArgumentParser(
        description='Apply translated strings into a script.json template',
    )
    parser.add_argument(
        '-t',
        '--template',
        required=True,
        help='template JSON file (e.g. script.json)',
    )
    parser.add_argument(
        '-i',
        '--input',
        required=True,
        help='translated strings file, one per line (e.g. strings.fr.txt)',
    )
    parser.add_argument(
        '-o',
        '--output',
        required=True,
        help='output JSON file (e.g. script.fr.json)',
    )
    args = parser.parse_args()

    with open(args.template, encoding='utf-8') as f:
        data = json.load(f)

    lines = Path(args.input).read_text(encoding='utf-8').splitlines()

    index = apply_strings(data, lines, 0)

    if index < len(lines):
        print(f'Warning: {len(lines) - index} extra lines in input were ignored')

    Path(args.output).write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    print(f'{index} strings applied, written to {args.output}')


if __name__ == '__main__':
    main()
