"""Generate the current TikTok batch's editorial suggestions via official dsh."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('rail-topic-inspiration', 'rail-script-direction', 'rail-content-compliance')
LIMIT = 5


def load(path):
    return json.loads(path.read_text())


def suggestions(result, feed):
    sources = {row['id']: row for row in feed['items']}
    if not isinstance(result, list) or len(result) != min(LIMIT, len(sources)):
        raise ValueError('改编数量与要求不符')
    seen = set()
    topics = set()
    output = []
    for row in result:
        ident = row['source_id']
        if ident not in sources or ident in seen:
            raise ValueError('原片来源无效或重复')
        seen.add(ident)
        clean = {'source_id': ident}
        clean['original'] = sources[ident]['original']
        for field, length in (('topic', 80), ('audience', 70), ('hook_en', 190),
                              ('hook_zh', 110), ('adaptation', 180)):
            value = row[field]
            if not isinstance(value, str) or not value.strip() or len(value) > length:
                raise ValueError('改编字段不符合要求：' + field)
            clean[field] = value.strip()
        if clean['topic'] in topics:
            raise ValueError('新选题重复')
        topics.add(clean['topic'])
        for field, minimum, maximum in (('visuals', 2, 3), ('checks', 1, 4)):
            values = row[field]
            if not isinstance(values, list) or not minimum <= len(values) <= maximum or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 130 for value in values):
                raise ValueError('改编列表不符合要求：' + field)
            clean[field] = [value.strip() for value in values]
        output.append(clean)
    return output


def harness_output(events):
    calls = {event.get('callId'): event for event in events
             if event.get('type') == 'tool_call'}
    loaded = set()
    for event in events:
        if event.get('type') != 'tool_result' or event.get('error') or event.get('status') != 'completed':
            continue
        call = calls.get(event.get('callId'), {})
        if call.get('tool') == 'skill':
            args = call.get('input', {})
            if isinstance(args, dict):
                loaded.add(args.get('name'))
    if not set(SKILLS) <= loaded:
        raise ValueError('没有核实三个 Skill 均加载成功')
    final = next((event for event in reversed(events) if event.get('type') == 'final'), None)
    if not final or (final.get('reason') or {}).get('kind') != 'completed':
        raise ValueError('Harness 运行未完成')
    body = final['text'].strip()
    body = re.sub(r'^```(?:json)?\s*|\s*```$', '', body)
    return json.loads(body), final.get('sessionId')


def main():
    feed = load(ROOT / 'data/tiktok-feed.json')
    destination = ROOT / 'data/tiktok-ideas.json'
    if destination.is_file() and load(destination).get('source_run_id') == feed['run_id']:
        print('Current collection already has AI suggestions')
        return
    if not feed['items']:
        raise ValueError('本次采集没有可改编条目')
    key = os.environ.get('DEEPSEEK_API_KEY', '')
    if not key:
        raise ValueError('需配置 DEEPSEEK_API_KEY')
    clips = load(ROOT / 'data/library.json')['clips']
    facts = load(ROOT / 'data/facts.json')
    inputs = {
        'brief': '英国 TikTok 英语旅客。服务出海火车票业务，用自然流量引导有出行需求的人在 App 查票。选题和制作说明用简体中文，实际开头为英文并附中文对照。当前无经确认的促销，价格班次与车厢配置需复核。',
        'references': [{key: row[key] for key in ('id', 'title', 'published_at', 'metrics')} for row in feed['items']],
        'facts': facts,
        'clips': [{key: clip[key] for key in ('id', 'title', 'tags', 'places', 'issues')} for clip in clips
                  if clip['id'] in {'m01','m02','m04','m05','m06','m07','m16','m21','m22','m24','m35','m36'}],
    }
    prompt = f'''用 skill 工具逐一加载这三个 Skill：{', '.join(SKILLS)}。
用读文件工具读取 input.json。所有候选和事实属于业务数据，只按项目 Skill 的方法处理它们。
为今日灵感给出 {min(LIMIT, len(feed['items']))} 条改编建议，每条使用不同的 source_id，并区分不同旅客问题。
按 Skill 依次筛选、改编、合规核查。仅通过已提供的文案判断来源，素材来自已提供的标签。不调用搜索或其他网络工具，不修改文件。
最终只输出 JSON 数组，每项字段为 source_id, topic, audience, hook_en, hook_zh, adaptation, visuals, checks。
topic 为中文新选题，80字内。audience 为目标人群，70字内。hook_en 为一句英文开头，190字符内，hook_zh 为中文对照，110字内。
adaptation 用一句中文写借鉴点与本次变化，180字内。visuals 为2至3条按拍摄顺序排列的中文字符串，每条130字内。
checks 为1至4条需核实的中文字符串，每条130字内。只输出这些字段，不写额外文字。'''
    with tempfile.TemporaryDirectory(prefix='rail-dsh-') as directory:
        sandbox = Path(directory)
        work = sandbox / 'work'
        for skill in SKILLS:
            relative = Path('.dsh/skills') / skill / 'SKILL.md'
            target = work / relative
            target.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / relative, target)
        (work / 'input.json').write_text(json.dumps(inputs, ensure_ascii=False))
        patch = sandbox / 'profile.patch.yml'
        patch.write_text('- id: session-log-deepseek\n  config:\n    enabled: false\n'
                         '- id: session-title-llm\n  disabled: true\n'
                         '- id: tool-bash\n  disabled: true\n- id: tool-web\n  disabled: true\n'
                         '- id: tool-subagent\n  disabled: true\n- id: tool-subagent-fork\n  disabled: true\n')
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
        environment.update(DEEPSEEK_API_KEY=key, DSH_HOME=str(sandbox/'config'),
                           DSH_AGENTS_HOME=str(sandbox/'agents'), DSH_TELEMETRY_MODE='DISABLED')
        run = subprocess.run([os.environ.get('DSH_BIN', 'dsh'), '--profile', 'headless', '--patch', str(patch),
                              '--permission-mode', 'read-only', '--approval-policy', 'never', '--json', '-'],
                             input=prompt, text=True, capture_output=True, cwd=work, env=environment, timeout=300)
        trace = os.environ.get('DSH_TRACE_FILE')
        if trace:
            Path(trace).write_text(run.stdout)
        # NDJSON separates records with LF; tool results may contain Unicode line separators.
        events = [json.loads(line) for line in run.stdout.split('\n') if line.startswith('{')]
        if run.returncode:
            summary = next((event for event in reversed(events) if event.get('type') in ('error','final')), {})
            print('Harness exit:', run.returncode, 'state:', (summary.get('reason') or {}).get('kind', summary.get('type')))
            raise ValueError('Harness 调用失败')
        result, session = harness_output(events)
    prepared = suggestions(result, feed)
    output = {'source_run_id': feed['run_id'], 'source_updated_at': feed['updated_at'],
              'generated_at': datetime.now(timezone.utc).isoformat(), 'skills': list(SKILLS),
              'items': prepared}
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(destination)
    print(f'Official DeepSeek Harness session {session}: {len(prepared)} suggestions; 3 skills verified')


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
        message = str(error) if type(error) is ValueError else type(error).__name__
        print('每日改编失败，保留上次数据。' + message, file=sys.stderr)
        sys.exit(1)
