"""Pure parsing/validation helpers embedded in cloud Dify code nodes (no network)."""
import json
import re
from urllib.parse import urlsplit


def _object(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def prepare_request(video_url: str, language: str, base_url: str) -> dict:
    out = {'ready': 0, 'submit_url': '', 'base_url': '', 'request_body': '{}'}
    try:
        base = urlsplit(base_url.rstrip('/'))
        media = urlsplit(video_url)
        allowed_base = (base.hostname == 'dashscope.aliyuncs.com'
                        or bool(re.fullmatch(r'[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', base.hostname or '')))
        if (base.scheme != 'https' or not allowed_base or base.port not in (None, 443)
                or base.username or base.password or base.query or base.fragment
                or base.path not in ('', '/api/v1')):
            return out
        if (media.scheme != 'https' or not media.hostname or media.username or media.password
                or media.hostname in ('localhost', 'host.docker.internal') or media.fragment):
            return out
        base_normalized = 'https://' + base.netloc + '/api/v1'
    except (TypeError, ValueError, AttributeError):
        return out
    payload = {'model': 'paraformer-v2', 'input': {'file_urls': [video_url]},
               'parameters': {'language_hints': ['zh' if language == 'zh-CN' else 'en']}}
    return {'ready': 1, 'submit_url': base_normalized + '/services/audio/asr/transcription',
            'base_url': base_normalized, 'request_body': json.dumps(payload, ensure_ascii=False)}


def parse_submission(body: str, status: int, base_url: str) -> dict:
    result = {'ready': 0, 'task_id': '', 'poll_url': ''}
    data = _object(body)
    output = data.get('output')
    if status != 200 or not isinstance(output, dict) or data.get('code'):
        return result
    task_id = output.get('task_id')
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', task_id):
        return result
    if output.get('task_status') not in ('PENDING', 'RUNNING', 'SUCCEEDED'):
        return result
    return {'ready': 1, 'task_id': task_id, 'poll_url': base_url + '/tasks/' + task_id}


def parse_poll(body: str, status: int) -> dict:
    failed = {'done': 1, 'result_url': '', 'poll_status': 'unavailable'}
    data = _object(body)
    output = data.get('output')
    if status != 200 or not isinstance(output, dict) or data.get('code'):
        return failed
    state = output.get('task_status')
    if state in ('PENDING', 'RUNNING'):
        return {'done': 0, 'result_url': '', 'poll_status': 'pending'}
    if state != 'SUCCEEDED':
        return failed
    rows = output.get('results')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return failed
    row = rows[0]
    if row.get('subtask_status') != 'SUCCEEDED':
        return failed
    result_url = row.get('transcription_url')
    try:
        parsed = urlsplit(result_url)
        # Signed ASR transcripts are on Alibaba OSS; never fetch arbitrary response URLs.
        if (not isinstance(result_url, str) or parsed.scheme != 'https'
                or not (parsed.hostname or '').endswith('.aliyuncs.com')
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path or parsed.fragment):
            return failed
    except (ValueError, TypeError, AttributeError):
        return failed
    return {'done': 1, 'result_url': result_url, 'poll_status': 'succeeded'}


def parse_result(body: str, status: int, extracted_texts: list = None) -> dict:
    unavailable = {'audio': json.dumps({'transcript': '', 'audio_status': 'unavailable'}, ensure_ascii=False)}
    if status != 200:
        return unavailable
    # Dify HTTP turns Content-Disposition JSON attachments into files rather than body.
    # Preserve ordinary JSON body responses; only a single extracted attachment is valid.
    payload = body.strip() if isinstance(body, str) else ''
    if not payload:
        if (not isinstance(extracted_texts, list) or len(extracted_texts) != 1
                or not isinstance(extracted_texts[0], str) or not extracted_texts[0].strip()):
            return unavailable
        payload = extracted_texts[0].strip()
    data = _object(payload)
    if data.get('code') or not isinstance(data.get('transcripts'), list):
        return unavailable
    parts = []
    for row in data['transcripts']:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            return unavailable
        if row['text'].strip():
            parts.append(row['text'].strip())
    text = '\n'.join(parts)
    if not text or text.lower() == 'not a valid audio file':
        return unavailable
    return {'audio': json.dumps({'transcript': text, 'audio_status': 'ok'}, ensure_ascii=False)}


def unpack_audio(audio: str) -> dict:
    data = _object(audio)
    transcript = data.get('transcript')
    status = data.get('audio_status')
    if status != 'ok' or not isinstance(transcript, str) or not transcript.strip():
        return {'transcript': '', 'audio_status': 'unavailable'}
    return {'transcript': transcript, 'audio_status': 'ok'}
