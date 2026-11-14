"""Local bilingual railway vocabulary and explicit eligibility rules."""
import json
import math
import re
import unicodedata

CONCEPTS = {
    '湖泊': ('湖泊','湖面','湖水','lochs','loch','lake','lakes'),
    '山地': ('高地','山区','山坡','雪山','highland','highlands','mountain','mountains','hillside'),
    '积雪': ('积雪','雪山','雪景','snow','snowy'),
    '车窗视角': ('车窗','窗外','train window','through the window','window view','from the window'),
    '夜间': ('夜间','夜里','深夜','夜景','night','nighttime','overnight'),
    '清晨': ('清晨','早上','日出','dawn','morning','sunrise'),
    '发车': ('发车','出发','驶离','depart','departure','departs','leaving'),
    '站台': ('站台','月台','platform'),
    '车站': ('车站','火车站','station'),
    '列车': ('列车','火车','train','railway'),
    '卧铺车厢': ('卧铺','包厢','sleeper','club room','berth','cabin'),
    '铁路高架桥': ('高架桥','铁路桥','viaduct','rail bridge'),
}
PLACES = {
    '苏格兰': ('苏格兰','scotland','scottish'),
    '英格兰': ('英格兰','england'),
    '英国': ('英国','united kingdom','uk','great britain'),
    '瑞士': ('瑞士','switzerland','swiss'),
    'Loch Treig': ('loch treig','特雷格湖'),
    'Euston': ('euston','尤斯顿'),
    'Fort William': ('fort william','威廉堡'),
    'Glenfinnan': ('glenfinnan','格伦芬南'),
}
PARENTS = {'Loch Treig': {'苏格兰','英国'}, 'Fort William': {'苏格兰','英国'},
           'Glenfinnan': {'苏格兰','英国'}, 'Euston': {'英格兰','英国'},
           '苏格兰': {'英国'}, '英格兰': {'英国'}}
LINES = {'West Highland Line': ('west highland line','西高地线','西高地铁路')}
FRAMING = {'横屏': ('横屏','landscape orientation'), '竖屏': ('竖屏','portrait orientation'),
           '方形': ('方形','square format')}

def normalize(text):
    return unicodedata.normalize('NFKC', str(text)).casefold().strip()

def contains(text, term):
    term = normalize(term)
    if re.search('[a-z0-9]', term):
        return bool(re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text))
    return bool(term) and term in text

def detect(text, vocabulary):
    return {label for label, aliases in vocabulary.items() if any(contains(text, alias) for alias in aliases)}


def search_fields(body):
    """Reject oversized requirements rather than silently weakening them."""
    fields = {}
    for key,limit in (('script',1000),('location',500),('line',500),('required',4000)):
        value = body.get(key,'')
        if not isinstance(value,str):
            raise ValueError(key+' 必须是文本')
        if len(value)>limit:
            raise ValueError(f'{key} 超过 {limit} 字上限，请保留硬条件后缩短文本')
        fields[key] = value.strip()
    if not fields['script']:
        raise ValueError('请输入 1–1000 字的脚本')
    return fields


def explicit_names(value, vocabulary):
    """Only a whole-field alias can replace an explicitly supplied place."""
    return {next((label for label,aliases in vocabulary.items()
                  if normalize(value) in {normalize(label),*(normalize(alias) for alias in aliases)}),value)} if value else set()


def is_reference_request(body):
    context = body.get('reference_context')
    return (isinstance(context,dict) and bool(context.get('job_id')) and bool(context.get('shot_id'))
            and bool(body.get('request_fingerprint') or body.get('external_request_id')))


def search(conn, body):
    fields = search_fields(body)
    script = fields['script']
    purpose = body.get('purpose','internal')
    orientation = body.get('orientation','')
    if purpose not in ('internal','publish') or orientation not in ('','横屏','竖屏','方形'):
        raise ValueError('请选择有效的用途和画幅')
    seconds = float(body.get('seconds') or 0)
    if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
        raise ValueError('最短视频时长须介于 0 和 3600 秒')
    query = normalize(script)
    manual_place = fields['location']
    manual_line = fields['line']
    infer = body.get('infer', True) is True
    places = explicit_names(manual_place, PLACES) if manual_place else detect(query, PLACES) if infer else set()
    places -= set().union(*(PARENTS.get(p,set()) for p in places))
    lines = explicit_names(manual_line, LINES) if manual_line else detect(query, LINES) if infer else set()
    if not orientation and infer:
        options = detect(query, FRAMING)
        if len(options)==1: orientation = next(iter(options))
    # Reference specifications contain motion, framing and timeline constraints;
    # they are not literal review tags and must survive for visual validation.
    hard = ([] if is_reference_request(body) else
            list(dict.fromkeys(x.strip() for x in re.split('[,，、\n]',fields['required']) if x.strip())))
    preferences = detect(query, CONCEPTS)
    # Named conditions and common visual concepts are complemented by literal script terms.
    words = set(re.findall(r'[a-z0-9]{3,}|[\u3400-\u9fff]{2,}',query))
    words -= {'the','and','from','with','then','this','that','into','for','was','are','you'}
    rows = conn.execute("""SELECT a.id asset_id,a.name,a.extension,a.info,s.id segment_id,s.payload,s.state
                           FROM segment s JOIN asset a ON a.id=s.asset WHERE a.status='ready'""")
    matches, excluded = [], []
    for row in rows:
        p = json.loads(row['payload'])
        evidence = normalize(' '.join([p.get('description',''), *p.get('tags',[])]))
        discovery = normalize(' '.join([row['name'],p.get('location',''),p.get('line','')]))
        observed = detect(evidence, CONCEPTS)
        hit = preferences & observed
        named = (detect(discovery,PLACES) & places) | (detect(discovery,LINES) & lines)
        literal = sorted(w for w in words if contains(evidence+' '+discovery,w))
        relevant = bool(hit or named or literal)
        verified = bool(p.get('locationEvidence'))
        located = detect(normalize(p.get('location','')), PLACES) if verified else set()
        located |= set().union(*(PARENTS.get(place,set()) for place in located))
        reasons = []
        if row['state'] != 'approved':
            reasons.append({'hold':'待补证','pending':'尚未审核','rejected':'已拒绝'}.get(row['state'],'未正式入库'))
        elif not p.get('authorizationVerified') or not p.get('license') or not p.get('usage'):
            reasons.append('缺少已核验的使用授权')
        if purpose=='publish' and p.get('publicationApproved') is not True:
            reasons.append('未获对外发布审核')
        for place in sorted(places):
            if place not in located and not (place not in PLACES and verified and contains(normalize(p.get('location','')),place)):
                reasons.append('缺少已核实地点：'+place)
        for line in sorted(lines):
            if not verified or not contains(normalize(p.get('line','')),line):
                reasons.append('缺少已核实线路：'+line)
        dimensions = json.loads(row['info'])
        width, height = dimensions.get('width',0), dimensions.get('height',0)
        actual = ('横屏' if width>height else '竖屏' if width<height else '方形') if width and height else ''
        if orientation and orientation != actual:
            reasons.append('画幅不符：需要'+orientation)
        if seconds and p['end']-p['start'] < seconds:
            reasons.append(f'需要至少 {seconds:g} 秒的视频片段')
        for item in hard:
            # A must-have visual feature requires an explicit review tag.
            if normalize(item) not in {normalize(tag) for tag in p.get('tags',[])}:
                reasons.append('缺少已标注特征：'+item)
        entry = {'assetId':row['asset_id'],'id':row['segment_id'],'name':row['name'],
                 'score':len(hit)*4+len(named)*2+min(4,len(literal))}
        if reasons:
            if relevant or row['state']=='approved':
                excluded.append({**entry,'reasons':reasons})
        elif (hit or (not preferences and relevant) or hard or
              (not preferences and (places or lines))):
            why = []
            if hit: why.append('画面匹配：'+'、'.join(sorted(hit)))
            if places: why.append('已核实地点：'+p['location'])
            if lines: why.append('已核实线路：'+p['line'])
            if literal and not hit: why.append('文字命中：'+'、'.join(literal[:5]))
            if hard: why.append('满足必需特征：'+'、'.join(hard))
            why.append('符合本次用途及已选规则')
            matches.append({**entry,'reasons':why,'missing':sorted(preferences-observed-set(hard))})
        else:
            excluded.append({**entry,'reasons':['描述和标签没有匹配脚本']})
    rank = lambda item: (-item['score'],item['id'])
    matches.sort(key=rank)
    excluded.sort(key=rank)
    rules = (['对外发布' if purpose=='publish' else '内部选材'] + sorted(places) + sorted(lines)
             + ([orientation] if orientation else []) + ([f'视频至少 {seconds:g} 秒'] if seconds else []) + hard)
    gaps = [] if matches else list(dict.fromkeys(reason for row in excluded[:5] for reason in row['reasons']))
    return {'matches':matches[:15],'excluded':excluded[:20],'totalMatches':len(matches),
            'totalExcluded':len(excluded),'rules':rules,'preferences':sorted(preferences),
            'gaps':gaps or ([] if matches else ['没有找到匹配脚本且通过审核的素材'])}
