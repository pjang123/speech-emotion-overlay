; Inno Setup script for the Emotion Overlay installer.
; Build after PyInstaller:  iscc /DAppVersion=0.1.0 packaging\installer.iss
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\Emotion Overlay.
; Settings and images live in %LOCALAPPDATA%\EmotionOverlay and are kept on uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\EmotionOverlay"
#endif

[Setup]
AppId={{6B1E0F7A-3C55-4D7E-9B2A-8E4F2C1D9A31}
AppName=Emotion Overlay
AppVersion={#AppVersion}
AppPublisher=pjang123
AppPublisherURL=https://github.com/pjang123/speech-emotion-overlay
DefaultDirName={localappdata}\Programs\Emotion Overlay
DefaultGroupName=Emotion Overlay
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=EmotionOverlay-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\EmotionOverlay.exe
UninstallDisplayName=Emotion Overlay

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove files from an older version before installing the new one.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\Emotion Overlay"; Filename: "{app}\EmotionOverlay.exe"
Name: "{group}\Uninstall Emotion Overlay"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Emotion Overlay"; Filename: "{app}\EmotionOverlay.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EmotionOverlay.exe"; Description: "Start Emotion Overlay now"; Flags: nowait postinstall skipifsilent
