# HUMINT Field — the companion app

A write-only reporting app for Android. It sends field reports into the
console's intake queue and does nothing else: it cannot read an entity, a
report, a document or a map, and it does not hold the console's address.

Phase one (the server side) is already in the console. This is phase two.

---

## What it is for

An analyst standing in a car park with a phone, with no network, who has
just seen something. They open the app, pick a form, fill it in, take a
photo, and put the phone away. Later, back in range of the console, they
scan a code off somebody's screen and the queue empties.

## The PIN

The app asks for a PIN before it shows anything, and that PIN is not a
screen lock — it is part of the key.

The queue is encrypted under a random 32-byte data key. That key is wrapped
twice: first under PBKDF2-HMAC-SHA256 of the PIN (310,000 iterations), then
under an AES key that lives in the device keystore and cannot be exported,
even from a rooted handset. Unwrapping needs both. A copy of the app's data
directory is inert without the phone; the phone is inert without the PIN.

**A forgotten PIN cannot be recovered.** Not by the analyst, not by an
admin, not by anyone — that is what "the PIN holds the key" means. Reports
already uploaded are safe on the console. Anything still queued is gone. The
lock screen offers to erase and start again, and says so in those words
before it does.

What this is honestly worth: six digits is a million possibilities, guessing
has to happen on that handset because the hardware key never leaves it, and
each guess costs a PBKDF2 derivation. With the throttling below that puts a
patient attacker in the range of days to weeks at six digits, and out of
reach past eight. It is not a passphrase and should not be described as one.
The app accepts up to twelve digits and it is worth using more than six.

**Wrong PINs** get increasing delays — a few seconds, then a minute, then up
to half an hour. Nothing is ever destroyed by failed attempts. An analyst
fumbling a PIN cold and wet must never lose reports over it, so the queue is
never wiped automatically, at any threshold.

**The fingerprint shortcut** (Settings) wraps the same data key under a
second keystore key that requires biometric authentication. It is a second
door to the same room, not a bypass — and enrolling a new fingerprint on the
handset invalidates it, dropping the analyst back to the PIN. That is the
behaviour you want on a phone somebody else has had their hands on.

**It locks** on launch, and ninety seconds after the app goes to the
background. Not instantly: an analyst who glances at a message mid-report
should not have to type a PIN to carry on. Locking closes the database, so
the key is gone from the storage layer and not merely hidden behind a
screen.

### Upgrading over an older build

An APK from before the PIN existed keyed the database differently, so its
store cannot be opened by this one. The app handles that rather than
crashing on it: setting a PIN for the first time discards any store left
behind by a previous key, and if an unreadable one somehow survives, the
lock screen says so in words and offers to clear it. Either way the reports
on that handset are unrecoverable — the key they were written under is gone
— which is a one-time cost of introducing the PIN, not a recurring one.

Nothing already uploaded is affected; that lives on the console.

## Light and dark

Settings → Appearance: match the system, always dark, or always light.
Defaults to matching the system. Dark is the scheme that got the attention —
this is a tool used outside at night, where a white screen is both hard on
the eyes and visible from a long way off — but a phone in direct sun needs
the other one, and the system setting does not always know which situation
its owner is in.

The choice is stored in plain SharedPreferences rather than the encrypted
store, because the lock screen has to be drawn in the right colours before
there is any key to decrypt anything with.

## What is on the phone, and what is not

**On the phone:** the reports that have not been sent yet, and the photos,
clips and recordings attached to them. All of it in a SQLCipher database and
encrypted media files, under the key described above — which needs both this
handset's hardware and the analyst's PIN.

**Not on the phone, ever:** the console's address and the device token.
Those arrive by scanning the enrollment QR at the moment of upload, live in
memory for that one session, and are wiped when the upload finishes, when
the app goes to the background, or after fifteen minutes idle. Nothing about
the console is written to storage at any point.

That is the whole design. A handset that is lost, lent or seized gives up
the reports that had not been sent — encrypted — and no route to anything
else. It does not even say where the console is.

**What it does not defend against:** somebody holding the unlocked phone
with the app already open and the PIN already entered. Nothing can.

## Building it

```bash
cd android
./gradlew assembleDebug        # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease      # signed if keystore.properties exists, see below
./gradlew test                 # the unit tests, including TitleParityTest
```

### The JDK, which will bite you

**Gradle must run on JDK 17 or 21. Not the JDK Android Studio bundles.**

Current Studio ships JBR 25, and Gradle 8.11 cannot run on it. The failure is
not obvious — the build dies before reading a single source file with:

```
* What went wrong:
25.0.3
java.lang.IllegalArgumentException: 25.0.3
    at ...JavaVersion.parse(JavaVersion.java:307)
```

which is an unhelpful way of saying "this JDK is too new". If you see a bare
version number as the entire error, this is it.

In Android Studio: **Settings → Build, Execution, Deployment → Build Tools →
Gradle → Gradle JDK**, and pick a 21. Studio will download one for you from
that dropdown if you have none.

From the command line, point `JAVA_HOME` at a 21 before running `./gradlew`.

### The SDK

`compileSdk 35`, so you need `platforms;android-35` and
`build-tools;35.0.0`. A fresh Studio install may have only the newest
platform, and Gradle cannot fetch a missing one unless the SDK
command-line tools are installed. Either tick them in Studio's SDK Manager,
or:

```bash
$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager \
    "platforms;android-35" "build-tools;35.0.0"
```

The build reads `../api/field_templates.json` and copies it into the app's
assets. That file is the contract between the app and the console, and this
is why the app lives inside the platform repository rather than beside it: a
second hand-maintained copy of the form definitions would drift, and the
drift would be silent. Building the app outside the repository fails with a
message saying so.

### Tests

```bash
./gradlew test
```

`QrDecodeTest` builds the awkward frame shapes a real camera produces — rows
padded to a stride, interleaved pixels, a buffer that stops before the last
row's padding, every rotation the sensor reports — and asserts a code comes
back out of each one. It exists because the first scanner failed on every
frame and said nothing: the camera opened, the preview ran, and no code was
ever found. These run on the JVM, so that class of failure is now caught
before anybody carries the phone anywhere.

`TitleParityTest` is the other one worth knowing about. A report's title is
composed twice — on the phone and, as a fallback, on the console — from the
same pattern. Two implementations of one rule in two languages drift, so the
expected answers are generated by running the console's own Python:

```bash
python3 tools/gen_title_cases.py      # run from anywhere; writes the fixture
```

Change either implementation without the other and the test fails.

### A signed release

Make a keystore once and keep it somewhere safe:

```bash
keytool -genkeypair -v -keystore field-release.jks -keyalg RSA \
        -keysize 4096 -validity 10000 -alias field
```

Then `android/keystore.properties`, which is **not** in the repository:

```properties
storeFile=field-release.jks
storePassword=…
keyAlias=field
keyPassword=…
```

`./gradlew assembleRelease` produces a signed, minified APK. Without that
file the release variant is simply unsigned, so a fresh clone still builds.

## Putting it on a handset

```bash
adb install -r app/build/outputs/apk/release/app-release.apk
```

Sizes, so a surprise is not a surprise: **release is about 17 MB**, debug
about 81. The gap is R8 — the debug build ships unminified dex. Roughly 12 MB
of the release is SQLCipher's native library, built for `arm64-v8a` (the
fleet) and `x86_64` (so it still installs on an emulator); `abiFilters` in
`app/build.gradle.kts` drops the other two architectures. Cutting x86_64 as
well takes it to about 10 MB if you never use an emulator.

There is no Play Store listing and there should not be. Sideload it, or use
whatever device-management you already have.

## Publishing a build for other people

### Never the debug APK

`app-debug.apk` is the one already sitting in your build folder after any
run from Android Studio, and it is the wrong file to hand out. Three reasons,
and the second one is the one that matters here:

- It is signed with the **universal Android debug key**, whose private half
  ships with every copy of the SDK on earth. Anybody can build a malicious
  version of this app that Android will happily install straight over yours,
  because to Android it is signed by the same person.
- It is built `debuggable`, which means anything that can reach the device
  over `adb` can attach to the running process and read its memory — the
  decrypted data key included. The entire premise of this app is that a
  handset someone else is holding gives up nothing. A debug build gives up
  everything the moment it is unlocked.
- Its applicationId is `org.humint.field.debug`, so it is a *different app*
  as far as Android is concerned. A user who installs it can never update to
  a real build; they would have to uninstall first, and uninstalling takes
  the queued reports with it.

It is also about 81 MB against the release's 17.

### Make the signing key, once

```bash
mkdir -p ~/humint-keys && cd ~/humint-keys
keytool -genkeypair -v -keystore field-release.jks -keyalg RSA \
        -keysize 4096 -validity 10000 -alias field
```

**Keep this file and its passwords forever, somewhere you would keep a
password manager's export.** Android identifies an app by its applicationId
*and* its signing key. Lose the key and you cannot ship an update that
installs over the old one — every user has to uninstall first, and for this
app uninstalling destroys whatever reports were still queued on the handset.
There is no recovery process and no appeal; this is the one irreversible
mistake available here.

Keep it **outside the repository**, as above. `android/.gitignore` catches
`*.jks` and `keystore.properties` if you put them inside, but a key that is
not in the tree cannot be committed by an accident the gitignore did not
anticipate.

Then write `android/keystore.properties` — not committed, same gitignore:

```properties
storeFile=/Users/you/humint-keys/field-release.jks
storePassword=…
keyAlias=field
keyPassword=…
```

An absolute path is fine and is the safer habit.

### Build it

```bash
cd android
./gradlew clean assembleRelease
# app/build/outputs/apk/release/app-release.apk
```

Android Studio's **Build → Generate Signed App Bundle or APK…** does the
same thing through a wizard that will also create the keystore for you.
Choose **APK**, not App Bundle — an `.aab` is a Play Store upload format and
is not installable by a human. Note that the wizard writes to a destination
folder you pick, usually `app/release/`, rather than the Gradle path above.

If the build dies with a bare version number as the whole error, that is the
JDK trap — see "The JDK, which will bite you".

**If you already ran `assembleRelease` before making the keystore**, Gradle
left you `app-release-unsigned.apk` in the same folder. Android will not
install that — an unsigned APK is rejected by the package installer, which
is a confusing failure if you do not know to look for it. You do not have to
rebuild; sign the file you have:

```bash
BT=$ANDROID_HOME/build-tools/35.0.0
cd app/build/outputs/apk/release

$BT/zipalign -p -f 4 app-release-unsigned.apk humint-field-1.0.apk
$BT/apksigner sign --ks ~/humint-keys/field-release.jks \
                   --ks-key-alias field humint-field-1.0.apk
```

Align before signing, never after — zipalign rewrites offsets inside the
archive and would invalidate the signature.

### Check what you are about to publish

Worth the thirty seconds, because the failure mode is shipping a debug
build under a release name:

```bash
BT=$ANDROID_HOME/build-tools/35.0.0
APK=app/build/outputs/apk/release/app-release.apk

$BT/apksigner verify --print-certs "$APK"   # your key, not "Android Debug"
$BT/aapt dump badging "$APK" | head -3      # org.humint.field, no .debug
shasum -a 256 "$APK"
```

The certificate line should carry the name you typed into `keytool`. If it
says `CN=Android Debug, O=Android, C=US`, stop — that is the debug key and
the keystore was not picked up.

### Bump the version before each release

`versionCode` and `versionName` in `app/build.gradle.kts`. `versionCode` is
an integer that must increase every release; Android refuses to install a
build whose code is lower than the installed one. `versionName` is the
string people see. They start at `1` and `"1.0"`.

### Put it on GitHub

A GitHub **Release** — not a file committed to the repository. Tag it,
attach `app-release.apk`, and **put the SHA-256 in the release notes.** For
an app whose whole job is being trustworthy on a handset someone else might
get hold of, a published hash is what lets a user confirm that the file they
downloaded is the file you built. Attaching it without one asks them to
trust the network.

Tell them, in the release notes, what they will have to do:

> Android will not install an app from outside the Play Store until you let
> it. When you open the downloaded file, it offers a settings screen —
> allow installs from whatever app you downloaded it with (Chrome, Files),
> then go back and install. You can turn the permission off again
> afterwards.
>
> Verify the download first if you can:
> `sha256sum humint-field-1.0.apk` on Linux, `shasum -a 256` on a Mac,
> `Get-FileHash` in PowerShell. It should match the hash above.

Rename the file to something with a version in it —
`humint-field-1.0.apk` — before you attach it. Three releases all called
`app-release.apk` in a downloads folder is somebody's bad afternoon.

Two things worth saying plainly in the notes, because they are unusual
enough that people will otherwise assume a bug: the app asks for a PIN on
first launch and that PIN *is* the encryption, so there is no way to recover
the reports if it is forgotten; and the app never stores the console's
address or token, so every upload begins by scanning a QR.

### Current release

**HUMINT Field 1.1** (`versionCode` 2) —
[`humint-field-1.1.apk`](https://github.com/xPirate/humint-platform/releases/tag/v1.1),
17,951,878 bytes.

    SHA-256  65a1062facf6dd4f564c69ae48ab2182ea61449b2020d12a95da02492e8f25e1

The file hash changes with every build. The signing certificate does not —
every release is signed with the same key, which is what lets a new version
install over the old one without losing the queue:

    Signer certificate SHA-256
    47:54:2F:4F:BE:D5:5A:9C:00:A6:1E:01:98:3C:6A:A2:
    AA:41:F1:66:1B:DE:ED:61:01:E1:CE:4C:BB:5C:DA:A0

`apksigner verify --print-certs humint-field-1.1.apk` prints it. If the file
hash matches the release notes, the download is the file that was built; if
the certificate matches this one, it was built by the same hands as the last.

## No Google Play Services, anywhere

Deliberate, and worth keeping that way — these handsets are off-network and
may be de-Googled, and an app that needs GMS to read a QR code is an app
that does not start on one.

| Job | What this app uses | What it avoids |
|---|---|---|
| QR scanning | ZXing core, on a CameraX analyser | ML Kit barcode scanning |
| Position | `android.location.LocationManager` | FusedLocationProviderClient |
| Camera, video | CameraX (AndroidX) | — |
| Storage | Room over SQLCipher | — |
| Biometrics | androidx.biometric | — |
| JSON | `org.json`, which ships with Android | a serialization plugin |

There is no crash reporter and no analytics, and there must not be. A
library that uploads a stack trace containing the console's address would
quietly undo the entire point of the app.

## The permissions, and the ones that are missing

`INTERNET`, `CAMERA`, `RECORD_AUDIO`, `ACCESS_FINE_LOCATION`.

Not `ACCESS_BACKGROUND_LOCATION`: position is read while a report is open on
screen and not otherwise. A reporting tool that also tracks the analyst is a
different product, and a worse one to have on a phone that might be
searched.

Not `READ_MEDIA_IMAGES`: the app captures its own photos into its own
sandbox and cannot read the gallery, so a seized phone's gallery is not
something this app can be made to hand over.

The app also sets `FLAG_SECURE`, which keeps it out of the recents thumbnail
and blocks screenshots. The cost is that the analyst cannot screenshot their
own report; the report is going to the console anyway, and a screenshot of
it would land in the gallery outside everything this app encrypts.

## The seven forms

From `api/field_templates.json`, so the console and the app always agree:

| Form | For |
|---|---|
| **Signal** | Something heard on the air — frequency, band, mode, who, what was said, and a recording |
| **Person** | Someone seen — name or description, what they look like, who they were with, a photo |
| **Vehicle** | Plate, colour, make, model, body, occupants, heading, a photo or clip |
| **Activity** | Size, activity, location, unit, time, equipment — a SALUTE spot report |
| **Bearing** | A DF cut: frequency, bearing, reference, antenna, how sharp the null was |
| **Place** | What is normally here, and what is here now |
| **Note** | No form. Talk, type, or both |

Every one also carries a criticality, the phone's position and accuracy, a
note about where the analyst was standing, and free text.

Adding a field is an edit to that JSON file and nothing else — the app
renders the form from it and the console lays the report out from it. A
field key the console has not seen yet is shown under its raw name rather
than dropped, and the two ends compare registry versions on connecting so a
mismatch is reported rather than silently tolerated.

## Testing it without a handset

`./gradlew test` is the part that needs nothing at all — JVM unit tests, no
device, no emulator, a few seconds. Everything else needs an emulated Pixel,
which Android Studio will create for you under **Device Manager**; a Pixel 7
API 35 image is the closest thing to the target.

### An emulator cannot reach `localhost`

This is the one that wastes an evening. Inside the emulator, `localhost` is
the emulator. The machine running it is **`10.0.2.2`**, always, regardless of
its real address on your network.

So when the app asks for the console's address — by QR or typed — the value
to give it is `http://10.0.2.2:8080`, not `http://localhost:8080` and not
`http://127.0.0.1:8080`. If you want the QR itself to carry that, type it
into **Address the app should connect to** when you enroll the device,
because that is the address the console remembers, puts in the QR, and
prints on the card.

A physical handset on the same Wi-Fi uses the machine's real LAN address
(`http://192.168.1.x:8080`), which is what the card should say for a device
that will actually go out.

### Plain HTTP is allowed, deliberately

Android has refused cleartext HTTP by default since API 28, and this app
re-permits it in `res/xml/network_security_config.xml`. That is not an
oversight: the console this talks to is on a LAN or a Tailnet, usually with
a self-signed certificate or none, and an app that insisted on valid TLS
would be an app that cannot upload. The file carries the longer version of
that reasoning. If you put a real certificate in front of the console, use
`https://` in the address and nothing here gets in the way.

### The console side

The console's own suite posts all seven forms the way the app will; see
"Testing this yourself" in `docs/DESIGN.md` for the `curl` version. The one
check that matters most is the last one there: a device token must be
refused by `/api/entities`. If it ever is not, the premise of this app is
gone.
