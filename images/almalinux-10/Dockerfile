# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

# Job image for RHEL 10 and its rebuilds, built on AlmaLinux 10: the base
# system and the build tools.
#
# What is being built brings its own dependencies; a job installs them
# when it starts. This image only saves each job from installing the
# tools every job needs.
#
# The build context is the images directory, so that every image can
# copy in the same job scripts.

FROM docker.io/library/almalinux:10

# EPEL and CRB hold packages that builds commonly need and the base
# repositories leave out. The RPM macros for Python are there because
# rosdep rules for RHEL use them in package names.
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
        python3-rpm-macros \
        which \
    && dnf clean all

# Tools and tests assume a UTF-8 locale.
ENV LANG=en_US.UTF-8

# The build tools are the distribution's packages, so that a build and its
# tests see the same Python packages a user of the distribution has. In
# particular nothing here may bring a pytest of its own.
RUN dnf install -y \
        python3-catkin_pkg \
        python3-colcon-common-extensions \
        python3-pyyaml \
        python3-rosdistro \
        python3-rospkg \
        python3-vcstool \
    && dnf clean all

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
