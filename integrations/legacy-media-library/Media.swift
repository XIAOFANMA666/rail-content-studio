import Foundation
import AppKit
import AVFoundation
import Vision

let source = URL(fileURLWithPath: CommandLine.arguments[1])
let folder = URL(fileURLWithPath: CommandLine.arguments[2])
let image = NSImage(contentsOf: source)
let asset = AVURLAsset(url: source)
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.maximumSize = CGSize(width: 960, height: 960)
let isVideo = ["mp4", "mov", "m4v"].contains(source.pathExtension.lowercased())
let duration = isVideo ? CMTimeGetSeconds(asset.duration) : 0
func frame(_ seconds: Double) throws -> CGImage {
    if isVideo {
        return try generator.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: nil)
    }
    if let result = image?.cgImage(forProposedRect: nil, context: nil, hints: nil) { return result }
    throw NSError(domain: "无法解码媒体", code: 1)
}
func signature(_ frame: CGImage) -> [UInt8] {
    var pixels = [UInt8](repeating: 0, count: 16 * 16 * 4)
    pixels.withUnsafeMutableBytes { bytes in
        let context = CGContext(data: bytes.baseAddress, width: 16, height: 16, bitsPerComponent: 8, bytesPerRow: 64, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        context?.draw(frame, in: CGRect(x: 0, y: 0, width: 16, height: 16))
    }
    return pixels
}
func describe(_ frame: CGImage) -> ([String], [String], String) {
    let classification = VNClassifyImageRequest()
    let faces = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: frame)
    var tags: [String] = []
    var risks: [String] = []
    var method = "Apple Vision"
    do {
        try handler.perform([classification])
        let map = ["train":"列车", "railroad":"铁路", "train_station":"车站", "mountain":"山地", "lake":"湖泊", "landscape":"风景", "person":"人物", "building":"建筑", "sky":"天空", "text":"文字", "water":"水域", "vehicle":"交通工具"]
        tags = (classification.results ?? []).filter { $0.confidence >= 0.25 }.prefix(4).map {
            let label = $0.identifier.components(separatedBy: ",").first ?? $0.identifier
            return map[label] ?? label.replacingOccurrences(of: "_", with: " ")
        }
    } catch { method = "基础画面分析" }
    do {
        try handler.perform([faces])
        if !(faces.results ?? []).isEmpty { risks.append("代表帧检测到人脸，请复核") }
    } catch { risks.append("人脸检测不可用，请人工检查") }
    let pixels = signature(frame)
    var total = 0
    for i in stride(from: 0, to: pixels.count, by: 4) {
        total += Int(pixels[i]) + Int(pixels[i+1]) + Int(pixels[i+2])
    }
    let brightness = total / (256 * 3)
    let light = brightness < 75 ? "低亮度" : brightness > 175 ? "高亮度" : "中等亮度"
    let orientation = frame.width > frame.height ? "横屏" : frame.width < frame.height ? "竖屏" : "方形"
    let description = tags.isEmpty ? "\(orientation)画面，\(light)。主体和场景请人工补充。" : "\(orientation)画面，\(light)。代表帧可能包含\(tags.joined(separator: "、"))。"
    return (Array(Set(tags + [orientation, light])).sorted(), risks, description + "〔\(method)〕")
}
do {
    guard duration.isFinite, !isVideo || duration > 0 else { throw NSError(domain: "无法读取视频时长", code: 2) }
    var boundaries: [Double] = [0]
    if isVideo {
        let step = max(0.6, duration / 60)
        var previous = signature(try frame(0))
        for time in stride(from: step, to: duration - 0.35, by: step) {
            guard let sample = try? frame(time) else { continue }
            let current = signature(sample)
            let distance = zip(previous, current).reduce(0) { $0 + abs(Int($1.0)-Int($1.1)) } / current.count
            if distance > 42, time - boundaries.last! >= 1, duration - time >= 1 { boundaries.append(time) }
            previous = current
        }
    }
    boundaries.append(duration)
    var parts: [[String: Any]] = []
    for index in 0..<(boundaries.count - 1) {
        let start = boundaries[index], end = boundaries[index+1]
        let sample = try frame((start+end)/2)
        let poster = folder.appendingPathComponent("\(index).jpg")
        guard let jpeg = NSBitmapImageRep(cgImage: sample).representation(using: .jpeg, properties: [.compressionFactor: 0.83]) else {
            throw NSError(domain: "无法生成预览", code: 3)
        }
        try jpeg.write(to: poster, options: .atomic)
        let (tags, risks, description) = describe(sample)
        parts.append(["start":start, "end":end, "poster":poster.lastPathComponent, "tags":tags, "risks":risks, "description":description])
    }
    let size = isVideo ? asset.tracks(withMediaType: .video).first.map { $0.naturalSize.applying($0.preferredTransform) } : image?.representations.first.map { CGSize(width: $0.pixelsWide, height: $0.pixelsHigh) }
    let result: [String:Any] = ["duration":duration, "width":Int(abs(size?.width ?? 0)), "height":Int(abs(size?.height ?? 0)), "parts":parts]
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result))
} catch {
    FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
    exit(1)
}
