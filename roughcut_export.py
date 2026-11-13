"""Render the current workstation timeline locally; never modify the Jianying draft."""
import copy
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def ffmpeg_exe():
    candidates = [shutil.which('ffmpeg')]
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    for exe in filter(None, candidates):
        filters = subprocess.run([exe, '-hide_banner', '-filters'], capture_output=True, text=True)
        if filters.returncode == 0 and ' subtitles ' in filters.stdout:
            return exe
    raise ValueError('缺少支持字幕的 FFmpeg，请安装 FFmpeg 后重试。')


def prepare(result, swaps):
    result = copy.deepcopy(result)
    if not isinstance(swaps, dict):
        raise ValueError('镜头修改格式不正确。')
    try:
        for si, changes in swaps.items():
            if not str(si).isdigit() or not isinstance(changes, dict):
                raise ValueError()
            seg = result['segments'][int(si)]
            for hi, cid in changes.items():
                if not str(hi).isdigit() or cid not in result['clips']:
                    raise ValueError()
                seg['shots'][int(hi)]['clip'] = cid
    except (IndexError, KeyError, TypeError, ValueError):
        raise ValueError('镜头编号或素材无效，请刷新工作台后重试。')
    total = float(result['total_s'])
    if not math.isfinite(total) or not 0 < total <= 180:
        raise ValueError('仅支持导出 3 分钟以内的粗剪。')
    return result


def ass_time(seconds):
    centis = round(seconds * 100)
    return f'{centis // 360000}:{centis // 6000 % 60:02}:{centis // 100 % 60:02}.{centis % 100:02}'


def ass_text(text):
    # Prevent script text from becoming ASS override commands.
    return str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')


def subtitles(result):
    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,Arial,46,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,300,1
Style: Sticker,Arial,68,&H002ED6FF,&H002ED6FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,8,70,70,500,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    for seg in result['segments']:
        events.append((0, seg['start'], seg['dur'], 'Subtitle', seg['subtitle']))
        events.extend((1, st['start'], st['dur'], 'Sticker', st['text']) for st in seg.get('stickers', []))
    return header + '\n'.join(f'Dialogue: {layer},{ass_time(start)},{ass_time(start + dur)},{style},,0,0,0,,{ass_text(text)}' for layer, start, dur, style, text in events) + '\n'


def local_file(root, path):
    root = Path(root).resolve()
    file = (root / path).resolve()
    if root not in file.parents or not file.is_file():
        raise ValueError('视频或配音文件缺失，请重新生成粗剪。')
    return file


def render(root, out, result, swaps=None):
    result = prepare(result, swaps or {})
    exe = ffmpeg_exe()
    exports = Path(out) / 'exports'
    exports.mkdir(parents=True, exist_ok=True)
    name = 'roughcut-' + uuid.uuid4().hex + '.mp4'
    def run(args, cwd):
        try:
            subprocess.run([exe, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y'] + args,
                           cwd=cwd, capture_output=True, check=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('视频合成失败或超时，请检查素材和配音文件后重试。') from exc
    with tempfile.TemporaryDirectory(prefix='.render-', dir=exports) as work:
        work = Path(work)
        parts = []
        for seg in result['segments']:
            for shot in seg['shots']:
                clip = result['clips'][shot['clip']]
                source = local_file(root, clip['file'])
                # Round absolute boundaries, avoiding accumulated per-shot frame drift.
                frames = round((shot['start'] + shot['dur']) * 30) - round(shot['start'] * 30)
                if frames < 1:
                    raise ValueError('镜头时长不足一帧。')
                offset = max(0, min(shot['src_in'], max(0, clip['clip_s'] - shot['dur'])))
                part = f'part{len(parts):03}.mp4'
                run(['-ss', str(offset), '-i', str(source), '-an', '-vf',
                     'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=180',
                     '-frames:v', str(frames), '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20', '-pix_fmt', 'yuv420p', '-threads', '2', part], work)
                parts.append(f"file '{part}'")
        (work / 'parts.txt').write_text('\n'.join(parts), encoding='utf-8')
        (work / 'captions.ass').write_text(subtitles(result), encoding='utf-8')
        args = ['-f', 'concat', '-safe', '1', '-i', 'parts.txt']
        filters, labels = [], []
        for i, seg in enumerate(result['segments'], 1):
            audio_base = 'audio/' + Path(seg['audio']).name if result.get('preview_only') else seg['audio']
            suffix = '.wav' if (Path(root) / (audio_base + '.wav')).is_file() else '.m4a'
            args += ['-i', str(local_file(root, audio_base + suffix))]
            delay = round(seg['start'] * 1000)
            filters.append(f'[{i}:a]aresample=48000,atrim=duration={seg["dur"]},asetpts=PTS-STARTPTS,adelay={delay}:all=1[a{i}]')
            labels.append(f'[a{i}]')
        filters.append(''.join(labels) + f'amix=inputs={len(labels)}:normalize=0,apad,atrim=duration={result["total_s"]}[voice]')
        args += ['-filter_complex', ';'.join(filters), '-map', '0:v:0', '-map', '[voice]', '-vf', 'subtitles=captions.ass',
                 '-t', str(result['total_s']), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p',
                 '-threads', '2', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', 'finished.mp4']
        run(args, work)
        os.replace(work / 'finished.mp4', exports / name)
    return {'url': '/exports/' + name, 'filename': name, 'duration': result['total_s'], 'run_id': result.get('run_id')}
