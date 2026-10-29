import AppKit
import Combine
import Darwin
import Foundation
import SwiftUI
import UniformTypeIdentifiers
import WebKit

// App Sandbox is off on purpose. The window is a shell around the machine's
// Python and ffmpeg-full, both of which live outside this bundle. Sandboxing
// the process would hide them. ATS stays on; only local networking is allowed,
// via Info.plist NSAllowsLocalNetworking.
//
// Deployment target is macOS 15. WKWebView hosts the studio. The native
// SwiftUI WebView needs macOS 26, so this binary stays on the older view.

@main
struct VeditMain {
    static func main() {
        let args = CommandLine.arguments.dropFirst().filter { !$0.hasPrefix("-psn_") }
        if args.isEmpty {
            WindowProof.writeLaunchMarker()
            VeditGUI.main()
        } else {
            StudioProcess.execPython(Array(args))
        }
    }
}

struct VeditGUI: App {
    @NSApplicationDelegateAdaptor(VeditAppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup("Briefcut") {
            EditorScreen()
        }
        .defaultSize(width: 1100, height: 780)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

@MainActor
final class VeditAppDelegate: NSObject, NSApplicationDelegate {
    static weak var engine: StudioEngine?

    func applicationWillTerminate(_ notification: Notification) {
        VeditAppDelegate.engine?.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@MainActor
struct EditorScreen: View {
    @StateObject private var engine = StudioEngine()

    var body: some View {
        Group {
            if let url = engine.pageURL {
                StudioWebView(url: url)
            } else {
                Text(engine.statusLine)
                    .font(.title3)
                    .multilineTextAlignment(.leading)
                    .frame(maxWidth: 520, alignment: .leading)
                    .padding(28)
            }
        }
        .frame(minWidth: 720, minHeight: 520)
        .task { await engine.start() }
    }
}

@MainActor
final class StudioEngine: ObservableObject {
    @Published var pageURL: URL?
    @Published var statusLine = "Opening the editor."
    private var child: Process?

    func start() async {
        VeditAppDelegate.engine = self
        guard child?.isRunning != true else { return }
        guard !Task.isCancelled else { return }
        guard let python = StudioProcess.findPython() else {
            statusLine = "Python 3.13 or newer is required. Install it, or set VEDIT_PYTHON to a supported interpreter, then open Briefcut again."
            return
        }
        let port: UInt16
        do {
            port = try StudioProcess.freePort()
        } catch {
            statusLine = "The editor could not pick a local port."
            return
        }
        WindowProof.notePort(port)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [
            "-m", "vedit", "studio",
            "--host", "127.0.0.1",
            "--port", String(port),
        ]
        process.environment = StudioProcess.environment()
        do {
            process.currentDirectoryURL = try StudioProcess.workspace()
        } catch {
            statusLine = "Briefcut could not open its project folder. Check VEDIT_WORKSPACE or access to Application Support."
            return
        }
        let logURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("vedit-studio-\(port).log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        guard let log = try? FileHandle(forWritingTo: logURL) else {
            statusLine = "The editor could not record the engine log."
            return
        }
        process.standardOutput = log
        process.standardError = log
        do {
            try process.run()
        } catch {
            statusLine = "The editor engine did not start."
            return
        }
        child = process
        let health = URL(string: "http://127.0.0.1:\(port)/api/health")!
        let deadline = Date().addingTimeInterval(25)
        while Date() < deadline {
            if Task.isCancelled {
                stop()
                return
            }
            if !process.isRunning {
                statusLine = StudioProcess.failureText(at: logURL)
                return
            }
            if await StudioProcess.healthOK(health) {
                pageURL = URL(string: "http://127.0.0.1:\(port)/")
                statusLine = "Editor ready."
                return
            }
            try? await Task.sleep(for: .milliseconds(200))
        }
        if pageURL == nil {
            statusLine = StudioProcess.failureText(at: logURL)
            stop()
        }
    }

    func stop() {
        guard let child, child.isRunning else { return }
        child.terminate()
    }
}

enum StudioProcess {
    static func findPython() -> String? {
        if let override = ProcessInfo.processInfo.environment["VEDIT_PYTHON"], !override.isEmpty {
            return pythonVersion(at: override) == nil ? nil : override
        }
        let names = ["python3.14", "python3.13", "python3"]
        let pathEnv = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin"
        let directories = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
            + pathEnv.split(separator: ":").map(String.init)
        var checked: Set<String> = []
        var candidates: [(String, [Int])] = []
        for directory in directories {
            for name in names {
                let path = URL(fileURLWithPath: directory).appendingPathComponent(name).resolvingSymlinksInPath().path
                if checked.insert(path).inserted, let version = pythonVersion(at: path) {
                    candidates.append((path, version))
                }
            }
        }
        return candidates.max { $0.1.lexicographicallyPrecedes($1.1) }?.0
    }

    private static func pythonVersion(at path: String) -> [Int]? {
        guard path.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: path) else { return nil }
        let process = Process()
        let output = Pipe()
        let finished = DispatchSemaphore(value: 0)
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = ["-I", "-S", "-c", "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}'); sys.exit(0 if v >= (3,13) and v.releaselevel == 'final' else 1)"]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { _ in finished.signal() }
        do { try process.run() } catch { return nil }
        if finished.wait(timeout: .now() + 2) == .timedOut {
            process.terminate()
            return nil
        }
        guard process.terminationStatus == 0 else { return nil }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        let version = text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: ".").compactMap { Int($0) }
        return version.count == 3 ? version : nil
    }

    static func workspace() throws -> URL {
        let directory: URL
        if let override = ProcessInfo.processInfo.environment["VEDIT_WORKSPACE"], !override.isEmpty {
            guard override.hasPrefix("/") else { throw POSIXError(.EINVAL) }
            directory = URL(fileURLWithPath: override, isDirectory: true)
        } else {
            directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                    appropriateFor: nil, create: true)
                .appendingPathComponent("Vedit", isDirectory: true)
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        guard FileManager.default.isWritableFile(atPath: directory.path) else { throw POSIXError(.EACCES) }
        return directory
    }

    static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let resources = Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let src = resources.appendingPathComponent("src").path
        let existing = env["PYTHONPATH"] ?? ""
        env["PYTHONPATH"] = existing.isEmpty ? src : src + ":" + existing
        env["PYTHONUNBUFFERED"] = "1"
        let ffmpeg = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
        let ffprobe = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
        if (env["VEDIT_FFMPEG"] ?? "").isEmpty, FileManager.default.isExecutableFile(atPath: ffmpeg) {
            env["VEDIT_FFMPEG"] = ffmpeg
        }
        if (env["VEDIT_FFPROBE"] ?? "").isEmpty, FileManager.default.isExecutableFile(atPath: ffprobe) {
            env["VEDIT_FFPROBE"] = ffprobe
        }
        var path = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        for extra in ["/opt/homebrew/bin", "/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/bin"] where !path.contains(extra) {
            path = extra + ":" + path
        }
        env["PATH"] = path
        return env
    }

    static func freePort() throws -> UInt16 {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        if sock < 0 {
            throw POSIXError(.EADDRNOTAVAIL)
        }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { pointer in
                bind(sock, pointer, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if bound != 0 {
            throw POSIXError(.EADDRNOTAVAIL)
        }
        var resolved = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &resolved) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { pointer in
                getsockname(sock, pointer, &length)
            }
        }
        if named != 0 {
            throw POSIXError(.EADDRNOTAVAIL)
        }
        return UInt16(bigEndian: resolved.sin_port)
    }

    static func healthOK(_ url: URL) async -> Bool {
        var request = URLRequest(url: url, timeoutInterval: 1)
        request.httpMethod = "GET"
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                return false
            }
            guard let text = String(data: data, encoding: .utf8) else { return false }
            return text.contains("\"ok\"")
        } catch {
            return false
        }
    }

    static func failureText(at logURL: URL) -> String {
        let raw = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        let line = raw.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
        if line.isEmpty {
            return "The editor engine stopped before the window could open."
        }
        return line
    }

    static func execPython(_ args: [String]) -> Never {
        guard let python = findPython() else {
            fputs("Python 3.13 or newer is required. Check VEDIT_PYTHON or install a supported interpreter.\n", stderr)
            exit(1)
        }
        for (key, value) in environment() {
            setenv(key, value, 1)
        }
        let argv = [python, "-m", "vedit"] + args
        let cStrings: [UnsafeMutablePointer<CChar>] = argv.map { strdup($0) }
        var pointers: [UnsafeMutablePointer<CChar>?] = cStrings.map { Optional($0) }
        pointers.append(nil)
        pointers.withUnsafeMutableBufferPointer { buffer in
            _ = execv(python, buffer.baseAddress)
        }
        for pointer in cStrings {
            free(pointer)
        }
        fputs("Could not start Python.\n", stderr)
        exit(1)
    }
}

struct StudioWebView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator {
        Coordinator(origin: url)
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        if ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1" {
            configuration.userContentController.add(context.coordinator, name: "veditState")
        }
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
        private let origin: URL
        private var downloads: [ObjectIdentifier: (download: WKDownload, temporary: URL, destination: URL)] = [:]

        init(origin: URL) { self.origin = origin }

        private func isStudioURL(_ url: URL?) -> Bool {
            guard let url else { return false }
            return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
        }

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            if isStudioURL(navigationAction.request.url) {
                decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow)
            } else {
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            guard isStudioURL(navigationResponse.response.url) else {
                decisionHandler(.cancel)
                return
            }
            let response = navigationResponse.response as? HTTPURLResponse
            let attachment = response?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased().hasPrefix("attachment") == true
            decisionHandler(attachment || !navigationResponse.canShowMIMEType ? .download : .allow)
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
            guard isStudioURL(response.url), (response as? HTTPURLResponse)?.statusCode == 200 else {
                completionHandler(nil)
                showDownloadError("The file is no longer available. Return to the project and try the download again.")
                return
            }
            let name = URL(fileURLWithPath: suggestedFilename).lastPathComponent
            let panel = NSSavePanel()
            panel.nameFieldStringValue = name.isEmpty ? "campaign.zip" : name
            panel.canCreateDirectories = true
            panel.title = "Save your campaign file"
            panel.prompt = "Save"
            if let proofDestination = WindowProof.downloadDestination(filename: panel.nameFieldStringValue) {
                startDownload(download, to: proofDestination, completionHandler: completionHandler)
                return
            }
            panel.begin { [weak self] response in
                guard let self, response == .OK, let destination = panel.url else {
                    completionHandler(nil)
                    return
                }
                self.startDownload(download, to: destination, completionHandler: completionHandler)
            }
        }

        private func startDownload(_ download: WKDownload, to destination: URL,
                                   completionHandler: @escaping (URL?) -> Void) {
            // WebKit requires a nonexistent file. Save beside the user's
            // destination and replace atomically only after a complete download.
            let temporary = destination.deletingLastPathComponent()
                .appendingPathComponent(".vedit-download-\(UUID().uuidString).part")
            downloads[ObjectIdentifier(download)] = (download, temporary, destination)
            completionHandler(temporary)
        }

        func downloadDidFinish(_ download: WKDownload) {
            guard let target = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
            do {
                if FileManager.default.fileExists(atPath: target.destination.path) {
                    _ = try FileManager.default.replaceItemAt(target.destination, withItemAt: target.temporary)
                } else {
                    try FileManager.default.moveItem(at: target.temporary, to: target.destination)
                }
                if !WindowProof.noteDownload(path: target.destination, error: nil) {
                    NSWorkspace.shared.activateFileViewerSelecting([target.destination])
                }
            } catch {
                try? FileManager.default.removeItem(at: target.temporary)
                showDownloadError("The download finished, but Briefcut could not save it. Choose another folder and try again.")
            }
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            if let target = downloads.removeValue(forKey: ObjectIdentifier(download)) {
                try? FileManager.default.removeItem(at: target.temporary)
            }
            // A cancelled save panel is intentional; no failure dialog needed.
            if (error as NSError).code != NSURLErrorCancelled {
                showDownloadError("The download was interrupted. Keep Briefcut open and try the download again.")
            }
        }

        private func showDownloadError(_ message: String) {
            if WindowProof.noteDownload(path: nil, error: message) { return }
            let alert = NSAlert()
            alert.messageText = "File could not be saved"
            alert.informativeText = message
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            WindowProof.begin(in: webView)
        }

        func webView(
            _ webView: WKWebView,
            runOpenPanelWith parameters: WKOpenPanelParameters,
            initiatedByFrame frame: WKFrameInfo,
            completionHandler: @escaping ([URL]?) -> Void
        ) {
            let panel = NSOpenPanel()
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.allowedContentTypes = [.movie, .video, .mpeg4Movie, .quickTimeMovie, .audio]
            panel.begin { response in
                completionHandler(response == .OK ? panel.urls : nil)
            }
        }

        /// The page pushes editor state here. Calling into a playing movie from Swift can stall.
        func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == "veditState" else { return }
            var state: [String: String] = [:]
            if let dict = message.body as? [String: String] {
                state = dict
            } else if let dict = message.body as? [String: Any] {
                for (key, value) in dict {
                    state[key] = value as? String ?? String(describing: value)
                }
            } else if let dict = message.body as? NSDictionary {
                for (key, value) in dict {
                    guard let name = key as? String else { continue }
                    state[name] = value as? String ?? String(describing: value)
                }
            }
            WindowProof.notePushed(state)
        }
    }
}

// Window proof runs only when VEDIT_WINDOW_PROOF=1. A normal open does not
// set that variable, so the shipped app never drives itself.
enum WindowProof {
    @MainActor private static var started = false
    @MainActor private static var pushed: [String: String] = [:]

    @MainActor
    static func notePushed(_ state: [String: String]) {
        pushed = state
        writeStateFile(state)
    }

    @MainActor private static var downloadedPath: URL?
    @MainActor private static var downloadError: String?

    @MainActor
    static func downloadDestination(filename: String) -> URL? {
        guard ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1",
              let directory = outputDirectory() else { return nil }
        return directory.appendingPathComponent(filename)
    }

    @MainActor
    static func noteDownload(path: URL?, error: String?) -> Bool {
        guard ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1" else { return false }
        downloadedPath = path
        downloadError = error
        if let directory = outputDirectory() {
            let record = ["path": path?.path ?? "", "error": error ?? ""]
            if let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) {
                try? data.write(to: directory.appendingPathComponent("download.json"), options: .atomic)
            }
        }
        return true
    }

    private static let stateScript = """
    const text = id => document.getElementById(id)?.textContent || "";
    const player = document.getElementById("player");
    return {
      ready: text("connection-status") === "Editor ready" ? "1" : "",
      status: text("project-status"),
      result: text("result"),
      clip: text("clip-name"),
      duration: text("proof-duration"),
      width: text("proof-width"),
      height: text("proof-height"),
      audio: text("proof-has_audio"),
      video: text("proof-has_video"),
      color: text("proof-color"),
      canRender: document.getElementById("make")?.disabled ? "no" : "yes",
      delivered: document.getElementById("delivery-panel")?.hidden ? "no" : "yes",
      bundle: document.getElementById("download-bundle")?.getAttribute("href") || "",
      effect: document.getElementById("effect-preset")?.value || "none",
      currentTime: String(player?.currentTime || 0),
      readyState: String(player?.readyState || 0),
      error: text("error-text")
    };
    """

    private static let attachScript = """
    const binary = atob(bytes);
    const raw = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) raw[i] = binary.charCodeAt(i);
    const file = new File([raw], name, { type: "video/mp4" });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const input = document.getElementById("clip");
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return input.files.length ? input.files[0].name : "";
    """

    private static let sentenceScript = """
    const goal = document.getElementById("goal");
    goal.value = sentence;
    goal.dispatchEvent(new Event("input", { bubbles: true }));
    return goal.value;
    """

    private static let makeScript = """
    const button = document.getElementById("make");
    button.click();
    return button.disabled ? "busy" : "clicked";
    """

    nonisolated static func writeLaunchMarker() {
        guard ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1" else { return }
        guard let dir = outputDirectory() else { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let text = "vedit-window-proof\npid \(ProcessInfo.processInfo.processIdentifier)\n"
        try? Data(text.utf8).write(to: dir.appendingPathComponent("launch-marker.txt"))
    }

    nonisolated static func notePort(_ port: UInt16) {
        guard ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1" else { return }
        guard let dir = outputDirectory() else { return }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? Data("\(port)\n".utf8).write(to: dir.appendingPathComponent("port.txt"))
    }

    @MainActor
    static func begin(in webView: WKWebView) {
        guard !started else { return }
        guard ProcessInfo.processInfo.environment["VEDIT_WINDOW_PROOF"] == "1" else { return }
        guard clipURL() != nil, outputDirectory() != nil else { return }
        started = true
        Task { await run(in: webView) }
    }

    @MainActor
    private static func run(in webView: WKWebView) async {
        var record: [String: Any] = ["ok": false]
        do {
            await present(webView)
            let loaded = try await wait(webView, seconds: 25, label: "campaign studio") { $0["ready"] == "1" }
            record["loaded"] = loaded
            try await snapshot(webView, name: "loaded.png")
            let clipName = try await attachClip(webView)
            record["clip"] = clipName
            try await setSentence(webView, "Make a concise landscape story from this footage.")
            _ = try await call(webView, """
                document.getElementById("provider-local").checked = true;
                document.getElementById("captions").checked = false;
                document.getElementById("aspect").value = "16:9";
                if (!document.getElementById("effects-section")?.hidden) {
                    const preset = document.getElementById("effect-preset");
                    preset.value = "float";
                    preset.dispatchEvent(new Event("change", { bubbles: true }));
                    document.getElementById("effect-duration").value = "1.5";
                }
                document.getElementById("plan").click();
                """, [:])
            let planned = try await wait(webView, seconds: 45, label: "editable campaign story") {
                $0["canRender"] == "yes" && $0["status"] == "Ready to edit"
            }
            record["planned"] = planned
            _ = try await call(webView, makeScript, [:])
            let rendered = try await wait(webView, seconds: 120, label: "verified campaign") {
                $0["status"] == "Verified" && $0["delivered"] == "yes"
                    && $0["width"] == "1920" && $0["height"] == "1080"
                    && !($0["bundle"] ?? "").isEmpty
            }
            record["rendered"] = rendered
            // Push playback evidence from WebKit rather than evaluating
            // JavaScript against a movie that is already playing.
            _ = try await call(webView, """
                const player = document.getElementById("player");
                const report = () => window.webkit.messageHandlers.veditState.postMessage({
                    currentTime: String(player.currentTime),
                    readyState: String(player.readyState),
                    playError: player.error ? player.error.message : ""
                });
                player.addEventListener("timeupdate", report);
                player.addEventListener("error", report);
                player.play().catch(error => window.webkit.messageHandlers.veditState.postMessage({playError: error.message}));
                return "play requested";
                """, [:])
            let playback = try await waitPushed(seconds: 15, label: "native video playback") {
                (Double($0["currentTime"] ?? "") ?? 0) > 0.1
            }
            record["playback"] = playback
            await webView.pauseAllMediaPlayback()
            _ = try await call(webView, "document.getElementById('download-bundle').click();", [:])
            let deadline = Date().addingTimeInterval(20)
            while downloadedPath == nil && downloadError == nil && Date() < deadline {
                try await Task.sleep(for: .milliseconds(100))
            }
            if let downloadError { throw ProofFailure(downloadError) }
            guard let path = downloadedPath,
                  let attrs = try? FileManager.default.attributesOfItem(atPath: path.path),
                  let bytes = attrs[.size] as? NSNumber, bytes.intValue > 0 else {
                throw ProofFailure("campaign download did not produce a saved file")
            }
            record["download"] = ["path": path.path, "bytes": bytes.intValue]
            record["ok"] = true
        } catch {
            record["error"] = String(describing: error)
        }
        if let window = webView.window {
            record["window"] = ["width": String(Int(window.frame.width)), "height": String(Int(window.frame.height))]
        }
        writeRecord(record)
        armExit(after: 8)
        await webView.pauseAllMediaPlayback()
        try? await snapshot(webView, name: (record["ok"] as? Bool) == true ? "result.png" : "failed.png")
        NSApp.terminate(nil)
    }

    private static func armExit(after seconds: Double) {
        Task.detached {
            try? await Task.sleep(for: .seconds(seconds))
            exit(0)
        }
    }

    @MainActor
    private static func present(_ webView: WKWebView) async {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if let window = webView.window {
                window.setContentSize(NSSize(width: 1100, height: 760))
                window.makeKeyAndOrderFront(nil)
                NSApp.activate()
                webView.layoutSubtreeIfNeeded()
                try? await Task.sleep(for: .milliseconds(250))
                return
            }
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    @MainActor
    private static func attachClip(_ webView: WKWebView) async throws -> String {
        guard let url = clipURL() else { throw ProofFailure("VEDIT_WINDOW_CLIP is missing") }
        let data = try Data(contentsOf: url)
        let name = url.lastPathComponent
        let value = try await call(webView, attachScript, [
            "bytes": data.base64EncodedString(),
            "name": name,
        ])
        let attached = value as? String ?? ""
        if attached != name {
            throw ProofFailure("clip did not attach: \(attached)")
        }
        return name
    }

    @MainActor
    private static func setSentence(_ webView: WKWebView, _ sentence: String) async throws {
        _ = try await call(webView, sentenceScript, ["sentence": sentence])
    }

    @MainActor
    private static func wait(
        _ webView: WKWebView,
        seconds timeout: Double,
        label: String,
        where predicate: ([String: String]) -> Bool
    ) async throws -> [String: String] {
        let deadline = Date().addingTimeInterval(timeout)
        var last: [String: String] = [:]
        while Date() < deadline {
            last = try await readState(webView)
            if let data = try? JSONSerialization.data(withJSONObject: last, options: [.sortedKeys]) {
                try? data.write(to: outputDirectory()?.appendingPathComponent("state.json") ?? URL(fileURLWithPath: "/dev/null"))
            }
            if predicate(last) { return last }
            try await Task.sleep(for: .milliseconds(300))
        }
        throw ProofFailure("timed out waiting for \(label): \(last)")
    }

    /// Render proof reads the page's own posts. Injected script against a live movie does not return.
    @MainActor
    private static func waitPushed(
        seconds timeout: Double,
        label: String,
        where predicate: ([String: String]) -> Bool
    ) async throws -> [String: String] {
        let deadline = Date().addingTimeInterval(timeout)
        var last = pushed
        while Date() < deadline {
            last = pushed
            writeStateFile(last)
            if let err = last["playError"], !err.isEmpty {
                throw ProofFailure("playback failed: \(err) \(last)")
            }
            if predicate(last) { return last }
            try await Task.sleep(for: .milliseconds(200))
        }
        throw ProofFailure("timed out waiting for \(label): \(last)")
    }

    @MainActor
    private static func writeStateFile(_ state: [String: String]) {
        guard let dir = outputDirectory() else { return }
        guard JSONSerialization.isValidJSONObject(state),
              let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]) else {
            return
        }
        try? data.write(to: dir.appendingPathComponent("state.json"), options: .atomic)
    }

    @MainActor
    private static func readState(_ webView: WKWebView) async throws -> [String: String] {
        let value = try await call(webView, stateScript, [:])
        guard let dict = value as? NSDictionary else {
            throw ProofFailure("editor state was not an object")
        }
        var state: [String: String] = [:]
        for (key, item) in dict {
            guard let name = key as? String else { continue }
            state[name] = String(describing: item)
        }
        return state
    }

    @MainActor
    private static func call(_ webView: WKWebView, _ body: String, _ arguments: [String: Any]) async throws -> Any? {
        try await webView.callAsyncJavaScript(body, arguments: arguments, in: nil, contentWorld: .page)
    }

    @MainActor
    private static func snapshot(_ webView: WKWebView, name: String) async throws {
        guard let dir = outputDirectory() else { return }
        webView.layoutSubtreeIfNeeded()
        let image = try await webView.takeSnapshot(configuration: nil)
        guard let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else {
            throw ProofFailure("snapshot \(name) was empty")
        }
        try png.write(to: dir.appendingPathComponent(name), options: .atomic)
    }

    @MainActor
    private static func writeRecord(_ record: [String: Any]) {
        guard let dir = outputDirectory() else { return }
        guard JSONSerialization.isValidJSONObject(record),
              let data = try? JSONSerialization.data(withJSONObject: record, options: [.prettyPrinted, .sortedKeys]) else {
            return
        }
        try? data.write(to: dir.appendingPathComponent("proof.json"), options: .atomic)
    }

    nonisolated private static func outputDirectory() -> URL? {
        guard let raw = ProcessInfo.processInfo.environment["VEDIT_WINDOW_OUT"], !raw.isEmpty else { return nil }
        return URL(fileURLWithPath: raw, isDirectory: true)
    }

    nonisolated private static func clipURL() -> URL? {
        guard let raw = ProcessInfo.processInfo.environment["VEDIT_WINDOW_CLIP"], !raw.isEmpty else { return nil }
        return URL(fileURLWithPath: raw)
    }

}

private struct ProofFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}
