# NAIA Android 업데이트 1.1

기존 NAIA 앱의 Android 도구를 0.2.9로 업데이트합니다. 예전 GitHub 계정에 접속하거나 앱을 재설치할 필요가 없습니다. 0.2.9는 히스토리의 **사진·폴더**에서 기기의 사진이나 폴더를 선택하는 기능을 추가합니다. 선택한 사진은 보관함에 복사됩니다. 기기 원본과 기존 사진·설정·NAIA 연결 정보는 그대로 유지합니다.

## 기존 갤탭에서 처음 한 번

1. 새 저장소에 배포된 뒤, 기존 NAIA의 **Settings → Extension → GitHub에서 설치**에 이 주소를 입력합니다.
   `https://github.com/FechoKurrera/naia-android-string-compat/tree/android-tools-repair`
2. **NAIA Android 업데이트 1.1**을 신뢰하고 활성화합니다.
3. 새 업데이트 패널에서 **내장 0.2.9 수정본 적용 → 수정한 확장 다시 불러오기**를 순서대로 누른 다음 앱 화면을 새로고침합니다. 재불러오기 결과와 **NAIA Android 도구 0.2.9** 표시를 확인합니다.
4. **예전 NAIA Android 복구·업데이트**는 꺼 두면 됩니다. **NAIA Android 도구 0.2.9**는 계속 켜 둡니다.

구버전은 예전 저장소 주소에 고정되어 있고 다른 주소로 이동을 거부합니다. NAIA v2.0.48의 GitHub 설치는 이미 설치한 확장 ID를 덮어쓰지 않으므로, 새 **업데이트 도구에만** 별도 ID `naia_android_tools_repair_v2`를 씁니다. 실제 Android 도구의 ID `naia_zz_android_tools_v1`와 데이터 위치는 그대로입니다. 구버전 복구 도구의 파일 삭제나 앱 데이터 초기화는 필요 없습니다.

이후에는 새 업데이트 패널에서 **GitHub 최신 수정본 확인 → 확인한 수정본 적용 → 수정한 확장 다시 불러오기**를 사용합니다. 확인한 파일은 10분간 적용할 수 있습니다. 새 저장소의 지정 브랜치와 확인한 커밋의 파일만 사용하며, 자동 적용하지 않습니다.

## 보존 범위

업데이트 전 백업은 앱의 `config/android-tools/repair-backups/`에 저장합니다. 페어링, 저장 위치, 승인·활성·차단 설정, 사진 보관함을 초기화하지 않습니다. 기존 ChatGPT 연결 0.1.1 및 저장 위치 확장 0.1.0은 과거 검증된 파일과 정확히 일치할 때만 기존 호환 수정을 받습니다. 별도 구형 ChatGPT v1은 건드리지 않습니다.

## 소스와 검증

NAIA v2.0.48 확장 API 1, 원본 `DNT-LAB/NAIA2.0` 기준 커밋 `6e97c7f4f37e495ad21657ed29d28dd9993a808e`입니다. 새 저장소의 `android-tools` 브랜치는 도구 소스, `android-tools-repair`는 업데이트 도구와 검증된 내장 묶음입니다. `release-manifest.json`에는 배포 파일 해시가 있습니다.

원본 v2.0.48의 `core/extension_install_service.py`와 `app/backend/server/extension_install_routes.py` 기준으로 앱 내 설치는 GitHub URL을 받습니다. 배포 ZIP은 확장 디렉터리를 복원하거나 검증할 때 쓰는 패키지이며, ZIP 파일 자체를 폰에서 누르거나 URL 입력란에 넣는 방식은 지원하지 않습니다.

복원용 소스 ZIP을 풀었거나 동일한 소스 세트를 준비한 뒤 아래 명령으로 같은 묶음을 재현할 수 있습니다. 빌드는 네트워크에 접속하지 않습니다.

```sh
python android-tools-repair/build_payload.py
python -m unittest discover -s android-tools-repair/tests
python android-tools-repair/build_delivery.py --output-dir delivery
```

배포에 쓴 앱 본체나 서명키는 포함하지 않습니다. GPL-3.0 라이선스이며 포함된 html2canvas는 해당 MIT 고지를 유지합니다.

게시자는 실제 새 계정과 저장소에 대한 권한을 먼저 확인한 뒤 `build_delivery.py --owner GITHUB_LOGIN --output-dir delivery`로 출처를 한 번 고정합니다. 소스에 기록된 `update-source.json`의 owner가 null인 복원본에서는 원격 확인을 시도하지 않습니다. 로컬 내장 묶음 검증과 적용은 가능합니다.
