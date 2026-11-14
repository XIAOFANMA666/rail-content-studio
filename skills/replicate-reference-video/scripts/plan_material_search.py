#!/usr/bin/env python3
"""Build bounded, traceable material-search requests; no I/O to any service.

Import build_plan(analysis, technical, policy) and validate_policy(policy).
The caller owns inventory inspection, persisted task deduplication, submission,
same-task network recovery, actual clip checks and approval. A ready request is
not evidence that an asset exists or has been approved.
"""
import argparse
import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re

SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = SKILL_ROOT / 'references/material-search-policy.json'
SEMANTIC_FIELDS = ('subject', 'action', 'shot_size', 'scene', 'direction', 'camera', 'composition')
LABELS = {'subject': '主体', 'action': '动作', 'shot_size': '景别', 'scene': '场景',
          'direction': '运动方向', 'camera': '机位运镜', 'composition': '构图',
          'lighting': '光线', 'color': '色调'}
STOP_WORDS = frozenset('a an the of and with in on at from to for by footage video shot view showing'.split())


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha256(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{name} must be an integer in [{lower}, {upper}]')
    return value


def _number(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{name} must be finite in [{lower}, {upper}]')
    return value


def _fixed(data, name, expected):
    if name not in data or type(data[name]) is not type(expected) or data[name] != expected:
        raise ValueError(f'{name} must be {expected!r}')


def validate_policy(policy):
    """Return a validated copy; reject unbounded retries and relaxed hard rules."""
    if not isinstance(policy, dict):
        raise ValueError('policy must be an object')
    _canonical(policy)  # Reject non-JSON data and NaN/Infinity before hashing.
    p = copy.deepcopy(policy)
    _fixed(p, 'schema', 'reference-material-search-policy/v1')
    _fixed(p, 'version', 1)
    _fixed(p, 'task_budget_scope', 'complete_semantic_plan')
    _fixed(p, 'historical_incomplete_requests', 'retain_candidates_and_source_cooldowns_without_consuming_current_plan_budget')
    for key in ('policy_id', 'skill_name', 'skill_summary'):
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise ValueError(f'{key} must be nonempty text')
    _fixed(p, 'inventory_order', ['legacy_approved', 'project_approved', 'project_historical'])
    _fixed(p, 'external_provider_order', ['legacy_agent', 'configured_project_provider'])
    for key, bounds in {
        'max_external_tasks_per_shot': (0, 2), 'max_legacy_agent_rounds_per_task': (1, 3),
        'max_concurrent_tasks': (1, 5), 'max_query_variants': (1, 3),
        'short_query_max_words': (1, 8), 'max_candidates_per_task': (1, 20),
        'max_auto_analyze_per_task': (0, 5),
    }.items():
        _integer(p.get(key), key, *bounds)
    _number(p.get('poll_interval_seconds'), 'poll_interval_seconds', 1, 60)
    _fixed(p, 'source_orientation', 'any_crop_verified')
    _fixed(p, 'default_kind', 'video')
    _fixed(p, 'image_requires_explicit_static_requirement', True)
    if p.get('purpose') not in ('internal', 'publish'):
        raise ValueError('purpose must be internal or publish')
    retry = p.get('network_retry')
    if not isinstance(retry, dict):
        raise ValueError('network_retry must be an object')
    _integer(retry.get('max_retries'), 'network_retry.max_retries', 0, 3)
    _number(retry.get('base_delay_seconds'), 'network_retry.base_delay_seconds', 1, 120)
    _number(retry.get('multiplier'), 'network_retry.multiplier', 2, 4)
    _number(retry.get('max_delay_seconds'), 'network_retry.max_delay_seconds', retry['base_delay_seconds'], 600)
    _number(retry.get('max_total_wait_seconds'), 'network_retry.max_total_wait_seconds', retry['max_delay_seconds'], 3600)
    for key in ('respect_retry_after', 'respect_cooldown_until', 'resume_existing_task', 'server_wait_is_minimum'):
        _fixed(retry, key, True)
    _fixed(retry, 'resume_method', 'POST')
    _fixed(retry, 'resume_endpoint', '/api/commons/resume-search')
    statuses = retry.get('retryable_http_statuses')
    if not isinstance(statuses, list) or 429 not in statuses or any(type(s) is not int or s not in (429, 502, 503, 504) for s in statuses):
        raise ValueError('retryable_http_statuses must include 429 and only supported transient statuses')
    dedup = p.get('deduplication')
    if not isinstance(dedup, dict):
        raise ValueError('deduplication must be an object')
    for key, value in {
        'algorithm': 'sha256', 'canonical_json': 'utf8_sorted_keys_compact_allow_nan_false',
        'history_key': 'request_fingerprint', 'persist_before_submit': True,
        'reuse_all_recorded_tasks': True, 'reconcile_unknown_submission_before_retry': True,
        'second_task_requires': 'terminal_no_match_and_revised_query',
        'network_retry_creates_new_task': False,
    }.items():
        _fixed(dedup, key, value)
    limits = p.get('request_limits')
    if not isinstance(limits, dict):
        raise ValueError('request_limits must be an object')
    for key, cap in {'query_chars': 1000, 'required_chars': 500, 'keywords_chars': 155, 'place_chars': 100}.items():
        _integer(limits.get(key), 'request_limits.' + key, 1, cap)
    _number(limits.get('max_duration_seconds'), 'max_duration_seconds', 1, 180)
    checks = p.get('post_validation')
    if not isinstance(checks, dict):
        raise ValueError('post_validation must be an object')
    required = set(SEMANTIC_FIELDS) | {'duration', 'target_crop', 'place', 'media_type', 'rights', 'approval'}
    if (not isinstance(checks.get('required_fields'), list)
            or not all(isinstance(field, str) for field in checks['required_fields'])
            or not required.issubset(checks['required_fields'])):
        raise ValueError('post_validation.required_fields omits a hard condition')
    _fixed(checks, 'motion_rules_apply_to', 'video')
    _fixed(checks, 'image_duration_basis', 'timeline_exposure_frames')
    _fixed(checks, 'image_hold_requires_explicit_static_requirement', True)
    for key in ('continuous_interval_required', 'normal_speed', 'validate_after_crop'):
        _fixed(checks, key, True)
    for key in ('allow_loop', 'allow_freeze', 'allow_mirror', 'source_orientation_is_hard_filter', 'tags_are_evidence', 'approval_may_be_inferred'):
        _fixed(checks, key, False)
    outcomes = p.get('outcomes')
    if not isinstance(outcomes, dict) or any(not isinstance(outcomes.get(k), str) or not outcomes[k].strip()
            for k in ('no_results', 'no_match', 'failed', 'rate_limited', 'pending_approval', 'matched')):
        raise ValueError('outcomes must distinguish no_results, no_match, failed, rate_limited, pending_approval and matched')
    return p


def _fps(technical):
    if 'fps_num' in technical or 'fps_den' in technical:
        num = _integer(technical.get('fps_num'), 'fps_num', 1, 1000000)
        den = _integer(technical.get('fps_den'), 'fps_den', 1, 1000000)
        result = Fraction(num, den)
        basis = 'fps_num/fps_den'
    else:
        raw = technical.get('fps_rational', technical.get('avg_frame_rate', technical.get('fps')))
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError('technical must contain a valid frame rate')
        try:
            result = Fraction(str(raw))
        except (ValueError, ZeroDivisionError):
            raise ValueError('invalid frame rate') from None
        basis = 'rational_string' if isinstance(raw, str) and '/' in raw else 'provided_numeric_fps'
    if not 0 < result <= 240:
        raise ValueError('frame rate must be greater than zero and at most 240')
    return result, basis


def _english(value):
    return isinstance(value, str) and bool(re.search(r'[A-Za-z]', value)) and not re.search(r'[\u3400-\u9fff]', value)


def _queries(shot, policy):
    raw = shot.get('queries', [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError('shot queries must be a list of English queries')
    full = []
    for query in raw:
        if _english(query):
            query = ' '.join(query.split())
            if query and query.casefold() not in {q.casefold() for q in full}:
                full.append(query)
    if not full:
        tags = shot.get('tags', [])
        if isinstance(tags, list):
            tags = [tag.strip() for tag in tags if _english(tag)]
            if tags:
                full = [' '.join(tags)]
    full = full[:policy['max_query_variants']]
    recall = []
    for query in full:
        words = [word for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", query) if word.casefold() not in STOP_WORDS]
        short = ' '.join(words[:policy['short_query_max_words']])
        while len(short) > policy['request_limits']['keywords_chars'] and words:
            words.pop()
            short = ' '.join(words[:policy['short_query_max_words']])
        if short and short.casefold() not in {q.casefold() for q in recall}:
            recall.append(short)
    return full, recall


def fingerprint_request(request, requirements, policy):
    """Hash the exact submitted request and full conditions; independent of time."""
    p = validate_policy(policy)
    return _sha256({'request': request, 'requirements': requirements, 'policy_sha256': _sha256(p)})


def build_plan(analysis, technical, policy):
    """Plan every supplied shot without submitting/searching/approving anything.

    Missing semantics or English recall terms block that shot. Explicit static
    declarations (motion_required=False or static_requirement=True) permit an
    image; unknown motion defaults to video. All exact conditions remain in the
    request script and requirements even when keywords are shortened.
    """
    p = validate_policy(policy)
    if not isinstance(analysis, dict) or not isinstance(analysis.get('shots'), list) or not analysis['shots']:
        raise ValueError('analysis.shots must be a nonempty list')
    if not isinstance(technical, dict):
        raise ValueError('technical must be an object')
    _canonical(analysis)
    width = _integer(technical.get('width'), 'width', 1, 16384)
    height = _integer(technical.get('height'), 'height', 1, 16384)
    total = _integer(technical.get('total_frames'), 'total_frames', 1, 100000000)
    fps, fps_basis = _fps(technical)
    orientation = 'portrait' if height > width else 'landscape' if width > height else 'square'
    policy_hash = _sha256(p)
    skill_path = SKILL_ROOT / 'SKILL.md'
    skill_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path.is_file() else None
    result = {
        'schema': 'reference-material-search-plan/v1', 'policy_id': p['policy_id'], 'policy_sha256': policy_hash,
        'skill': {'name': p['skill_name'], 'summary': p['skill_summary'], 'summary_sha256': _sha256(p['skill_summary']),
                  'source': str(skill_path), 'sha256': skill_hash, 'distribution': 'project_embedded'},
        'technical': {'width': width, 'height': height, 'total_frames': total, 'fps_num': fps.numerator,
                      'fps_den': fps.denominator, 'fps_basis': fps_basis, 'fps': float(fps)},
        'input_analysis_sha256': _sha256(analysis), 'inventory_order': p['inventory_order'],
        'external_provider_order': p['external_provider_order'],
        'execution_note': 'Plan only. Check existing inventory first; scheduler must reuse persisted task fingerprints before any submission.',
        'shots': [],
    }
    ids = set()
    for shot in analysis['shots']:
        if not isinstance(shot, dict):
            raise ValueError('each shot must be an object')
        sid = shot.get('id')
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', sid) or sid in ids:
            raise ValueError('shot id must be unique and contain letters, digits, underscores or hyphens')
        ids.add(sid)
        start = _integer(shot.get('start_frame'), sid + '.start_frame', 0, total - 1)
        end = _integer(shot.get('end_frame'), sid + '.end_frame', start + 1, total)
        frames = end - start
        duration = Fraction(frames, 1) / fps
        blockers = []
        semantics = {}
        for key in SEMANTIC_FIELDS:
            value = shot.get(key, '')
            if not isinstance(value, str) or not value.strip():
                blockers.append({'code': 'missing_semantic_condition', 'field': key})
                value = ''
            semantics[key] = value.strip()
        for key in ('lighting', 'color'):
            if isinstance(shot.get(key), str) and shot[key].strip():
                semantics[key] = shot[key].strip()
        place = shot.get('place', '')
        if not isinstance(place, str):
            raise ValueError(sid + '.place must be text')
        place = place.strip()
        motion = shot.get('motion_required')
        static = shot.get('static_requirement')
        if motion is not None and type(motion) is not bool:
            raise ValueError(sid + '.motion_required must be boolean when supplied')
        if static is not None and type(static) is not bool:
            raise ValueError(sid + '.static_requirement must be boolean when supplied')
        wanted_kind = shot.get('media_type', shot.get('kind'))
        explicit_static = motion is False or static is True
        if wanted_kind not in (None, 'image', 'video'):
            blockers.append({'code': 'unsupported_media_type', 'value': wanted_kind})
        if motion is True and (static is True or wanted_kind == 'image'):
            blockers.append({'code': 'conflicting_motion_and_image_requirements'})
        if wanted_kind == 'image' and not explicit_static:
            blockers.append({'code': 'image_requires_explicit_static_requirement'})
        kind = 'image' if explicit_static and motion is not True and wanted_kind != 'video' else 'video'
        full_queries, recall_queries = _queries(shot, p)
        if not recall_queries:
            blockers.append({'code': 'missing_english_query', 'instruction': 'Supply English retrieval queries or English concept tags; do not fabricate a translation.'})
        post_validation = copy.deepcopy(p['post_validation'])
        post_validation['applicable_media_type'] = kind
        if kind == 'image':
            # Holding an explicitly requested still is not freezing a motion clip.
            for key in ('continuous_interval_required', 'normal_speed', 'allow_loop', 'allow_freeze'):
                post_validation.pop(key)
            post_validation['static_hold_allowed'] = True
        required = {
            **semantics, 'shot_id': sid, 'start_frame': start, 'end_frame': end, 'duration_frames': frames,
            'duration_seconds_exact': f'{duration.numerator}/{duration.denominator}', 'duration_seconds': float(duration),
            'fps_num': fps.numerator, 'fps_den': fps.denominator,
            'target_width': width, 'target_height': height, 'target_orientation': orientation,
            'source_orientation': p['source_orientation'], 'place': place,
            'place_status': shot.get('place_status', 'provided_unverified' if place else 'unspecified'),
            'media_type': kind, 'motion_required': kind == 'video',
            'static_declaration': {'motion_required': motion, 'static_requirement': static, 'requested_media_type': wanted_kind},
            'post_validation': post_validation,
            'duration_verification': 'continuous source interval at normal speed covers required output frames' if kind == 'video'
                                     else 'explicit static image displayed for required output frames; not evidence of moving footage',
        }
        conditions = '；'.join(f'{LABELS[key]}={value}' for key, value in semantics.items())
        conditions += f'；地点={place or "未指定，不添加地点限制"}'
        conditions += f'；目标={width}×{height} {orientation}；时长={frames}帧，帧率={fps.numerator}/{fps.denominator}fps，精确秒数={duration.numerator}/{duration.denominator}'
        conditions += '；允许高分辨率素材裁切，但最终裁切后必须保持景别、主体完整性和构图'
        conditions += '；必须为连续实拍视频，正常速度，不循环、不定格、不镜像' if kind == 'video' else '；明确静态图像需求，不得用于声称重现运动动作'
        query = f'分镜 {sid} 的完整素材要求：{conditions}。检索词只负责召回，候选必须按以上全部条件后验。'
        # Legacy required is capped at 500; the full script/query remains authoritative.
        required_text = conditions if len(conditions) <= p['request_limits']['required_chars'] else '完整硬条件见script，逐项验证，禁止仅凭检索标签判定匹配；最终画幅裁切后仍须保持景别、动作、方向、构图及准确时长。'
        request = {'query': query, 'keywords': recall_queries[0] if recall_queries else '', 'kind': kind,
                   'place': place, 'required': required_text, 'duration': float(duration) if kind == 'video' else 0,
                   'orientation': '', 'purpose': p['purpose'], 'mode': 'agent' if kind == 'video' else 'fixed'}
        for field, limit_key in (('query', 'query_chars'), ('required', 'required_chars'), ('keywords', 'keywords_chars'), ('place', 'place_chars')):
            if len(request[field]) > p['request_limits'][limit_key]:
                blockers.append({'code': 'legacy_request_field_too_long', 'field': field, 'length': len(request[field]),
                                 'limit': p['request_limits'][limit_key], 'instruction': 'Do not truncate hard conditions; resolve adapter capacity or provide an explicit reviewed concise specification.'})
        if kind == 'video' and duration > p['request_limits']['max_duration_seconds']:
            blockers.append({'code': 'legacy_duration_limit', 'required_seconds_exact': required['duration_seconds_exact']})
        fingerprint = _sha256({'request': request, 'requirements': required, 'policy_sha256': policy_hash})
        result['shots'].append({
            'shot_id': sid, 'status': 'blocked' if blockers else 'ready_for_inventory_check', 'blockers': blockers,
            'request_fingerprint': fingerprint, 'request': request, 'requirements': required,
            'english_queries': full_queries, 'recall_queries': recall_queries,
            'original_requirement': copy.deepcopy(shot), 'original_requirement_sha256': _sha256(shot),
            'provenance': {'policy_id': p['policy_id'], 'policy_sha256': policy_hash, 'skill_sha256': skill_hash,
                           'skill_summary': p['skill_summary'], 'skill_summary_sha256': result['skill']['summary_sha256']},
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', required=True, type=Path)
    parser.add_argument('--technical', required=True, type=Path)
    parser.add_argument('--policy', type=Path, default=DEFAULT_POLICY)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = build_plan(json.loads(args.analysis.read_text()), json.loads(args.technical.read_text()), json.loads(args.policy.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'output': str(args.output), 'shots': len(result['shots']),
                      'blocked': sum(s['status'] == 'blocked' for s in result['shots'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
