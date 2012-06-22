# Copyright 2012 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

import subprocess
import sys

import portage
from portage import os
from portage import _unicode_decode
from portage.const import (BASH_BINARY, PORTAGE_BIN_PATH,
	PORTAGE_PYM_PATH, USER_CONFIG_PATH)
from portage.process import find_binary
from portage.tests import TestCase
from portage.tests.resolver.ResolverPlayground import ResolverPlayground
from portage.util import ensure_dirs

class SlotAbiEmergeTestCase(TestCase):

	def testSlotAbiEmerge(self):

		debug = False

		ebuilds = {
			"dev-libs/glib-1.2.10" : {
				"SLOT": "1"
			},
			"dev-libs/glib-2.30.2" : {
				"EAPI": "4-slot-abi",
				"SLOT": "2/2.30"
			},
			"dev-libs/glib-2.32.3" : {
				"EAPI": "4-slot-abi",
				"SLOT": "2/2.32"
			},
			"dev-libs/dbus-glib-0.98" : {
				"EAPI": "4-slot-abi",
				"DEPEND":  "dev-libs/glib:2=",
				"RDEPEND": "dev-libs/glib:2="
			},
		}
		installed = {
			"dev-libs/glib-1.2.10" : {
				"EAPI": "4-slot-abi",
				"SLOT": "1"
			},
			"dev-libs/glib-2.30.2" : {
				"EAPI": "4-slot-abi",
				"SLOT": "2/2.30"
			},
			"dev-libs/dbus-glib-0.98" : {
				"EAPI": "4-slot-abi",
				"DEPEND":  "dev-libs/glib:2/2.30=",
				"RDEPEND": "dev-libs/glib:2/2.30="
			},
		}

		world = ["dev-libs/glib:1", "dev-libs/dbus-glib"]

		playground = ResolverPlayground(ebuilds=ebuilds,
			installed=installed, world=world, debug=debug)
		settings = playground.settings
		eprefix = settings["EPREFIX"]
		eroot = settings["EROOT"]
		trees = playground.trees
		portdb = trees[eroot]["porttree"].dbapi
		vardb = trees[eroot]["vartree"].dbapi
		portdir = settings["PORTDIR"]
		var_cache_edb = os.path.join(eprefix, "var", "cache", "edb")

		portage_python = portage._python_interpreter
		ebuild_cmd = (portage_python, "-Wd",
			os.path.join(PORTAGE_BIN_PATH, "ebuild"))
		emerge_cmd = (portage_python, "-Wd",
			os.path.join(PORTAGE_BIN_PATH, "emerge"))

		test_ebuild = portdb.findname("dev-libs/dbus-glib-0.98")
		self.assertFalse(test_ebuild is None)

		test_commands = (
			emerge_cmd + ("--oneshot", "dev-libs/glib",),
			(lambda: "dev-libs/glib:2/2.32=" in vardb.aux_get("dev-libs/dbus-glib-0.98", ["RDEPEND"])[0],),
			emerge_cmd + ("--oneshot", "=dev-libs/glib-2.30.2",  "--ignore-built-slot-abi-deps", "y"),
			emerge_cmd + ("--oneshot", "dev-libs/dbus-glib"),
			(lambda: "dev-libs/glib:2/2.30=" in vardb.aux_get("dev-libs/dbus-glib-0.98", ["RDEPEND"])[0],),
		)

		distdir = playground.distdir
		pkgdir = playground.pkgdir
		fake_bin = os.path.join(eprefix, "bin")
		portage_tmpdir = os.path.join(eprefix, "var", "tmp", "portage")
		profile_path = settings.profile_path
		user_config_dir = os.path.join(os.sep, eprefix, USER_CONFIG_PATH)

		features = []
		if not portage.process.sandbox_capable or \
			os.environ.get("SANDBOX_ON") == "1":
			features.append("-sandbox")

		make_conf = (
			"FEATURES=\"%s\"\n" % (" ".join(features),),
			"PORTDIR=\"%s\"\n" % (portdir,),
			"PORTAGE_GRPNAME=\"%s\"\n" % (os.environ["PORTAGE_GRPNAME"],),
			"PORTAGE_USERNAME=\"%s\"\n" % (os.environ["PORTAGE_USERNAME"],),
			"PKGDIR=\"%s\"\n" % (pkgdir,),
			"PORTAGE_INST_GID=%s\n" % (portage.data.portage_gid,),
			"PORTAGE_INST_UID=%s\n" % (portage.data.portage_uid,),
			"PORTAGE_TMPDIR=\"%s\"\n" % (portage_tmpdir,),
			"CLEAN_DELAY=0\n",
			"DISTDIR=\"%s\"\n" % (distdir,),
			"EMERGE_WARNING_DELAY=0\n",
		)

		path =  os.environ.get("PATH")
		if path is not None and not path.strip():
			path = None
		if path is None:
			path = ""
		else:
			path = ":" + path
		path = fake_bin + path

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
			"PATH" : path,
			"PORTAGE_PYTHON" : portage_python,
			"PYTHONPATH" : pythonpath,
		}

		if "__PORTAGE_TEST_HARDLINK_LOCKS" in os.environ:
			env["__PORTAGE_TEST_HARDLINK_LOCKS"] = \
				os.environ["__PORTAGE_TEST_HARDLINK_LOCKS"]

		dirs = [distdir, fake_bin, portage_tmpdir,
			user_config_dir, var_cache_edb]
		true_symlinks = ["chown", "chgrp"]
		true_binary = find_binary("true")
		self.assertEqual(true_binary is None, False,
			"true command not found")
		try:
			for d in dirs:
				ensure_dirs(d)
			with open(os.path.join(user_config_dir, "make.conf"), 'w') as f:
				for line in make_conf:
					f.write(line)
			for x in true_symlinks:
				os.symlink(true_binary, os.path.join(fake_bin, x))
			with open(os.path.join(var_cache_edb, "counter"), 'wb') as f:
				f.write(b"100")
			# non-empty system set keeps --depclean quiet
			with open(os.path.join(profile_path, "packages"), 'w') as f:
				f.write("*dev-libs/token-system-pkg")

			if debug:
				# The subprocess inherits both stdout and stderr, for
				# debugging purposes.
				stdout = None
			else:
				# The subprocess inherits stderr so that any warnings
				# triggered by python -Wd will be visible.
				stdout = subprocess.PIPE

			for i, args in enumerate(test_commands):

				if hasattr(args[0], '__call__'):
					self.assertTrue(args[0](),
						"callable at index %s failed" % (i,))
					continue

				proc = subprocess.Popen(args,
					env=env, stdout=stdout)

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
					"emerge failed with args %s" % (args,))
		finally:
			playground.cleanup()
