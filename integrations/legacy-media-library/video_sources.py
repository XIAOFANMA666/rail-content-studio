"""Video discovery from Internet Archive and manually submitted source pages."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import http_transport
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from search_agent import NoRedirect


def source_url(value):
    url = str(value or '').strip()
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ('https','http') and parts.hostname and not parts.username
                 and not parts.password and len(url)<=2000 and not any(c.isspace() or ord(c)<32 for c in url)
                 and parts.port in (None,80,443))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('请输入完整的网页链接（https:// 或 http://）')
    return parts._replace(scheme=parts.scheme.lower(),netloc=parts.netloc.lower(),fragment='').geturl()


def lead(url, title, provider, **metadata):
    url = source_url(url)
    key = -1-int.from_bytes(sha256(url.encode()).digest()[:6],'big')
    info = dict(discovery=url,title=title,provider=provider,description='',author='',license='',
                licenseUrl='',originalClaim='',mime='',width=0,height=0,size=None,
                fetchedAt=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    return key,{**info,**metadata}


def archive_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (parts.scheme!='https' or parts.netloc!=host
            or not (host=='archive.org' or host.endswith('.archive.org'))):
        raise ValueError('下载地址不属于 Internet Archive')
    return url


class ArchiveRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        archive_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def archive_read(url, limit, mime=None, timeout=8):
    return http_transport.read(url, limit,
        headers={'User-Agent':'AgentRailMediaLibrary/1.0 (video discovery)'},
        mime=mime, timeout=timeout, validate_url=archive_url, follow_redirects=True)


def archive_file(info, limit, timeout):
    identifier = info['identifier']
    metadata = json.loads(archive_read('https://archive.org/metadata/'+identifier,2*1024*1024,timeout=timeout))
    item = metadata.get('metadata',{})
    if any(item.get(field) for field in ('access-restricted-item','private')):
        return None
    if item.get('licenseurl')!=info['licenseUrl']:
        return None
    choices = []
    for file in metadata.get('files',[]):
        name = file.get('name','')
        if file.get('private') or not name.lower().endswith('.mp4'):
            continue
        try:
            size = int(file.get('size') or 0)
        except (ValueError,TypeError):
            continue
        if 0 < size <= limit:
            choices.append((file.get('source')!='original',-size,name))
    if not choices:
        return None
    _,negative_size,name = min(choices)
    url = 'https://archive.org/download/'+identifier+'/'+quote(name,safe='')
    return {**info,'mediaUrl':url,'size':-negative_size,'mime':'video/mp4'}


def archive_license(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http','https') or parts.netloc!='creativecommons.org':
        return ''
    match = re.fullmatch(r'/licenses/(by|by-sa)/(\d\.\d)/?',parts.path,re.I)
    if match:
        return f'CC {match[1].upper()} {match[2]}'
    if parts.path.rstrip('/')=='/publicdomain/zero/1.0':
        return 'CC0 1.0'
    return ''


def archive_search(query, timeout):
    tokens = re.findall(r'[^\W_]+',query,flags=re.UNICODE)[:14]
    if not tokens:
        return []
    fields = ['identifier','title','creator','description','licenseurl','rights']
    params = [('q','mediatype:movies AND '+' AND '.join(f'"{token}"' for token in tokens)),
              ('rows',4),('output','json'), *[('fl[]',field) for field in fields]]
    url = 'https://archive.org/advancedsearch.php?'+urlencode(params)
    raw = http_transport.read(url, 1024*1024,
        headers={'User-Agent':'AgentRailMediaLibrary/1.0 (video discovery)'},
        timeout=timeout, validate_url=archive_url)
    docs = json.loads(raw)['response']['docs']
    from commons import plain

    def value(doc,key):
        field = doc.get(key,'')
        return plain(' / '.join(str(x) for x in field) if isinstance(field,list) else field)

    results = []
    for doc in docs:
        identifier = doc.get('identifier','')
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',identifier):
            continue
        license_url = value(doc,'licenseurl')
        results.append(lead('https://archive.org/details/'+identifier,value(doc,'title') or identifier,
                            'Internet Archive', description=value(doc,'description'),author=value(doc,'creator'),
                            license=archive_license(license_url) or value(doc,'rights'),licenseUrl=license_url,
                            restrictions=value(doc,'rights'),identifier=identifier,mime='video/unknown',
                            query=query))
    return results
