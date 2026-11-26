# Copyright 2026 The ros2ci authors
# SPDX-License-Identifier: Apache-2.0

"""
The two jobs a release goes through before anything is built.

Both run inside a job container and follow the agent's contract, as the
other job scripts do: parameters in ``params.json``, results in
``out/results.json``, exit code 0 for success, 75 for a problem that is
not the release's own, anything else for a failure.

Neither runs anything from the repositories it reads. They fetch a tag,
read the ``package.xml`` files in it, and look rosdep keys up.

**The release check** (``"job": "release_check"``) looks at every release
in a rosdistro and says whether it can be built. It builds nothing. For
each release:

1. the tag exists in the source repository and names a commit,
2. every ``package.xml`` at the tag is valid and has the release's
   version,
3. every rosdep key the packages name resolves on every target that
   requires it: to another released package, or through the rosdep rules.

A release that is already in the source archive is not fetched again.
Only its tag is looked at: if it names another commit than the one that
was archived, the tag has moved, and that is reported.

**The source archive job** (``"job": "source_archive"``) fetches one
release at the commit the check found and makes the tarball that every
build of that release will read: ``out/source.tar.gz``, with
``out/record.json`` beside it saying what it is.

A release entry in the distribution file::

    ament_cmake:
      release:
        url: https://github.com/ament/ament_cmake.git
        version: 2.9.3
        tags:
          release: '{version}'
        skip:
        - ament_cmake_some_test_package

``url`` is the source repository. ``version`` is the version every
package in it has. ``tags.release`` says how the tag is named; without
it the tag is the version. ``skip`` names packages that are not
released. The packages are not listed: they are found in the source.

Parameters of the release check:

``rosdistro``
    The ROS distribution. Default ``rolling``.
``rosdistro_url``
    The base URL of the rosdistro to check: where its ``index-v4.yaml``
    is. It should name a commit.
``rosdep_rules``
    The rosdep rule files, as paths under ``rosdistro_url``.
``archived``
    What the source archive already holds: a list of objects with
    ``repository``, ``version``, ``url``, ``tag``, ``commit``, ``packages``
    and ``skipped``.
``targets``
    The targets to resolve keys for: a list of objects with ``os``,
    ``version``, ``required`` and ``skip_keys``, the keys that builds for
    the target are told to leave out. Default: every platform the
    distribution file names, all required.

Parameters of the source archive job: ``rosdistro``, ``rosdistro_url``,
and ``repository``, ``url``, ``version``, ``tag``, ``commit`` and
``skip`` as the release check reported them.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

from core_workspace import DEFAULT_ROSDISTRO_URL, DEFAULT_RULES
from core_workspace import EXIT_FAILED, EXIT_INFRASTRUCTURE, EXIT_OK
from core_workspace import Failure, JOB_DIR, OUT_DIR
from core_workspace import retry, run, section
from package_build import ROSDEP_CACHE_DIR, ROSDEP_SOURCES_DIR, write_local_rosdistro

WORK = '/ws'

# Directories with one of these files are left out, as colcon leaves
# them out of a workspace.
IGNORE_MARKERS = ('COLCON_IGNORE', 'AMENT_IGNORE', 'CATKIN_IGNORE')

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
VERSION = re.compile(r'^[0-9][0-9A-Za-z.+~]*$')
# Only https, and nothing git would read as an option or as another
# transport.
URL = re.compile(r'^https://[A-Za-z0-9][A-Za-z0-9._~/-]*$')
TAG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/+-]*$')
COMMIT = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')

GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')


def fetch_text(url):
    """A small text file from the rosdistro."""
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode()
        except urllib.error.HTTPError as e:
            raise Failure('bad_rosdistro', '%s: %s' % (url, e), EXIT_FAILED)
        except OSError as e:
            if attempt == 3:
                raise Failure('rosdistro_unreachable', '%s: %s' % (url, e), EXIT_INFRASTRUCTURE)


def git_output(arguments, cwd=None):
    """What a git command prints, or None if it failed."""
    import subprocess
    result = subprocess.run(['git'] + arguments, cwd=cwd, env=GIT_ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, errors='replace')
    if result.returncode != 0:
        sys.stdout.write(result.stderr)
        return None
    return result.stdout


def tag_commit(url, tag):
    """
    The commit a tag names in a remote repository.

    Returns (commit, problem). An annotated tag names a tag object; the
    commit behind it is what counts.
    """
    for attempt in (1, 2, 3):
        output = git_output(['ls-remote', url, 'refs/tags/' + tag, 'refs/tags/' + tag + '^{}'])
        if output is not None:
            break
    else:
        return None, 'the repository %s cannot be read' % url
    found = dict(reversed(line.split('\t')) for line in output.splitlines() if '\t' in line)
    commit = found.get('refs/tags/%s^{}' % tag) or found.get('refs/tags/' + tag)
    if not commit:
        return None, 'there is no tag %s in %s' % (tag, url)
    return commit, None


def fetch_tag(url, tag, commit, directory):
    """
    Check out the tag into a new directory. Returns a problem, or None.

    Only the one commit is fetched. If the tag no longer names the commit
    that was expected, nothing is checked out.
    """
    os.makedirs(directory)
    if git_output(['init', '-q', directory]) is None:
        return 'git could not make a repository'
    if retry(3, ['git', '-C', directory, 'fetch', '-q', '--depth', '1', '--no-tags', url, 'refs/tags/' + tag],
             env=GIT_ENV) != 0:
        return 'tag %s could not be fetched from %s' % (tag, url)
    fetched = (git_output(['rev-parse', 'FETCH_HEAD^{commit}'], cwd=directory) or '').strip()
    if fetched != commit:
        return 'tag %s names commit %s, not %s: it has moved' % (tag, fetched, commit)
    if git_output(['-c', 'advice.detachedHead=false', 'checkout', '-q', '--detach', commit], cwd=directory) is None:
        return 'commit %s could not be checked out' % commit
    return None


def find_packages(directory):
    """
    The packages in a source tree: (found, problems).

    Each found package is a dict with its name, path, version and build
    type, and under ``parsed`` what catkin_pkg read. A manifest that is
    not valid is a problem.
    """
    from catkin_pkg.package import InvalidPackage, parse_package

    found, problems = [], []
    for where, subdirectories, files in os.walk(directory):
        subdirectories[:] = sorted(name for name in subdirectories if name != '.git')
        if any(marker in files for marker in IGNORE_MARKERS):
            subdirectories[:] = []
            continue
        if 'package.xml' not in files:
            continue
        # A package does not hold other packages.
        subdirectories[:] = []
        path = os.path.relpath(where, directory)
        try:
            parsed = parse_package(os.path.join(where, 'package.xml'))
            parsed.evaluate_conditions({'ROS_VERSION': '2', 'ROS_PYTHON_VERSION': '3',
                                        'ROS_DISTRO': os.environ.get('ROS_DISTRO', '')})
            parsed.validate()
        except (InvalidPackage, ValueError, OSError) as e:
            problems.append('%s/package.xml is not valid: %s' % (path, ' '.join(str(e).split())))
            continue
        found.append({'name': parsed.name, 'path': path, 'version': parsed.version,
                      'build_type': parsed.get_build_type(), 'depends': dependency_keys(parsed),
                      'test_depends': test_only_keys(parsed),
                      'groups': sorted(group.name for group in parsed.member_of_groups
                                       if group.evaluated_condition is not False),
                      'parsed': parsed})
    return found, problems


def described(package):
    """A found package without what only this script can use."""
    return {key: value for key, value in package.items() if key != 'parsed'}


def read_release(name, entry):
    """
    One release entry of the distribution file: (release, problem).
    """
    release = entry.get('release')
    if not isinstance(release, dict):
        return None, None
    if not NAME.match(str(name)):
        return None, 'the repository name is not usable'
    url, version = release.get('url'), str(release.get('version', ''))
    if not isinstance(url, str) or not URL.match(url):
        return None, 'the release needs a "url": the https address of the source repository'
    if not VERSION.match(version):
        return None, 'the release needs a "version" such as 1.2.3, not %r' % version
    template = (release.get('tags') or {}).get('release', '{version}')
    if not isinstance(template, str) or '{version}' not in template:
        return None, '"tags: release:" must be a tag name with {version} in it'
    tag = template.replace('{version}', version)
    if not TAG.match(tag) or '..' in tag:
        return None, 'the tag %r is not usable' % tag
    skip = release.get('skip', [])
    if not isinstance(skip, list) or not all(isinstance(item, str) and NAME.match(item) for item in skip):
        return None, '"skip" must be a list of package names'
    listed = release.get('packages')
    if listed is not None and (not isinstance(listed, list) or not all(isinstance(item, str) for item in listed)):
        return None, '"packages" must be a list of package names'
    return {'repository': name, 'url': url, 'version': version, 'tag': tag, 'skip': sorted(skip),
            'listed': listed}, None


def load_distribution(rosdistro_url, rosdistro):
    """The distribution file of a rosdistro, parsed, its URL, and its index entry."""
    import yaml

    try:
        index = yaml.safe_load(fetch_text(rosdistro_url + '/index-v4.yaml'))
        entry = index['distributions'][rosdistro]
        url = rosdistro_url + '/' + entry['distribution'][0]
        distribution = yaml.safe_load(fetch_text(url))
        if not isinstance(distribution.get('repositories'), dict):
            raise ValueError('it has no "repositories"')
    except (yaml.YAMLError, KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
        raise Failure('bad_rosdistro', 'the rosdistro cannot be read: %s' % e, EXIT_FAILED)
    return distribution, url, entry


def fetch_rules(rosdistro_url, rosdistro, rules, env):
    """Fetch the rosdep rules the keys are resolved from."""
    os.makedirs(ROSDEP_SOURCES_DIR, exist_ok=True)
    for name in os.listdir(ROSDEP_SOURCES_DIR):
        os.remove(os.path.join(ROSDEP_SOURCES_DIR, name))
    shutil.rmtree(ROSDEP_CACHE_DIR, ignore_errors=True)
    with open(os.path.join(ROSDEP_SOURCES_DIR, '20-default.list'), 'w') as f:
        for rule in rules:
            f.write('yaml %s/%s\n' % (rosdistro_url, rule))
    if retry(3, ['rosdep', 'update', '--rosdistro', rosdistro], env=env) != 0:
        raise Failure('rosdep_update_failed', 'the rosdep rules could not be fetched', EXIT_INFRASTRUCTURE)


class Target:
    """Answers whether a rosdep key has a package on one platform."""

    def __init__(self, rosdistro, os_name, os_version, required, skip_keys=()):
        from rosdep2 import create_default_installer_context
        from rosdep2.catkin_support import get_catkin_view

        self.os_name, self.os_version, self.required = os_name, str(os_version), required
        self.label = '%s %s' % (os_name, os_version)
        self.skip_keys = set(skip_keys)
        context = create_default_installer_context()
        self.installer_keys = context.get_os_installer_keys(os_name)
        self.default_installer_key = context.get_default_os_installer_key(os_name)
        self.view = get_catkin_view(rosdistro, os_name, self.os_version, update=False)

    def problem_with(self, key):
        """Why the key does not resolve here, or None if it does."""
        from rosdep2 import ResolutionError

        if key in self.skip_keys:
            return None
        try:
            definition = self.view.lookup(key)
        except KeyError:
            return 'there is no rosdep rule for it'
        try:
            installer_key, _ = definition.get_rule_for_platform(
                self.os_name, self.os_version, self.installer_keys, self.default_installer_key)
        except ResolutionError:
            return 'its rosdep rule has nothing for this platform'
        if installer_key != self.default_installer_key:
            return 'it is installed through %s, which a package cannot depend on' % installer_key
        return None


# Every package depends on ros_workspace without saying so, except the
# packages ros_workspace is itself built from. The packaging adds that
# dependency, so the order of builds has to know it too.
WITHOUT_ROS_WORKSPACE = ('ament_cmake_core', 'ament_package', 'ros_workspace')


def dependency_keys(parsed):
    """
    Every rosdep key a package needs to be built and to run: the ones it
    names, and ros_workspace.
    """
    keys = set() if parsed.name in WITHOUT_ROS_WORKSPACE else {'ros_workspace'}
    for dependencies in (parsed.build_depends, parsed.buildtool_depends, parsed.build_export_depends,
                         parsed.buildtool_export_depends, parsed.exec_depends):
        keys.update(d.name for d in dependencies if d.evaluated_condition is not False)
    return sorted(keys)


def test_only_keys(parsed):
    """The rosdep keys a package needs for its tests and for nothing else."""
    keys = {d.name for d in parsed.test_depends if d.evaluated_condition is not False}
    return sorted(keys - set(dependency_keys(parsed)))


def release_check(params):
    rosdistro = params.get('rosdistro', 'rolling')
    rosdistro_url = params.get('rosdistro_url', DEFAULT_ROSDISTRO_URL).rstrip('/')
    rules = params.get('rosdep_rules', DEFAULT_RULES)
    if not re.match(r'^[a-z][a-z0-9_]*$', rosdistro):
        raise Failure('bad_parameters', '%s is not a valid rosdistro' % rosdistro, EXIT_FAILED)
    archived = {}
    for entry in params.get('archived', []):
        archived[(entry['repository'], entry['version'])] = entry
    os.environ['ROS_DISTRO'] = rosdistro
    env = dict(os.environ, ROS_VERSION='2', ROS_PYTHON_VERSION='3',
               ROSDISTRO_INDEX_URL=rosdistro_url + '/index-v4.yaml', PYTHONUNBUFFERED='1')
    os.environ.update(env)

    problems, releases, moved = [], [], []

    def problem(repository, message, package=None):
        problems.append({'repository': repository, 'package': package, 'message': message})
        print('PROBLEM %s%s: %s' % (repository, ' (%s)' % package if package else '', message), flush=True)

    section('Read the distribution')
    distribution, distribution_url, index_entry = load_distribution(rosdistro_url, rosdistro)
    print('Read %s: %d repositories.' % (distribution_url, len(distribution['repositories'])))
    targets = params.get('targets')
    if targets is None:
        targets = [{'os': os_name, 'version': version, 'required': True}
                   for os_name, versions in sorted((distribution.get('release_platforms') or {}).items())
                   for version in versions]

    section('Look at each release')
    checked = {}
    for name, entry in sorted(distribution['repositories'].items()):
        release, message = read_release(name, entry if isinstance(entry, dict) else {})
        if message:
            problem(str(name), message)
        if not release:
            continue
        commit, message = tag_commit(release['url'], release['tag'])
        known = archived.get((name, release['version']))
        record = dict(release, commit=commit, packages=[], skipped=[])
        releases.append(record)
        # The list of packages to skip may change for an archived release:
        # the tarball stays the same, and only what is built from it
        # changes. The release is then read again, at the archived commit.
        reread = bool(known) and sorted(known.get('skipped', [])) != release['skip']
        if reread and commit != known['commit']:
            problem(name, 'the list of packages to skip changed, but tag %s no longer names the archived '
                          'commit %s' % (release['tag'], known['commit']))
        if known and not reread:
            # Already archived: builds read the archive, whatever upstream
            # does. What is worth knowing is whether the tag still says
            # the same thing.
            record.update(state='archived', commit=known['commit'], packages=known['packages'],
                          skipped=known.get('skipped', []))
            # What was archived is the release. An entry that now says
            # something else about the same version cannot take effect.
            for what, was, now in (('url', known.get('url'), release['url']),
                                   ('tag', known.get('tag'), release['tag'])):
                if was is not None and was != now:
                    problem(name, 'version %s is already archived with another %s; a change to a release '
                                  'needs a new version' % (release['version'], what))
            if commit != known['commit']:
                record['state'] = 'moved'
                moved.append({'repository': name, 'version': release['version'], 'tag': release['tag'],
                              'archived_commit': known['commit'], 'commit_now': commit})
                print('MOVED %s: tag %s was archived as %s and now %s' % (
                    name, release['tag'], known['commit'],
                    'names ' + commit if commit else 'is gone'), flush=True)
            else:
                print('%s %s: archived, tag unchanged.' % (name, release['version']))
            continue
        record['state'] = 'failed'
        if message:
            problem(name, message)
            continue
        directory = os.path.join(WORK, 'check', name)
        message = fetch_tag(release['url'], release['tag'], commit, directory)
        if message:
            problem(name, message)
            continue
        found, invalid = find_packages(directory)
        for message in invalid:
            problem(name, message)
        names = [package['name'] for package in found]
        for package in found:
            if package['version'] != release['version']:
                problem(name, 'its version is %s, and the release is %s' % (package['version'], release['version']),
                        package['name'])
        for duplicate in sorted({n for n in names if names.count(n) > 1}):
            problem(name, 'the source holds it more than once', duplicate)
        for skipped in release['skip']:
            if skipped not in names:
                problem(name, 'it is to be skipped, but the source has no such package', skipped)
        kept = [package for package in found if package['name'] not in release['skip']]
        if not kept:
            problem(name, 'the release holds no packages')
        if release['listed'] is not None and sorted(release['listed']) != sorted(p['name'] for p in kept):
            problem(name, '"packages" lists %s, and the source holds %s' % (
                ', '.join(sorted(release['listed'])) or 'nothing', ', '.join(sorted(p['name'] for p in kept))))
        record['packages'] = [described(package) for package in kept]
        record['skipped'] = release['skip']
        checked[name] = kept
        if all(p['repository'] != name for p in problems):
            record['state'] = 'new'
        print('%s %s: tag %s is commit %s, %d packages%s.' % (
            name, release['version'], release['tag'], commit, len(kept),
            '' if record['state'] == 'new' else ', with problems'))

    section('Resolve rosdep keys')
    # A key is another released package, or it has a rule on the target.
    released = {}
    for record in releases:
        for package in record['packages']:
            other = released.setdefault(package['name'], record['repository'])
            if other != record['repository']:
                problem(record['repository'], 'repository %s releases a package of this name too' % other,
                        package['name'])
    warnings = []
    if checked:
        # rosdep is given a distribution file written from what was found
        # above, with every release's packages in it. The rosdistro's own
        # file does not list packages, and an entry in it that is wrong
        # must be reported here, not trip rosdep up.
        write_local_rosdistro(os.path.join(WORK, 'rosdistro'), rosdistro, index_entry,
                              distribution.get('release_platforms') or {},
                              [dict(record, packages=[p['name'] for p in record['packages']],
                                    groups={p['name']: p.get('groups', []) for p in record['packages']})
                               for record in releases if record['packages']])
        env['ROSDISTRO_INDEX_URL'] = 'file://' + os.path.join(WORK, 'rosdistro', 'index-v4.yaml')
        os.environ['ROSDISTRO_INDEX_URL'] = env['ROSDISTRO_INDEX_URL']
        fetch_rules(rosdistro_url, rosdistro, rules, env)
        resolvers = [Target(rosdistro, t['os'], t['version'], t.get('required', True), t.get('skip_keys', ()))
                     for t in targets]
        for name, packages in sorted(checked.items()):
            for package in packages:
                for key in package['depends'] + package['test_depends']:
                    if key in released:
                        continue
                    for target in resolvers:
                        why = target.problem_with(key)
                        if not why:
                            continue
                        message = 'the key %s does not resolve on %s: %s' % (key, target.label, why)
                        if target.required:
                            problem(name, message, package['name'])
                        else:
                            warnings.append('%s (%s): %s' % (name, package['name'], message))
        # A problem found here takes the release back out.
        for record in releases:
            if record['state'] == 'new' and any(p['repository'] == record['repository'] for p in problems):
                record['state'] = 'failed'
    else:
        print('No release is new; nothing to resolve.')

    for record in releases:
        record.pop('listed', None)
    return {
        'job': 'release_check',
        'rosdistro': rosdistro,
        'rosdistro_url': rosdistro_url,
        'targets': targets,
        'releases': releases,
        'problems': problems,
        'moved_tags': moved,
        'warnings': warnings,
    }, ('release_check_failed', '%d problems' % len(problems)) if problems else None


def source_archive(params):
    rosdistro = params.get('rosdistro', 'rolling')
    os.environ['ROS_DISTRO'] = rosdistro
    release, message = read_release(params.get('repository'), {'release': {
        'url': params.get('url'), 'version': params.get('version'),
        'tags': {'release': '{version}'}, 'skip': params.get('skip', [])}})
    tag, commit = params.get('tag', ''), params.get('commit', '')
    if message or not release or not TAG.match(tag) or '..' in tag or not COMMIT.match(commit):
        raise Failure('bad_parameters', message or 'the release is not described properly', EXIT_FAILED)
    name, version = release['repository'], release['version']

    section('Fetch %s at tag %s' % (name, tag))
    directory = os.path.join(WORK, 'archive', name)
    message = fetch_tag(release['url'], tag, commit, directory)
    if message:
        raise Failure('fetch_failed', message, EXIT_FAILED)

    section('Read the packages')
    found, invalid = find_packages(directory)
    kept = [package for package in found if package['name'] not in release['skip']]
    wrong = [p['name'] for p in kept if p['version'] != version]
    if invalid or wrong or not kept:
        raise Failure('bad_source', '; '.join(invalid) or 'the packages do not all have version %s' % version,
                      EXIT_FAILED)
    for package in kept:
        print('%s %s (%s) in %s' % (package['name'], package['version'], package['build_type'], package['path']))

    section('Make the tarball')
    # git writes the same archive for the same commit: files in order,
    # dated by the commit. gzip is told to leave its own date out.
    tarball = os.path.join(OUT_DIR, 'source.tar.gz')
    plain = os.path.join(WORK, 'source.tar')
    code = run(['git', 'archive', '--format=tar', '--prefix=%s-%s/' % (name, version), '-o', plain, commit],
               cwd=directory, env=GIT_ENV)
    if code == 0:
        code = run(None, env=GIT_ENV, shell_string='gzip -n -c "%s" > "%s"' % (plain, tarball))
    if code != 0:
        raise Failure('archive_failed', 'the tarball could not be made', EXIT_FAILED)
    digest = hashlib.sha256()
    with open(tarball, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    record = {
        'rosdistro': rosdistro,
        'rosdistro_url': params.get('rosdistro_url', ''),
        'repository': name,
        'url': release['url'],
        'version': version,
        'tag': tag,
        'commit': commit,
        'sha256': digest.hexdigest(),
        'size': os.path.getsize(tarball),
        'packages': [described(package) for package in kept],
        'skipped': release['skip'],
    }
    with open(os.path.join(OUT_DIR, 'record.json'), 'w') as f:
        json.dump(record, f, indent=1)
        f.write('\n')
    print('%s: %d bytes, sha256 %s' % (os.path.basename(tarball), record['size'], record['sha256']))
    return dict(record, job='source_archive'), None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    results, status, message, code = {}, 'success', '', EXIT_OK
    try:
        with open(os.path.join(JOB_DIR, 'params.json')) as f:
            params = json.load(f)
        jobs = {'release_check': release_check, 'source_archive': source_archive}
        if params.get('job') not in jobs:
            raise Failure('bad_parameters', '"job" must be one of %s' % ', '.join(sorted(jobs)), EXIT_FAILED)
        results, failed = jobs[params['job']](params)
        if failed:
            status, message, code = failed[0], failed[1], EXIT_FAILED
    except Failure as failure:
        status, message, code = failure.status, str(failure), failure.code
        print('error: %s' % failure, flush=True)
    results.update(schema_version=1, status=status, message=message)
    with open(os.path.join(OUT_DIR, 'results.json'), 'w') as f:
        json.dump(results, f, indent=1)
        f.write('\n')
    section('Result: %s%s' % (status, ' (%s)' % message if message else ''))
    return code


if __name__ == '__main__':
    sys.exit(main())
