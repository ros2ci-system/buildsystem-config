# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

"""
The core workspace job: build a workspace from source and test it.

It runs inside a job container and follows the agent's contract:

* parameters come from ``params.json`` in the job directory,
* the repos file is an input, in the ``in`` subdirectory,
* everything written to the ``out`` subdirectory is kept, and
  ``out/results.json`` says what was built and how every test went,
* the exit code is 0 for success, 75 for a problem outside the code under
  test (a network failure, a package mirror that is down), and anything
  else for a failure of the build or its tests.

The steps are the same on every platform:

1. fetch the sources named by the repos file,
2. bring the system up to date and install the workspace's dependencies
   through rosdep,
3. build with colcon,
4. test with colcon, and collect each test's result.

What differs is where dependencies come from. ``system`` resolution
installs them with the operating system's package manager. ``conda``
resolution has rosdep write a pixi manifest and installs them into a conda
environment, in which the build and the tests then run.

Parameters, all optional:

``rosdistro``
    The ROS distribution. Default ``rolling``.
``repos_file``
    The name of the repos file among the inputs. Default ``workspace.repos``.
``rosdistro_url``
    The base URL of the rosdistro to resolve against: where its
    ``index-v4.yaml`` is.
``rosdep_rules``
    The rosdep rule files, as paths under ``rosdistro_url``.
``resolution``
    ``system`` or ``conda``. Default: ``conda`` on Windows, else ``system``.
``packages_up_to``
    Build and test only these packages and what they need.
``skip_keys``
    rosdep keys the image provides, which rosdep is not to resolve.
``cmake_args``
    Extra arguments for CMake.
``test``
    Whether to run the tests. Default true.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INFRASTRUCTURE = 75

WINDOWS = os.name == 'nt'

JOB_DIR = os.environ.get('CI_JOB_DIR') or ('C:\\job' if WINDOWS else '/job')
IN_DIR = os.path.join(JOB_DIR, 'in')
OUT_DIR = os.path.join(JOB_DIR, 'out')

# Short, because Windows limits the length of a path.
WORKSPACE = 'C:\\ws' if WINDOWS else '/ws'

VCVARS = 'C:\\BuildTools\\VC\\Auxiliary\\Build\\vcvars64.bat'

DEFAULT_ROSDISTRO_URL = 'https://raw.githubusercontent.com/ros2ci-test-ros-world/rosdistro/main'
DEFAULT_RULES = ['rosdep/base.yaml', 'rosdep/python.yaml']

# The tools a conda environment needs in order to build, as rosdep keys.
# On a system image they are part of the image.
CONDA_TOOL_KEYS = ['cmake', 'git', 'ninja-build', 'python3-colcon-common-extensions']


class Failure(Exception):
    """A step failed. ``code`` is the job's exit code."""

    def __init__(self, status, message, code):
        super().__init__(message)
        self.status = status
        self.code = code


def section(title):
    print('\n' + '=' * 72, flush=True)
    print('== ' + title, flush=True)
    print('=' * 72, flush=True)


def run(command, cwd=None, env=None, shell_string=None, watch=None):
    """
    Run a command, passing its output through. Returns its exit code.

    ``watch`` is called with each line of output.
    """
    shown = shell_string or ' '.join(command)
    print('$ ' + shown, flush=True)
    process = subprocess.Popen(
        shell_string or command,
        cwd=cwd,
        env=env,
        shell=shell_string is not None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        errors='replace',
    )
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if watch:
            watch(line.rstrip('\r\n'))
    return process.wait()


def retry(attempts, command, **kwargs):
    """Run a command that depends on the network, a few times if need be."""
    code = 1
    for attempt in range(1, attempts + 1):
        code = run(command, **kwargs)
        if code == 0:
            return 0
        if attempt < attempts:
            delay = 10 * attempt
            print('-- failed with %d; trying again in %d seconds' % (code, delay), flush=True)
            time.sleep(delay)
    return code


class Job:

    def __init__(self, params):
        self.rosdistro = params.get('rosdistro', 'rolling')
        self.repos_file = os.path.join(IN_DIR, params.get('repos_file', 'workspace.repos'))
        self.rosdistro_url = params.get('rosdistro_url', DEFAULT_ROSDISTRO_URL).rstrip('/')
        self.rules = params.get('rosdep_rules', DEFAULT_RULES)
        self.resolution = params.get('resolution', 'conda' if WINDOWS else 'system')
        self.up_to = params.get('packages_up_to', [])
        self.skip_keys = params.get('skip_keys', [])
        self.cmake_args = params.get('cmake_args', [])
        self.test = params.get('test', True)
        for name, value in (('rosdistro', self.rosdistro),):
            if not re.match(r'^[a-z][a-z0-9_]*$', value):
                raise Failure('bad_parameters', '%s is not a valid %s' % (value, name), EXIT_FAILED)
        if self.resolution not in ('system', 'conda'):
            raise Failure('bad_parameters', 'resolution must be "system" or "conda"', EXIT_FAILED)
        for name in self.up_to + self.skip_keys:
            if not re.match(r'^[A-Za-z0-9][A-Za-z0-9+._-]*$', name):
                raise Failure('bad_parameters', '%r is not a package or key name' % name, EXIT_FAILED)

        self.manifest = os.path.join(WORKSPACE, 'pixi.toml')
        self.stages = []
        self.built = []
        self.failed = []
        self.aborted = []
        self.warnings = []

        # rosdep reads its sources list and keeps its cache where these
        # say, so nothing depends on what the image happened to hold.
        self.env = dict(os.environ)
        self.env.update({
            'ROS_DISTRO': self.rosdistro,
            'ROS_VERSION': '2',
            'ROS_PYTHON_VERSION': '3',
            'ROSDISTRO_INDEX_URL': self.rosdistro_url + '/index-v4.yaml',
            'ROSDEP_SOURCE_PATH': os.path.join(WORKSPACE, 'rosdep', 'sources.list.d'),
            'ROS_HOME': os.path.join(WORKSPACE, 'rosdep', 'home'),
            'ROSDEP_PIXI_MANIFEST': self.manifest,
            'DEBIAN_FRONTEND': 'noninteractive',
            'PYTHONUNBUFFERED': '1',
        })

    # -- helpers -----------------------------------------------------

    def stage(self, name, function):
        section(name)
        started = time.time()
        try:
            function()
        except Failure:
            self.stages.append({'name': name, 'seconds': round(time.time() - started, 1), 'ok': False})
            raise
        self.stages.append({'name': name, 'seconds': round(time.time() - started, 1), 'ok': True})

    def in_build_environment(self, command, **kwargs):
        """Run a command where the compiler and the dependencies are."""
        if self.resolution == 'system':
            return run(command, cwd=WORKSPACE, env=self.env, **kwargs)
        inner = ['pixi', 'run', '--manifest-path', self.manifest] + command
        if WINDOWS:
            # The Visual Studio tools are found through a batch file that
            # sets up the environment of the shell that calls it.
            line = 'call "%s" >NUL && %s' % (VCVARS, subprocess.list2cmdline(inner))
            return run(inner, cwd=WORKSPACE, env=self.env, shell_string=line, **kwargs)
        return run(inner, cwd=WORKSPACE, env=self.env, **kwargs)

    def selected_paths(self):
        """The source directories of the packages to build."""
        if not self.up_to:
            return ['src']
        result = subprocess.run(
            ['colcon', 'list', '--paths-only', '--base-paths', 'src'] + self.selection(),
            cwd=WORKSPACE, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, errors='replace')
        paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or not paths:
            sys.stdout.write(result.stderr)
            raise Failure('bad_parameters',
                          'no packages match: %s' % ', '.join(self.up_to), EXIT_FAILED)
        print('%d packages selected.' % len(paths), flush=True)
        return paths

    def selection(self):
        return ['--packages-up-to'] + self.up_to if self.up_to else []

    # -- steps -------------------------------------------------------

    def fetch(self):
        if not os.path.isfile(self.repos_file):
            raise Failure('bad_parameters', 'there is no input named %s' % os.path.basename(self.repos_file), EXIT_FAILED)
        os.makedirs(os.path.join(WORKSPACE, 'src'), exist_ok=True)
        code = retry(3, ['vcs', 'import', '--shallow', '--retry', '3', '--input', self.repos_file, 'src'],
                     cwd=WORKSPACE, env=self.env)
        if code != 0:
            raise Failure('fetch_failed', 'the sources could not be fetched', EXIT_INFRASTRUCTURE)
        run(['vcs', 'export', '--exact', 'src'], cwd=WORKSPACE, env=self.env,
            watch=self.exact_versions.append if hasattr(self, 'exact_versions') else None)

    def update_system(self):
        """Apply pending updates, so the result does not depend on the image's age."""
        if self.resolution != 'system':
            print('Nothing to do: dependencies come from conda at pinned versions.')
            return
        if shutil.which('apt-get'):
            commands = [['apt-get', 'update'], ['apt-get', '-y', 'dist-upgrade']]
        elif shutil.which('dnf'):
            commands = [['dnf', '-y', 'upgrade']]
        else:
            raise Failure('unsupported_image', 'this image has neither apt-get nor dnf', EXIT_FAILED)
        for command in commands:
            if retry(3, command, env=self.env) != 0:
                raise Failure('update_failed', 'the system could not be brought up to date', EXIT_INFRASTRUCTURE)

    def install_dependencies(self):
        sources = self.env['ROSDEP_SOURCE_PATH']
        os.makedirs(sources, exist_ok=True)
        with open(os.path.join(sources, '20-default.list'), 'w') as f:
            for rule in self.rules:
                f.write('yaml %s/%s\n' % (self.rosdistro_url, rule))
        if retry(3, ['rosdep', 'update', '--rosdistro', self.rosdistro], env=self.env) != 0:
            raise Failure('rosdep_update_failed', 'the rosdep rules could not be fetched', EXIT_INFRASTRUCTURE)

        # Dependencies are installed for the packages being built, and not
        # for others that merely share a repository with them.
        paths = self.selected_paths()
        arguments = ['--ignore-src', '-y', '--rosdistro', self.rosdistro]
        if self.skip_keys:
            arguments += ['--skip-keys', ' '.join(self.skip_keys)]
        if self.resolution == 'conda':
            # The environment has to hold the build tools as well. They are
            # asked for the way a package asks for anything: by rosdep key.
            tools = os.path.join(WORKSPACE, 'tools', 'ci_tools')
            os.makedirs(tools, exist_ok=True)
            with open(os.path.join(tools, 'package.xml'), 'w') as f:
                f.write('<?xml version="1.0"?>\n<package format="3">\n'
                        '  <name>ci_tools</name>\n  <version>0.0.0</version>\n'
                        '  <description>The tools the build itself needs.</description>\n'
                        '  <maintainer email="nobody@example.invalid">nobody</maintainer>\n'
                        '  <license>Apache-2.0</license>\n')
                for key in CONDA_TOOL_KEYS:
                    f.write('  <exec_depend>%s</exec_depend>\n' % key)
                f.write('  <export><build_type>ament_cmake</build_type></export>\n</package>\n')
            paths.append('tools')
            # Written afresh every time, so the environment holds exactly
            # what this workspace needs and nothing left from before.
            arguments += ['--os=conda:%s' % self.rosdistro, '--reinstall']
        command = ['rosdep', 'install', '--from-paths'] + paths + arguments

        # First without installing. A key that does not resolve is a fault
        # in the workspace or the rules, and trying again will not fix it.
        unresolved = []

        def note(line):
            if 'Cannot locate rosdep definition' in line or 'No definition of' in line or 'no pin for conda' in line:
                unresolved.append(line.strip())

        if run(command + ['--simulate'], cwd=WORKSPACE, env=self.env, watch=note) != 0:
            self.warnings.extend(unresolved)
            raise Failure('unresolved_dependencies',
                          'some rosdep keys do not resolve on this target', EXIT_FAILED)
        # Then for real. What can fail now is mostly the package manager
        # reaching its mirrors, which is not the workspace's fault. But a
        # rule can also name a package that does not exist, and trying
        # again will not make it exist.
        unknown = []

        def watch_install(line):
            if re.search(r'Unable to locate package|has no installation candidate|No match for argument|'
                         r'No matching distribution found|No candidates were found', line):
                unknown.append(line.strip())

        for attempt in (1, 2):
            if run(command, cwd=WORKSPACE, env=self.env, watch=watch_install) == 0:
                break
            if unknown:
                self.warnings.extend(sorted(set(unknown)))
                raise Failure('unknown_packages',
                              'a rosdep rule names a package this target does not have', EXIT_FAILED)
            if attempt == 2:
                raise Failure('install_failed', 'the dependencies could not be installed', EXIT_INFRASTRUCTURE)
            print('-- failed; trying again in 20 seconds', flush=True)
            time.sleep(20)
        self.record_installed()

    def record_installed(self):
        """Keep the list of what the build actually ran against."""
        path = os.path.join(OUT_DIR, 'installed-packages.txt')
        if self.resolution == 'conda':
            command = ['pixi', 'list', '--manifest-path', self.manifest]
            lock = os.path.join(WORKSPACE, 'pixi.lock')
            if os.path.isfile(lock):
                shutil.copy(lock, os.path.join(OUT_DIR, 'pixi.lock'))
            shutil.copy(self.manifest, os.path.join(OUT_DIR, 'pixi.toml'))
        elif shutil.which('dpkg-query'):
            command = ['dpkg-query', '-W', '-f', '${Package} ${Version} ${Architecture}\n']
        else:
            command = ['rpm', '-qa', '--qf', '%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n']
        try:
            output = subprocess.run(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    errors='replace').stdout
        except OSError as e:
            output = 'could not list the installed packages: %s\n' % e
        with open(path, 'w') as f:
            f.write('\n'.join(sorted(output.splitlines())) + '\n')
        print('Recorded %d installed packages.' % len(output.splitlines()))

    def watch_colcon(self, line):
        # colcon reports each package as it ends:
        #   Finished <<< name [1.2s]
        #   Failed   <<< name [1.2s, exited with code 2]
        #   Aborted  <<< name [1.2s]
        match = re.match(r'^(Finished|Failed|Aborted)\s+<<<\s+(\S+)', line)
        if match:
            {'Finished': self.built, 'Failed': self.failed, 'Aborted': self.aborted}[match.group(1)].append(match.group(2))

    def build(self):
        command = ['colcon', 'build', '--event-handlers', 'console_cohesion+', 'console_package_list+']
        cmake_args = ['-DCMAKE_BUILD_TYPE=RelWithDebInfo'] + list(self.cmake_args)
        if WINDOWS:
            # One install directory keeps paths short, and Ninja uses the
            # compiler the environment already points at.
            command += ['--merge-install', '--cmake-args', '-G', 'Ninja'] + cmake_args
        else:
            command += ['--cmake-args'] + cmake_args
        code = self.in_build_environment(command + self.selection(), watch=self.watch_colcon)
        if code != 0:
            raise Failure('build_failed', 'the build failed in: %s' % (', '.join(self.failed) or 'unknown'), EXIT_FAILED)

    def run_tests(self):
        command = ['colcon', 'test', '--event-handlers', 'console_direct+', '--return-code-on-test-failure']
        if WINDOWS:
            command.append('--merge-install')
        tested = {'Finished': [], 'Failed': [], 'Aborted': []}

        def watch(line):
            match = re.match(r'^(Finished|Failed|Aborted)\s+<<<\s+(\S+)', line)
            if match:
                tested[match.group(1)].append(match.group(2))

        code = self.in_build_environment(command + self.selection(), watch=watch)
        self.tests = collect_test_results(os.path.join(WORKSPACE, 'build'))
        print('\nTests: %(total)d run, %(failed)d failed, %(errors)d errors, %(skipped)d skipped' % self.tests)
        if tested['Aborted']:
            self.warnings.append('tests were aborted in: ' + ', '.join(tested['Aborted']))
        if code != 0 or self.tests['failed'] or self.tests['errors']:
            where = sorted({case['package'] for case in self.tests['cases'] if case['outcome'] in ('failed', 'error')}
                           | set(tested['Failed']))
            raise Failure('tests_failed', 'tests failed in: %s' % ', '.join(where), EXIT_FAILED)

    # -- the whole job -----------------------------------------------

    def main(self):
        self.tests = None
        self.exact_versions = []
        status, message, code = 'success', '', EXIT_OK
        try:
            self.stage('Fetch the sources', self.fetch)
            self.stage('Bring the system up to date', self.update_system)
            self.stage('Install dependencies', self.install_dependencies)
            self.stage('Build', self.build)
            if self.test:
                self.stage('Test', self.run_tests)
        except Failure as failure:
            status, message, code = failure.status, str(failure), failure.code
        self.write_results(status, message)
        section('Result: %s%s' % (status, ' (%s)' % message if message else ''))
        return code

    def write_results(self, status, message):
        os.makedirs(OUT_DIR, exist_ok=True)
        if self.exact_versions:
            with open(os.path.join(OUT_DIR, 'sources.repos'), 'w') as f:
                f.write('\n'.join(self.exact_versions) + '\n')
        results = {
            'schema_version': 1,
            'job': 'core_workspace',
            'status': status,
            'message': message,
            'rosdistro': self.rosdistro,
            'resolution': self.resolution,
            'stages': self.stages,
            'packages': {
                'built': sorted(self.built),
                'failed': sorted(self.failed),
                'aborted': sorted(self.aborted),
            },
            'tests': self.tests,
            'warnings': self.warnings,
        }
        with open(os.path.join(OUT_DIR, 'results.json'), 'w') as f:
            json.dump(results, f, indent=1)
            f.write('\n')


def collect_test_results(build_dir):
    """
    Read every test's result from the reports the test runners wrote.

    Each package's reports are JUnit-style XML files under
    ``<build>/<package>/test_results``, with a pure Python package's also
    in ``<build>/<package>/pytest.xml``.
    """
    cases = []
    if os.path.isdir(build_dir):
        for package in sorted(os.listdir(build_dir)):
            package_dir = os.path.join(build_dir, package)
            reports = []
            for directory, _, files in os.walk(os.path.join(package_dir, 'test_results')):
                reports += [os.path.join(directory, name) for name in sorted(files) if name.endswith('.xml')]
            single = os.path.join(package_dir, 'pytest.xml')
            if os.path.isfile(single):
                reports.append(single)
            seen = set()
            for report in reports:
                for case in read_junit(report):
                    key = (case['suite'], case['name'])
                    if key in seen:
                        continue
                    seen.add(key)
                    case['package'] = package
                    cases.append(case)
    count = lambda outcome: sum(1 for case in cases if case['outcome'] == outcome)  # noqa: E731
    return {
        'total': len(cases),
        'passed': count('passed'),
        'failed': count('failed'),
        'errors': count('error'),
        'skipped': count('skipped'),
        'cases': cases,
    }


def read_junit(path):
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError):
        return []
    cases = []
    for case in root.iter('testcase'):
        outcome, message = 'passed', ''
        for child in case:
            tag = child.tag.lower()
            if tag in ('failure', 'error', 'skipped'):
                outcome = {'failure': 'failed', 'error': 'error', 'skipped': 'skipped'}[tag]
                message = (child.get('message') or child.text or '').strip()[:2000]
                if tag != 'skipped':
                    break
        try:
            seconds = round(float(case.get('time') or 0), 3)
        except ValueError:
            seconds = 0
        cases.append({
            'suite': case.get('classname') or '',
            'name': case.get('name') or '',
            'outcome': outcome,
            'seconds': seconds,
            'message': message,
        })
    return cases


if __name__ == '__main__':
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        with open(os.path.join(JOB_DIR, 'params.json')) as f:
            parameters = json.load(f)
        sys.exit(Job(parameters).main())
    except Failure as failure:
        print('error: %s' % failure, flush=True)
        with open(os.path.join(OUT_DIR, 'results.json'), 'w') as f:
            json.dump({'schema_version': 1, 'job': 'core_workspace', 'status': failure.status,
                       'message': str(failure)}, f, indent=1)
        sys.exit(failure.code)
