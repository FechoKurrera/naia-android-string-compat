## 0.2.9 · 업데이트 저장소 이전과 기기 사진 선택

- 검증 범위는 호스트에서 실행한 코드와 배포 파일입니다. Galaxy에서 폴더 권한 승인, 선택 결과 수신 및 실제 불러오기를 확인했다는 뜻이 아닙니다.
- 업데이트 프로그램 1.1.0은 새 저장소의 고정 브랜치를 조회하고, 확인한 동일 커밋의 manifest와 ZIP만 받습니다. 이전 주소 리디렉션은 사용하지 않습니다.
- 새 업데이트 도구만 별도 ID로 설치합니다. 실제 Android 도구 ID와 데이터 위치는 그대로이며, 구 업데이트 도구를 삭제하지 않습니다.

## 0.2.8 · 보관함 삭제와 파일 미리보기

- 제품 파일 변경 범위: 보관함 UI·서비스, 해당 loopback/iframe 연결, 버전과 설명. 기존 페어링·설정은 유지합니다.
- Python 관련 회귀121개 통과: 보관함26개, 실제 HTTP 이미지 미리보기→가져오기→선택삭제→전체삭제 흐름, 원본 보존, 복구/일회용 확인, 활성화 검사 및 화면 연결 등을 포함합니다.
- JavaScript61개 통과: 보관함26개와 기존 화면 조작35개. 복수 선택, 삭제 확인·취소·부분 실패, 선택 유지, 큰 미리보기, 지연 응답과 URL 정리, 삭제 중 닫기/재열기를 검사했습니다.
- 복구 패키지25개 검사 통과. 새 ZIP을 기존 manifest 검증·교체 절차로 처리합니다.
- 미리보기는 원본을 복사·수정하거나 보관함에 등록하지 않습니다. 삭제는 보관함 복사본·미리보기·인덱스에 한정하며, 원본/기존 생성 히스토리를 지우지 않습니다.
- 실제 Galaxy WebView 시각 검사와 기기 적용 확인은 수행하지 않았습니다. 호스트 Chromium 실행 파일을 준비하지 못해 DOM fixture와 JavaScript 구문 검사, 실제 Python HTTP 서비스 검사로 검증했습니다. 네이티브 SAF 지원을 추가한 릴리스는 아닙니다.

## 0.2.7 · 앱 안의 사진·폴더 보관함 검증

- Python:135 tests passed. 실제 loopback HTTP 경로로 폴더 탐색→복수 이미지 가져오기→중복/실패 집계→목록→미리보기→PNG 원본 및 프롬프트 확인→재실행 후 목록 보존을 검증했습니다.
- JavaScript:50 tests passed (기존 화면 연결35, 새 보관함 DOM/비동기 처리15). 버튼 교체, 복수 선택, 하위 폴더 옵션, 취소·재열기, 두 사진 비교, 명시적 히스토리 추가, 불확실한 업로드 재전송 차단을 검사했습니다.
- 복구 패키지25 tests와 원본 extension manager 조합 재로드7 tests passed. 기존 연결·설정 파일 보존을 검사했습니다.
- 원본 파일은 수정하지 않으며 가져온 파일은 별도 디스크 보관함에 저장합니다. 썸네일/미리보기는 필요할 때 만들어지며 이미지 디코딩은 한 번에 하나입니다.
- 실제 Galaxy WebView와 기기 파일 접근 권한은 이 호스트 검사로 검증되지 않았습니다. Playwright 브라우저 실행 파일이 없어 실제 브라우저 시각 검사는 수행하지 못했고 DOM fixture와 JavaScript 구문 검사를 사용했습니다. 네이티브 SAF 권한 요청을 구현하거나 검증한 릴리스가 아닙니다.

# 0.2.5 generic Web UI control — 2026-10-23

User requested removal of Settings and per-feature restrictions. Screen+control now authorizes visible web controls including Settings, input, generation and file buttons. Direct API edit/generate tool permissions remain separate. Existing saved permission values and credentials are preserved. Secret field values remain redacted in observations and captures.

Default screen reads and UI action results now request JPEG images. Capture failures are bounded capture_error metadata; completed actions are not reported as failed merely because their follow-up image failed. Capture rendering is limited to 8 seconds and late canvases are released. Background DOM mutations no longer invalidate a completed cloned image. New visibility and collection rules retain rendered fixed panels and prioritize Prompt/Params/Negative and navigation tabs over large grids.

Validation: 89 Android-tools Python tests, 35 JavaScript screen tests, 25 relay tests. Tests cover settings/generation/file-control discovery, secret redaction, generic input without direct API edit permission, background capture mutations, post-action images and timeout semantics. Relay source 7d9ccd1cc06defcef1014c0da198458640a2a70a was published to the existing owner-private Site. The repair/downloader tests are run against this exact payload before publication.

Limits: JavaScript tests use a DOM/rendering harness; they do not prove Android WebView rendering. On the installed 0.2.4, live capture failed with screen_changed_during_capture and metadata exposed only five header controls. The new 0.2.5 client must be applied and the page refreshed before live verification. No live 0.2.5 menu/capture success is claimed. This is snapshot-based web UI control, not continuous video or Android system UI control. Native file pickers, cross-origin iframe contents, and drag gestures remain outside the API.

Payload SHA-256: `a7437d55dc7c4191e3e537fe7bde1166d9b4e50a240a71723def33d57caa7197`.

---

# 0.2.4 snapshot navigation fix — 2026-10-23

Keeps at most four snapshots for 120 seconds. Additional reads and unrelated trusted input no longer discard unchanged audited navigation targets. Exact DOM identity, signature, dirty-target, visibility and overlay checks remain. Edits and page-wide scrolling still reject intervening user input. Every action consumes all older snapshots. Snapshots are bound to their originating browser client; lease handoffs return screen_session_changed before dispatch. Android shell view-switch title whitespace and tabindex-only controls are recognized without enabling restricted actions.

Passed: 88 Android-tools Python tests, 27 JavaScript screen-agent tests, 25 repair/downloader tests. The two edit/revocation integration tests now obtain a real snapshot before attempting an action. The relay passed 23 tests including legacy and versioned screen result round trips and strict metadata rejection.

Live verification: the deployed relay had rejected Android agent_version with HTTP400. The compatibility fix was published to the existing private Site (source commit 2b59d5f21cc6162f7a8558dc85444b42625ade07). Actual tablet screen reads then completed and returned agent_version 0.2.3. Quick and a separate Next-page attempt still returned stale_snapshot on that installed code. The 0.2.4 client fix is locally verified; device menu execution remains pending its installation and page refresh. No claim of live 0.2.4 click success is made.

Existing credentials, permission settings, extension identity, generation and storage behavior are unchanged.

Payload SHA-256: `a52426066ac849ea52ce63e8cf8d9334d270ecd212b567ae035b543c176a3c40`.

---

# 0.2.3 transport lifecycle fix — 2026-10-23

The transport iframe is mounted outside the settings dialog. A separate lazy-loaded settings iframe cannot claim screen commands. Foreground parent messages drive polling and repeat readiness handshakes even when iframe timers are suspended; pageshow and visibility restoration wake the transport. No automatic page reload or credential/permission change occurs.

State results report runtime_version, ready and busy; screen results report agent_version. The exact observed branded view-switch title is recognized while restricted handlers remain blocked. Automatic trusted scroll/resize events no longer discard a snapshot; real pointer/keyboard input still does, and changed target bounds/values/handlers/overlays are revalidated.

Passed: 85 Python tests (including executing the packaged parent/panel scripts with iframe timers suspended), 19 screen JavaScript tests. Device rendering/background behavior remains unverified. A page refresh after updating is still required to replace old injected code and its old local endpoint. This fixes a structural failure mode; it does not establish that every observed tablet failure had that cause.

Payload SHA-256: `db75ae1d50c3641bfd787385f58a738defe2b7fbc913b3e178dd0a1c7edb9d98`.

---

# 0.2.2 navigation patch — 2026-10-23

Scope: target-scoped DOM invalidation for UI actions; Android full/simple view switches, standard tab/disclosure navigation, and three existing viewer/menu handlers. Settings/auth, generation, file operations and edit permission guards remain in place.

Passed: 17 JavaScript policy/transport regression tests, 8 loader tests, 11 screen integration tests, 25 repair/downloader tests. Background mutations no longer expire an unchanged Quick button; target/ancestor changes, covered controls, changed handlers, trusted user input and protected screens still reject actions. Live tablet behavior after installing 0.2.2 remains unverified.

Payload SHA-256: `68b384f424717f899565bbd132fa86a1b3d9c64d1edc47604d22a7f53ebedd00`.

The historical 0.2.1 verification below applies to that release, not a rerun of every test for 0.2.2.

---

# 0.2.1 repair verification — 2026-10-23

This release repairs extension initialization and reload failures. It does not replace the APK or change generation pricing, credentials, or permission choices.

## Source references

- Host: DNT-LAB/NAIA2.0 at `6e97c7f4f37e495ad21657ed29d28dd9993a808e`.
- Android tools: `909ecb69385f42c924077390afe3285a9366cb25`.
- ChatGPT connection: `fafe03b4e5546d21ea85e1f92e4f855c8754da3f`.
- Storage picker: `5e6a750bf15a72006d4c95d13c6178e65b2bcc22`.
- Repair payload SHA-256: `c1ea165ce262cb44dd8138d95df2bfe1077c1f88a58e9c98526f7a19c83ad1c1`.

## Checks completed

- 84 Android tools Python tests, 12 screen-agent JavaScript tests, 8 legacy extension tests, 25 repair/downloader tests, and 7 integration regressions passed. The final defensive loader guard was additionally checked with all 8 loader tests.
- Reproduced the three reported failures using shipped source fixtures and the real host ExtensionManager: unexpected `screen_service`, duplicate ChatGPT bridge registration, and storage panel revision mismatch.
- The actual bundled repair restored all three extensions in the same Python process, with versions 0.2.1 / 0.1.2 / 0.1.1. Mixed repeated reloads rendered each UI addition once.
- Verified byte-for-byte preservation of extension permission configuration, settings, offline dummy OAuth state, and offline dummy pairing state. Previously blocked ChatGPT v1 stayed blocked. The old updater remained loaded without applying anything.
- Verified fresh helper entry plus already-applied payload can explicitly reload reviewed extensions again.
- Exercised source validation, archive constraints, rollback, interrupted transaction recovery, immutable-commit downloads, and the host's non-reentrant manager lock.
- Repeated payload builds produced identical bytes. No real login, model inference, paid image generation, or device command was used in these tests.

## Limits

This is local host and source-level verification. The user's Galaxy/WebView has not run this repair yet. Managed browser preview was unavailable. The separately installed convenience extension 0.4.1 was not available in the pinned host source and was not included in the integration run. Live OAuth, relay connectivity, and the device UI must be checked after applying the repair; passing these tests does not establish those outcomes.


## 0.2.6 Comic Maker verification — 2026-10-23

- 118 Python tests passed with exact upstream PromptServer fixture enabled.
- 35 screen-agent JavaScript tests passed.
- 25 updater tests and 7 real NAIA extension reload tests passed.
- Cached busy state reproduced on exact EXten source; 12 compatibility regressions cover failures, cancellation, active plans, hot reload and worker ownership.
- 14 PromptServer tests include original FastAPI lifespan/SQLite, fake Story/Director responses through streaming plan/save/use, and real localhost startup/shutdown/restart with data preserved.
- Tests forbid real NovelAI transport. Startup/status read the token zero times. No user generation was triggered.
- Source fixture files are diagnostic inputs only and are not in the published payload. PromptServer downloads directly from its author at the pinned manifest commit upon explicit preparation.
- APK/device installation and actual paid or free generation are separate from host verification; do not describe tests as a Galaxy generation success.
- Payload SHA256: bf4df33133e98e7b650e8c3ccd7fb1212928291bfb25302ec9eb95bccc9ac89b
- Manifest SHA256: 6fbd711d8120dd49265d29d8be36bc3af0a1ada52470174245524734294b04bd
