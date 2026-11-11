# Offline regression verification

`test_combined_reload.py` contains 7 integration tests using the actual pinned
NAIA `ExtensionManager`, shipped originals, repaired sources, and repair payload.
`test_legacy_repair.py` contains 8 focused lifecycle/cache/UI composition tests.
The fixtures contain code only; OAuth and pairing values in tests are fake.

Use Python 3.12 and the upstream runtime dependencies (including FastAPI).
Set `NAIA_ANDROID_PORT_ROOT` to the directory with these siblings:

- `source/NAIA2.0`: upstream checkout at `6e97c7f4f37e495ad21657ed29d28dd9993a808e`
- `android-tools-repair`: this repository, including its verified payload
- `android-tools`: the payload ZIP's `main/` files
- `compat-repair-source/chatgpt` and `compat-repair-source/storage`: unpack the
  corresponding original source from `fixtures/original-legacy.zip`, then overlay
  payload `repairs/naia_chatgpt_connect_v2/` and `repairs/naia_storage_picker_v1/`
- `android-tools-updater`: unpack `fixtures/original-updater-1.0.0.zip` (only its
  passive registration is exercised; its update action is never invoked)

Fetch the pinned upstream source with:

```sh
git clone https://github.com/DNT-LAB/NAIA2.0.git source/NAIA2.0
git -C source/NAIA2.0 checkout 6e97c7f4f37e495ad21657ed29d28dd9993a808e
```

Run from the parent directory containing those siblings:

```sh
export NAIA_ANDROID_PORT_ROOT="$PWD"
python -m unittest discover -s android-tools-repair/verification -v
```

The default layout also works without the environment variable. Fixture paths
are relative to each test file. Tests prohibit outbound urllib requests and use
temporary app directories. They reproduce all three original errors, verify
same-process repair/reload, UI wrapper composition, version refresh, and exact
preservation of test settings, approval state, OAuth and pairing bytes.

Limitations: this is Linux/Python integration, not Android/WebView rendering.
No paid generation, live login, relay request, or thumbnail download is executed.
The separate convenience extension version 0.4.1 was unavailable and is not
covered. The blocked legacy v1 directory is a nonexecuting record fixture.
