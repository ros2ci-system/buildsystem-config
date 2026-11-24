# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

# Job image for Ubuntu 26.04: the base system and the build tools.
#
# What is being built brings its own dependencies; a job installs them
# when it starts. This image only saves each job from installing the
# tools every job needs.
#
# The build context is the images directory, so that every image can
# copy in the same job scripts.

FROM docker.io/library/ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        curl \
        git \
        gnupg \
        locales \
        patch \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# The ROS package repository, as the image of ci.ros2.org has it. A few
# dependencies of the core are packaged only there, such as
# python3-rosdistro-modules. The key is in a file that only this
# repository is checked against.
RUN curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
        | gpg --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://repo.ros2.org/ubuntu/testing/ resolute main" \
        > /etc/apt/sources.list.d/ros2-testing.list

# Tools and tests assume a UTF-8 locale.
RUN locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

# The build tools are the distribution's packages, so that a build and its
# tests see the same Python packages a user of the distribution has. In
# particular nothing here may bring a pytest of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-catkin-pkg-modules \
        python3-colcon-common-extensions \
        python3-rosdistro-modules \
        python3-rospkg-modules \
        python3-vcstool \
        python3-yaml \
    && rm -rf /var/lib/apt/lists/*

# rosdep is the test project's copy, at a fixed commit, so that a rebuild
# of this image does not silently change it. It goes in an environment of
# its own, without its dependencies: those are the packages above. The
# environment sees the system's packages, because what is built needs them.
RUN python3 -m venv --system-site-packages /opt/ci \
    && /opt/ci/bin/pip install --no-cache-dir --no-deps \
        https://github.com/ros2ci-test-ros-world/rosdep/archive/d4f7250da5c27b7a947548ea9fef7534d3bcef73.zip
ENV PATH=/opt/ci/bin:$PATH

# The job scripts: what runs in the container for each kind of job. Last,
# because they change most often and everything above stays cached.
COPY job-scripts /opt/job-scripts
