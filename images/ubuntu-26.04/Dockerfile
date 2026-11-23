# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

# Job image for Ubuntu 26.04: the base system and the build tools.
#
# What is being built brings its own dependencies; a job installs them
# when it starts. This image only saves each job from installing the
# tools every job needs.

FROM docker.io/library/ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        locales \
        patch \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Tools and tests assume a UTF-8 locale.
RUN locale-gen en_US.UTF-8
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
