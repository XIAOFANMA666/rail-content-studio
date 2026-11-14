"""Private local configuration for reference-video analysis and media search.

Only configuration presence is returned. Values are stored in the existing .env
format read by llm.load_env/model_config; no provider request is made here.
"""
import ipaddress
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit


_FIELDS = ('VIDEO_BASE_URL', 'VIDEO_MODEL', 'VIDEO_API_KEY', 'PEXELS_API_KEY')
_LOCK = threading.RLock()


def _read(root):
    path = Path(root).resolve() / '.env'
    if path.is_symlink():
        raise ValueError('配置文件不能是符号链接')
    try:
        content = path.read_text(encoding='utf-8') if path.exists() else ''
    except (OSError, UnicodeError):
        raise ValueError('无法读取本地配置文件') from None
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return path, content, values


def _status(values):
    # Match llm.load_env precedence without changing its ROOT or exposing values.
    env_fields = _FIELDS + ('LLM_BASE_URL', 'LLM_API_KEY', 'DEEPSEEK_API_KEY', 'DEEPSEEK_VIDEO_MODEL')
    effective = dict(values)
    for key in env_fields:
        if os.environ.get(key):
            effective[key] = os.environ[key]
    return {
        **{key: bool(effective.get(key)) for key in _FIELDS},
        'vision': bool(effective.get('VIDEO_API_KEY') or effective.get('LLM_API_KEY') or effective.get('DEEPSEEK_API_KEY')),
        'search': bool(effective.get('PEXELS_API_KEY')),
        'environment_override': any(bool(os.environ.get(key)) for key in _FIELDS),
    }


def status(root):
    """Return only booleans; configured does not mean connectivity was tested."""
    with _LOCK:
        return _status(_read(root)[2])


def _validate(key, value):
    if not isinstance(value, str):
        raise ValueError('配置项必须是文字')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('配置项不能包含换行或控制字符')
    value = value.strip()
    # Empty form fields preserve saved values, especially password inputs.
    if not value:
        return None
    limit = 2048 if key == 'VIDEO_BASE_URL' else 200 if key == 'VIDEO_MODEL' else 4096
    if len(value) > limit:
        raise ValueError('配置项超过允许长度')
    if key == 'VIDEO_BASE_URL':
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
            if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError()
            if any(char.isspace() for char in value) or '\\' in value:
                raise ValueError()
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == 'localhost'
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback):
                raise ValueError()
            if port is not None and not 1 <= port <= 65535:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise ValueError('模型地址须为 HTTPS；本机 localhost 或回环地址可使用 HTTP，不能包含账号、查询参数或片段') from None
        return value.rstrip('/')
    if key == 'VIDEO_MODEL':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]*', value):
            raise ValueError('模型名称格式无效')
    elif any(char.isspace() for char in value):
        raise ValueError('密钥不能包含空白字符')
    return value


def save_settings(root, body):
    """Atomically save the four allowed fields and return boolean status only."""
    if not isinstance(body, dict) or not body or set(body) - set(_FIELDS):
        raise ValueError('配置字段无效')
    updates = {}
    for key, value in body.items():
        validated = _validate(key, value)
        if validated is not None:
            updates[key] = validated
    with _LOCK:
        path, content, values = _read(root)
        if not updates:
            return _status(values)
        lines, replaced = [], set()
        for line in content.splitlines(keepends=True):
            stripped = line.strip()
            key = stripped.split('=', 1)[0].strip() if '=' in stripped and not stripped.startswith('#') else None
            if key in updates:
                if key not in replaced:
                    newline = '\r\n' if line.endswith('\r\n') else '\n'
                    lines.append(key + '=' + updates[key] + newline)
                    replaced.add(key)
                # Remove duplicate occurrences so llm's last-value parsing agrees.
            else:
                lines.append(line)
        if lines and not lines[-1].endswith(('\n', '\r')):
            lines[-1] += '\n'
        for key in _FIELDS:
            if key in updates and key not in replaced:
                lines.append(key + '=' + updates[key] + '\n')
        temp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix='.reference-settings-', dir=path.parent)
            temp_path = Path(name)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(''.join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except OSError:
            raise ValueError('本地配置保存失败，原配置保留') from None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        values.update(updates)
        return _status(values)
