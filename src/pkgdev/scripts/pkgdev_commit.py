import os
import re
import subprocess
import tempfile
from collections import defaultdict
from itertools import zip_longest

from pkgcheck import reporters, scan
from pkgcore.ebuild.atom import MalformedAtom
from pkgcore.ebuild.atom import atom as atom_cls
from pkgcore.operations import observer as observer_mod
from pkgcore.restrictions import packages
from snakeoil.cli import arghparse
from snakeoil.mappings import OrderedSet
from snakeoil.osutils import pjoin

from .. import git
from ..mangle import Mangler
from .argparsers import cwd_repo_argparser

commit = arghparse.ArgumentParser(
    prog='pkgdev commit', description='create git commit',
    parents=(cwd_repo_argparser,))
commit.add_argument(
    '-m', '--message',
    help='specify commit message')
commit.add_argument(
    '-n', '--dry-run', action='store_true',
    help='pretend to create commit')
commit.add_argument(
    '-s', '--scan', action='store_true',
    help='run pkgcheck against staged changes')
commit.add_argument(
    '-f', '--force', action='store_true',
    help='forcibly create commit with QA errors')

add_actions = commit.add_mutually_exclusive_group()
add_actions.add_argument(
    '-u', '--update', dest='git_add_arg', const='--update', action='store_const',
    help='stage all changed files')
add_actions.add_argument(
    '-a', '--all', dest='git_add_arg', const='--all', action='store_const',
    help='stage all changed/new/removed files')


def grouper(iterable, n, fillvalue=None):
    """Iterate over a given iterable in n-size groups."""
    args = [iter(iterable)] * n
    return zip_longest(*args, fillvalue=fillvalue)


@commit.bind_delayed_default(1000, 'changes')
def _git_changes(namespace, attr):
    # stage changes as requested
    if namespace.git_add_arg:
        git.run(['add', namespace.git_add_arg, namespace.cwd])

    # determine staged changes
    p = git.run(
        ['diff-index', '--name-status', '--cached', '-z', 'HEAD'],
        stdout=subprocess.PIPE)

    # ebuild path regex, validation is handled on instantiation
    _ebuild_re = re.compile(r'^(?P<category>[^/]+)/[^/]+/(?P<package>[^/]+)\.ebuild$')

    # if no changes exist, exit early
    if not p.stdout:
        commit.error('no staged changes exist')

    data = p.stdout.strip('\x00').split('\x00')
    paths = []
    pkgs = {}
    changes = defaultdict(OrderedSet)
    for status, path in grouper(data, 2):
        paths.append(path)
        path_components = path.split(os.sep)
        if path_components[0] in namespace.repo.categories:
            changes['pkgs'].add(os.sep.join(path_components[:2]))
            if mo := _ebuild_re.match(path):
                try:
                    atom = atom_cls(f"={mo.group('category')}/{mo.group('package')}")
                    pkgs[atom] = status
                except MalformedAtom:
                    pass
        else:
            changes[path_components[0]].add(path)

    namespace.paths = [pjoin(namespace.repo.location, x) for x in paths]
    namespace.pkgs = pkgs
    setattr(namespace, attr, changes)


def commit_msg_prefix(git_changes):
    """Determine commit message prefix using GLEP 66 as a guide.

    See https://www.gentoo.org/glep/glep-0066.html#commit-messages for
    details.
    """
    # changes limited to a single type
    if len(git_changes) == 1:
        change_type = next(iter(git_changes))
        changes = git_changes[change_type]
        if len(changes) == 1:
            change = changes[0]
            # changes limited to a single object
            if change_type == 'pkgs':
                return f'{change}: '
            elif change_type == 'eclass' and change.endswith('.eclass'):
                # use eclass file name
                return f'{os.path.basename(change)}: '
            else:
                # use change path's parent directory
                return f'{os.path.dirname(change)}: '
        else:
            # multiple changes of the same object type
            common_path = os.path.commonpath(changes)
            if change_type == 'pkgs':
                if common_path:
                    return f'{common_path}/*: '
                else:
                    return '*/*: '
            else:
                return f'{common_path}: '

    # no prefix used for global changes
    return ''


def commit_msg_summary(repo, pkgs):
    """Determine commit message summary."""
    if len(pkgs) == 1:
        # single ebuild change
        atom, status = next(iter(pkgs.items()))
        pkg_matches = repo.match(atom.unversioned_atom)
        if status == 'A':
            if len(pkg_matches) > 1:
                return f'version bump to {atom.version}'
            else:
                return 'initial import'
        elif status == 'D':
            if len(pkg_matches) >= 1:
                return f'remove {atom.version}'
            else:
                return 'treeclean'
    elif len({x.unversioned_atom for x in pkgs}) == 1:
        # multiple ebuild changes for the same package
        atom = next(iter(pkgs)).unversioned_atom
        pkg_matches = repo.match(atom)
        if len(set(pkgs.values())) == 1:
            status = next(iter(pkgs.values()))
            if status == 'A':
                if len(pkg_matches) == len(pkgs):
                    return 'initial import'
                else:
                    versions = ', '.join(x.version for x in pkgs)
                    return f'version bumps to {versions}'
            elif status == 'D':
                if len(pkg_matches) >= 1:
                    return 'remove old'
                else:
                    return 'treeclean'
    return ''


@commit.bind_delayed_default(1001, 'commit_args')
def _commit_args(namespace, attr):
    args = []
    if namespace.repo.repo_id == 'gentoo':
        # gentoo repo requires signoffs and signed commits
        args.extend(['--signoff', '--gpg-sign'])
    if namespace.dry_run:
        args.append('--dry-run')
    if namespace.verbosity:
        args.append('-v')

    # determine commit message prefix
    msg_prefix = commit_msg_prefix(namespace.changes)

    if namespace.message:
        # ignore determined prefix when using custom prefix
        if not re.match(r'^\S+: ', namespace.message):
            message = msg_prefix + namespace.message
        else:
            message = namespace.message
        args.extend(['-m', message])
    else:
        # open editor using determined commit message template
        msg_summary = commit_msg_summary(namespace.repo, namespace.pkgs)
        template = tempfile.NamedTemporaryFile(mode='w')
        template.write(msg_prefix + msg_summary)
        template.flush()
        args.extend(['-t', template.name])
        # make sure tempfile isn't garbage collected until it's used
        namespace._commit_template = template

    setattr(namespace, attr, args)


@commit.bind_main_func
def _commit(options, out, err):
    repo = options.repo
    git_add_files = []

    if pkgs := options.changes.get('pkgs'):
        pkgs = [atom_cls(x) for x in pkgs]
        # manifest all changed packages
        failed = repo.operations.digests(
            domain=options.domain,
            restriction=packages.OrRestriction(*pkgs),
            observer=observer_mod.formatter_output(out))
        if any(failed):
            return 1

        # include existing Manifest files for staging
        manifests = (pjoin(repo.location, f'{x.cpvstr}/Manifest') for x in pkgs)
        git_add_files.extend(filter(os.path.exists, manifests))

    # mangle files
    git_add_files.extend(Mangler(options, options.paths))

    # stage modified files
    if git_add_files:
        git.run(['add'] + git_add_files, cwd=repo.location)

    # scan staged changes for QA issues if requested
    if options.scan:
        pipe = scan(['--exit', '--staged'])
        with reporters.FancyReporter(out) as reporter:
            for result in pipe:
                reporter.report(result)
        # fail on errors unless force committing
        if pipe.errors and not options.force:
            return 1

    # create commit
    git.run(['commit'] + options.commit_args)

    return 0
