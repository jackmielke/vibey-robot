// Renders an SF Symbol onto a rounded gradient tile → PNG at the given size.
import Cocoa
let args = CommandLine.arguments
let size = CGFloat(Double(args[1])!), out = args[2]
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let inset = size * 0.09
let rect = NSRect(x: inset, y: inset, width: size - 2*inset, height: size - 2*inset)
let path = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.225, yRadius: rect.width * 0.225)
NSGradient(starting: NSColor(calibratedRed: 0.55, green: 0.36, blue: 0.98, alpha: 1),
           ending: NSColor(calibratedRed: 0.18, green: 0.62, blue: 0.98, alpha: 1))!.draw(in: path, angle: -60)
let cfg = NSImage.SymbolConfiguration(pointSize: size * 0.42, weight: .medium)
    .applying(.init(paletteColors: [.white]))
if let sym = NSImage(systemSymbolName: "antenna.radiowaves.left.and.right", accessibilityDescription: nil)?
    .withSymbolConfiguration(cfg) {
    let s = sym.size
    sym.draw(in: NSRect(x: (size - s.width)/2, y: (size - s.height)/2, width: s.width, height: s.height))
}
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
