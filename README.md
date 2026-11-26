# buildsystem-config

Configuration for an experimental ROS 2 build system. The system itself is in
[buildsystem](https://github.com/ros2ci-system/buildsystem).

**This is a test project. It is not an official ROS project, and nothing here
is connected to the ROS build farm or to any official ROS infrastructure.**

## Images

Each directory under `images/` is the build context of one job image: a build
file and anything it copies in.

| Directory | Base | For |
|---|---|---|
| `images/ubuntu-26.04` | Ubuntu 26.04 | Linux builds on Ubuntu |
| `images/almalinux-10` | AlmaLinux 10 | Linux builds on RHEL 10 and its rebuilds |
| `images/windows-ltsc2025` | Windows Server LTSC 2025 | Windows builds |

These hold the base operating system and the build tools: a compiler, CMake,
git, Python, and colcon, vcstool and rosdep. On Windows they hold the Visual
Studio build tools and pixi. The dependencies of what is being built are not
in the image; a job installs them when it starts.

An image is a cache. No job's result may depend on which image it started
from, so a job brings itself up to date when it starts.

To store an image's context on a coordinator and build it:

```
ci-admin store-context ubuntu-26.04.tar images/ubuntu-26.04
ci-admin submit --pool release --os linux --arch amd64 --job build-ubuntu.json --wait
```

where `build-ubuntu.json` is:

```json
{
  "kind": "image_build",
  "image": {"build": {"context": "stored:ubuntu-26.04.tar", "build_file": "Dockerfile"}},
  "timeout_seconds": 3600
}
```

## Job scripts

`images/job-scripts` holds what runs inside a job container, one script per
kind of job. Every image copies them in.

| Script | Job |
|---|---|
| `core_workspace.py` | Builds a workspace from source and tests it |
| `package_build.py` | Builds one ROS package as a package of the target: Debian packages in the Ubuntu image, RPM packages in the AlmaLinux image |

A package build takes the source as a tarball and the package's name:

```json
{
  "image": {"build": {"context": "stored:images.tar", "build_file": "ubuntu-26.04/Dockerfile"}},
  "command": ["/opt/ci/bin/python", "/opt/job-scripts/package_build.py"],
  "parameters": {"package": "ament_package", "rosdistro": "rolling"},
  "inputs": [{"name": "source.tar.gz", "url": "https://example.com/ament_package.tar.gz"}],
  "timeout_seconds": 3600
}
```

It leaves the binary packages, the source package, a copy of the rosdep rules
it resolved dependencies from, and `results.json` in the job's outputs. The
same job description builds RPM packages when its `build_file` is
`almalinux-10/Dockerfile`.

The packaging is written during the job: for Debian by
[dyndeb](https://github.com/ros2ci-test-ros-world/dyndeb), and for RPM by
bloom's dynamic RPM generator with the `bloom-rpm-macros` package, as on
build.ros2.org. The top of each script lists its parameters.

The ROS packages a package depends on are installed as packages. The job
names the repositories to take them from, each with the key its index is
signed with:

```json
"parameters": {
  "package": "ament_cmake_core",
  "revision": 2,
  "repositories": [{"url": "http://coordinator:8445/rolling-resolute", "key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n..."}]
}
```

`revision` is the rebuild counter the coordinator handed out for this build;
the package's version ends in it. The job also leaves `repo-metadata.json`,
which describes each package it built the way the distribution's own tools
do (`dpkg-deb`, `createrepo_c`). The coordinator checks that description
when the build is published, and writes the repository's index from it.

The Ubuntu image has the ROS package repository for a few tools that are
packaged only there. apt is told never to take a `ros-*` package from it,
so a package build cannot pick up a ROS package that build.ros2.org built.

## License

Apache-2.0. See [LICENSE](LICENSE).
