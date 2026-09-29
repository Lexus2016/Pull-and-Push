// Pull-and-Push for macOS — a native window around the local dashboard.
//
// The app starts the bundled Python engine (`pull-and-push web`) on 127.0.0.1 with a per-launch
// token, shows the dashboard in a WKWebView and keeps the engine alive while the app runs:
// closing the window does not stop research runs, quitting does (after a confirmation when one
// is active). The engine exits by itself when the app is gone — it watches its stdin, a pipe
// only this process holds (`--parent-pipe`), so even a crash or a force-quit leaves no agents
// running.

import AppKit
import WebKit

// MARK: - Strings (native bits only; the dashboard localizes itself)

enum L {
    static let lang: String = {
        let p = Locale.preferredLanguages.first ?? "en"
        return p.hasPrefix("uk") ? "uk" : p.hasPrefix("ru") ? "ru" : "en"
    }()
    static func t(_ en: String, _ uk: String, _ ru: String) -> String {
        lang == "uk" ? uk : lang == "ru" ? ru : en
    }
    static let cancel = t("Cancel", "Скасувати", "Отмена")
    static let starting = t("Starting the engine…", "Запускаю рушій…", "Запускаю движок…")
}

// MARK: - Self-test

/// `PP_SELFTEST=<result.json>`: load the dashboard in this very WKWebView, exercise the wiring a
/// browser test cannot reach (vendored assets, the native bridge and its origin check, confirm(),
/// a real download), write the results and quit through the normal Quit path. No screen needed —
/// CI runs it (macos/selftest.sh). Native panels are answered automatically in this mode.
enum SelfTest {
    static let out = ProcessInfo.processInfo.environment["PP_SELFTEST"]
    static var on: Bool { out != nil }
    static var results: [String: Any] = [:]
    static var done = false

    static func finish(_ extra: [String: Any] = [:]) {
        guard on, !done else { return }
        done = true
        results.merge(extra) { $1 }
        if let out, let data = try? JSONSerialization.data(withJSONObject: results, options: [.prettyPrinted]) {
            try? data.write(to: URL(fileURLWithPath: out))
        }
        NSApp.terminate(nil)
    }

    static let script = """
    const r = {};
    await new Promise(res => setTimeout(res, 1500));      // loadMeta / loadProjects settle
    const pp = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pp;
    r.bridge = !!pp;
    r.libs = {Chart: typeof Chart, marked: typeof marked, DOMPurify: typeof DOMPurify, hljs: typeof hljs};
    await document.fonts.ready;
    r.font = document.fonts.check('14px "IBM Plex Sans"');
    r.external = [...document.querySelectorAll('script[src],link[href]')].map(e => e.src || e.href)
      .filter(u => !u.startsWith(location.origin) && !u.startsWith('data:'));
    r.installed = (typeof META !== 'undefined' && META.installed) || null;
    r.reveal_missing = await pp.postMessage({cmd: 'reveal', path: '/nonexistent-pp-selftest'});
    r.reveal_home = await pp.postMessage({cmd: 'reveal', path: META.home});
    r.pick = await pp.postMessage({cmd: 'pick', kind: 'folder'});
    r.confirm = confirm('selftest');
    r.errors = window.__ppErrors || [];
    const d = await (await fetch(api('/api/projects'))).json();
    r.projects = d.projects;
    if (d.projects.length) setTimeout(() => { location.href = api('/api/projects/' + d.projects[0] + '/export'); }, 50);
    return JSON.stringify(r);
    """
}

// MARK: - Environment

enum Env {
    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    static let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
    static let background = NSColor(srgbRed: 8 / 255, green: 13 / 255, blue: 20 / 255, alpha: 1)
    static var python: URL { Bundle.main.resourceURL!.appendingPathComponent("python/bin/python3") }
    static var dataDir: String {
        ProcessInfo.processInfo.environment["TYANI_TOLKAI_HOME"] ?? "\(home)/.tyani-tolkai"
    }
    static var logDir: URL {
        FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/Pull-and-Push")
    }
    static var engineLog: URL { logDir.appendingPathComponent("engine.log") }

    /// A Finder-launched app gets launchd's PATH (/usr/bin:/bin:/usr/sbin:/sbin) — none of the
    /// agent CLIs live there (claude: npm or ~/.local/bin, codex: brew or npm). Ask the user's login
    /// shell once, the way code editors do, and keep the usual install dirs as a fallback.
    static func userPATH() -> String {
        var dirs = (loginShellPATH() ?? "").split(separator: ":").map(String.init)
        dirs += ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "\(home)/.local/bin",
                 "\(home)/.npm-global/bin", "\(home)/.bun/bin", "\(home)/.volta/bin",
                 "\(home)/.cargo/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        var seen = Set<String>()
        return dirs.filter { !$0.isEmpty && seen.insert($0).inserted }.joined(separator: ":")
    }

    static func loginShellPATH() -> String? {
        let shell = String(cString: getpwuid(getuid()).pointee.pw_shell)   // $SHELL is unset under launchd
        guard let out = run(shell.isEmpty ? "/bin/zsh" : shell,
                            ["-ilc", "printf '__PP__%s__PP__' \"$PATH\""], timeout: 6) else { return nil }
        let parts = out.components(separatedBy: "__PP__")
        return parts.count >= 3 ? parts[1] : nil
    }

    /// Runs a tool and returns its stdout, or nil when it fails or outlives `timeout` (a slow or
    /// interactive shell rc file must not hang the app's start).
    @discardableResult
    static func run(_ tool: String, _ args: [String], env: [String: String]? = nil,
                    timeout: TimeInterval = 10) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        if let env { p.environment = env }
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let box = DataBox()
        let done = DispatchSemaphore(value: 0)
        DispatchQueue.global().async { box.data = out.fileHandleForReading.readDataToEndOfFile(); done.signal() }
        if done.wait(timeout: .now() + timeout) == .timedOut { p.terminate(); return nil }
        p.waitUntilExit()
        return p.terminationStatus == 0 ? String(decoding: box.data, as: UTF8.self) : nil
    }

    /// Projects are git repositories. Without the Command Line Tools /usr/bin/git is only a stub
    /// (running it pops Apple's installer), so a git elsewhere on PATH is run for real and the
    /// stub is used only when xcode-select reports the tools installed.
    static func gitWorks(path: String) -> Bool {
        for dir in path.split(separator: ":") where dir != "/usr/bin" {
            let git = "\(dir)/git"
            if FileManager.default.isExecutableFile(atPath: git) {
                return run(git, ["--version"]) != nil
            }
        }
        return run("/usr/bin/xcode-select", ["-p"]) != nil && run("/usr/bin/git", ["--version"]) != nil
    }
}

final class DataBox { var data = Data(); var ok = false }

extension Env {
    /// A dashboard already serving this data dir (e.g. the terminal's `pull-and-push web`): the app
    /// becomes a window onto it instead of starting a second engine on the same projects — two
    /// would both drive runs (each marks the other's live runs "stopped" when it starts).
    static func runningDashboard() -> (page: URL, token: String?)? {
        let f = URL(fileURLWithPath: dataDir).appendingPathComponent("dashboard.json")
        guard let d = try? Data(contentsOf: f),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let base = j["url"] as? String, let pid = (j["pid"] as? NSNumber)?.int32Value, pid > 0,
              kill(pid, 0) == 0 || errno == EPERM else { return nil }
        let token = j["token"] as? String
        var page = URLComponents(string: base + "/"), probe = URLComponents(string: base + "/api/meta")
        if let token {
            page?.queryItems = [URLQueryItem(name: "token", value: token)]
            probe?.queryItems = page?.queryItems
        }
        guard let pageURL = page?.url, let probeURL = probe?.url else { return nil }
        var req = URLRequest(url: probeURL)                 // it must answer: a PID may be reused
        req.timeoutInterval = 2
        let box = DataBox(), done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, r, _ in
            box.ok = (r as? HTTPURLResponse)?.statusCode == 200
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 3)
        return box.ok ? (pageURL, token) : nil
    }
}

// MARK: - Engine (the bundled Python dashboard server)

final class Engine {
    private(set) var url: URL?                  // http://127.0.0.1:<port>/?token=…
    private(set) var token = (0..<32).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    private(set) var attached = false          // showing a dashboard someone else started
    var onReady: ((URL) -> Void)?
    var onExit: ((Int32, String) -> Void)?
    private var process: Process?
    private var parentPipe: Pipe?
    private var log: FileHandle?
    private var pending = Data()
    private var tail: [String] = []

    var isRunning: Bool { process?.isRunning ?? false }
    var origin: URL? {
        guard let u = url, var c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return nil }
        c.query = nil; c.path = ""
        return c.url
    }

    func attach(_ page: URL, token: String?) {
        url = page
        self.token = token ?? ""
        attached = true
    }

    func start(path: String) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: Env.logDir, withIntermediateDirectories: true)
        let prev = Env.logDir.appendingPathComponent("engine.previous.log")
        try? fm.removeItem(at: prev)
        try? fm.moveItem(at: Env.engineLog, to: prev)
        fm.createFile(atPath: Env.engineLog.path, contents: nil)
        log = try? FileHandle(forWritingTo: Env.engineLog)
        url = nil; tail = []; pending = Data()

        let p = Process()
        p.executableURL = Env.python
        p.arguments = ["-m", "tyani_tolkai.cli", "web", "--host", "127.0.0.1", "--port", "auto",
                       "--parent-pipe"]
        var env = ProcessInfo.processInfo.environment
        for k in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"] {
            env[k] = nil                                       // the bundled Python, nothing else
        }
        env["PATH"] = path
        env["TYANI_TOLKAI_WEB_PASSWORD"] = token
        env["PYTHONDONTWRITEBYTECODE"] = "1"   // .pyc written into the bundle would break its signature
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PULL_AND_PUSH_APP"] = Env.version
        p.environment = env
        p.currentDirectoryURL = fm.homeDirectoryForCurrentUser
        let out = Pipe()
        let parent = Pipe()                    // we never write; EOF tells the engine we are gone
        p.standardOutput = out
        p.standardError = out
        p.standardInput = parent
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            if d.isEmpty { h.readabilityHandler = nil }
            DispatchQueue.main.async { self?.consume(d) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.exited(proc.terminationStatus) }
        }
        try p.run()
        try? parent.fileHandleForReading.close()   // the child holds the read end
        process = p
        parentPipe = parent
    }

    private func consume(_ d: Data) {
        guard !d.isEmpty else { return }
        try? log?.write(contentsOf: d)
        pending.append(d)
        while let nl = pending.firstIndex(of: 0x0A) {
            let line = String(decoding: pending[pending.startIndex..<nl], as: UTF8.self)
            pending.removeSubrange(pending.startIndex...nl)
            tail.append(line)
            if tail.count > 40 { tail.removeFirst(tail.count - 40) }
            if url == nil, let r = line.range(of: "WebUI ready: "),
               let u = URL(string: String(line[r.upperBound...]).trimmingCharacters(in: .whitespaces)) {
                url = u
                onReady?(u)
            }
        }
    }

    private func exited(_ status: Int32) {
        let wasRunning = process != nil
        process = nil
        parentPipe = nil
        try? log?.close()
        log = nil
        if wasRunning { onExit?(status, tail.suffix(15).joined(separator: "\n")) }
    }

    /// Graceful stop: EOF on the parent pipe + SIGTERM (the engine kills live agents on the way
    /// out), SIGKILL after 10 s. `done` runs on the main queue.
    func stop(_ done: @escaping () -> Void) {
        guard let p = process, p.isRunning else { done(); return }
        onExit = nil
        try? parentPipe?.fileHandleForWriting.close()
        p.terminate()
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(10)
            while p.isRunning && Date() < deadline { usleep(100_000) }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            DispatchQueue.main.async(execute: done)
        }
    }

    /// GET an engine API path (token appended) → decoded JSON, on the main queue.
    func get(_ path: String, _ done: @escaping ([String: Any]?) -> Void) {
        guard let o = origin, var c = URLComponents(url: o.appendingPathComponent(path),
                                                    resolvingAgainstBaseURL: false) else { done(nil); return }
        c.queryItems = [URLQueryItem(name: "token", value: token)]
        var req = URLRequest(url: c.url!)
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            DispatchQueue.main.async { done(obj) }
        }.resume()
    }

    func activeRuns(_ done: @escaping ([String]) -> Void) {
        get("api/runs/active") { done(($0?["active"] as? [String]) ?? []) }
    }
}

// MARK: - Web view

final class WebController: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate,
                           WKScriptMessageHandlerWithReply {
    let view: WKWebView
    unowned let engine: Engine
    weak var window: NSWindow?
    private var destinations: [ObjectIdentifier: URL] = [:]

    init(engine: Engine) {
        self.engine = engine
        let cfg = WKWebViewConfiguration()
        cfg.applicationNameForUserAgent = "PullAndPushApp/\(Env.version)"
        view = WKWebView(frame: .zero, configuration: cfg)
        super.init()
        cfg.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "pp")
        if SelfTest.on {
            cfg.userContentController.addUserScript(WKUserScript(source: """
            window.__ppErrors = [];
            addEventListener('error', e => __ppErrors.push(String(e.message)));
            addEventListener('unhandledrejection', e => __ppErrors.push(String(e.reason)));
            """, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = false    // a swipe must not leave the dashboard
        view.underPageBackgroundColor = Env.background
        if #available(macOS 13.3, *) { view.isInspectable = true }
        let zoom = UserDefaults.standard.double(forKey: "pageZoom")
        if zoom > 0 { view.pageZoom = zoom }
    }

    func showStatus(_ text: String) {
        let esc = text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        view.loadHTMLString("""
        <html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#080d14;
        color:#94a8bd;font:14px -apple-system,system-ui,sans-serif}
        .s{width:26px;height:26px;margin:0 auto 14px;border:3px solid #16222f;border-top-color:#5ee9da;
        border-radius:50%;animation:r .9s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
        </style></head><body><div style="text-align:center"><div class="s"></div>\(esc)</div></body></html>
        """, baseURL: nil)
    }

    func load(_ url: URL) { view.load(URLRequest(url: url)) }

    private func isEngine(_ u: URL) -> Bool {
        guard let o = engine.origin else { return false }
        return u.scheme == o.scheme && u.host == o.host && u.port == o.port
    }

    // navigation: the dashboard stays in the window, everything else opens in the browser
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let u = a.request.url else { return decisionHandler(.cancel) }
        if a.shouldPerformDownload { return decisionHandler(.download) }
        if isEngine(u) || u.scheme == "about" || u.scheme == "data" || u.scheme == "blob" {
            return decisionHandler(.allow)
        }
        if (a.targetFrame?.isMainFrame ?? true), ["http", "https", "mailto"].contains(u.scheme ?? "") {
            NSWorkspace.shared.open(u)
        }
        decisionHandler(.cancel)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let cd = (r.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(!r.canShowMIMEType || cd.lowercased().hasPrefix("attachment") ? .download : .allow)
    }

    func webView(_ w: WKWebView, navigationAction: WKNavigationAction, didBecome d: WKDownload) {
        d.delegate = self
    }

    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse, didBecome d: WKDownload) {
        d.delegate = self
    }

    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { w.reload() }

    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        guard SelfTest.on, SelfTest.results["js"] == nil, let u = w.url, isEngine(u) else { return }
        SelfTest.results["js"] = "running"
        w.callAsyncJavaScript(SelfTest.script, arguments: [:], in: nil, in: .page) { r in
            switch r {
            case .success(let v):
                SelfTest.results["js"] = (v as? String).flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) } ?? "no result"
                if ((SelfTest.results["js"] as? [String: Any])?["projects"] as? [String])?.isEmpty ?? true {
                    SelfTest.finish()                          // nothing to export
                } else {                                       // the export download finishes the test
                    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { SelfTest.finish(["download": "timed out"]) }
                }
            case .failure(let e): SelfTest.finish(["js": "error: \(e)"])
            }
        }
    }

    // downloads (project export, an iteration's snapshot): a Save panel, then show it in Finder
    func download(_ d: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        if SelfTest.on {
            let u = FileManager.default.temporaryDirectory.appendingPathComponent("pp-selftest-\(suggestedFilename)")
            try? FileManager.default.removeItem(at: u)
            destinations[ObjectIdentifier(d)] = u
            return completionHandler(u)
        }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        let finish: (NSApplication.ModalResponse) -> Void = { [weak self] r in
            guard r == .OK, let u = panel.url else { return completionHandler(nil) }
            try? FileManager.default.removeItem(at: u)     // the panel asked about replacing it already
            self?.destinations[ObjectIdentifier(d)] = u
            completionHandler(u)
        }
        if let win = window { panel.beginSheetModal(for: win, completionHandler: finish) } else { finish(panel.runModal()) }
    }

    func downloadDidFinish(_ d: WKDownload) {
        guard let u = destinations.removeValue(forKey: ObjectIdentifier(d)) else { return }
        if SelfTest.on {
            let size = (try? FileManager.default.attributesOfItem(atPath: u.path)[.size] as? Int) ?? 0
            try? FileManager.default.removeItem(at: u)
            return SelfTest.finish(["download": ["name": u.lastPathComponent, "bytes": size]])
        }
        NSWorkspace.shared.activateFileViewerSelecting([u])
    }

    func download(_ d: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        destinations.removeValue(forKey: ObjectIdentifier(d))
        if SelfTest.on { return SelfTest.finish(["download": "failed: \(error.localizedDescription)"]) }
        sheet(L.t("Download failed", "Не вдалося завантажити", "Не удалось скачать"), error.localizedDescription)
    }

    // JavaScript alert / confirm / prompt (Delete, Reset, Run, Rename … rely on them) and <input type=file>
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        if SelfTest.on { return completionHandler() }
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "OK")
        present(a) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        if SelfTest.on { return completionHandler(true) }
        let a = NSAlert()
        a.messageText = m
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: L.cancel)
        present(a) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = p
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: L.cancel)
        a.window.initialFirstResponder = field
        present(a) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = p.allowsDirectories
        panel.allowsMultipleSelection = p.allowsMultipleSelection
        let finish: (NSApplication.ModalResponse) -> Void = { completionHandler($0 == .OK ? panel.urls : nil) }
        if let win = window { panel.beginSheetModal(for: win, completionHandler: finish) } else { finish(panel.runModal()) }
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url, !isEngine(u) { NSWorkspace.shared.open(u) }   // target=_blank, window.open
        return nil
    }

    // the dashboard's bridge (window.webkit.messageHandlers.pp): native pickers and Finder
    func userContentController(_ ucc: WKUserContentController, didReceive m: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let o = m.frameInfo.securityOrigin
        guard m.frameInfo.isMainFrame, let e = engine.origin, o.protocol == e.scheme, o.host == e.host,
              o.port == e.port ?? -1 else { return replyHandler(nil, "refused") }
        guard let body = m.body as? [String: Any], let cmd = body["cmd"] as? String else {
            return replyHandler(nil, "bad message")
        }
        switch cmd {
        case "pick" where SelfTest.on:
            replyHandler("/selftest", nil)
        case "pick":
            let folder = (body["kind"] as? String) == "folder"
            let panel = NSOpenPanel()
            panel.canChooseDirectories = folder
            panel.canChooseFiles = !folder
            panel.allowsMultipleSelection = false
            if let s = body["start"] as? String, s.hasPrefix("/") {
                var dir: ObjCBool = false
                if FileManager.default.fileExists(atPath: s, isDirectory: &dir) {
                    panel.directoryURL = URL(fileURLWithPath: dir.boolValue ? s : (s as NSString).deletingLastPathComponent)
                }
            }
            let finish: (NSApplication.ModalResponse) -> Void = { replyHandler($0 == .OK ? panel.url?.path : nil, nil) }
            if let win = window { panel.beginSheetModal(for: win, completionHandler: finish) } else { finish(panel.runModal()) }
        case "reveal":
            guard let path = body["path"] as? String, path.hasPrefix("/"),
                  FileManager.default.fileExists(atPath: path) else { return replyHandler(false, nil) }
            if !SelfTest.on { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path) }
            replyHandler(true, nil)
        default:
            replyHandler(nil, "unknown command \(cmd)")
        }
    }

    private func present(_ a: NSAlert, _ done: @escaping (NSApplication.ModalResponse) -> Void) {
        if let win = window, win.attachedSheet == nil { a.beginSheetModal(for: win, completionHandler: done) }
        else { done(a.runModal()) }
    }

    private func sheet(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        present(a) { _ in }
    }

    func zoom(by factor: Double?) {
        view.pageZoom = factor.map { min(2.0, max(0.6, view.pageZoom * $0)) } ?? 1.0
        UserDefaults.standard.set(view.pageZoom, forKey: "pageZoom")
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    let engine = Engine()
    var window: NSWindow!
    var web: WebController!
    var path = ""
    var quitting = false
    var badgeTimer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        web = WebController(engine: engine)
        window = NSWindow(contentRect: initialFrame(), styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Pull-and-Push"
        window.appearance = NSAppearance(named: .darkAqua)
        window.titlebarAppearsTransparent = true
        window.backgroundColor = Env.background
        window.minSize = NSSize(width: 900, height: 600)
        window.isReleasedWhenClosed = false
        window.contentView = web.view
        window.setFrameAutosaveName("Main")
        web.window = window
        web.showStatus(L.starting)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        guard FileManager.default.isExecutableFile(atPath: Env.python.path) else {
            fail(L.t("The app is damaged", "Застосунок пошкоджено", "Приложение повреждено"),
                 L.t("The bundled Python engine is missing. Download the app again.",
                     "Бракує вбудованого рушія Python. Завантаж застосунок знову.",
                     "Не хватает встроенного движка Python. Скачай приложение заново."))
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let path = Env.userPATH()
            let existing = Env.runningDashboard()
            let git = existing != nil || Env.gitWorks(path: path)
            DispatchQueue.main.async {
                self.path = path
                if let (page, token) = existing {
                    self.engine.attach(page, token: token)
                    self.web.load(page)
                    self.startBadge()
                    return
                }
                if !git { self.offerCommandLineTools() }
                self.startEngine()
            }
        }
    }

    func initialFrame() -> NSRect {
        let vis = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(1440, vis.width * 0.92), h = min(920, vis.height * 0.92)
        return NSRect(x: vis.midX - w / 2, y: vis.midY - h / 2, width: w, height: h)
    }

    func startEngine() {
        web.showStatus(L.starting)
        engine.onReady = { [weak self] url in
            self?.web.load(url)
            self?.startBadge()
        }
        engine.onExit = { [weak self] status, tail in self?.engineStopped(status, tail) }
        do { try engine.start(path: path) } catch {
            fail(L.t("Could not start the engine", "Не вдалося запустити рушій", "Не удалось запустить движок"),
                 error.localizedDescription)
        }
    }

    func engineStopped(_ status: Int32, _ tail: String) {
        badgeTimer?.invalidate()
        NSApp.dockTile.badgeLabel = nil
        guard !quitting else { return }
        if SelfTest.on { return SelfTest.finish(["engine": "stopped (code \(status)): \(tail)"]) }
        web.showStatus(L.t("The engine stopped.", "Рушій зупинився.", "Движок остановился."))
        let a = NSAlert()
        a.alertStyle = .warning
        a.messageText = L.t("The engine stopped unexpectedly (code \(status))",
                            "Рушій несподівано зупинився (код \(status))",
                            "Движок неожиданно остановился (код \(status))")
        a.informativeText = tail.isEmpty ? Env.engineLog.path : tail
        a.addButton(withTitle: L.t("Restart", "Перезапустити", "Перезапустить"))
        a.addButton(withTitle: L.t("Open the log", "Відкрити журнал", "Открыть журнал"))
        a.addButton(withTitle: L.t("Quit", "Вийти", "Выйти"))
        switch a.runModal() {
        case .alertFirstButtonReturn: startEngine()
        case .alertSecondButtonReturn: openLog(); startEngine()
        default: NSApp.terminate(nil)
        }
    }

    func offerCommandLineTools() {
        if SelfTest.on { SelfTest.results["git"] = false; return }
        let a = NSAlert()
        a.messageText = L.t("Git is needed", "Потрібен Git", "Нужен Git")
        a.informativeText = L.t(
            "Every project keeps its history in git, which comes with Apple's Command Line Tools. Install them now (a few minutes), then reopen the app.",
            "Кожен проєкт зберігає історію в git, а він входить до Command Line Tools від Apple. Встанови їх зараз (кілька хвилин), потім знову відкрий застосунок.",
            "Каждый проект хранит историю в git, а он входит в Command Line Tools от Apple. Установи их сейчас (несколько минут), затем снова открой приложение.")
        a.addButton(withTitle: L.t("Install", "Встановити", "Установить"))
        a.addButton(withTitle: L.t("Later", "Пізніше", "Позже"))
        if a.runModal() == .alertFirstButtonReturn {
            Env.run("/usr/bin/xcode-select", ["--install"], timeout: 5)
        }
    }

    func fail(_ title: String, _ text: String) {
        if SelfTest.on { return SelfTest.finish(["fatal": "\(title): \(text)"]) }
        let a = NSAlert()
        a.alertStyle = .critical
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: L.t("Quit", "Вийти", "Выйти"))
        a.runModal()
        NSApp.terminate(nil)
    }

    // runs keep going with the window closed: the Dock icon shows how many
    func startBadge() {
        badgeTimer?.invalidate()
        badgeTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.engine.activeRuns { NSApp.dockTile.badgeLabel = $0.isEmpty ? nil : "\($0.count)" }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        if quitting || !engine.isRunning { return .terminateNow }
        engine.activeRuns { names in
            if !names.isEmpty && !SelfTest.on {
                let a = NSAlert()
                a.messageText = L.t("Research is running", "Дослідження ще триває", "Исследование ещё идёт")
                a.informativeText = L.t(
                    "\(names.joined(separator: ", ")) — quitting stops the agents now. Everything kept so far stays; press Run later to continue.",
                    "\(names.joined(separator: ", ")) — вихід зупинить агентів зараз. Усе, що вже збережено, лишиться; пізніше натисни Run, щоб продовжити.",
                    "\(names.joined(separator: ", ")) — выход остановит агентов сейчас. Всё, что уже сохранено, останется; позже нажми Run, чтобы продолжить.")
                a.addButton(withTitle: L.t("Quit", "Вийти", "Выйти"))
                a.addButton(withTitle: L.cancel)
                if a.runModal() != .alertFirstButtonReturn {
                    NSApp.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
            self.quitting = true
            self.web.showStatus(L.t("Stopping…", "Зупиняю…", "Останавливаю…"))
            self.engine.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    // MARK: menu actions

    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func reload() { if let u = engine.url { web.load(u) } else { web.view.reload() } }
    @objc func zoomIn() { web.zoom(by: 1.1) }
    @objc func zoomOut() { web.zoom(by: 1 / 1.1) }
    @objc func zoomReset() { web.zoom(by: nil) }
    @objc func openData() {
        try? FileManager.default.createDirectory(atPath: Env.dataDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: Env.dataDir))
    }
    @objc func openLog() { NSWorkspace.shared.open(Env.engineLog) }

    /// Links the bundle's CLI into ~/.local/bin: the terminal (and agents working there) then drive
    /// the same projects, and `research start` finds this app's dashboard by itself.
    @objc func installCommand() {
        let target = Bundle.main.resourceURL!.appendingPathComponent("bin/pull-and-push").path
        let dir = "\(Env.home)/.local/bin", link = "\(dir)/pull-and-push"
        let fm = FileManager.default
        let a = NSAlert()
        do {
            try fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
            if (try? fm.destinationOfSymbolicLink(atPath: link)) != nil { try fm.removeItem(atPath: link) }
            else if fm.fileExists(atPath: link) {
                throw NSError(domain: "pp", code: 1, userInfo: [NSLocalizedDescriptionKey: L.t(
                    "\(link) already exists and is not a link — remove it first.",
                    "\(link) уже існує і це не посилання — спершу видали його.",
                    "\(link) уже существует и это не ссылка — сначала удали его.")])
            }
            try fm.createSymbolicLink(atPath: link, withDestinationPath: target)
            let onPath = path.split(separator: ":").contains { $0 == dir }
            a.messageText = L.t("Command installed", "Команду встановлено", "Команда установлена")
            a.informativeText = L.t("pull-and-push → \(link)", "pull-and-push → \(link)", "pull-and-push → \(link)")
                + (onPath ? "" : L.t("\n\nAdd ~/.local/bin to your PATH to run it by name.",
                                     "\n\nДодай ~/.local/bin до PATH, щоб запускати її за назвою.",
                                     "\n\nДобавь ~/.local/bin в PATH, чтобы запускать её по имени."))
        } catch {
            a.alertStyle = .warning
            a.messageText = L.t("Could not install the command", "Не вдалося встановити команду",
                                "Не удалось установить команду")
            a.informativeText = error.localizedDescription
        }
        a.runModal()
    }
    @objc func openHomepage() { NSWorkspace.shared.open(URL(string: "https://github.com/Lexus2016/Pull-and-Push")!) }
    @objc func reportIssue() { NSWorkspace.shared.open(URL(string: "https://github.com/Lexus2016/Pull-and-Push/issues")!) }
    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: NSAttributedString(string: L.t(
                "AI agents improve your experiment in a loop; a fixed judge scores every version.\nData: \(Env.dataDir)",
                "ШІ-агенти покращують твій експеримент у циклі; незмінний суддя оцінює кожну версію.\nДані: \(Env.dataDir)",
                "ИИ-агенты улучшают твой эксперимент в цикле; неизменный судья оценивает каждую версию.\nДанные: \(Env.dataDir)"),
                attributes: [.font: NSFont.systemFont(ofSize: 11)])])
    }

    func buildMenu() {
        let main = NSMenu()
        func menu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            let host = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            host.submenu = m
            main.addItem(host)
            return m
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            i.target = target
            return i
        }
        let t = L.t
        _ = menu("Pull-and-Push", [
            item(t("About Pull-and-Push", "Про Pull-and-Push", "О Pull-and-Push"), #selector(about), target: self),
            .separator(),
            item(t("Hide Pull-and-Push", "Сховати Pull-and-Push", "Скрыть Pull-and-Push"), #selector(NSApplication.hide(_:)), "h"),
            item(t("Hide Others", "Сховати інші", "Скрыть остальные"), #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item(t("Show All", "Показати всі", "Показать все"), #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item(t("Quit Pull-and-Push", "Вийти з Pull-and-Push", "Выйти из Pull-and-Push"), #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = menu(t("File", "Файл", "Файл"), [
            item(t("Open Data Folder", "Відкрити теку даних", "Открыть папку данных"), #selector(openData), target: self),
            item(t("Open Engine Log", "Відкрити журнал рушія", "Открыть журнал движка"), #selector(openLog), target: self),
            item(t("Install the pull-and-push Command…", "Встановити команду pull-and-push…",
                   "Установить команду pull-and-push…"), #selector(installCommand), target: self),
            .separator(),
            item(t("Close Window", "Закрити вікно", "Закрыть окно"), #selector(NSWindow.performClose(_:)), "w"),
        ])
        _ = menu(t("Edit", "Редагування", "Правка"), [
            item(t("Undo", "Скасувати", "Отменить"), Selector(("undo:")), "z"),
            item(t("Redo", "Повторити", "Повторить"), Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item(t("Cut", "Вирізати", "Вырезать"), #selector(NSText.cut(_:)), "x"),
            item(t("Copy", "Копіювати", "Копировать"), #selector(NSText.copy(_:)), "c"),
            item(t("Paste", "Вставити", "Вставить"), #selector(NSText.paste(_:)), "v"),
            item(t("Select All", "Виділити все", "Выделить всё"), #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = menu(t("View", "Вигляд", "Вид"), [
            item(t("Reload", "Оновити", "Обновить"), #selector(reload), "r", target: self),
            .separator(),
            item(t("Actual Size", "Реальний розмір", "Реальный размер"), #selector(zoomReset), "0", target: self),
            item(t("Zoom In", "Збільшити", "Увеличить"), #selector(zoomIn), "=", target: self),
            item(t("Zoom Out", "Зменшити", "Уменьшить"), #selector(zoomOut), "-", target: self),
            .separator(),
            item(t("Enter Full Screen", "На весь екран", "На весь экран"), #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        let win = menu(t("Window", "Вікно", "Окно"), [
            item(t("Minimize", "Згорнути", "Свернуть"), #selector(NSWindow.performMiniaturize(_:)), "m"),
            item(t("Zoom", "Масштабувати", "Масштабировать"), #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Pull-and-Push", #selector(showWindow), "1", target: self),
        ])
        let help = menu(t("Help", "Довідка", "Справка"), [
            item(t("Pull-and-Push on GitHub", "Pull-and-Push на GitHub", "Pull-and-Push на GitHub"), #selector(openHomepage), target: self),
            item(t("Report a Problem", "Повідомити про проблему", "Сообщить о проблеме"), #selector(reportIssue), target: self),
        ])
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
        NSApp.helpMenu = help
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
