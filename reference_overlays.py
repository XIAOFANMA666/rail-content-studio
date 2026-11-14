"""Reuse validated reference graphics at their original time and position.

These regions are original reference graphics, not newly matched footage. The
caller must record that provenance and visually verify that the crop excludes
the reference background. This module cannot establish that fact from metadata.
Both input video streams must share the reference's zero-based timeline, frame
rate and dimensions. The crop API uses progressing reference video. The RGBA API
holds a verified static graphic inside its measured interval, while explicitly
declared App UI segments retain the original progressing reference video.
"""
import math
import re
from collections import deque
from pathlib import Path


KINDS = {'text_card', 'logo', 'screenshot'}
MAX_EVENTS = 300


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(label + '必须是有效整数')
    return value


def validate_overlays(events, technical):
    """Return clean events or reject unsafe, incomplete or background-bearing crops."""
    if not isinstance(technical, dict):
        raise ValueError('视频规格无效')
    width = _integer(technical.get('width'), '视频宽度', 1)
    height = _integer(technical.get('height'), '视频高度', 1)
    total = _integer(technical.get('total_frames'), '视频总帧数', 1)
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError('图形图层须为列表，最多300项')
    clean = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('图形图层格式无效')
        if event.get('source') != 'reference' or event.get('kind') not in KINDS:
            raise ValueError('仅支持参考原片中的文字卡、标识或截图图层')
        # Require an explicit observation, not an omitted value coerced to False.
        if type(event.get('background_included')) is not bool:
            raise ValueError('必须明确确认裁剪框是否包含原背景')
        if event['background_included']:
            raise ValueError('裁剪框包含原背景，请缩小或重新分离图形后再合成')
        evidence = event.get('evidence')
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
            raise ValueError('必须提供图形边界与来源的实际观察依据')
        values = {key: _integer(event.get(key), key, 1 if key in ('width', 'height', 'end_frame') else 0)
                  for key in ('start_frame', 'end_frame', 'x', 'y', 'width', 'height')}
        if not 0 <= values['start_frame'] < values['end_frame'] <= total:
            raise ValueError('图形图层时间范围超出参考视频')
        if values['x'] + values['width'] > width or values['y'] + values['height'] > height:
            raise ValueError('图形裁剪范围超出参考画幅')
        # A full-frame screenshot is source footage; require a separate explicit
        # handling path rather than allowing it to masquerade as an overlay.
        if values['width'] == width and values['height'] == height:
            raise ValueError('整帧原片不能作为替代素材的图形叠层')
        clean.append({**values, 'source': 'reference', 'kind': event['kind'],
                      'background_included': False, 'evidence': evidence.strip()})
    return clean


def ffmpeg_filter(events, fps, base_label='0:v', ref_label='1:v'):
    """Return (filter_complex, output_label), the latter without square brackets.

    Call validate_overlays with real technical metadata first. This function also
    validates every interpolated scalar independently. Input labels may be an
    input video stream (0:v or 0:v:0) or a simple pre-existing graph label.
    Event end frames are exclusive. The time enable range ends half a frame
    before end_frame, ensuring the next frame is never accidentally included.
    """
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 < fps <= 240:
        raise ValueError('输出帧率无效')
    for label in (base_label, ref_label):
        if not isinstance(label, str) or not re.fullmatch(r'(?:[0-9]+:v(?::[0-9]+)?|[A-Za-z][A-Za-z0-9_]*)', label):
            raise ValueError('滤镜输入标识无效')
        if label.startswith('rov_'):
            raise ValueError('滤镜输入标识与内部图层名称冲突')
    if base_label == ref_label:
        raise ValueError('背景与参考必须是两个独立输入')
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError('图形图层数量无效')
    if not events:
        return '', base_label
    # Verify contract even if callers skipped the full dimensional validation.
    max_width = max_height = max_frame = 1
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('图形图层格式无效')
        for key in ('x', 'y', 'start_frame', 'width', 'height', 'end_frame'):
            _integer(event.get(key), key, 1 if key in ('width', 'height', 'end_frame') else 0)
        max_width = max(max_width, event['x'] + event['width'] + 1)
        max_height = max(max_height, event['y'] + event['height'] + 1)
        max_frame = max(max_frame, event['end_frame'])
    checked = validate_overlays(events, {'width': max_width, 'height': max_height, 'total_frames': max_frame})
    count = len(checked)
    # RGBA before cropping avoids chroma subsampling rounding odd coordinates.
    parts = [f'[{ref_label}]format=rgba,split={count}' + ''.join(f'[rov_src_{i}]' for i in range(count))]
    previous = base_label
    for i, event in enumerate(checked):
        parts.append(f'[rov_src_{i}]crop=w={event["width"]}:h={event["height"]}:x={event["x"]}:y={event["y"]}:exact=1[rov_crop_{i}]')
        start = event['start_frame'] / fps
        end = (event['end_frame'] - .5) / fps
        output = f'rov_out_{i}'
        parts.append(f'[{previous}][rov_crop_{i}]overlay=x={event["x"]}:y={event["y"]}:'
                     f'enable=\'between(t,{start:.12f},{end:.12f})\':eof_action=pass:repeatlast=0:shortest=0:format=rgb[{output}]')
        previous = output
    return ';'.join(parts), previous


def _fps(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 240:
        raise ValueError('输出帧率无效')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(label + '必须提供实际观察依据')
    return value.strip()


def _geometry(event, technical):
    values = {key: _integer(event.get(key), key, 1 if key in ('width', 'height', 'end_frame') else 0)
              for key in ('start_frame', 'end_frame', 'x', 'y', 'width', 'height')}
    width = _integer(technical.get('width'), '视频宽度', 1)
    height = _integer(technical.get('height'), '视频高度', 1)
    total = _integer(technical.get('total_frames'), '视频总帧数', 1)
    if not values['start_frame'] < values['end_frame'] <= total:
        raise ValueError('图形时间范围超出参考视频')
    if values['x'] + values['width'] > width or values['y'] + values['height'] > height:
        raise ValueError('图形范围超出参考画幅')
    return values


def _image_path(value, asset_root):
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('图形 PNG 路径无效')
    root = Path(asset_root).resolve(strict=True)
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not path.is_relative_to(root) or path.suffix.lower() != '.png' or not path.is_file():
        raise ValueError('图形须为任务目录中的 PNG 文件')
    return path


def _load_png(path, size=None, rgba=False):
    # Imported lazily: the original bounded-crop API needs only the standard library.
    from PIL import Image
    with Image.open(path) as image:
        if image.format != 'PNG' or image.width * image.height > 40_000_000:
            raise ValueError('图形文件不是有效的有限尺寸 PNG')
        if size is not None and image.size != size:
            raise ValueError('图形像素尺寸必须与原参考裁剪框一致')
        if rgba and image.mode != 'RGBA':
            raise ValueError('图形必须包含显式 RGBA 通道')
        image.load()
        return image.copy()


def _exterior(binary, width, height):
    """Return the True pixels reachable from the image boundary (4 connected)."""
    seen = bytearray(width * height)
    queue = deque()
    for y in range(height):
        for x in (0, width - 1):
            pos = y * width + x
            if binary[pos] and not seen[pos]:
                seen[pos] = 1
                queue.append(pos)
    for x in range(width):
        for y in (0, height - 1):
            pos = y * width + x
            if binary[pos] and not seen[pos]:
                seen[pos] = 1
                queue.append(pos)
    while queue:
        pos = queue.popleft()
        y, x = divmod(pos, width)
        neighbours = []
        if x: neighbours.append(pos - 1)
        if x + 1 < width: neighbours.append(pos + 1)
        if y: neighbours.append(pos - width)
        if y + 1 < height: neighbours.append(pos + width)
        for nxt in neighbours:
            if binary[nxt] and not seen[nxt]:
                seen[nxt] = 1
                queue.append(nxt)
    return seen


def extract_white_card_rgba(raw_crop, verification_crop, destination, *, evidence):
    """Extract a solid white panel, preserving enclosed text and emoji pixels.

    Only use on a visually verified white card that is static over two different
    backgrounds. This is deliberately not general chroma key/background removal.
    The opaque panel is the largest shared near-white component with all enclosed
    holes filled. The source RGB is untouched; the returned edge tolerance is an
    explicit limitation of reconstructing a matte from a flattened video.
    """
    from PIL import Image, ImageFilter
    evidence = _text(evidence, '白底卡片验证')
    first = _load_png(raw_crop).convert('RGB')
    second = _load_png(verification_crop, first.size).convert('RGB')
    width, height = first.size
    near_white = lambda pixel: min(pixel) > 235 and max(pixel) - min(pixel) < 18
    mask = Image.new('L', first.size)
    mask.putdata([255 if near_white(a) and near_white(b) else 0
                  for a, b in zip(first.getdata(), second.getdata())])
    mask = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    remaining = bytearray(mask.tobytes())
    largest = []
    for pos in range(width * height):
        if not remaining[pos]:
            continue
        queue, component = deque([pos]), []
        remaining[pos] = 0
        while queue:
            cur = queue.popleft()
            component.append(cur)
            y, x = divmod(cur, width)
            neighbours = []
            if x: neighbours.append(cur - 1)
            if x + 1 < width: neighbours.append(cur + 1)
            if y: neighbours.append(cur - width)
            if y + 1 < height: neighbours.append(cur + width)
            for nxt in neighbours:
                if remaining[nxt]:
                    remaining[nxt] = 0
                    queue.append(nxt)
        if len(component) > len(largest):
            largest = component
    if len(largest) < width * height * .15:
        raise ValueError('缺少可验证的白色连续卡片，不能自动删除背景')
    panel = bytearray(width * height)
    for pos in largest:
        panel[pos] = 1
    outside = _exterior(bytearray(not val for val in panel), width, height)
    alpha = bytes(0 if val else 255 for val in outside)
    if not any(outside):
        raise ValueError('无法区分卡片外部背景，请提供更精确的图形边界或显式矩形图形')
    graphic = first.convert('RGBA')
    graphic.putalpha(Image.frombytes('L', first.size, alpha))
    graphic.save(destination, format='PNG')
    return {'method': 'two_frame_white_panel_enclosed_holes', 'evidence': evidence,
            'coordinate_tolerance_px': 2, 'pixel_identity_proven': False,
            'note': 'Only exterior pixels removed; enclosed text/emoji retained. Edge matte is inferred.'}


def validate_graphics(plan, technical, asset_root):
    """Validate PNG mattes and explicit source UI segments; preserve provenance.

    Files are restricted to asset_root. An RGBA extension alone is insufficient:
    dimensions, source RGB identity, transparency topology and declared extraction
    method are checked. Visual evidence is still necessary for semantic identity.
    """
    if not isinstance(plan, dict) or plan.get('schema') != 'reference-graphics/v1':
        raise ValueError('原图形计划格式无效')
    _fps(technical.get('fps'))
    layers, segments = plan.get('rgba_overlays'), plan.get('reference_graphic_segments')
    if not isinstance(layers, list) or not isinstance(segments, list) or len(layers) + len(segments) > MAX_EVENTS:
        raise ValueError('原图形事件数量无效')
    clean_layers, clean_segments = [], []
    ids = set()
    for event in layers + segments:
        if not isinstance(event, dict):
            raise ValueError('原图形事件格式无效')
        name = event.get('id')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name) or name in ids:
            raise ValueError('图形 ID 无效或重复')
        ids.add(name)
        if event.get('source') != 'reference' or event.get('counts_as_new_stock_asset') is not False:
            raise ValueError('原图形必须明确标为参考复用，不能算作新匹配素材')
        geom = _geometry(event, technical)
        evidence = _text(event.get('evidence'), '图形来源')
        common = {**geom, 'id': name, 'source': 'reference', 'counts_as_new_stock_asset': False,
                  'evidence': evidence, 'type': event.get('type')}
        if event in segments:
            if event.get('type') != 'reference_graphic_segment' or event.get('kind') != 'app_screen_recording':
                raise ValueError('整帧参考复用仅支持明确标识的 App 界面记录')
            if event.get('intentional_reference_reuse') is not True:
                raise ValueError('原界面片段必须明确记录为有意复用')
            if (geom['x'], geom['y'], geom['width'], geom['height']) != (0, 0, technical['width'], technical['height']):
                raise ValueError('原界面片段必须使用完整原画幅')
            clean_segments.append({**common, 'kind': 'app_screen_recording', 'intentional_reference_reuse': True})
            continue
        if event.get('type') != 'reference_rgba_graphic' or event.get('kind') not in KINDS:
            raise ValueError('仅支持原参考字卡、logo 和截图 RGBA 图形')
        if event.get('background_included') is not False:
            raise ValueError('已分离的图层不能携带原活动背景')
        if geom['width'] == technical['width'] and geom['height'] == technical['height']:
            raise ValueError('全帧原图须使用 reference_graphic_segment')
        sample = _integer(event.get('reference_frame'), '参考采样帧')
        verify = _integer(event.get('verification_frame'), '复核采样帧')
        if not geom['start_frame'] <= sample < geom['end_frame'] or not geom['start_frame'] <= verify < geom['end_frame'] or sample == verify:
            raise ValueError('静态图层必须在原时间段内使用不同帧复核')
        if event.get('temporal_mode') != 'verified_static_graphic':
            raise ValueError('静态 PNG 只能用于经过时间稳定性复核的图形')
        method = event.get('matte_type')
        if method not in ('white_panel_exterior', 'opaque_rectangle'):
            raise ValueError('不支持未经验证的任意背景删除')
        if (event['kind'] == 'text_card') != (method == 'white_panel_exterior'):
            raise ValueError('白底字卡须使用外部背景遮罩；logo/截图须显式矩形复用')
        tolerance = _integer(event.get('coordinate_tolerance_px'), '边缘像素误差')
        if tolerance > 4:
            raise ValueError('图形边缘误差过大，不能自动合成')
        raw_path = _image_path(event.get('raw_crop'), asset_root)
        rgba_path = _image_path(event.get('rgba_path'), asset_root)
        size = (geom['width'], geom['height'])
        raw = _load_png(raw_path, size).convert('RGB')
        rgba = _load_png(rgba_path, size, rgba=True)
        if raw.tobytes() != rgba.convert('RGB').tobytes():
            raise ValueError('RGBA 图形必须保留原始 RGB 像素，不能伪造源图形')
        alpha = rgba.getchannel('A').tobytes()
        if method == 'opaque_rectangle':
            if min(alpha) != 255:
                raise ValueError('矩形 logo/截图不得任意挖去内部像素')
        else:
            if min(alpha) != 0 or max(alpha) != 255:
                raise ValueError('白色卡片须有可验证的透明外部和不透明内部')
            if any(value not in (0, 255) for value in alpha):
                raise ValueError('白卡外部遮罩须保持内部不透明，半透明边缘需要单独复核')
            transparent = bytearray(value == 0 for value in alpha)
            outside = _exterior(transparent, *size)
            if transparent != outside:
                raise ValueError('遮罩删除了内部文字或图案；只允许删除卡片外部背景')
        clean_layers.append({**common, 'kind': event['kind'], 'background_included': False,
            'raw_crop': str(raw_path), 'rgba_path': str(rgba_path), 'matte_type': method,
            'reference_frame': sample, 'verification_frame': verify,
            'temporal_mode': 'verified_static_graphic', 'coordinate_tolerance_px': tolerance,
            'raw_crop_contains_reference_background': event.get('raw_crop_contains_reference_background'),
            'edge_pixel_identity_proven': False, 'matte_evidence': _text(event.get('matte_evidence'), '图形分离')})
    clean_segments.sort(key=lambda event: event['start_frame'])
    if any(a['end_frame'] > b['start_frame'] for a, b in zip(clean_segments, clean_segments[1:])):
        raise ValueError('原界面片段时间范围不能重叠')
    return {'schema': 'reference-graphics/v1', 'rgba_overlays': clean_layers,
            'reference_graphic_segments': clean_segments,
            'provenance': plan.get('provenance', {}), 'unresolved': plan.get('unresolved', [])}


def prepare_graphics(manifest, technical, asset_root):
    """Adapt measured reference-overlay-manual/v1 evidence into a render plan."""
    if not isinstance(manifest, dict) or manifest.get('schema') != 'reference-overlay-manual/v1':
        raise ValueError('原图形证据清单格式无效')
    provenance = manifest.get('provenance', {})
    for source, target in (('video_width', 'width'), ('video_height', 'height'), ('total_frames', 'total_frames'), ('fps', 'fps')):
        if provenance.get(source) != technical.get(target):
            raise ValueError('图形证据与参考视频规格不一致')
    mapping = {'white_card': ('text_card', 'white_panel_exterior'),
               'white_compound_card': ('text_card', 'white_panel_exterior'),
               'opaque_logo': ('logo', 'opaque_rectangle'),
               'opaque_screenshot': ('screenshot', 'opaque_rectangle')}
    layers, segments = [], []
    for row in manifest.get('overlays', []):
        if not isinstance(row, dict) or row.get('kind') not in mapping:
            raise ValueError('证据中的图形类型未受支持')
        bbox = row.get('bbox_px')
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError('图形证据缺少原始像素位置')
        kind, matte = mapping[row['kind']]
        layers.append({**dict(zip(('x', 'y', 'width', 'height'), bbox)),
            'id': row.get('id'), 'source': 'reference', 'type': 'reference_rgba_graphic', 'kind': kind,
            'counts_as_new_stock_asset': False, 'background_included': False,
            'start_frame': row.get('start_frame'), 'end_frame': row.get('end_frame'),
            'reference_frame': row.get('reference_frame'), 'verification_frame': row.get('verification_frame'),
            'temporal_mode': 'verified_static_graphic', 'matte_type': matte,
            'raw_crop': row.get('raw_crop'), 'rgba_path': row.get('reusable_rgba_crop'),
            'raw_crop_contains_reference_background': row.get('raw_crop_contains_reference_background'),
            'coordinate_tolerance_px': row.get('coordinate_tolerance_px'),
            'evidence': row.get('timing_evidence'), 'matte_evidence': row.get('matte_method')})
    for row in manifest.get('preserved_evidence_graphics', []):
        if not isinstance(row, dict) or row.get('kind') != 'original_full_screen_phone_recording':
            raise ValueError('仅支持已说明来源的完整 App 录屏片段')
        if row.get('may_retain_for_same_case_evidence') is not True or row.get('counts_as_new_stock_asset') is not False:
            raise ValueError('原 App 图形片段必须明确允许原案例复用且不能记作新素材')
        bbox = row.get('bbox_px')
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError('原 App 图形片段画幅无效')
        segments.append({**dict(zip(('x', 'y', 'width', 'height'), bbox)),
            'id': row.get('id'), 'source': 'reference', 'type': 'reference_graphic_segment',
            'kind': 'app_screen_recording', 'intentional_reference_reuse': True,
            'counts_as_new_stock_asset': False, 'evidence': row.get('note'),
            'start_frame': row.get('start_frame'), 'end_frame': row.get('end_frame')})
    return validate_graphics({'schema': 'reference-graphics/v1', 'rgba_overlays': layers,
        'reference_graphic_segments': segments, 'provenance': provenance,
        'unresolved': manifest.get('unresolved', [])}, technical, asset_root)


def graphics_ffmpeg(plan, technical, asset_root, base_label='0:v', ref_label='1:v', first_image_input=2):
    """Build argv inputs + graph; caller must map output and cap total_frames.

    Base and reference inputs must already share native dimensions, fps and a
    zero-based timeline. PNGs intentionally hold only *verified static graphics*;
    full-screen reference UI uses the original progressing video at the same time.
    Returned input_args are an argv list, never concatenate them into shell code.
    """
    plan = validate_graphics(plan, technical, asset_root)
    fps = _fps(technical.get('fps'))
    _integer(first_image_input, '首个图形输入序号', 0)
    for label in (base_label, ref_label):
        if not isinstance(label, str) or not re.fullmatch(r'(?:[0-9]+:v(?::[0-9]+)?|[A-Za-z][A-Za-z0-9_]*)', label) or label.startswith('rgv_'):
            raise ValueError('图形滤镜输入标识无效')
        if ':' in label and int(label.split(':')[0]) >= first_image_input:
            raise ValueError('PNG 输入序号与视频输入冲突')
    if base_label == ref_label:
        raise ValueError('背景与参考必须是两个独立输入')
    layers, segments = plan['rgba_overlays'], plan['reference_graphic_segments']
    inputs, parts, provenance = [], [], []
    previous = base_label
    if segments:
        parts.append(f'[{ref_label}]format=rgba,split={len(segments)}' + ''.join(f'[rgv_ref_{i}]' for i in range(len(segments))))
    for i, event in enumerate(segments):
        output = f'rgv_segment_{i}'
        start, end = event['start_frame'] / fps, (event['end_frame'] - .5) / fps
        parts.append(f'[{previous}][rgv_ref_{i}]overlay=x=0:y=0:enable=\'between(t,{start:.12f},{end:.12f})\':'
                     f'eof_action=pass:repeatlast=0:shortest=0:format=rgb[{output}]')
        previous = output
        provenance.append({key: event[key] for key in ('id', 'type', 'source', 'kind', 'counts_as_new_stock_asset', 'start_frame', 'end_frame', 'evidence')})
    for i, event in enumerate(layers):
        inputs.extend(['-loop', '1', '-framerate', str(fps), '-t', f'{technical["total_frames"] / fps:.12f}', '-i', event['rgba_path']])
        start, end = event['start_frame'] / fps, (event['end_frame'] - .5) / fps
        output = f'rgv_overlay_{i}'
        parts.append(f'[{first_image_input + i}:v]format=rgba[rgv_png_{i}]')
        parts.append(f'[{previous}][rgv_png_{i}]overlay=x={event["x"]}:y={event["y"]}:'
                     f'enable=\'between(t,{start:.12f},{end:.12f})\':eof_action=pass:repeatlast=0:shortest=0:format=rgb[{output}]')
        previous = output
        provenance.append({key: event[key] for key in ('id', 'type', 'source', 'kind', 'counts_as_new_stock_asset', 'start_frame', 'end_frame', 'reference_frame', 'verification_frame', 'matte_type', 'coordinate_tolerance_px', 'edge_pixel_identity_proven', 'evidence')})
    return {'input_args': inputs, 'filter_complex': ';'.join(parts), 'output_label': previous,
            'provenance': provenance, 'unresolved': plan['unresolved']}
