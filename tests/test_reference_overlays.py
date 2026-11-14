"""Real FFmpeg fixture tests; no real-reference likeness claims."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

from PIL import Image, ImageDraw

import reference_overlays as overlays


def event(**changes):
    return {**{'source':'reference','kind':'text_card','start_frame':5,'end_frame':10,
        'x':11,'y':13,'width':17,'height':19,'background_included':False,
        'evidence':'Synthetic opaque test card; no background inside the rectangle.'},**changes}


class OverlayContractTests(unittest.TestCase):
    technical={'width':64,'height':48,'total_frames':15}

    def test_valid_event_retains_original_timing_and_geometry(self):
        self.assertEqual(overlays.validate_overlays([event()],self.technical),[event()])

    def test_requires_explicit_background_free_evidence(self):
        for value in (True,None,'false',0):
            with self.subTest(value=value),self.assertRaises(ValueError):
                overlays.validate_overlays([event(background_included=value)],self.technical)
        with self.assertRaises(ValueError):overlays.validate_overlays([event(evidence='')],self.technical)

    def test_bounds_types_and_full_frame_rejected(self):
        for changes in ({'x':-1},{'x':True},{'width':0},{'height':1.5},{'width':60},
                        {'start_frame':10},{'end_frame':16},{'source':'elsewhere'},
                        {'kind':'background'},{'x':0,'y':0,'width':64,'height':48}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                overlays.validate_overlays([event(**changes)],self.technical)

    def test_filter_rejects_injection_and_bad_fps(self):
        for label in ('0:v];movie=secret[out','[0:v]','0:a','rov_src_0','../../bad'):
            with self.subTest(label=label),self.assertRaises(ValueError):
                overlays.ffmpeg_filter([event()],10,base_label=label)
        for rate in (True,float('nan'),0,-30,float('inf')):
            with self.assertRaises(ValueError):overlays.ffmpeg_filter([event()],rate)
        with self.assertRaises(ValueError):overlays.ffmpeg_filter([event(x='1;movie=x')],10)

    def test_empty_and_multiple_regions(self):
        self.assertEqual(overlays.ffmpeg_filter([],30),('', '0:v'))
        graph,label=overlays.ffmpeg_filter([event(),event(x=30)],10)
        self.assertIn('split=2',graph);self.assertEqual(label,'rov_out_1')
        self.assertNotIn('Synthetic',graph)
        self.assertIn('repeatlast=0',graph)


class OverlayRenderTests(unittest.TestCase):
    def test_exact_pixels_temporal_boundaries_and_unchanged_background(self):
        exe=Path(__file__).resolve().parents[1]/'.venv/bin/ffmpeg'
        if not exe.is_file():self.skipTest('FFmpeg not installed')
        width,height,count=64,48,15
        base_pixel=bytes((0,0,230));red=bytes((240,10,0));green=bytes((0,230,10))
        base=base_pixel*(width*height*count)
        reference=b''.join((red if n<7 else green)*(width*height) for n in range(count))
        checked=overlays.validate_overlays([event()],{'width':width,'height':height,'total_frames':count})
        graph,label=overlays.ffmpeg_filter(checked,10)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'base.rgb').write_bytes(base);(root/'ref.rgb').write_bytes(reference)
            args=[str(exe),'-v','error','-f','rawvideo','-pixel_format','rgb24','-video_size',f'{width}x{height}',
                  '-framerate','10','-i',str(root/'base.rgb'),'-f','rawvideo','-pixel_format','rgb24',
                  '-video_size',f'{width}x{height}','-framerate','10','-i',str(root/'ref.rgb'),
                  '-filter_complex',graph,'-map',f'[{label}]','-frames:v',str(count),'-f','rawvideo','-pix_fmt','rgb24','-']
            result=subprocess.run(args,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            self.assertEqual(len(result.stdout),len(base))
            for n in range(count):
                frame=result.stdout[n*width*height*3:(n+1)*width*height*3]
                for y in range(height):
                    for x in range(width):
                        inside=5<=n<10 and 11<=x<28 and 13<=y<32
                        expected=(red if n<7 else green) if inside else base_pixel
                        actual=frame[(y*width+x)*3:(y*width+x+1)*3]
                        self.assertEqual(actual,expected,(n,x,y,tuple(actual),tuple(expected)))


class AlphaGraphicTests(unittest.TestCase):
    technical = {'width': 64, 'height': 48, 'total_frames': 15, 'fps': 10}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = Image.new('RGB', (23, 21), (230, 20, 10))
        draw = ImageDraw.Draw(self.raw)
        draw.rectangle((3, 3, 19, 17), fill='white')
        draw.rectangle((8, 7, 10, 13), fill='black')
        draw.rectangle((13, 8, 16, 11), fill=(40, 160, 30))
        self.raw.save(self.root / 'raw.png')
        verify = self.raw.copy()
        for y in range(verify.height):
            for x in range(verify.width):
                if not (3 <= x <= 19 and 3 <= y <= 17):
                    verify.putpixel((x, y), (10, 170, 200))
        verify.save(self.root / 'verify.png')
        self.extraction = overlays.extract_white_card_rgba(self.root / 'raw.png', self.root / 'verify.png',
            self.root / 'alpha.png', evidence='Synthetic white card remains unchanged over red and blue backgrounds.')

    def plan(self):
        layer = {'id': 'card', 'type': 'reference_rgba_graphic', 'source': 'reference', 'kind': 'text_card',
            'counts_as_new_stock_asset': False, 'x': 11, 'y': 13, 'width': 23, 'height': 21,
            'start_frame': 5, 'end_frame': 10, 'reference_frame': 6, 'verification_frame': 8,
            'background_included': False, 'temporal_mode': 'verified_static_graphic',
            'raw_crop': str(self.root / 'raw.png'), 'rgba_path': str(self.root / 'alpha.png'),
            'raw_crop_contains_reference_background': True, 'matte_type': 'white_panel_exterior',
            'coordinate_tolerance_px': 2, 'matte_evidence': 'Two-frame white-card exterior with enclosed holes filled.',
            'evidence': 'Static card inspected on two different backgrounds throughout frames 5 to 9.'}
        segment = {'id': 'app_ui', 'type': 'reference_graphic_segment', 'source': 'reference',
            'kind': 'app_screen_recording', 'counts_as_new_stock_asset': False, 'intentional_reference_reuse': True,
            'start_frame': 11, 'end_frame': 13, 'x': 0, 'y': 0, 'width': 64, 'height': 48,
            'evidence': 'Synthetic full-screen App UI evidence, explicitly retained from the reference.'}
        return {'schema': 'reference-graphics/v1', 'rgba_overlays': [layer], 'reference_graphic_segments': [segment],
            'unresolved': [{'id': 'spoken_subtitles', 'description': 'Not extracted by this test.'}]}

    def test_alpha_preserves_black_text_and_coloured_emoji(self):
        with Image.open(self.root / 'alpha.png') as alpha:
            self.assertEqual(alpha.getpixel((0, 0)), (230, 20, 10, 0))
            self.assertEqual(alpha.getpixel((8, 9)), (0, 0, 0, 255))
            self.assertEqual(alpha.getpixel((14, 9)), (40, 160, 30, 255))
        self.assertFalse(self.extraction['pixel_identity_proven'])
        checked = overlays.validate_graphics(self.plan(), self.technical, self.root)
        self.assertFalse(checked['rgba_overlays'][0]['counts_as_new_stock_asset'])
        self.assertEqual(checked['reference_graphic_segments'][0]['type'], 'reference_graphic_segment')

    def test_rejects_arbitrary_mask_that_deletes_internal_text(self):
        with Image.open(self.root / 'alpha.png') as image:
            image.putpixel((8, 9), (0, 0, 0, 0))
            image.save(self.root / 'alpha.png')
        with self.assertRaisesRegex(ValueError, '内部文字'):
            overlays.validate_graphics(self.plan(), self.technical, self.root)

    def test_rejects_forged_pixels_missing_alpha_or_outside_path(self):
        with Image.open(self.root / 'alpha.png') as image:
            image.putpixel((8, 9), (250, 250, 250, 255))
            image.save(self.root / 'forged.png')
        for path in ('forged.png', 'raw.png', '../not-allowed.png'):
            plan = self.plan()
            plan['rgba_overlays'][0]['rgba_path'] = path
            with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                overlays.validate_graphics(plan, self.technical, self.root)

    def test_rejects_source_segment_as_new_stock_and_animated_png_claim(self):
        for field, value in (('counts_as_new_stock_asset', True), ('intentional_reference_reuse', False),
                             ('kind', 'train'), ('width', 63)):
            plan = self.plan()
            plan['reference_graphic_segments'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                overlays.validate_graphics(plan, self.technical, self.root)
        plan = self.plan()
        plan['rgba_overlays'][0]['temporal_mode'] = 'unknown'
        with self.assertRaises(ValueError):
            overlays.validate_graphics(plan, self.technical, self.root)

    def test_manifest_adapter_retains_unresolved_and_source_provenance(self):
        layer = self.plan()['rgba_overlays'][0]
        manifest = {'schema': 'reference-overlay-manual/v1', 'provenance': {'video_width': 64,
            'video_height': 48, 'fps': 10, 'total_frames': 15, 'font_identity': 'unknown'},
            'overlays': [{'id': 'card', 'kind': 'white_card', 'bbox_px': [11, 13, 23, 21],
                **{key: layer[key] for key in ('reference_frame', 'verification_frame', 'start_frame', 'end_frame',
                    'coordinate_tolerance_px', 'raw_crop', 'raw_crop_contains_reference_background')},
                'reusable_rgba_crop': layer['rgba_path'], 'timing_evidence': layer['evidence'],
                'matte_method': layer['matte_evidence']}], 'preserved_evidence_graphics': [],
            'unresolved': [{'id': 'spoken_subtitles'}]}
        plan = overlays.prepare_graphics(manifest, self.technical, self.root)
        self.assertEqual(plan['provenance']['font_identity'], 'unknown')
        self.assertEqual(plan['unresolved'], manifest['unresolved'])
        render = overlays.graphics_ffmpeg(plan, self.technical, self.root)
        self.assertNotIn(str(self.root), render['filter_complex'])
        self.assertIn(str((self.root / 'alpha.png').resolve()), render['input_args'])
        with self.assertRaises(ValueError):
            overlays.graphics_ffmpeg(plan, self.technical, self.root, base_label='0:v];movie=x')
        with self.assertRaises(ValueError):
            overlays.graphics_ffmpeg(plan, self.technical, self.root, first_image_input=1)

    def test_actual_composition_replaces_only_alpha_pixels_and_explicit_ui_segment(self):
        exe = Path(__file__).resolve().parents[1] / '.venv/bin/ffmpeg'
        if not exe.is_file(): self.skipTest('FFmpeg not installed')
        width, height, count = 64, 48, 15
        blue = (10, 30, 220)
        base = bytes(blue) * (width * height * count)
        reference = b''.join(bytes((n * 10, 190, 20)) * (width * height) for n in range(count))
        (self.root / 'base.rgb').write_bytes(base)
        (self.root / 'ref.rgb').write_bytes(reference)
        render = overlays.graphics_ffmpeg(self.plan(), self.technical, self.root)
        args = [str(exe), '-v', 'error', '-f', 'rawvideo', '-pixel_format', 'rgb24',
            '-video_size', '64x48', '-framerate', '10', '-i', str(self.root / 'base.rgb'),
            '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', '64x48', '-framerate', '10',
            '-i', str(self.root / 'ref.rgb'), *render['input_args'], '-filter_complex', render['filter_complex'],
            '-map', '[' + render['output_label'] + ']', '-frames:v', '15', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
        result = subprocess.run(args, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(len(result.stdout), len(base))
        with Image.open(self.root / 'alpha.png') as alpha:
            for n in range(count):
                frame = result.stdout[n * width * height * 3:(n + 1) * width * height * 3]
                for y in range(height):
                    for x in range(width):
                        expected = (n * 10, 190, 20) if 11 <= n < 13 else blue
                        if 5 <= n < 10 and 11 <= x < 34 and 13 <= y < 34:
                            pixel = alpha.getpixel((x - 11, y - 13))
                            if pixel[3]: expected = pixel[:3]
                        actual = tuple(frame[(y * width + x) * 3:(y * width + x + 1) * 3])
                        self.assertEqual(actual, expected, (n, x, y, actual, expected))
        self.assertFalse(render['provenance'][0]['counts_as_new_stock_asset'])


if __name__=='__main__':unittest.main()
