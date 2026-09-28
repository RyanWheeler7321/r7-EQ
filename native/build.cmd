@echo off
rem build.cmd [compressor^|space^|limiter]  (default: all). A DLL loaded by the audio engine is locked; build only the others.
setlocal
call "%ProgramFiles(x86)%\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b %errorlevel%
pushd "%~dp0"
if not exist build mkdir build
if not exist ..\plugins mkdir ..\plugins
set result=0
if "%~1"=="" set compressor=1
if /i "%~1"=="compressor" set compressor=1
if defined compressor (
    cl /nologo /O2 /LD /std:c++17 /EHsc /MT /W4 /Fobuild\ compressor_vst.cpp /link /OUT:..\plugins\R7Compressor.dll /IMPLIB:build\R7Compressor.lib advapi32.lib
    if errorlevel 1 set result=1
)
if "%~1"=="" set space=1
if /i "%~1"=="space" set space=1
if defined space (
    cl /nologo /O2 /fp:fast /LD /std:c++17 /EHsc /MT /W4 /Fobuild\ space_vst.cpp /link /OUT:..\plugins\R7Space.dll /IMPLIB:build\R7Space.lib
    if errorlevel 1 set result=1
)
if "%~1"=="" set limiter=1
if /i "%~1"=="limiter" set limiter=1
if defined limiter (
    cl /nologo /O2 /LD /std:c++17 /EHsc /MT /W4 /Fobuild\ limiter_vst.cpp /link /OUT:..\plugins\R7Limiter.dll /IMPLIB:build\R7Limiter.lib
    if errorlevel 1 set result=1
)
popd
exit /b %result%
