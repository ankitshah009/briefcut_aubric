# Briefcut DMG release check

Date: 2026-11-04, 13:46 to 13:48 PST.

This is the proof that the Mac app disk image builds from the current tree, verifies, and launches.

## The build

- Version: Briefcut 1.2.0, bundle ID `ai.aubric.briefcut`.
- Source: commit `225b34e` on `briefcut-demo-polish`. `src/vedit` had no changes against that commit. The `src/vedit` copy inside the app is identical to the commit's `src/vedit`.
- Build host: Apple M5 Max, macOS 27.0.1, Swift 6.4.2, uv 0.13.5.
- Command: `uv run vedit package dmg --out <scratch>/Briefcut.dmg --app-dir <scratch>/Briefcut.app`. The app was staged outside the repo, so `dist/` was not touched.
- Build time: 12 seconds. The first build of the session took 37 seconds, with a cold Swift module cache.
- DMG size: 29,462,064 bytes (28 MB). The app is 56 MB unpacked: 51 MB of Python, 3.6 MB of Briefcut source.
- SHA-256: `788c24d0cea34610af0d935874392fbea03b467a7b2f70a5b7ff22c581a39b6d`
- The DMG is kept outside the repo, at `~/Movies/Briefcut-demo/release/Briefcut.dmg` on the build machine.

An earlier build from 13:36 is superseded. Commit `225b34e` landed after it and changed three CSS files, so the DMG was rebuilt.

## Disk image checks

- `hdiutil verify`: the checksum is VALID. The format is UDZO (zlib compressed, read only).
- `hdiutil attach -nobrowse -readonly` mounted it. It holds `Briefcut.app`, an `Applications` link and `README.txt`.
- Info.plist passes `plutil -lint`. The version is 1.2.0. `LSMinimumSystemVersion` is 15.0.
- The launcher `Contents/MacOS/vedit` is a thin arm64 Mach-O, built for macOS 15.0 with the 27.2 SDK.
- The bundled Python runs from the mounted image: CPython 3.13.16 with OpenSSL 3.5.9. With only the bundle on `PYTHONPATH`, it imports `vedit` 1.2.0, `vedit.studio` and PyYAML 6.0.3. The interpreter itself targets macOS 11.0.
- `codesign --verify --deep --strict -v`: valid on disk, and it satisfies its Designated Requirement. The signature is ad hoc, with no team ID. It seals 930 resource files.
- `spctl -a -vv`: rejected, for both the app and the DMG ("no usable signature"). This is expected for an ad hoc build that is not notarized.
- The image detached cleanly.

## Launch proof

The app was copied out of the DMG with `ditto` into a scratch folder, not /Applications. The copy still passed `codesign --verify --deep --strict`.

It was started by running `Contents/MacOS/vedit` directly with these overrides:

- `VEDIT_WORKSPACE` pointed at an empty scratch folder.
- `VEDIT_HOME` pointed at an empty scratch folder, so the real settings file was not read.
- `VEDIT_NO_KEYCHAIN=1`, so the Keychain was not read.

What happened:

- The Studio engine started from the bundled Python: `.../Briefcut.app/Contents/Resources/python/bin/python3.13 -m vedit studio --host 127.0.0.1 --port 62719`. The app picks a free port each time.
- The engine's working folder was the scratch workspace. The workspace override works.
- `GET /api/health` returned `{"ok": true, "product": "vedit", "version": "1.2.0"}`.
- `GET /api/capabilities` returned HTTP 200 (14,414 bytes). `render.ready` was true. `host.native_app` was true. `host.hardware_encoder` was true. `meta.configured` was false, because the settings folder was empty on purpose.
- The engine log showed the window load the page, every stylesheet and script, and the API calls. All 26 requests returned 200.
- The window was on screen at 1100 by 760 points.
- The app quit cleanly through a standard Quit event (`NSRunningApplication.terminate`). The app and its Python child both exited, and the port closed.
- After the launches, nothing in `~/Library/Application Support/Vedit` was newer than the launch.

The app binary also works as the CLI. `Briefcut.app/Contents/MacOS/vedit doctor` used the bundled Python and found ffmpeg-full. It passed 21 checks. The only required failure was the missing Meta key, which was left out on purpose.

## Screenshot

`app-window.png` is the app's own window. It shows no personal information.

`screencapture` could not take it, because the terminal running this check has no Screen Recording permission. The image comes from the app's built-in proof mode (`VEDIT_WINDOW_PROOF=1`), which takes a snapshot of its own web view. It is 2200 by 1456 pixels, a Retina capture of the 1100 by 760 window.

The page is dimmed because the Setup dialog opens on first run when no Meta key is set (`settings.js:571`). The editor under it is fully drawn. The dialog card itself does not appear in the snapshot.

## REA checks

`rea-agents@6.8.0` was run on the app, read only.

- `inspect-signature`: signed, identifier `ai.aubric.briefcut`, thin arm64. No team ID, no signing authorities, no timestamp, no entitlements. Hardened runtime is off.
- `inspect-plist`: valid XML. The bundle name is Briefcut, the executable is `vedit`, the version is 1.2.0, and the minimum system is 15.0. It has no icon entry.

## Known limits

- It is not notarized. The signature is ad hoc. Gatekeeper rejects it. A copy downloaded from the web needs `xattr -dr com.apple.quarantine /Applications/Briefcut.app` once before it opens.
- It runs on Apple Silicon only. The launcher is arm64 only. It needs macOS 15 or later.
- FFmpeg is not bundled. The judge's Mac needs `brew install ffmpeg-full`. The first-run panel shows that command.
- It needs a Meta API key, added in Settings, before it can make a story.
- It has no app icon. Finder and the Dock show the generic one.
- The bundled Python keeps uv's install path in `_sysconfigdata__darwin_darwin.py`. That path holds the build machine's login name.
- The engine log is in the per-user temp folder, as `vedit-studio-<port>.log`. Nothing in the app points to it.

## Issues found in the packaging code

These were not changed here.

1. `src/vedit/macos/VeditApp.swift:558`: the proof mode waits for `#connection-status` to read "Editor ready". `src/vedit/studio/static/studio.js:227` only ever writes "Meta connected", "Setup needed" or "Not connected". So proof mode can never pass its first step. It times out after 25 seconds (`VeditApp.swift:634`).
2. `src/vedit/packaging.py:208`: the app is signed ad hoc, without `--options runtime` or a timestamp. Notarization would need a Developer ID, the hardened runtime, and probably entitlements for the bundled Python.
3. `src/vedit/packaging.py:227`: macOS 27 marks `hdiutil create` as deprecated and prints a warning. The build still works.
4. `src/vedit/packaging.py:101`: the Info.plist template has no `CFBundleIconFile`, and no `.icns` file is staged.
5. `src/vedit/packaging.py:160`: the Python copy includes uv's rewritten `_sysconfigdata`, which records the build user's home path.

## Reproduce the launch check

```sh
hdiutil attach -nobrowse -readonly -mountpoint /tmp/bc-mnt Briefcut.dmg
ditto /tmp/bc-mnt/Briefcut.app /tmp/bc-run/Briefcut.app
hdiutil detach /tmp/bc-mnt
VEDIT_WORKSPACE=/tmp/bc-ws VEDIT_HOME=/tmp/bc-home VEDIT_NO_KEYCHAIN=1 \
  /tmp/bc-run/Briefcut.app/Contents/MacOS/vedit &
lsof -nP -iTCP -sTCP:LISTEN | grep python3   # find the port
curl -s http://127.0.0.1:<port>/api/capabilities
```
