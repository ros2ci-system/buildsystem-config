# escape=`

# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

# Job image for Windows: Windows Server Core, the Visual Studio C++ build
# tools, and pixi.
#
# What is being built brings its own dependencies; a job installs them
# with pixi when it starts. This image only saves each job from
# installing the tools every job needs. The build tools alone take a long
# time to install.

FROM mcr.microsoft.com/windows/servercore:ltsc2025

SHELL ["cmd", "/S", "/C"]

# The Visual Studio 2022 build tools, with the C++ workload. The
# installer exits with 3010 when it wants a restart, which a container
# does not need, so that counts as success.
ADD https://aka.ms/vs/17/release/vs_buildtools.exe C:\TEMP\vs_buildtools.exe
RUN (C:\TEMP\vs_buildtools.exe --quiet --wait --norestart --nocache `
        --installPath C:\BuildTools `
        --add Microsoft.VisualStudio.Workload.VCTools `
        --includeRecommended `
     || IF "%ERRORLEVEL%"=="3010" EXIT 0) `
    && del /q C:\TEMP\vs_buildtools.exe

# pixi, at a pinned version, so that a rebuild of this image does not
# silently change the tool every job uses.
ADD https://github.com/prefix-dev/pixi/releases/download/v0.78.0/pixi-x86_64-pc-windows-msvc.zip C:\TEMP\pixi.zip
RUN mkdir C:\pixi `
    && tar -xf C:\TEMP\pixi.zip -C C:\pixi `
    && del /q C:\TEMP\pixi.zip `
    && setx /M PATH "%PATH%;C:\pixi"
