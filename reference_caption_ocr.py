#!/usr/bin/env python3
"""Framewise local macOS Vision OCR for actual reference subtitles.

Outputs observations, not a guessed transcript. OCR errors remain reviewable;
per-frame frame numbers are authoritative and use half-open frame intervals.
"""
import argparse
import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path

SWIFT = r'''
import Foundation
import Vision
import ImageIO

let root = URL(fileURLWithPath: CommandLine.arguments[1])
let output = CommandLine.arguments[2]
let files = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
    .filter { $0.pathExtension == "png" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
FileManager.default.createFile(atPath: output, contents: nil)
let handle = try FileHandle(forWritingTo: URL(fileURLWithPath: output))
for (index, url) in files.enumerated() {
    autoreleasepool {
        let frame = Int(url.deletingPathExtension().lastPathComponent) ?? -1
        var row: [String:Any] = ["frame": frame, "file": url.path]
        do {
            guard let src = CGImageSourceCreateWithURL(url as CFURL,nil), let image = CGImageSourceCreateImageAtIndex(src,0,nil) else {
                throw NSError(domain: "ReferenceOCR",code: 1)
            }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.usesCPUOnly = true
            request.recognitionLanguages = ["en-US"]
            request.usesLanguageCorrection = false
            request.minimumTextHeight = 0.028
            request.customWords = ["TrainPal", "Railcard", "Railcards", "Borough", "Wymondham", "JEN", "O2"]
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
            let observations = request.results ?? []
            row["width"] = image.width
            row["height"] = image.height
            row["observations"] = observations.map { obs -> [String:Any] in
                let candidates = obs.topCandidates(3)
                let box = obs.boundingBox
                return ["text": candidates.first?.string ?? "", "confidence": candidates.first?.confidence ?? 0,
                    "bbox_crop_px": [box.minX * Double(image.width), (1-box.maxY) * Double(image.height), box.width * Double(image.width), box.height * Double(image.height)],
                    "alternatives": candidates.map { ["text": $0.string, "confidence": $0.confidence] }]
            }
        } catch { let err = error as NSError; row["error"] = ["description":err.description,"domain":err.domain,"code":err.code,"userInfo":String(describing:err.userInfo)] }
        if let data = try? JSONSerialization.data(withJSONObject:row, options:[.sortedKeys]) {
            handle.write(data); handle.write(Data([10]))
        }
        if index % 100 == 0 { fputs("Vision OCR \(index)/\(files.count)\n",stderr) }
    }
}
try handle.close()
'''

def canonical(value):
    """OCR alignment form, retaining meaningful currency/punctuation."""
    value=unicodedata.normalize('NFKD',value)
    return ''.join(c.lower() for c in value if c.isalnum() or c in "£%?.,'")

def closest_prefix(observed, target):
    """Find a target prefix by edit distance; target comes from reviewed task data."""
    observed=canonical(observed)
    if not observed:return 0,0
    previous=list(range(len(observed)+1));scores=[previous[-1]]
    for i,letter in enumerate(target,1):
        current=[i]
        for j,actual in enumerate(observed,1):
            current.append(min(current[-1]+1,previous[j]+1,previous[j-1]+(letter!=actual)))
        scores.append(current[-1]);previous=current
    best=min(range(len(scores)),key=lambda k:(scores[k],abs(k-len(observed)),k))
    return best,scores[best]

def make_reviewed_analysis(records,config,crop,fps,root):
    """Turn framewise observations plus explicit review data into static ASS events.

    No reference-specific words live in this implementation. Review data must
    name its source, phrase intervals and observed line positions.
    """
    from PIL import ImageFont
    font=config['font'];glyph_font=ImageFont.truetype(font['file'],font['pil_size_px'],index=font.get('collection_index',0))
    by_frame={r['frame']:r for r in records};all_events=[];phrase_results=[]
    for phrase in config['phrases']:
        complete=phrase['text'];target=canonical(complete)
        canonical_to_original=[]
        for i,char in enumerate(complete):
            canonical_to_original.extend([i+1]*len(canonical(char)))
        origins=phrase['line_origins_px'];state=[];previous=0
        for frame in range(phrase['start_frame'],phrase['end_frame']):
            relevant=[]
            for obs in by_frame.get(frame,{}).get('observations',[]):
                bx,by,bw,bh=obs['bbox_crop_px'];bx+=crop[0];by+=crop[1]
                nearest=min(range(len(origins)),key=lambda j:abs(by+bh/2-(origins[j][1]+18)))
                origin=origins[nearest]
                if abs(by+bh/2-(origin[1]+18))<=21 and bx>=origin[0]-25 and obs['confidence']>=.25:
                    relevant.append((nearest,bx,obs['text']))
            relevant.sort(key=lambda row:(row[0],row[1]))
            observed=' '.join(row[2] for row in relevant)
            count,distance=closest_prefix(observed,target)
            usable=bool(observed) and distance<=max(3,len(canonical(observed))*.45)
            if usable:previous=max(previous,count)
            text=complete[:canonical_to_original[previous-1]] if previous else ''
            correction=None
            for override in phrase.get('prefix_overrides',[]):
                if override['start_frame']<=frame<override['end_frame']:
                    text=override['text'];previous=len(canonical(text));correction=override.get('reason','visual review')
            state.append({'frame':frame,'text':text,'observed_text':observed,'edit_distance':distance,
                          'alignment_usable':usable,'visual_correction':correction})
        phrase_events=[]
        # Separate lines keep the original left edge fixed with make_ass's center alignment.
        for line_index,origin in enumerate(origins):
            active=None
            for row in state:
                lines=row['text'].split('\n');text=lines[line_index] if line_index<len(lines) else ''
                if active and active['text']==text:
                    active['end_frame']=row['frame']+1
                else:
                    if active and active['text']:phrase_events.append(active)
                    width=glyph_font.getlength(text)*font.get('ass_advance_scale',1)
                    active={'text':text,'start_frame':row['frame'],'end_frame':row['frame']+1,
                        'font_name':font['ass_font_name'],'font_status':'inferred',
                        'size':font['ass_size_px']/config['video_height'],
                        'x':(origin[0]+width/2)/config['video_width'],
                        'y':(origin[1]+font['ass_center_from_ink_top_px'])/config['video_height'],
                        'outline':font.get('outline_px',1),'bold':False,'color':'#FFFFFF','animation':'none'}
            if active and active['text']:phrase_events.append(active)
        all_events.extend(phrase_events)
        phrase_results.append({**phrase,'frame_states':state,'text_event_count':len(phrase_events)})
    all_events.sort(key=lambda e:(e['start_frame'],e['y']))
    result={'schema':'reference-subtitle-analysis/v1','method':'Every-frame actual Apple Vision OCR, spatial filtering, explicit visually reviewed transcript, monotonic prefix alignment; no video-specific text hardcoded in the recognizer',
            'status':'reviewed_transcript_with_frame_observation_timing','font_status':'inferred','font':font,
            'fps':fps,'crop_px':crop,'video_width':config['video_width'],'video_height':config['video_height'],
            'text_events':all_events,'phrases':phrase_results,
            'limitations':['OCR detection time can lag a newly typed single glyph by 1–2 frames; uncorrected intra-word prefixes have not all been pixel-verified.',
                           'The original white block cursor and its blink/fade are separate graphics; not represented by text_events.',
                           'Font identity is unknown. Selected installed font is a measured approximation, not an exact-font claim.',
                           'Reference text has a subtle dark shadow; current standard ASS events approximate this with a 1px outline, not a separately verified shadow.'],
            'review_evidence':config.get('review_evidence',[])}
    (root.parent/'subtitle-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

def run(args):
    source = Path(args.video).resolve()
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    binary_root = Path(__file__).resolve().parent / '.venv/bin'
    ffmpeg = str(binary_root / 'ffmpeg') if (binary_root/'ffmpeg').exists() else 'ffmpeg'
    ffprobe = str(binary_root / 'ffprobe') if (binary_root/'ffprobe').exists() else 'ffprobe'
    info = json.loads(subprocess.check_output([ffprobe,'-v','error','-select_streams','v:0','-show_streams','-of','json',str(source)]))['streams'][0]
    numerator, denominator = map(int, info['avg_frame_rate'].split('/'))
    fps = numerator / denominator
    crop = tuple(map(int,args.crop.split(',')))
    if len(crop)!=4: raise ValueError('crop must be x,y,width,height')
    x,y,w,h=crop
    if min(x,y)<0 or min(w,h)<1 or x+w>info['width'] or y+h>info['height']: raise ValueError('crop outside video')
    frames = root/'ocr-frames'; frames.mkdir(exist_ok=True)
    raw = Path(args.raw_jsonl).resolve() if args.raw_jsonl else root/'vision-raw.jsonl'
    if not args.reuse_raw:
        subprocess.run([ffmpeg,'-v','error','-i',str(source),'-vf',f'crop={w}:{h}:{x}:{y}',
                        '-vsync','0','-start_number','0','-y',str(frames/'%06d.png')],check=True)
        swift = root/'vision_ocr.swift'; swift.write_text(SWIFT)
        compiled=root/'vision_ocr'
        cache=root/'swift-module-cache';cache.mkdir(exist_ok=True)
        env={**os.environ,'CLANG_MODULE_CACHE_PATH':str(cache),'SWIFT_MODULECACHE_PATH':str(cache)}
        subprocess.run(['swiftc','-O','-module-cache-path',str(cache),str(swift),'-o',str(compiled),'-framework','Vision','-framework','ImageIO'],check=True,env=env)
        subprocess.run([str(compiled),str(frames),str(raw)],check=True)
    records = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
    errors = [record for record in records if 'error' in record]
    if errors:
        (root/'ocr-errors.json').write_text(json.dumps(errors,ensure_ascii=False,indent=2)+'\n')
    if records and len(errors)==len(records):
        raise RuntimeError(f'All {len(records)} OCR frames failed; this is an OCR execution/environment failure, NOT an empty transcript. '
                           f'If Vision reports nilError or CVPixelBuffer -6662, run the compiled helper in a host context with access to macOS Vision services. See {root / "ocr-errors.json"}')
    events=[]
    for record in records:
        obs = [o for o in record.get('observations',[]) if o['confidence']>=args.minimum_confidence]
        # Keep actual OCR ordering by baseline; preserve line breaks and raw alternatives.
        obs.sort(key=lambda o:(round(o['bbox_crop_px'][1]/15),o['bbox_crop_px'][0]))
        text='\n'.join(o['text'] for o in obs)
        boxes=[[round(o['bbox_crop_px'][0]+x,2),round(o['bbox_crop_px'][1]+y,2),round(o['bbox_crop_px'][2],2),round(o['bbox_crop_px'][3],2)] for o in obs]
        if events and text==events[-1]['text'] and events[-1]['end_frame']==record['frame']:
            events[-1]['end_frame']=record['frame']+1
        else:
            events.append({'start_frame':record['frame'],'end_frame':record['frame']+1,'text':text,'bbox_px':boxes,'ocr_confidences':[o['confidence'] for o in obs]})
    result={'schema':'reference-subtitle-ocr/v1','source':str(source),'fps':fps,'total_frames':len(records),'crop_px':crop,
            'method':'Actual local Apple Vision accurate OCR on every decoded video frame; no remote model or inferred whole transcript',
            'status':'unreviewed_ocr_observations','raw_observations':str(raw),'minimum_confidence':args.minimum_confidence,
            'notes':['OCR may confuse a typewriter cursor with I/l and partial words; these observations require review before rendering.',
                     'A bounding box locates observed raster glyphs, not a verified font size or baseline.',
                     'Frame intervals are exact for OCR observations; changing OCR outputs can include recognition flicker, not real text changes.'],
            'events':events}
    (root/'subtitle-ocr-observations.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if args.reviewed_phrases:
        reviewed=make_reviewed_analysis(records,json.loads(Path(args.reviewed_phrases).read_text()),crop,fps,root)
        print(json.dumps({'reviewed_text_events':len(reviewed['text_events']),'analysis':str(root.parent/'subtitle-analysis.json')}))
    print(json.dumps({'frames':len(records),'ocr_events':len(events),'output':str(root/'subtitle-ocr-observations.json')},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--crop',default='240,1500,680,260')
    parser.add_argument('--minimum-confidence',type=float,default=.3)
    parser.add_argument('--reuse-raw',action='store_true')
    parser.add_argument('--raw-jsonl',help='Use observations produced by a separately launched host Vision helper; combine with --reuse-raw')
    parser.add_argument('--reviewed-phrases',help='Optional task-specific visual corrections/line geometry JSON; never implicit transcript inference')
    run(parser.parse_args())
