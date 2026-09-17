Unicode true
SetCompressor /SOLID lzma

!define APP_NAME "PhoneMic"
!define PUBLISHER "PhoneMic Team"
!define EXE_NAME "PhoneMic.exe"
!define REGPATH_UNINST "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
!define REGPATH_RUN "Software\Microsoft\Windows\CurrentVersion\Run"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef BUILD_DATE
  !define BUILD_DATE "unknown"
!endif
!ifndef BUILD_COMMIT
  !define BUILD_COMMIT "unknown"
!endif

!include LogicLib.nsh

; ---------- 安装范围：当前用户（per-user） ----------
; 只装到 %LOCALAPPDATA%\Programs，只写 HKCU（卸载键 / 快捷方式 / 开机自启）
; → 安装与卸载都不请求管理员权限，不弹 UAC。
; 唯一需要提权的情形：检测到旧版本装在 Program Files 下（老包是 per-machine 安装），
; 那时交给旧版本自己的卸载器完成一次提权清理（安装前会弹一个确认框）。
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
; 记住上次安装目录：旧安装把自己的 $INSTDIR 记在卸载键的 UninstallString 里
InstallDirRegKey HKCU "${REGPATH_UNINST}" "UninstallString"

; 安装包内容来源（可用 /DSOURCE_DIR=... 覆盖，便于做只验语法的小样本打包）
!ifndef SOURCE_DIR
  !define SOURCE_DIR "build/phonemic_nuitka/PhoneMic.dist"
!endif

!ifndef BUILD_SUFFIX
  !define BUILD_SUFFIX "unknown"
!endif
OutFile "dist\${APP_NAME}_Setup_${BUILD_SUFFIX}.exe"

LicenseData "NOTICE.txt"
Page license
Page directory
Page instfiles

; ---------- 运行时状态（.onInit 探测，Section 使用） ----------
Var LegacyDir        ; 需要提权移除的旧版本目录（通常是旧 per-machine 版）；空 = 无
Var SameDirUpgrade   ; 旧版本就在 $INSTDIR 且目录可写 → 就地清空即可
Var OldAutoStartCmd ; 旧的开机自启命令行；空 = 原本未启用自启
Var SilentSuffix     ; " --silent" 或空，沿用旧的自启偏好

; ============================================================
;  .onInit：探测旧版本；记住开机自启偏好
; ============================================================
Function .onInit
  SetShellVarContext Current

  StrCpy $LegacyDir ""
  StrCpy $SameDirUpgrade ""
  StrCpy $SilentSuffix ""

  ; ---- 探测旧版本 ----
  ; 此时 $INSTDIR 可能来自 InstallDirRegKey（= 旧安装目录），也可能是 InstallDir 的默认值。
  ; 防御：万一 InstallDirRegKey 没能剥掉 UninstallString 两侧的引号，退回默认目录。
  StrCpy $0 $INSTDIR 1
  StrCmp $0 '"' 0 +2
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${APP_NAME}"

  ; 判据是「目录里还有程序文件」，避免把残留的空目录当成旧版本
  ${If} ${FileExists} "$INSTDIR\${EXE_NAME}"
    ; 目标目录是否可写 —— 这同时是「就地升级」与「需要提权的旧 per-machine 版」的判别依据
    ClearErrors
    FileOpen $0 "$INSTDIR\.write-test" w
    ${If} ${Errors}
      ; 不可写（典型：Program Files）→ 需要提权移除，本次安装改回用户目录
      StrCpy $LegacyDir "$INSTDIR"
      StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${APP_NAME}"
    ${Else}
      FileClose $0
      Delete "$INSTDIR\.write-test"
      StrCpy $SameDirUpgrade "1"
    ${EndIf}
  ${EndIf}

  ; ---- 兜底：Program Files 下另有一份 per-machine 安装 ----
  ; （卸载键丢了、手工删过 uninst.exe，或用户上次在确认框里选了「保留旧版本」）
  StrCpy $2 "$PROGRAMFILES32\${APP_NAME}"
  ${If} $LegacyDir == ""
  ${AndIf} $2 != $INSTDIR
  ${AndIf} ${FileExists} "$2\${EXE_NAME}"
    StrCpy $LegacyDir "$2"
  ${EndIf}
  StrCpy $2 "$PROGRAMFILES64\${APP_NAME}"
  ${If} $LegacyDir == ""
  ${AndIf} $2 != $INSTDIR
  ${AndIf} ${FileExists} "$2\${EXE_NAME}"
    StrCpy $LegacyDir "$2"
  ${EndIf}

  ; ---- 记住开机自启偏好（放在探测之后：两种旧位置都能还原出正确的旧命令行）----
  ; 清理旧版本会丢掉 HKCU 的 Run 值，装完要按新路径写回，否则用户的「已启用自启」
  ; 会被静默关掉，或出现「面板显示已启用、开机跑的却是旧版本」。
  ; startup.py 写的是绝对路径，所以拼出旧值即可，不需要做字符串搜索。
  StrCpy $2 "$INSTDIR\${EXE_NAME}"
  ${If} $LegacyDir != ""
    StrCpy $2 "$LegacyDir\${EXE_NAME}"
  ${EndIf}
  StrCpy $3 '"$2"'
  ReadRegStr $OldAutoStartCmd HKCU "${REGPATH_RUN}" "${APP_NAME}"
  StrCmp $OldAutoStartCmd "$3" auto_nosilent
  StrCmp $OldAutoStartCmd "$3 --silent" auto_silent
  StrCpy $OldAutoStartCmd ""                 ; 不是已知形式（被手工改过）→ 当作未启用
  Goto auto_checked
  auto_silent:
    StrCpy $SilentSuffix " --silent"
    Goto auto_checked
  auto_nosilent:
    StrCpy $SilentSuffix ""
  auto_checked:
FunctionEnd

; ----------  exe info ----------
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "PhoneMic"
VIAddVersionKey "CompanyName" "PhoneMic Team"
VIAddVersionKey "FileDescription" "PhoneMic Installer"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 PhoneMic Team. Licensed under Apache 2.0."
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

Section
  ; ---------- 0. 目标目录必须可写 ----------
  ; per-user 安装不请求管理员权限；目录不可写（例如用户手选到 Program Files）时，
  ; 解压阶段会弹「Error opening file for writing」并留下半个目录，所以在最前面拦掉。
  ClearErrors
  CreateDirectory "$INSTDIR"
  FileOpen $0 "$INSTDIR\.write-test" w
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "安装目录没有写入权限：$\r$\n$INSTDIR$\r$\n$\r$\n本次安装不请求管理员权限（当前用户安装），请返回上一页选择其他目录。$\r$\n$\r$\nThe install directory is not writable. This installer runs without administrator privileges (per-user install) - please choose another directory."
    Abort
  ${EndIf}
  FileClose $0
  Delete "$INSTDIR\.write-test"

  ; ---------- 1. 结束正在运行的程序 ----------
  ; exe 被占用时，覆盖与删除都会静默失败，最坏结果是「装完还是旧版」，而用户以为升级成功了。
  ; /T 会连**子进程**一起结束：cloudflared 是 PhoneMic 用 subprocess 拉起的直接子进程，
  ; 所以一并带走，不需要再按映像名单独杀它。
  ; （曾有一条 taskkill /F /IM cloudflared.exe：按名字匹配不认路径，会误杀用户为其他
  ;   服务跑的同名进程。已删除——代价是「PhoneMic 已不在、只剩一个孤儿 cloudflared」
  ;   时没人收拾它，见下面 2b 的残留校验。）
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /IM "${EXE_NAME}" /T'
  Pop $0
  Sleep 800

  ; ---------- 2. 移除旧版本 ----------
  ; 2a. 旧版本在 Program Files（老 per-machine 包）：删它需要管理员权限，交给它自己的
  ;     卸载器完成（弹一次 UAC），我们轮询等它跑完。必须同步等待：旧卸载器最后会
  ;     DeleteRegKey 掉卸载键，若它晚于下面的 WriteRegStr，新装的卸载入口会被顺手删掉
  ;     （表现是「应用和功能」里查无此程序、无法卸载）。
  ${If} $LegacyDir != ""
    MessageBox MB_YESNOCANCEL|MB_ICONEXCLAMATION "检测到旧版本（安装在系统目录）：$\r$\n$LegacyDir$\r$\n$\r$\n需要先移除它，这一步会请求一次管理员权限。移除后 PhoneMic 将安装在你的用户目录，此后不再需要管理员权限。$\r$\n$\r$\n「是」立即移除并继续／「否」保留旧版本直接安装／「取消」退出安装。$\r$\n$\r$\nAn older system-wide installation was found. Removing it requires administrator privileges (one UAC prompt)." IDYES legacy_remove IDNO legacy_end
    Abort                                  ; 「取消」→ 退出安装

  legacy_remove:
    IfFileExists "$LegacyDir\uninst.exe" 0 legacy_no_uninstaller
      ; 不用 _?= ：让旧卸载器照常把自己复制到 $TEMP 再运行，这样它才删得掉自己
      ; 和所在的目录；我们靠轮询等它完成（跨提权拿不到进程句柄，无法 ExecWait）。
      ClearErrors
      ExecShell "runas" "$LegacyDir\uninst.exe" "/S"
      ${If} ${Errors}
        MessageBox MB_OK|MB_ICONEXCLAMATION "未能启动旧版本的卸载程序（可能取消了管理员授权）。旧版本仍保留在：$\r$\n$LegacyDir$\r$\n建议稍后手动卸载它。"
        Goto legacy_end
      ${EndIf}
      ; 轮询等待：旧目录里的程序文件消失即视为清理完成（最多约 6 秒）
      StrCpy $1 0
      ${While} $1 < 20
        Sleep 300
        IntOp $1 $1 + 1
        ${IfNot} ${FileExists} "$LegacyDir\${EXE_NAME}"
          ${Break}
        ${EndIf}
      ${EndWhile}
      Sleep 1000                           ; 留出旧卸载器收尾（快捷方式 / 卸载键 / 自启值）的时间
      ${If} ${FileExists} "$LegacyDir\${EXE_NAME}"
        MessageBox MB_OK|MB_ICONEXCLAMATION "旧版本未能完全移除：$\r$\n$LegacyDir$\r$\n建议稍后手动卸载它。"
      ${EndIf}
      Goto legacy_end

  legacy_no_uninstaller:
    ; 卸载程序不在了（只剩残留目录）：直接删，删得掉就删，删不掉只能提示
    RMDir /r "$LegacyDir"
    ${If} ${FileExists} "$LegacyDir\*.*"
      MessageBox MB_OK|MB_ICONEXCLAMATION "检测到旧版本残留目录，但其中没有卸载程序、当前也没有管理员权限，未能删除：$\r$\n$LegacyDir$\r$\n请手动删除该目录。"
    ${EndIf}

  legacy_end:
  ${EndIf}

  ; 2b. 就地升级（旧版本就在 $INSTDIR、同一用户、目录可写）：直接清空。
  ;     为什么不只靠 File /r 覆盖：File 是纯增量复制，旧版本有、新版本没有的文件会永远
  ;     留下——最典型的是 bundled 版留下的 bin\cloudflared.exe，它会被 _find_binary 优先选中。
  ;     清空是零数据风险的：用户数据都在 %LOCALAPPDATA%\PhoneMic，$INSTDIR 只有程序文件。
  ${If} $SameDirUpgrade != ""
    RMDir /r "$INSTDIR"
    ; 清空不完全通常是「文件仍被占用」：taskkill 失败，或 PhoneMic 早已退出、只剩一个
    ; 孤儿 cloudflared 锁着 bin\cloudflared.exe（第 1 步不再按名字杀它）。
    ; 不在这里拦掉的话，紧接着的 File /r 覆盖被占用文件时会弹一个看不出原因的
    ; 「Error opening file for writing」，而 per-user 安装没有提权可退。
    ${If} ${FileExists} "$INSTDIR\*.*"
      MessageBox MB_OK|MB_ICONSTOP "无法清空原有的安装目录：$\r$\n$INSTDIR$\r$\n$\r$\n可能仍有一个 PhoneMic 或 cloudflared 进程在运行、占用着目录中的文件。$\r$\n请在任务管理器中结束它们，然后重新运行安装程序。$\r$\n$\r$\nThe existing install directory could not be cleared. A running PhoneMic or cloudflared process may still be locking files in it - please end them in Task Manager and run the installer again."
      Abort
    ${EndIf}
  ${EndIf}

  ; ---------- 3. 安装 ----------
  SetOutPath $INSTDIR
  File /r "${SOURCE_DIR}\*.*"
  WriteUninstaller "$INSTDIR\uninst.exe"

  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${EXE_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk" "$INSTDIR\uninst.exe"

  WriteRegStr HKCU "${REGPATH_UNINST}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${REGPATH_UNINST}" "UninstallString" '"$INSTDIR\uninst.exe"'
  WriteRegStr HKCU "${REGPATH_UNINST}" "QuietUninstallString" '"$INSTDIR\uninst.exe" /S'
  WriteRegStr HKCU "${REGPATH_UNINST}" "DisplayIcon" "$INSTDIR\${APP_NAME}.exe"
  WriteRegStr HKCU "${REGPATH_UNINST}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${REGPATH_UNINST}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${REGPATH_UNINST}" "BuildDate" "${BUILD_DATE}"
  WriteRegStr HKCU "${REGPATH_UNINST}" "BuildCommit" "${BUILD_COMMIT}"
  WriteRegStr HKCU "${REGPATH_UNINST}" "InstallScope" "peruser"

  ; ---------- 4. 恢复开机自启 ----------
  ; 清理旧版本会丢掉 HKCU 的 Run 值：原本启用自启的用户按新路径写回（含 --silent 偏好），
  ; 否则会出现「面板显示已启用、开机跑的却是旧版本」，或设置被静默关掉。
  ${If} $OldAutoStartCmd != ""
    StrCpy $0 '"$INSTDIR\${EXE_NAME}"'
    StrCpy $0 "$0$SilentSuffix"
    WriteRegStr HKCU "${REGPATH_RUN}" "${APP_NAME}" "$0"
  ${EndIf}
SectionEnd

Section Uninstall
  ; 程序在运行时删不掉自己的文件（静默失败）→ 先结束它（/T 连子进程 cloudflared 一起）
  nsExec::ExecToStack '"$SYSDIR\taskkill.exe" /F /IM "${EXE_NAME}" /T'
  Pop $0
  Sleep 500

  Delete "$INSTDIR\*.*"
  RMDir /r "$INSTDIR"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\Uninstall ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  DeleteRegKey HKCU "${REGPATH_UNINST}"
  ; Clear the startup registry entry (consistent with the entry written by phonemic/utils/startup.py)
  DeleteRegValue HKCU "${REGPATH_RUN}" "${APP_NAME}"
SectionEnd

Function un.onInit
  SetShellVarContext Current
FunctionEnd
