# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

"""
The package build job: build one ROS package as a package of the target.

It runs inside a job container and follows the agent's contract, as the
core workspace job does:

* parameters come from ``params.json`` in the job directory,
* the source tarball is an input, in the ``in`` subdirectory,
* everything written to the ``out`` subdirectory is kept: the packages
  under ``out/packages``, and ``out/results.json``, which lists them and
  says how every test went,
* the exit code is 0 for success, 75 for a problem outside the package
  (a network failure, a package mirror that is down), and anything else
  for a failure of the build or its tests.

Only Debian packages so far. The steps:

1. unpack the source tarball and find the package in it,
2. bring the system up to date,
3. fetch the rosdep rules,
4. generate the packaging: the ``debian`` directory, written by dyndeb
   from package.xml,
5. install the build dependencies the packaging names,
6. build the source package and the binary package, which also runs the
   package's tests,
7. collect the packages and the test results.

The packaging does not let a failing test stop the build, so the packages
exist either way. This job is what decides: failing tests fail the job.

Parameters:

``package``
    The name of the ROS package to build. Required.
``source``
    The name of the source tarball among the inputs. Default
    ``source.tar.gz``. It may hold one package or a repository of several.
``rosdistro``
    The ROS distribution. Default ``rolling``.
``rosdistro_url``
    The base URL of the rosdistro to resolve against: where its
    ``index-v4.yaml`` is.
``rosdep_rules``
    The rosdep rule files, as paths under ``rosdistro_url``.
``revision``
    Which build of this version this is. Default 1.
``test``
    Whether to run the package's tests. Default true.
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ElementTree

# What every job script needs is in the core workspace job, which is the
# older of the two.
from core_workspace import DEFAULT_ROSDISTRO_URL, DEFAULT_RULES
from core_workspace import EXIT_FAILED, EXIT_INFRASTRUCTURE, EXIT_OK
from core_workspace import Failure, IN_DIR, JOB_DIR, OUT_DIR
from core_workspace import read_junit, retry, run, section

WORK = '/ws'
SOURCE_DIR = os.path.join(WORK, 'source')
BUILD_DIR = os.path.join(WORK, 'build')
TEST_RESULTS_DIR = os.path.join(WORK, 'test_results')

# rosdep's own default place for its list of rule files. The rules are
# found there without any setting, which matters because the build runs
# the package's tests: they must not see settings made for rosdep.
ROSDEP_SOURCES_DIR = '/etc/ros/rosdep/sources.list.d'

# What dpkg-buildpackage leaves beside the source tree.
PACKAGE_PATTERNS = ('*.deb', '*.ddeb', '*.dsc', '*.orig.tar.*', '*.debian.tar.*', '*.buildinfo', '*.changes')


class Job:

    def __init__(self, params):
        self.package = params.get('package', '')
        self.source = params.get('source', 'source.tar.gz')
        self.rosdistro = params.get('rosdistro', 'rolling')
        self.rosdistro_url = params.get('rosdistro_url', DEFAULT_ROSDISTRO_URL).rstrip('/')
        self.rules = params.get('rosdep_rules', DEFAULT_RULES)
        self.revision = str(params.get('revision', 1))
        self.test = params.get('test', True)
        if not re.match(r'^[a-z][a-z0-9_]*$', self.package):
            raise Failure('bad_parameters', '"package" must be the name of a ROS package', EXIT_FAILED)
        if not re.match(r'^[a-z][a-z0-9_]*$', self.rosdistro):
            raise Failure('bad_parameters', '%s is not a valid rosdistro' % self.rosdistro, EXIT_FAILED)
        if not re.match(r'^[1-9][0-9]*$', self.revision):
            raise Failure('bad_parameters', '"revision" must be a whole number, 1 or more', EXIT_FAILED)
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9+._-]*$', self.source):
            raise Failure('bad_parameters', '"source" must be the name of an input', EXIT_FAILED)
        if not shutil.which('dpkg-buildpackage'):
            raise Failure('unsupported_image', 'this image cannot build Debian packages', EXIT_FAILED)

        self.version = None
        self.debian_name = 'ros-%s-%s' % (self.rosdistro, self.package.replace('_', '-'))
        self.tree = None
        self.stages = []
        self.artifacts = []
        self.tests = None
        self.warnings = []

        # rosdep is told where the rosdistro is, and nothing else.
        self.env = dict(os.environ)
        self.env.update({
            'ROS_DISTRO': self.rosdistro,
            'ROS_VERSION': '2',
            'ROS_PYTHON_VERSION': '3',
            'ROSDISTRO_INDEX_URL': self.rosdistro_url + '/index-v4.yaml',
            'DEBIAN_FRONTEND': 'noninteractive',
            'PYTHONUNBUFFERED': '1',
        })

    def stage(self, name, function):
        section(name)
        started = time.time()
        try:
            function()
        except Failure:
            self.stages.append({'name': name, 'seconds': round(time.time() - started, 1), 'ok': False})
            raise
        self.stages.append({'name': name, 'seconds': round(time.time() - started, 1), 'ok': True})

    # -- steps -------------------------------------------------------

    def unpack(self):
        archive = os.path.join(IN_DIR, self.source)
        if not os.path.isfile(archive):
            raise Failure('bad_parameters', 'there is no input named %s' % self.source, EXIT_FAILED)
        os.makedirs(SOURCE_DIR)
        try:
            with tarfile.open(archive) as tar:
                # Refuses members that would land outside the directory.
                tar.extractall(SOURCE_DIR, filter='data')
        except (tarfile.TarError, OSError) as e:
            raise Failure('bad_source', 'the source tarball cannot be unpacked: %s' % e, EXIT_FAILED)

        found = []
        for directory, subdirectories, files in os.walk(SOURCE_DIR):
            subdirectories.sort()
            if 'package.xml' not in files:
                continue
            try:
                root = ElementTree.parse(os.path.join(directory, 'package.xml')).getroot()
            except ElementTree.ParseError:
                continue
            if (root.findtext('name') or '').strip() == self.package:
                found.append((directory, (root.findtext('version') or '').strip()))
        if len(found) != 1:
            raise Failure('bad_source', 'the source holds %d packages named %s' % (len(found), self.package),
                          EXIT_FAILED)
        package_dir, self.version = found[0]
        if not re.match(r'^[0-9][0-9A-Za-z.+~]*$', self.version):
            raise Failure('bad_source', 'version %r cannot be a Debian version' % self.version, EXIT_FAILED)
        if os.path.exists(os.path.join(package_dir, 'debian')):
            raise Failure('bad_source', 'the package has a debian directory of its own', EXIT_FAILED)
        print('Found %s %s in %s' % (self.package, self.version, os.path.relpath(package_dir, SOURCE_DIR)))

        # The tree the package is built in, named as Debian expects, and
        # the upstream tarball made from it before anything is added.
        os.makedirs(BUILD_DIR)
        name = '%s-%s' % (self.debian_name, self.version)
        self.tree = os.path.join(BUILD_DIR, name)
        shutil.copytree(package_dir, self.tree, symlinks=True)
        # Made the same way every time, so the same source gives the same
        # tarball: sorted, owned by root, and compressed without a time.
        orig = os.path.join(BUILD_DIR, '%s_%s.orig.tar.gz' % (self.debian_name, self.version))
        code = run(None, cwd=BUILD_DIR, env=self.env, shell_string=(
            'tar --sort=name --owner=0 --group=0 --numeric-owner -cf - "%s" | gzip -n > "%s"' % (name, orig)))
        if code != 0:
            raise Failure('bad_source', 'the upstream tarball could not be made', EXIT_FAILED)

    def update_system(self):
        """Apply pending updates, so the result does not depend on the image's age."""
        for command in (['apt-get', 'update'], ['apt-get', '-y', 'dist-upgrade']):
            if retry(3, command, env=self.env) != 0:
                raise Failure('update_failed', 'the system could not be brought up to date', EXIT_INFRASTRUCTURE)

    def fetch_rules(self):
        os.makedirs(ROSDEP_SOURCES_DIR, exist_ok=True)
        for name in os.listdir(ROSDEP_SOURCES_DIR):
            os.remove(os.path.join(ROSDEP_SOURCES_DIR, name))
        with open(os.path.join(ROSDEP_SOURCES_DIR, '20-default.list'), 'w') as f:
            for rule in self.rules:
                f.write('yaml %s/%s\n' % (self.rosdistro_url, rule))
        if retry(3, ['rosdep', 'update', '--rosdistro', self.rosdistro], env=self.env) != 0:
            raise Failure('rosdep_update_failed', 'the rosdep rules could not be fetched', EXIT_INFRASTRUCTURE)

    def generate(self):
        unresolved = []

        def watch(line):
            # The generator lists each key it could not resolve, indented.
            if listing[0] and line.startswith('  '):
                unresolved.append(line.strip())
            else:
                listing[0] = 'do not resolve to a package' in line

        listing = [False]
        code = run(['dyndeb-generate', '--rosdistro', self.rosdistro, '--revision', self.revision, self.tree],
                   env=self.env, watch=watch)
        if code == 3:
            self.warnings.extend(unresolved)
            raise Failure('unresolved_dependencies', 'some rosdep keys do not resolve on this target', EXIT_FAILED)
        if code != 0:
            raise Failure('packaging_failed', 'the packaging could not be generated', EXIT_FAILED)
        for name in ('control', 'rules', 'changelog'):
            print('\n----- debian/%s' % name)
            with open(os.path.join(self.tree, 'debian', name)) as f:
                sys.stdout.write(f.read())
        sys.stdout.flush()

    def install_build_dependencies(self):
        command = ['apt-get', 'build-dep', '-y', '--no-install-recommends']
        if not self.test:
            command += ['-P', 'nocheck']
        command.append('./')
        unknown = []

        def watch(line):
            if re.search(r'Unable to locate package|has no installation candidate|'
                         r'but it is not installable|Unable to correct problems', line):
                unknown.append(line.strip())

        for attempt in (1, 2):
            if run(command, cwd=self.tree, env=self.env, watch=watch) == 0:
                break
            if unknown:
                self.warnings.extend(sorted(set(unknown)))
                raise Failure('unknown_packages',
                              'the packaging names a build dependency this target does not have', EXIT_FAILED)
            if attempt == 2:
                raise Failure('install_failed', 'the build dependencies could not be installed',
                              EXIT_INFRASTRUCTURE)
            print('-- failed; trying again in 20 seconds', flush=True)
            time.sleep(20)

        output = subprocess.run(['dpkg-query', '-W', '-f', '${Package} ${Version} ${Architecture}\n'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors='replace').stdout
        with open(os.path.join(OUT_DIR, 'installed-packages.txt'), 'w') as f:
            f.write('\n'.join(sorted(output.splitlines())) + '\n')
        print('Recorded %d installed packages.' % len(output.splitlines()))

    def build(self):
        # The build gets the environment the image has, plus where the
        # rosdistro is, for dh_rosdep, and where to leave test results.
        env = dict(os.environ)
        env.update({
            'ROSDISTRO_INDEX_URL': self.env['ROSDISTRO_INDEX_URL'],
            'ROS_TEST_RESULTS_DIR': TEST_RESULTS_DIR,
            'DEBIAN_FRONTEND': 'noninteractive',
            'PYTHONUNBUFFERED': '1',
        })
        command = ['dpkg-buildpackage', '-us', '-uc']
        if not self.test:
            env['DEB_BUILD_OPTIONS'] = (env.get('DEB_BUILD_OPTIONS', '') + ' nocheck').strip()
            command += ['-Pnocheck']
        unresolved = []

        def watch(line):
            # dh_rosdep lists each key it could not resolve, indented.
            if listing[0] and line.startswith('  '):
                unresolved.append(line.strip())
            else:
                listing[0] = line.startswith('dh_rosdep: these rosdep keys')

        listing = [False]

        code = run(command, cwd=self.tree, env=env, watch=watch)
        if code != 0:
            if unresolved:
                self.warnings.extend(unresolved)
                raise Failure('unresolved_dependencies',
                              'some install-time rosdep keys do not resolve on this target', EXIT_FAILED)
            raise Failure('build_failed', 'the package build failed', EXIT_FAILED)

    def collect(self):
        packages = os.path.join(OUT_DIR, 'packages')
        os.makedirs(packages, exist_ok=True)
        paths = sorted({path for pattern in PACKAGE_PATTERNS for path in glob.glob(os.path.join(BUILD_DIR, pattern))})
        for path in paths:
            shutil.copy(path, packages)
            digest = hashlib.sha256()
            with open(path, 'rb') as f:
                for block in iter(lambda: f.read(1 << 20), b''):
                    digest.update(block)
            self.artifacts.append({
                'name': 'packages/' + os.path.basename(path),
                'bytes': os.path.getsize(path),
                'sha256': digest.hexdigest(),
            })
            print('%10d  %s' % (os.path.getsize(path), os.path.basename(path)))
        if not any(a['name'].endswith('.deb') for a in self.artifacts):
            raise Failure('build_failed', 'the build left no binary package', EXIT_FAILED)
        if not any(a['name'].endswith('.dsc') for a in self.artifacts):
            raise Failure('build_failed', 'the build left no source package', EXIT_FAILED)
        for path in paths:
            if path.endswith('.deb'):
                run(['dpkg-deb', '--info', path])

        if not self.test:
            return
        self.tests = collect_test_results(TEST_RESULTS_DIR, self.package)
        print('\nTests: %(total)d run, %(failed)d failed, %(errors)d errors, %(skipped)d skipped' % self.tests)
        if not self.tests['reports']:
            self.warnings.append('the tests left no results')
        if self.tests['failed'] or self.tests['errors']:
            raise Failure('tests_failed', '%d tests failed' % (self.tests['failed'] + self.tests['errors']),
                          EXIT_FAILED)

    # -- the whole job -----------------------------------------------

    def main(self):
        status, message, code = 'success', '', EXIT_OK
        try:
            self.stage('Unpack the source', self.unpack)
            self.stage('Bring the system up to date', self.update_system)
            self.stage('Fetch the rosdep rules', self.fetch_rules)
            self.stage('Generate the packaging', self.generate)
            self.stage('Install build dependencies', self.install_build_dependencies)
            self.stage('Build', self.build)
            self.stage('Collect', self.collect)
        except Failure as failure:
            status, message, code = failure.status, str(failure), failure.code
        self.write_results(status, message)
        section('Result: %s%s' % (status, ' (%s)' % message if message else ''))
        return code

    def write_results(self, status, message):
        os.makedirs(OUT_DIR, exist_ok=True)
        results = {
            'schema_version': 1,
            'job': 'package_build',
            'status': status,
            'message': message,
            'package': self.package,
            'version': self.version,
            'rosdistro': self.rosdistro,
            'rosdistro_url': self.rosdistro_url,
            'format': 'deb',
            'package_name': self.debian_name,
            'revision': int(self.revision),
            'stages': self.stages,
            'artifacts': self.artifacts,
            'tests': self.tests,
            'warnings': self.warnings,
        }
        with open(os.path.join(OUT_DIR, 'results.json'), 'w') as f:
            json.dump(results, f, indent=1)
            f.write('\n')


def collect_test_results(directory, package):
    """Read every test's result from the JUnit XML files under a directory."""
    cases = []
    reports = 0
    for where, subdirectories, files in os.walk(directory):
        subdirectories.sort()
        for name in sorted(files):
            if not name.endswith('.xml'):
                continue
            reports += 1
            for case in read_junit(os.path.join(where, name)):
                case['package'] = package
                cases.append(case)
    count = lambda outcome: sum(1 for case in cases if case['outcome'] == outcome)  # noqa: E731
    return {
        'reports': reports,
        'total': len(cases),
        'passed': count('passed'),
        'failed': count('failed'),
        'errors': count('error'),
        'skipped': count('skipped'),
        'cases': cases,
    }


if __name__ == '__main__':
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        with open(os.path.join(JOB_DIR, 'params.json')) as f:
            parameters = json.load(f)
        sys.exit(Job(parameters).main())
    except Failure as failure:
        print('error: %s' % failure, flush=True)
        with open(os.path.join(OUT_DIR, 'results.json'), 'w') as f:
            json.dump({'schema_version': 1, 'job': 'package_build', 'status': failure.status,
                       'message': str(failure)}, f, indent=1)
        sys.exit(failure.code)
