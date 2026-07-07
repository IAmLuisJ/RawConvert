# Building & shipping the RawConvert.app (Tier 2)

The zip release (`make_release.sh`) works today. This document is the
upgrade path to a **signed, notarized, self-contained .app** — the true
download-and-double-click experience. It requires an
[Apple Developer Program](https://developer.apple.com/programs/) membership
($99/year).

## One-time setup

1. Enroll in the Apple Developer Program.
2. In Xcode (or developer.apple.com), create a **Developer ID Application**
   certificate and install it in your keychain. Find its exact name:
   `security find-identity -v -p codesigning`
3. Create an App Store Connect **API key** for notarization and store it:
   `xcrun notarytool store-credentials rawconvert-notary --key <key.p8>
   --key-id <ID> --issuer <issuer-uuid>`

## Optional: bundle exiftool

exiftool is redistributable (Perl Artistic License). Download the macOS
package from https://exiftool.org, place the standalone folder at
`packaging/vendor/exiftool`, and `make_app.sh` bundles it automatically
(the app sets `RAWCONVERT_EXIFTOOL` at startup). Add an attribution note to
an `ACKNOWLEDGMENTS` file in the app resources.

**Adobe DNG Converter must never be bundled** — Adobe's license forbids
redistribution. The app's checkup screen already sends users to Adobe's
free download.

## Each release

```sh
packaging/make_app.sh --sign "Developer ID Application: Luis Juarez (TEAMID)"
xcrun notarytool submit dist/RawConvert-<version>-app.zip \
    --keychain-profile rawconvert-notary --wait     # ~2-10 minutes
xcrun stapler staple dist/RawConvert.app
ditto -c -k --keepParent dist/RawConvert.app dist/RawConvert-<version>-app.zip
gh release upload v<version> dist/RawConvert-<version>-app.zip
```

## How the app works internally

- PyInstaller bundles a Python runtime; `rawconvert_gui.py` detects frozen
  mode (`sys.frozen`) and loads `gui/index.html` from the bundle resources.
- Conversion jobs normally run `python3 rawconvert.py …` as a subprocess;
  in the app there is no python3, so the app **re-executes itself** with
  `RAWCONVERT_RUN_CLI=1`, which dispatches straight into `rawconvert.main()`
  (see the top of `rawconvert_gui.main`). Covered by tests in `test_gui.py`.
- The GUI's "Quit RawConvert" control calls `POST /api/quit` since a
  windowed app has no Terminal window to close.

## Later, optional

- GitHub Actions macOS runner with the certificate + notary key in repo
  secrets, so `git tag && git push --tags` produces a notarized release.
- A DMG with a drag-to-Applications background, via `hdiutil` or
  `create-dmg`, if you outgrow the zip.
