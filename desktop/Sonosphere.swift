// Sonosphere.app: a native window around the local Sonosphere server
// (http://127.0.0.1:8888, run at login by the com.jimmy.sonosphere LaunchAgent).
import AppKit
import WebKit

let serverURL = URL(string: "http://127.0.0.1:8888/")!
let agentLabel = "com.jimmy.sonosphere"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var tries = 0

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.websiteDataStore = .default()
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.isHidden = true

        status = NSTextField(labelWithString: "Starting Sonosphere…")
        status.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 15)
        status.alignment = .center

        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor(red: 0.05, green: 0.05, blue: 0.10, alpha: 1).cgColor
        for v in [web!, status!] as [NSView] { v.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(v) }
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor), web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            web.topAnchor.constraint(equalTo: root.topAnchor), web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            status.centerXAnchor.constraint(equalTo: root.centerXAnchor), status.centerYAnchor.constraint(equalTo: root.centerYAnchor),
        ])

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Sonosphere"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 1200, height: 700)
        window.contentView = root
        window.center()
        window.setFrameAutosaveName("SonosphereMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        waitForServer()
    }

    func waitForServer() {
        var req = URLRequest(url: serverURL.appendingPathComponent("api/skins"))
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 {
                    self.web.load(URLRequest(url: serverURL))
                    return
                }
                self.tries += 1
                if self.tries == 1 { self.kickAgent() }
                if self.tries > 40 {
                    self.status.stringValue = "The Sonosphere server didn't start.\nLog: ~/Library/Logs/Sonosphere.log"
                    return
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitForServer() }
            }
        }.resume()
    }

    func kickAgent() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/\(agentLabel)"]
        try? p.run()
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) { status.isHidden = true; web.isHidden = false }
    func webView(_ w: WKWebView, didFail nav: WKNavigation!, withError e: Error) { retryLater() }
    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError e: Error) { retryLater() }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { w.reload() }
    func retryLater() {
        status.stringValue = "Reconnecting…"; status.isHidden = false; web.isHidden = true
        tries = 0
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.waitForServer() }
    }

    // Links that open a new window (Apple Music pages, sign-in) go to the default browser.
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url { NSWorkspace.shared.open(u) }
        return nil
    }
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler h: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = a.request.url, a.navigationType == .linkActivated, u.host != serverURL.host {
            NSWorkspace.shared.open(u); h(.cancel); return
        }
        h(.allow)
    }
    // alert()/confirm() from the page
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.runModal(); completionHandler()
    }
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    @objc func reload() { web.reload() }

    func buildMenu() {
        let main = NSMenu()
        func item(_ t: String, _ a: Selector?, _ k: String, _ m: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = m; return i
        }
        let appMenu = NSMenu()
        appMenu.addItem(item("Hide Sonosphere", #selector(NSApplication.hide(_:)), "h"))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Quit Sonosphere", #selector(NSApplication.terminate(_:)), "q"))
        let edit = NSMenu(title: "Edit")
        edit.addItem(item("Undo", Selector(("undo:")), "z"))
        edit.addItem(item("Redo", Selector(("redo:")), "z", [.command, .shift]))
        edit.addItem(.separator())
        edit.addItem(item("Cut", #selector(NSText.cut(_:)), "x"))
        edit.addItem(item("Copy", #selector(NSText.copy(_:)), "c"))
        edit.addItem(item("Paste", #selector(NSText.paste(_:)), "v"))
        edit.addItem(item("Select All", #selector(NSText.selectAll(_:)), "a"))
        let view = NSMenu(title: "View")
        let r = item("Reload", #selector(reload), "r"); r.target = self; view.addItem(r)
        view.addItem(item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]))
        let win = NSMenu(title: "Window")
        win.addItem(item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"))
        win.addItem(item("Close", #selector(NSWindow.performClose(_:)), "w"))
        for (t, m) in [("Sonosphere", appMenu), ("Edit", edit), ("View", view), ("Window", win)] {
            let top = NSMenuItem(title: t, action: nil, keyEquivalent: ""); top.submenu = m; main.addItem(top)
        }
        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
