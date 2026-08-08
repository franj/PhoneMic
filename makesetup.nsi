Unicode true
SetCompressor /SOLID lzma

!define APP_NAME "PhoneMic"
!define PUBLISHER "PhoneMic Team"
!define EXE_NAME "PhoneMic.exe"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef BUILD_DATE
  !define BUILD_DATE "unknown"
!endif
!ifndef BUILD_COMMIT
  !define BUILD_COMMIT "unknown"
!endif

InstallDir "$PROGRAMFILES\${APP_NAME}"
!define SOURCE_DIR "build/phonemic_nuitka/PhoneMic.dist"

!ifndef BUILD_SUFFIX
  !define BUILD_SUFFIX "unknown"
!endif
OutFile "dist\${APP_NAME}_Setup_${BUILD_SUFFIX}.exe"

RequestExecutionLevel admin
LicenseData "NOTICE.txt"
Page license
Page directory
Page instfiles

; ----------  exe info ----------
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "PhoneMic"
VIAddVersionKey "CompanyName" "PhoneMic Team"
VIAddVersionKey "FileDescription" "PhoneMic Installer"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 PhoneMic Team. Licensed under Apache 2.0."
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

Section
  SetOutPath $INSTDIR
  File /r "${SOURCE_DIR}\*.*"
  WriteUninstaller "$INSTDIR\uninst.exe"

  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\uninst.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" '"$INSTDIR\uninst.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\${APP_NAME}.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "BuildDate" "${BUILD_DATE}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "BuildCommit" "${BUILD_COMMIT}"
SectionEnd

Section Uninstall
  Delete "$INSTDIR\*.*"
  RMDir /r "$INSTDIR"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
  ; Clear the startup registry entry (consistent with the entry written by phonemic/utils/startup.py)
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_NAME}"
SectionEnd