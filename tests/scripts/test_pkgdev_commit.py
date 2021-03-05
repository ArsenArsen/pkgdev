import os
import shutil
from datetime import datetime
from functools import partial
from unittest.mock import patch

import pytest
from pkgdev.mangle import copyright_regex
from pkgdev.scripts import run
from snakeoil.contexts import chdir, os_environ
from snakeoil.osutils import pjoin


class TestPkgdevCommitParseArgs:

    def test_non_repo_cwd(self, capsys, tool):
        with pytest.raises(SystemExit):
            tool.parse_args(['commit'])
        out, err = capsys.readouterr()
        err = err.strip().split('\n')[-1]
        assert err.endswith('error: not in ebuild repo')

    def test_non_git_repo_cwd(self, repo, capsys, tool):
        with pytest.raises(SystemExit), \
                chdir(repo.location):
            tool.parse_args(['commit'])
        out, err = capsys.readouterr()
        err = err.strip().split('\n')[-1]
        assert err.endswith('error: not in git repo')

    def test_non_ebuild_git_repo_cwd(self, make_repo, git_repo, capsys, tool):
        os.mkdir(pjoin(git_repo.path, 'repo'))
        repo = make_repo(pjoin(git_repo.path, 'repo'))
        with pytest.raises(SystemExit), \
                chdir(repo.location):
            tool.parse_args(['commit'])
        out, err = capsys.readouterr()
        err = err.strip().split('\n')[-1]
        assert err.endswith('error: not in ebuild git repo')

    def test_git_commit_args(self, repo, make_git_repo, tool):
        git_repo = make_git_repo(repo.location)
        repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0', commit=False)
        author_opt = '--author="A U Thor <author@example.com>"'
        with chdir(repo.location):
            options, _ = tool.parse_args(['commit', author_opt])
        assert options.commit_args == [author_opt]


class TestPkgdevCommit:

    script = partial(run, 'pkgdev')

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.cache_dir = str(tmp_path)
        self.scan_args = ['--scan-args', f'--config no --cache-dir {self.cache_dir}']
        # args for running pkgdev like a script
        self.args = ['pkgdev', 'commit'] + self.scan_args

    def test_empty_repo(self, capsys, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        with patch('sys.argv', self.args), \
                pytest.raises(SystemExit) as excinfo, \
                chdir(git_repo.path):
            self.script()
        assert excinfo.value.code == 2
        out, err = capsys.readouterr()
        assert not out
        assert err.strip() == 'pkgdev commit: error: no staged changes exist'

    def test_custom_unprefixed_message(self, capsys, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        ebuild_path = repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')
        with open(ebuild_path, 'a+') as f:
            f.write('# comment\n')

        with patch('sys.argv', self.args + ['-u', '-m', 'msg']), \
                pytest.raises(SystemExit) as excinfo, \
                chdir(git_repo.path):
            self.script()
        assert excinfo.value.code == 0
        out, err = capsys.readouterr()
        assert err == out == ''

        commit_msg = git_repo.log(['-1', '--pretty=tformat:%B', 'HEAD'])
        assert commit_msg == ['cat/pkg: msg']

    def test_custom_prefixed_message(self, capsys, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        ebuild_path = repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')
        with open(ebuild_path, 'a+') as f:
            f.write('# comment\n')

        with patch('sys.argv', self.args + ['-u', '-m', 'prefix: msg']), \
                pytest.raises(SystemExit) as excinfo, \
                chdir(git_repo.path):
            self.script()
        assert excinfo.value.code == 0
        out, err = capsys.readouterr()
        assert err == out == ''

        commit_msg = git_repo.log(['-1', '--pretty=tformat:%B', 'HEAD'])
        assert commit_msg == ['prefix: msg']

    def test_edited_commit_message(self, capsys, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        ebuild_path = repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')
        with open(ebuild_path, 'a+') as f:
            f.write('# comment\n')

        with os_environ(GIT_EDITOR="sed -i '1s/$/commit/'"), \
                patch('sys.argv', self.args + ['-u']), \
                pytest.raises(SystemExit) as excinfo, \
                chdir(git_repo.path):
            self.script()
        assert excinfo.value.code == 0
        out, err = capsys.readouterr()
        assert err == out == ''

        commit_msg = git_repo.log(['-1', '--pretty=tformat:%B', 'HEAD'])
        assert commit_msg == ['cat/pkg: commit']

    def test_generated_commit_summaries(self, capsys, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')

        def commit():
            with patch('sys.argv', self.args + ['-a']), \
                    pytest.raises(SystemExit) as excinfo, \
                    chdir(git_repo.path):
                self.script()
            assert excinfo.value.code == 0
            out, err = capsys.readouterr()
            assert err == out == ''
            return git_repo.log(['-1', '--pretty=tformat:%B', 'HEAD'])

        # initial package import
        repo.create_ebuild('cat/newpkg-0')
        assert commit() == ['cat/newpkg: initial import']

        # single bump
        repo.create_ebuild('cat/pkg-1')
        assert commit() == ['cat/pkg: version bump 1']

        # multiple bumps
        repo.create_ebuild('cat/pkg-2')
        repo.create_ebuild('cat/pkg-3')
        assert commit() == ['cat/pkg: version bumps 2, 3']

        # single removal
        os.remove(pjoin(git_repo.path, 'cat/pkg/pkg-3.ebuild'))
        assert commit() == ['cat/pkg: remove 3']

        # multiple removal
        os.remove(pjoin(git_repo.path, 'cat/pkg/pkg-2.ebuild'))
        os.remove(pjoin(git_repo.path, 'cat/pkg/pkg-1.ebuild'))
        assert commit() == ['cat/pkg: remove old']

        # treeclean
        shutil.rmtree(pjoin(git_repo.path, 'cat/pkg'))
        assert commit() == ['cat/pkg: treeclean']

    def test_non_gentoo_file_mangling(self, repo, make_git_repo):
        git_repo = make_git_repo(repo.location)
        ebuild_path = repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')

        def commit(args):
            with patch('sys.argv', self.args + args), \
                    pytest.raises(SystemExit) as excinfo, \
                    chdir(git_repo.path):
                self.script()
            assert excinfo.value.code == 0

        # append line missing EOF newline to ebuild
        with open(ebuild_path, 'a+') as f:
            f.write('# comment')
        # verify file doesn't end with newline
        with open(ebuild_path) as f:
            assert f.read()[-1] != '\n'

        # non-gentoo repos aren't mangled by default
        commit(['-u', '-m', 'mangling'])
        with open(ebuild_path) as f:
            assert f.read()[-1] != '\n'

        # but they can be forcibly mangled
        with open(ebuild_path, 'a+') as f:
            f.write('# comment')
        commit(['-M', '-u', '-m', 'mangling'])
        # mangled pre-commit, file now ends with newline
        with open(ebuild_path) as f:
            assert f.read()[-1] == '\n'

    def test_gentoo_file_mangling(self, make_repo, make_git_repo):
        repo = make_repo(repo_id='gentoo')
        git_repo = make_git_repo(repo.location)
        ebuild_path = repo.create_ebuild('cat/pkg-0')
        git_repo.add_all('cat/pkg-0')

        def commit(args):
            with patch('sys.argv', self.args + args), \
                    pytest.raises(SystemExit) as excinfo, \
                    chdir(git_repo.path):
                self.script()
            assert excinfo.value.code == 0

        # append line missing EOF newline to ebuild
        with open(ebuild_path, 'a+') as f:
            f.write('# comment')
        # verify file doesn't end with newline
        with open(ebuild_path) as f:
            assert f.read()[-1] != '\n'

        # gentoo repos are mangled by default
        commit(['-n', '-u', '-m', 'mangling'])
        with open(ebuild_path) as f:
            assert f.read()[-1] == '\n'

        for years, org in (
                ('1999-2020', 'Gentoo Authors'),
                ('1999-2020', 'Gentoo Foundation'),
                ('2020', 'Gentoo Authors'),
                ('2020', 'Gentoo Foundation'),
                ):
            # munge the copyright header
            with open(ebuild_path, 'r+') as f:
                lines = f.read().splitlines()
                lines[0] = f'# Copyright {years} {org}\n'
                f.seek(0)
                f.truncate()
                f.write('\n'.join(lines) + '\n')
            commit(['-n', '-u', '-m', 'mangling'])
            # verify the copyright header was updated
            with open(ebuild_path) as f:
                lines = f.read().splitlines()
                mo = copyright_regex.match(lines[0])
                assert mo.group('end') == str(datetime.today().year)
                assert mo.group('holder') == 'Gentoo Authors'
