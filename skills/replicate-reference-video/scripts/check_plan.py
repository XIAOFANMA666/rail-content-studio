"""Validate timing and declared evidence, not visual/semantic correctness."""
import argparse
import json
import math
from pathlib import Path

CHECKS = ('subject', 'action', 'shot_size', 'direction', 'camera', 'composition', 'scene', 'place')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(plan, inventory):
    errors = []
    if not all(integer(plan.get(k)) for k in ('fps_num', 'fps_den', 'total_frames')):
        return ['fps_num, fps_den and total_frames must be positive integers']
    fps = plan['fps_num'] / plan['fps_den']
    assets = {c['id']: c for c in inventory.get('clips', [])}
    shots = plan.get('shots')
    if not isinstance(shots, list) or not shots:
        return ['shots must be a nonempty list']
    cursor, ids = 0, set()
    for index, shot in enumerate(shots):
        prefix = f'shots[{index}]'
        if not isinstance(shot, dict):
            errors.append(prefix + ': must be an object')
            continue
        sid = shot.get('id')
        if not isinstance(sid, str) or not sid or sid in ids:
            errors.append(prefix + ': missing or duplicate shot ID')
        else:
            ids.add(sid)
        start, end = shot.get('start_frame'), shot.get('end_frame')
        if (type(start) is not int or type(end) is not int or start != cursor
                or end <= start or end > plan['total_frames']):
            errors.append(prefix + ': invalid, overlapping or discontinuous frame range')
            continue
        cursor = end
        if shot.get('status') != 'matched':
            errors.append(prefix + ': unresolved match: ' + str(shot.get('status')))
        clip = assets.get(shot.get('clip_id'))
        if not clip or not Path(clip.get('absolute_file', '')).is_file():
            errors.append(prefix + ': unavailable production asset')
        checks = shot.get('checks', {})
        if not isinstance(checks, dict) or any(checks.get(k) is not True for k in CHECKS):
            errors.append(prefix + ': hard constraints incomplete or failed')
        if not isinstance(shot.get('evidence'), str) or not shot['evidence'].strip():
            errors.append(prefix + ': missing viewing evidence')
        a, b, duration = shot.get('src_in'), shot.get('src_out'), shot.get('clip_s')
        if not all(number(v) for v in (a, b, duration)) or not 0 <= a < b <= duration:
            errors.append(prefix + ': invalid source interval')
            continue
        if clip and number(clip.get('clip_s')) and b > clip['clip_s']:
            errors.append(prefix + ': interval exceeds inventory duration')
        speed = shot.get('speed', 1)
        if speed != 1:
            errors.append(prefix + ': speed change requires separate reviewed adaptation')
        if abs((b - a) - (end - start) / fps) > 1 / fps + 1e-9:
            errors.append(prefix + ': source duration differs by more than one output frame')
    if cursor != plan['total_frames']:
        errors.append('timeline does not cover total_frames')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('plan')
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    try:
        plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
        inventory = json.loads(Path(args.inventory).read_text(encoding='utf-8'))
        errors = validate(plan, inventory)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        errors = [str(exc)]
    print(json.dumps({'structural_pass': not errors, 'errors': errors,
                      'visual_audio_font_verified': False}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if errors else 0)
