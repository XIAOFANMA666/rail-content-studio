"""Export existing production assets without searching or approving media."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def inventory(project):
    root = Path(project).resolve()
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('rail_media', root / 'media_library.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    legacy = json.loads((root / 'data/library.json').read_text(encoding='utf-8'))
    items = module.merge_library(root, root / 'output', legacy)['clips']
    result, missing, seen = [], [], set()
    for clip in items:
        path = (root / clip['file']).resolve()
        if root not in path.parents or not path.is_file():
            missing.append(clip['id'])
            continue
        if clip['id'] in seen:
            raise ValueError('Duplicate clip ID: ' + clip['id'])
        seen.add(clip['id'])
        result.append({**clip, 'absolute_file': str(path)})
    return {'project': str(root), 'clips': result, 'missing_files': missing,
            'note': 'Metadata candidates only; inspect actual source intervals before matching.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    data = inventory(args.project)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an earlier task's evidence.
    with output.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
