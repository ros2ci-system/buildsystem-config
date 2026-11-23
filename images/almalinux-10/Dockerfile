# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

# Job image for RHEL 10 and its rebuilds, built on AlmaLinux 10: the base
# system and the build tools.
#
# What is being built brings its own dependencies; a job installs them
# when it starts. This image only saves each job from installing the
# tools every job needs.

FROM docker.io/library/almalinux:10

# EPEL and CRB hold packages that builds commonly need and the base
# repositories leave out.
RUN dnf install -y dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb \
    && dnf install -y \
        cmake \
        gcc \
        gcc-c++ \
        git \
        glibc-langpack-en \
        make \
        patch \
        python3 \
        python3-devel \
        python3-pip \
        which \
    && dnf clean all

# Tools and tests assume a UTF-8 locale.
ENV LANG=en_US.UTF-8

# The Python build tools go in their own environment, so that they do not
# fight the system's packages. It can still see the system's packages,
# because what is built needs the system's Python libraries.
RUN python3 -m venv --system-site-packages /opt/ci \
    && /opt/ci/bin/pip install --no-cache-dir \
        colcon-common-extensions \
        rosdep \
        vcstool
ENV PATH=/opt/ci/bin:$PATH
