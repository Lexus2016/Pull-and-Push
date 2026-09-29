// Renders the app icon (1024×1024 PNG): the dashboard's dark panel with two opposed arrows —
// pull (cold teal) and push (warm amber). Usage: swift make_icon.swift <out.png>

import AppKit

let size = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

func rgb(_ hex: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: a)
}

// the macOS icon grid: an 824-pt rounded square centred on the canvas
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: tile, xRadius: 186, yRadius: 186)
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.45)
shadow.shadowBlurRadius = 28
shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.set()
rgb(0x080d14).setFill()
shape.fill()
NSGraphicsContext.restoreGraphicsState()
NSGradient(starting: rgb(0x16263a), ending: rgb(0x070b11))!.draw(in: shape, angle: -90)
rgb(0x82acd4, 0.28).setStroke()
shape.lineWidth = 4
shape.stroke()

// faint grid lines, like the dashboard's charts
NSGraphicsContext.saveGraphicsState()
shape.addClip()
rgb(0x82acd4, 0.07).setStroke()
for i in 1..<6 {
    let y = tile.minY + CGFloat(i) * tile.height / 6
    let line = NSBezierPath()
    line.move(to: NSPoint(x: tile.minX, y: y))
    line.line(to: NSPoint(x: tile.maxX, y: y))
    line.lineWidth = 3
    line.stroke()
}
NSGraphicsContext.restoreGraphicsState()

func arrow(y: CGFloat, right: Bool, color: NSColor) {
    let x0: CGFloat = 250, x1: CGFloat = 774, shaft: CGFloat = 70, headW: CGFloat = 176, headH: CGFloat = 214
    let p = NSBezierPath()
    if right {
        p.move(to: NSPoint(x: x0, y: y - shaft / 2))
        p.line(to: NSPoint(x: x1 - headW, y: y - shaft / 2))
        p.line(to: NSPoint(x: x1 - headW, y: y - headH / 2))
        p.line(to: NSPoint(x: x1, y: y))
        p.line(to: NSPoint(x: x1 - headW, y: y + headH / 2))
        p.line(to: NSPoint(x: x1 - headW, y: y + shaft / 2))
        p.line(to: NSPoint(x: x0, y: y + shaft / 2))
    } else {
        p.move(to: NSPoint(x: x1, y: y - shaft / 2))
        p.line(to: NSPoint(x: x0 + headW, y: y - shaft / 2))
        p.line(to: NSPoint(x: x0 + headW, y: y - headH / 2))
        p.line(to: NSPoint(x: x0, y: y))
        p.line(to: NSPoint(x: x0 + headW, y: y + headH / 2))
        p.line(to: NSPoint(x: x0 + headW, y: y + shaft / 2))
        p.line(to: NSPoint(x: x1, y: y + shaft / 2))
    }
    p.close()
    p.lineJoinStyle = .round
    NSGraphicsContext.saveGraphicsState()
    let glow = NSShadow()
    glow.shadowColor = color.withAlphaComponent(0.55)
    glow.shadowBlurRadius = 46
    glow.set()
    color.setFill()
    p.fill()
    NSGraphicsContext.restoreGraphicsState()
    color.setStroke()
    p.lineWidth = 14
    p.stroke()
}
arrow(y: 628, right: true, color: rgb(0x5ee9da))
arrow(y: 396, right: false, color: rgb(0xffb663))

NSGraphicsContext.restoreGraphicsState()
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
