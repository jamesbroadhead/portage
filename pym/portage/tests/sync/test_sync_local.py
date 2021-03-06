# Copyright 2014 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import subprocess
import sys
import textwrap
import time

import portage
from portage import os, shutil
from portage import _unicode_decode
from portage.const import PORTAGE_PYM_PATH, TIMESTAMP_FORMAT
from portage.process import find_binary
from portage.tests import TestCase
from portage.tests.resolver.ResolverPlayground import ResolverPlayground
from portage.util import ensure_dirs

class SyncLocalTestCase(TestCase):
	"""
	Test sync with rsync and git, using file:// sync-uri.
	"""

	def _must_skip(self):
		if find_binary("rsync") is None:
			return "rsync: command not found"
		if find_binary("git") is None:
			return "git: command not found"

	def testSyncLocal(self):
		debug = False

		skip_reason = self._must_skip()
		if skip_reason:
			self.portage_skip = skip_reason
			self.assertFalse(True, skip_reason)
			return

		repos_conf = textwrap.dedent("""
			[test_repo]
			location = %(EPREFIX)s/var/repositories/test_repo
			sync-type = %(sync-type)s
			sync-uri = file:/%(EPREFIX)s/var/repositories/test_repo_sync
			auto-sync = yes
		""")

		profile = {
			"eapi": ("5",),
			"package.use.stable.mask": ("dev-libs/A flag",)
		}

		ebuilds = {
			"dev-libs/A-0": {}
		}

		playground = ResolverPlayground(ebuilds=ebuilds,
			profile=profile, user_config={}, debug=debug)
		settings = playground.settings
		eprefix = settings["EPREFIX"]
		eroot = settings["EROOT"]
		homedir = os.path.join(eroot, "home")
		distdir = os.path.join(eprefix, "distdir")
		repo = settings.repositories["test_repo"]
		metadata_dir = os.path.join(repo.location, "metadata")

		cmds = {}
		for cmd in ("emerge", "emaint"):
			cmds[cmd] =  (portage._python_interpreter,
				"-b", "-Wd", os.path.join(self.bindir, cmd))

		git_binary = find_binary("git")
		git_cmd = (git_binary,)

		committer_name = "Gentoo Dev"
		committer_email = "gentoo-dev@gentoo.org"

		def change_sync_type(sync_type):
			env["PORTAGE_REPOSITORIES"] = repos_conf % \
				{"EPREFIX": eprefix, "sync-type": sync_type}

		sync_cmds = (
			(homedir, cmds["emerge"] + ("--sync",)),
			(homedir, lambda: self.assertTrue(os.path.exists(
				os.path.join(repo.location, "dev-libs", "A")
				), "dev-libs/A expected, but missing")),
			(homedir, cmds["emaint"] + ("sync", "-A")),
		)

		rename_repo = (
			(homedir, lambda: os.rename(repo.location,
				repo.location + "_sync")),
		)

		delete_sync_repo = (
			(homedir, lambda: shutil.rmtree(
				repo.location + "_sync")),
		)

		git_repo_create = (
			(repo.location, git_cmd +
				("config", "--global", "user.name", committer_name,)),
			(repo.location, git_cmd +
				("config", "--global", "user.email", committer_email,)),
			(repo.location, git_cmd + ("init-db",)),
			(repo.location, git_cmd + ("add", ".")),
			(repo.location, git_cmd +
				("commit", "-a", "-m", "add whole repo")),
		)

		sync_type_git = (
			(homedir, lambda: change_sync_type("git")),
		)

		pythonpath =  os.environ.get("PYTHONPATH")
		if pythonpath is not None and not pythonpath.strip():
			pythonpath = None
		if pythonpath is not None and \
			pythonpath.split(":")[0] == PORTAGE_PYM_PATH:
			pass
		else:
			if pythonpath is None:
				pythonpath = ""
			else:
				pythonpath = ":" + pythonpath
			pythonpath = PORTAGE_PYM_PATH + pythonpath

		env = {
			"PORTAGE_OVERRIDE_EPREFIX" : eprefix,
			"DISTDIR" : distdir,
			"GENTOO_COMMITTER_NAME" : committer_name,
			"GENTOO_COMMITTER_EMAIL" : committer_email,
			"HOME" : homedir,
			"PATH" : os.environ["PATH"],
			"PORTAGE_GRPNAME" : os.environ["PORTAGE_GRPNAME"],
			"PORTAGE_USERNAME" : os.environ["PORTAGE_USERNAME"],
			"PORTAGE_REPOSITORIES" : repos_conf %
				{"EPREFIX": eprefix, "sync-type": "rsync"},
			"PYTHONPATH" : pythonpath,
		}

		if os.environ.get("SANDBOX_ON") == "1":
			# avoid problems from nested sandbox instances
			env["FEATURES"] = "-sandbox -usersandbox"

		dirs = [homedir, metadata_dir]
		try:
			for d in dirs:
				ensure_dirs(d)

			timestamp_path = os.path.join(metadata_dir, 'timestamp.chk')
			with open(timestamp_path, 'w') as f:
				f.write(time.strftime('%s\n' % TIMESTAMP_FORMAT, time.gmtime()))

			if debug:
				# The subprocess inherits both stdout and stderr, for
				# debugging purposes.
				stdout = None
			else:
				# The subprocess inherits stderr so that any warnings
				# triggered by python -Wd will be visible.
				stdout = subprocess.PIPE

			for cwd, cmd in rename_repo + sync_cmds + \
				delete_sync_repo + git_repo_create + sync_type_git + \
				rename_repo + sync_cmds:

				if hasattr(cmd, '__call__'):
					cmd()
					continue

				abs_cwd = os.path.join(repo.location, cwd)
				proc = subprocess.Popen(cmd,
					cwd=abs_cwd, env=env, stdout=stdout)

				if debug:
					proc.wait()
				else:
					output = proc.stdout.readlines()
					proc.wait()
					proc.stdout.close()
					if proc.returncode != os.EX_OK:
						for line in output:
							sys.stderr.write(_unicode_decode(line))

				self.assertEqual(os.EX_OK, proc.returncode,
					"%s failed in %s" % (cmd, cwd,))


		finally:
			playground.cleanup()
