"""Local hard-cut detection using spatial coverage and a motion-relative peak.

This detects abrupt picture changes, not semantic scene boundaries or animation
transitions. The constants below are explicit engineering defaults, not learned
video-specific boundaries. Full-screen inserts count as cuts; small overlays do
not. Very subtle cuts, rapid pans and cuts shorter than 100 ms need review.
"""
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

import numpy as np


def _ffmpeg():
    command = shutil.which('ffmpeg')
    local = Path(__file__).resolve().parent / '.venv/bin/ffmpeg'
    if not command and local.is_file() and os.access(local, os.X_OK):
        command = str(local)
    if not command:
        try:
            import imageio_ffmpeg
            command = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            pass
    if not command:
        raise ValueError('镜头切点检测需要 FFmpeg')
    return command


def _metrics(frames, shape):
    """Keep only two previous frames; memory does not grow with video pixels."""
    height, width = shape
    previous = previous2 = None
    values = []
    count = 0
    for frame in frames:
        if frame.shape != (height, width, 3):
            raise ValueError('抽帧尺寸不一致')
        current = frame.astype(np.float32)
        if previous is not None:
            difference = np.abs(current - previous).mean(axis=2) / 255.0
            blocks = difference.reshape(height // 24, 24, width // 24, 24).mean(axis=(1, 3))
            values.append({
                'frame': count,
                'mean': float(difference.mean()),
                'median_region': float(np.median(blocks)),
                'lower_quartile_region': float(np.quantile(blocks, .25)),
                'coverage': float((blocks > .03).mean()),
                'two_frame_difference': (float(np.abs(current - previous2).mean() / 255.0)
                                         if previous2 is not None else None),
            })
        previous2, previous = previous, current
        count += 1
    return values, count


def _select_cuts(metrics, fps, total):
    """Require a broadly distributed change, then suppress nearby weaker peaks."""
    means = np.array([m['mean'] for m in metrics])
    radius = max(3, round(fps * .2))
    candidates = []
    for index, metric in enumerate(metrics):
        # Exclude immediate neighbours: a cut's trailing blend/overlay should
        # not inflate the motion baseline of that cut itself.
        neighbours = np.concatenate((means[max(0, index-radius):max(0, index-1)],
                                     means[index+2:min(len(means), index+radius+1)]))
        baseline = float(np.median(neighbours)) if len(neighbours) else 0.0
        metric['motion_baseline'] = baseline
        metric['relative_peak'] = metric['mean'] / max(.005, baseline)
        metric['candidate'] = bool(
            metric['mean'] >= max(.055, 3 * baseline)
            and metric['median_region'] >= .04
            and metric['lower_quartile_region'] >= .025
            and metric['coverage'] >= .65
        )
        if metric['candidate']:
            candidates.append(metric['frame'])

    # A one-frame flash returning to the prior picture is not two new shots.
    flashes = set()
    for index in range(1, len(metrics)):
        a, b = metrics[index-1], metrics[index]
        returned = b['two_frame_difference']
        if (a['mean'] > .10 and b['mean'] > .10 and returned is not None
                and returned < min(.035, min(a['mean'], b['mean']) * .2)):
            flashes.update((a['frame'], b['frame']))

    separation = max(1, round(fps * .1))
    by_frame = {m['frame']: m for m in metrics}
    chosen = []
    for frame in sorted(candidates, key=lambda i: -by_frame[i]['mean']):
        if frame in flashes:
            continue
        if frame < separation or total-frame < separation:
            continue
        if all(abs(frame-other) > separation for other in chosen):
            chosen.append(frame)
    selected = set(chosen)
    for metric in metrics:
        metric['selected'] = metric['frame'] in selected
        metric['flash_rejected'] = metric['frame'] in flashes
    return [0] + sorted(chosen) + [total]


def detect_cut_details(path, technical):
    """Return CFR output-frame boundaries and auditable per-frame metrics.

    technical must supply width, height, fps and total_frames. FFmpeg normalizes
    decoded frames to the requested output fps so variable-frame-rate input is
    evaluated on the same timeline as the eventual output. Frame ranges are
    left-closed/right-open. No network or model calls are made.
    """
    fps, total = technical['fps'], technical['total_frames']
    if isinstance(fps, bool) or not math.isfinite(fps) or not 1 <= fps <= 60:
        raise ValueError('切点检测帧率必须在1至60之间')
    if type(total) is not int or not 1 <= total <= 10800:
        raise ValueError('切点检测最多支持10800帧')
    width, height = technical['width'], technical['height']
    if width == height:
        sample_width, sample_height = 96, 96
    elif width < height:
        sample_width, sample_height = 96, 168
    else:
        sample_width, sample_height = 168, 96
    frame_bytes = sample_width * sample_height * 3
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError('参考视频不存在')
    command = [_ffmpeg(), '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
               '-i', str(source), '-an', '-vf', f'fps={fps},scale={sample_width}:{sample_height}',
               '-frames:v', str(total), '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        expired = threading.Event()

        def timeout():
            expired.set()
            process.kill()

        timer = threading.Timer(120, timeout)
        timer.daemon = True
        timer.start()

        def decoded():
            while True:
                raw = process.stdout.read(frame_bytes)
                if not raw:
                    break
                if len(raw) != frame_bytes:
                    raise ValueError('参考视频解码得到不完整帧')
                yield np.frombuffer(raw, dtype=np.uint8).reshape(sample_height, sample_width, 3)

        try:
            metrics, count = _metrics(decoded(), (sample_height, sample_width))
            process.wait(timeout=5)
            if expired.is_set():
                raise ValueError('参考视频切点检测超时')
            if process.returncode:
                raise ValueError('参考视频解码失败，请检查视频是否损坏')
            if not count or abs(count-total) > 1:
                raise ValueError('参考视频实际帧数与时间轴不符，请检查视频轨道时长')
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
    cuts = _select_cuts(metrics, fps, total)
    return {'cuts': cuts, 'decoded_frames': count, 'fps': fps,
            'method': 'regional-rgb-adaptive-hard-cuts-v1',
            'limitations': ['只检测突变切点，不识别动画、叠化或淡入淡出转场。',
                            '细微跳切、快速摇镜及短于100毫秒的镜头仍需人工复核。'],
            'metrics': metrics}


def detect_cuts(path, technical):
    """Drop-in list-returning counterpart for reference_video.detect_cuts."""
    return detect_cut_details(path, technical)['cuts']
