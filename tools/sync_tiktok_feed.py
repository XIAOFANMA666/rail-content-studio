"""Publish a sanitized snapshot from the latest completed Apify task run."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from video_trends import canonical, count, date


def request(path, token):
    req = Request('https://api.apify.com/v2/' + path,
                  headers={'Authorization': 'Bearer ' + token})
    with urlopen(req, timeout=40) as response:
        content = response.read(8_000_001)
    if len(content) > 8_000_000:
        raise ValueError('采集结果过大')
    return json.loads(content)


def normalize_rows(rows, observed_at):
    if not isinstance(rows, list):
        raise ValueError('采集结果格式错误')
    items = {}
    titles = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('errorCode'):
            continue
        link = canonical('TikTok', row.get('webVideoUrl'))
        title = str(row.get('text') or '').strip()
        title = re.sub(r'(?:\\u[0-9a-fA-F]{4})+', lambda match: json.loads('"' + match[0] + '"'), title)
        title = title.encode('utf-8', errors='replace').decode('utf-8')
        published = date(row.get('createTimeISO'))
        if not link or not title or not published or published > date(observed_at):
            continue
        title_key = re.sub(r'\s+', ' ', title).casefold()
        if link[0] in items or title_key in titles:
            continue
        try:
            metrics = {key: count(row.get(field)) for key, field in (
                ('views', 'playCount'), ('likes', 'diggCount'),
                ('comments', 'commentCount'), ('shares', 'shareCount'), ('saves', 'collectCount'))}
        except ValueError:
            continue
        titles.add(title_key)
        author = row.get('authorMeta') or {}
        items[link[0]] = {
            'id': link[0], 'platform': 'TikTok', 'title': title[:500],
            'original': link[1], 'author': str(author.get('name') or '')[:100],
            'source': 'Apify · Clockworks', 'published_at': published.isoformat(),
            'metrics_at': observed_at, 'metrics_time_basis': 'collection', 'metrics': metrics,
        }
    return sorted(items.values(), key=lambda item: item['metrics']['views'] or 0, reverse=True)


def main():
    token = os.environ.get('APIFY_READ_TOKEN', '')
    task = os.environ.get('APIFY_TASK_ID', '')
    if not token or not re.fullmatch(r'[A-Za-z0-9]{17}', task):
        raise ValueError('缺少 Apify 读取配置')
    runs = request('actor-tasks/' + task + '/runs?desc=true&limit=20', token)['data']['items']
    run = next((r for r in runs if r['status'] == 'SUCCEEDED'), None)
    if run is None:
        raise ValueError('尚无成功的采集任务，保留已有数据')
    observed = run.get('finishedAt')
    dataset = run.get('defaultDatasetId', '')
    if not date(observed) or not re.fullmatch(r'[A-Za-z0-9]{17}', dataset):
        raise ValueError('采集记录不完整')
    rows = request('datasets/' + dataset + '/items?clean=true&limit=200', token)
    items = normalize_rows(rows, observed)
    if not items:
        raise ValueError('本次没有有效视频，保留已有数据')
    destination = ROOT / 'data' / 'tiktok-feed.json'
    previous = json.loads(destination.read_text()) if destination.exists() else {}
    if date(previous.get('updated_at')) and date(previous['updated_at']) > date(observed):
        raise ValueError('结果早于已有数据，保留更新的版本')
    payload = {
        'updated_at': observed, 'run_id': run['id'], 'last_run_status': runs[0]['status'],
        'schedule': '北京时间每天09:00采集，09:15起同步网页',
        'region': 'GB', 'region_basis': '英国代理搜索',
        'queries': ['UK train travel', 'London to Scotland sleeper train'],
        'items': items,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    if not destination.exists() or destination.read_text() != serialized:
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(serialized)
        temporary.replace(destination)
    print('TikTok feed ready:', len(items), 'videos; collected', observed)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never print provider request headers or raw error responses.
        print('TikTok synchronization failed; existing feed retained. ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
