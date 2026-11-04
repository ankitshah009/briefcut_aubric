"""Full-page screenshots via Chrome DevTools, with prefers-color-scheme emulation.

usage: shoot.py <out.png> <url> <width> <light|dark> [click-selector] [js-after]
"""
import base64, json, subprocess, sys, time, urllib.request, socket, os, tempfile, shutil
import websocket  # websocket-client

out, url, width, scheme = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
click = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else ""
js_after = sys.argv[6] if len(sys.argv) > 6 else ""
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
profile = tempfile.mkdtemp(prefix="shoot-")
chrome = subprocess.Popen(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", f"--window-size={width},1200", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json")); break
        except Exception:
            time.sleep(0.1)
    page = next(t for t in targets if t["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
    n = 0
    def send(method, **params):
        global n
        n += 1; ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == n:
                return msg.get("result", {})
    send("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
    send("Emulation.setDeviceMetricsOverride", width=width, height=1200, deviceScaleFactor=1, mobile=width < 500)
    send("Page.enable"); send("Runtime.enable")
    send("Page.navigate", url=url)
    time.sleep(4)
    if click:
        send("Runtime.evaluate", expression=f"document.querySelector({json.dumps(click)}).click()")
        time.sleep(1)
    if js_after:
        send("Runtime.evaluate", expression=js_after, awaitPromise=True)
        time.sleep(1)
    height = int(send("Runtime.evaluate", expression="document.documentElement.scrollHeight", returnByValue=True)["result"]["value"])
    send("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=width < 500)
    time.sleep(0.5)
    data = send("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"]
    open(out, "wb").write(base64.b64decode(data))
    errors = send("Runtime.evaluate", expression="JSON.stringify({err: document.getElementById('error').hidden ? '' : document.getElementById('error-text').textContent, live: document.getElementById('live').textContent})", returnByValue=True)["result"]["value"]
    print(out, f"{width}x{height}", scheme, errors)
finally:
    chrome.kill(); shutil.rmtree(profile, ignore_errors=True)
