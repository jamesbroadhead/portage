# Copyright 1998-2009 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

__all__ = [
	"close_portdbapi_caches", "FetchlistDict", "portagetree", "portdbapi"
]

import portage
portage.proxy.lazyimport.lazyimport(globals(),
	'portage.checksum',
	'portage.data:portage_gid,secpass',
	'portage.dbapi.dep_expand:dep_expand',
	'portage.dep:dep_getkey,flatten,match_from_list,paren_reduce,use_reduce',
	'portage.env.loaders:KeyValuePairFileLoader',
	'portage.package.ebuild.doebuild:doebuild',
	'portage.util:ensure_dirs,shlex_split,writemsg,writemsg_level',
	'portage.util.listdir:listdir',
	'portage.versions:best,catpkgsplit,_pkgsplit@pkgsplit,ver_regexp',
)

from portage.cache.cache_errors import CacheError
from portage.cache.mappings import Mapping
from portage.const import REPO_NAME_LOC
from portage.dbapi import dbapi
from portage.exception import PortageException, \
	FileNotFound, InvalidDependString, InvalidPackageName
from portage.localization import _
from portage.manifest import Manifest

from portage import eclass_cache, auxdbkeys, \
	eapi_is_supported, dep_check, \
	_eapi_is_deprecated
from portage import os
from portage import _encodings
from portage import _unicode_decode
from portage import _unicode_encode
from portage import OrderedDict

import os as _os
import codecs
import logging
import stat
import sys
import warnings

if sys.hexversion >= 0x3000000:
	long = int

def _src_uri_validate(cpv, eapi, src_uri):
	"""
	Take a SRC_URI structure as returned by paren_reduce or use_reduce
	and validate it. Raises InvalidDependString if a problem is detected,
	such as missing operand for a -> operator.
	"""
	uri = None
	operator = None
	for x in src_uri:
		if isinstance(x, list):
			if operator is not None:
				raise portage.exception.InvalidDependString(
					("getFetchMap(): '%s' SRC_URI arrow missing " + \
					"right operand") % (cpv,))
			uri = None
			_src_uri_validate(cpv, eapi, x)
			continue
		if x == '||':
			raise portage.exception.InvalidDependString(
				("getFetchMap(): '%s' SRC_URI contains invalid " + \
				"|| operator") % (cpv,))

		if x[-1:] == "?":
			if operator is not None:
				raise portage.exception.InvalidDependString(
					("getFetchMap(): '%s' SRC_URI arrow missing " + \
					"right operand") % (cpv,))
			uri = None
			continue
		if uri is None:
			if x == "->":
				raise portage.exception.InvalidDependString(
					("getFetchMap(): '%s' SRC_URI arrow missing " + \
					"left operand") % (cpv,))
			uri = x
			continue
		if x == "->":
			if eapi in ("0", "1"):
				raise portage.exception.InvalidDependString(
					("getFetchMap(): '%s' SRC_URI arrows are not " + \
					"supported with EAPI='%s'") % (cpv, eapi))
			operator = x
			continue
		if operator is None:
			uri = x
			continue

		# This should be the right operand of an arrow operator.
		if "/" in x:
			raise portage.exception.InvalidDependString(
				("getFetchMap(): '%s' SRC_URI '/' character in " + \
				"file name: '%s'") % (cpv, x))

		if x[-1:] == "?":
			raise portage.exception.InvalidDependString(
				("getFetchMap(): '%s' SRC_URI arrow missing " + \
				"right operand") % (cpv,))

		# Found the right operand, so reset state.
		uri = None
		operator = None

	if operator is not None:
		raise portage.exception.InvalidDependString(
			"getFetchMap(): '%s' SRC_URI arrow missing right operand" % \
			(cpv,))

class _repo_info(object):
	__slots__ = ('name', 'path', 'eclass_db', 'portdir', 'portdir_overlay')
	def __init__(self, name, path, eclass_db):
		self.name = name
		self.path = path
		self.eclass_db = eclass_db
		self.portdir = eclass_db.porttrees[0]
		self.portdir_overlay = ' '.join(eclass_db.porttrees[1:])

class portdbapi(dbapi):
	"""this tree will scan a portage directory located at root (passed to init)"""
	portdbapi_instances = []
	_use_mutable = True

	def _get_settings(self):
		warnings.warn("Use portdbapi.settings instead of portdbapi.mysettings",
			DeprecationWarning)
		return self.settings

	def _set_settings(self, settings):
		warnings.warn("Use portdbapi.settings instead of portdbapi.mysettings",
			DeprecationWarning)
		self.settings = settings

	def _del_settings (self):
		warnings.warn("Use portdbapi.settings instead of portdbapi.mysettings",
			DeprecationWarning)
		del self.settings

	mysettings = property(_get_settings, _set_settings, _del_settings,
		"Deprecated self.mysettings, only for backward compatibility")

	@property
	def _categories(self):
		return self.settings.categories

	def __init__(self, _unused_param=None, mysettings=None):
		"""
		@param _unused_param: deprecated, use mysettings['PORTDIR'] instead
		@type _unused_param: None
		@param mysettings: an immutable config instance
		@type mysettings: portage.config
		"""
		portdbapi.portdbapi_instances.append(self)

		from portage import config
		if mysettings:
			self.settings = mysettings
		else:
			from portage import settings
			self.settings = config(clone=settings)

		porttree_root = self.settings['PORTDIR']

		# always show this warning after this parameter
		# is unused in stable portage
		if _unused_param is not None and _unused_param != porttree_root:
			warnings.warn("The first parameter of the " + \
				"portage.dbapi.porttree.portdbapi" + \
				" constructor is now unused. Use " + \
				"mysettings['PORTDIR'] instead.",
				DeprecationWarning, stacklevel=2)

		# This is strictly for use in aux_get() doebuild calls when metadata
		# is generated by the depend phase.  It's safest to use a clone for
		# this purpose because doebuild makes many changes to the config
		# instance that is passed in.
		self.doebuild_settings = config(clone=self.settings)
		self.depcachedir = os.path.realpath(self.settings.depcachedir)

		if os.environ.get("SANDBOX_ON") == "1":
			# Make api consumers exempt from sandbox violations
			# when doing metadata cache updates.
			sandbox_write = os.environ.get("SANDBOX_WRITE", "").split(":")
			if self.depcachedir not in sandbox_write:
				sandbox_write.append(self.depcachedir)
				os.environ["SANDBOX_WRITE"] = \
					":".join(filter(None, sandbox_write))

		porttrees = [os.path.realpath(porttree_root)]
		porttrees.extend(os.path.realpath(x) for x in \
			shlex_split(self.settings.get('PORTDIR_OVERLAY', '')))
		treemap = {}
		repository_map = {}
		self.treemap = treemap
		self._repository_map = repository_map
		identically_named_paths = {}
		for path in porttrees:
			if path in repository_map:
				continue
			repo_name_path = os.path.join(path, REPO_NAME_LOC)
			try:
				repo_name = codecs.open(
					_unicode_encode(repo_name_path,
					encoding=_encodings['fs'], errors='strict'),
					mode='r', encoding=_encodings['repo.content'],
					errors='replace').readline().strip()
			except EnvironmentError:
				# warn about missing repo_name at some other time, since we
				# don't want to see a warning every time the portage module is
				# imported.
				pass
			else:
				identically_named_path = treemap.get(repo_name)
				if identically_named_path is not None:
					# The earlier one is discarded.
					del repository_map[identically_named_path]
					identically_named_paths[identically_named_path] = repo_name
					if identically_named_path == porttrees[0]:
						# Found another repo with the same name as
						# $PORTDIR, so update porttrees[0] to match.
						porttrees[0] = path
				treemap[repo_name] = path
				repository_map[path] = repo_name

		# Ensure that each repo_name is unique. Later paths override
		# earlier ones that correspond to the same name.
		porttrees = [x for x in porttrees if x not in identically_named_paths]
		ignored_map = {}
		for path, repo_name in identically_named_paths.items():
			ignored_map.setdefault(repo_name, []).append(path)
		self._ignored_repos = tuple((repo_name, tuple(paths)) \
			for repo_name, paths in ignored_map.items())

		self.porttrees = porttrees
		porttree_root = porttrees[0]
		self.porttree_root = porttree_root

		self.eclassdb = eclass_cache.cache(porttree_root)

		# This is used as sanity check for aux_get(). If there is no
		# root eclass dir, we assume that PORTDIR is invalid or
		# missing. This check allows aux_get() to detect a missing
		# portage tree and return early by raising a KeyError.
		self._have_root_eclass_dir = os.path.isdir(
			os.path.join(self.porttree_root, "eclass"))

		self.metadbmodule = self.settings.load_best_module("portdbapi.metadbmodule")

		#if the portdbapi is "frozen", then we assume that we can cache everything (that no updates to it are happening)
		self.xcache = {}
		self.frozen = 0

		self._repo_info = {}
		eclass_dbs = {porttree_root : self.eclassdb}
		local_repo_configs = self.settings._local_repo_configs
		default_loc_repo_config = None
		repo_aliases = {}
		if local_repo_configs is not None:
			default_loc_repo_config = local_repo_configs.get('DEFAULT')
			for repo_name, loc_repo_conf in local_repo_configs.items():
				if loc_repo_conf.aliases is not None:
					for alias in loc_repo_conf.aliases:
						overridden_alias = repo_aliases.get(alias)
						if overridden_alias is not None:
							writemsg_level(_("!!! Alias '%s' " \
								"created for '%s' overrides " \
								"'%s' alias in " \
								"'%s'\n") % (alias, repo_name,
								overridden_alias,
								self.settings._local_repo_conf_path),
								level=logging.WARNING, noiselevel=-1)
						repo_aliases[alias] = repo_name

		for path in self.porttrees:
			if path in self._repo_info:
				continue

			repo_name = self._repository_map.get(path)

			loc_repo_conf = None
			if local_repo_configs is not None:
				if repo_name is not None:
					loc_repo_conf = local_repo_configs.get(repo_name)
				if loc_repo_conf is None:
					loc_repo_conf = default_loc_repo_config

			layout_filename = os.path.join(path, "metadata/layout.conf")
			layout_file = KeyValuePairFileLoader(layout_filename, None, None)
			layout_data, layout_errors = layout_file.load()
			porttrees = []

			masters = None
			if loc_repo_conf is not None and \
				loc_repo_conf.masters is not None:
				masters = loc_repo_conf.masters
			else:
				masters = layout_data.get('masters', '').split()

			for master_name in masters:
				master_name = repo_aliases.get(master_name, master_name)
				master_path = self.treemap.get(master_name)
				if master_path is None:
					writemsg_level(_("Unavailable repository '%s' " \
						"referenced by masters entry in '%s'\n") % \
						(master_name, layout_filename),
						level=logging.ERROR, noiselevel=-1)
				else:
					porttrees.append(master_path)

			if not porttrees and path != porttree_root:
				# Make PORTDIR the default master, but only if our
				# heuristics suggest that it's necessary.
				profiles_desc = os.path.join(path, 'profiles', 'profiles.desc')
				eclass_dir = os.path.join(path, 'eclass')
				if not os.path.isfile(profiles_desc) or \
					not os.path.isdir(eclass_dir):
					porttrees.append(porttree_root)

			porttrees.append(path)

			if loc_repo_conf is not None and \
					loc_repo_conf.eclass_overrides is not None:
					for other_name in loc_repo_conf.eclass_overrides:
						other_path = self.treemap.get(other_name)
						if other_path is None:
							writemsg_level(_("Unavailable repository '%s' " \
								"referenced by eclass-overrides entry in " \
								"'%s'\n") % (other_name,
								self.settings._local_repo_conf_path),
								level=logging.ERROR, noiselevel=-1)
							continue
						porttrees.append(other_path)

			eclass_db = None
			for porttree in porttrees:
				tree_db = eclass_dbs.get(porttree)
				if tree_db is None:
					tree_db = eclass_cache.cache(porttree)
					eclass_dbs[porttree] = tree_db
				if eclass_db is None:
					eclass_db = tree_db.copy()
				else:
					eclass_db.append(tree_db)

			self._repo_info[path] = _repo_info(repo_name, path, eclass_db)

		self.auxdbmodule = self.settings.load_best_module("portdbapi.auxdbmodule")
		self.auxdb = {}
		self._pregen_auxdb = {}
		self._init_cache_dirs()
		depcachedir_w_ok = os.access(self.depcachedir, os.W_OK)
		cache_kwargs = {
			'gid'     : portage_gid,
			'perms'   : 0o664
		}

		if secpass < 1:
			# portage_gid is irrelevant, so just obey umask
			cache_kwargs['gid']   = -1
			cache_kwargs['perms'] = -1

		# XXX: REMOVE THIS ONCE UNUSED_0 IS YANKED FROM auxdbkeys
		# ~harring
		filtered_auxdbkeys = [x for x in auxdbkeys if not x.startswith("UNUSED_0")]
		filtered_auxdbkeys.sort()
		from portage.cache import metadata_overlay, volatile
		if not depcachedir_w_ok:
			for x in self.porttrees:
				db_ro = self.auxdbmodule(self.depcachedir, x,
					filtered_auxdbkeys, gid=portage_gid, readonly=True)
				self.auxdb[x] = metadata_overlay.database(
					self.depcachedir, x, filtered_auxdbkeys,
					gid=portage_gid, db_rw=volatile.database,
					db_ro=db_ro)
		else:
			for x in self.porttrees:
				if x in self.auxdb:
					continue
				# location, label, auxdbkeys
				self.auxdb[x] = self.auxdbmodule(
					self.depcachedir, x, filtered_auxdbkeys, **cache_kwargs)
				if self.auxdbmodule is metadata_overlay.database:
					self.auxdb[x].db_ro.ec = self._repo_info[x].eclass_db
		if "metadata-transfer" not in self.settings.features:
			for x in self.porttrees:
				if x in self._pregen_auxdb:
					continue
				if os.path.isdir(os.path.join(x, "metadata", "cache")):
					self._pregen_auxdb[x] = self.metadbmodule(
						x, "metadata/cache", filtered_auxdbkeys, readonly=True)
					try:
						self._pregen_auxdb[x].ec = self._repo_info[x].eclass_db
					except AttributeError:
						pass
		# Selectively cache metadata in order to optimize dep matching.
		self._aux_cache_keys = set(
			["DEPEND", "EAPI", "INHERITED", "IUSE", "KEYWORDS", "LICENSE",
			"PDEPEND", "PROPERTIES", "PROVIDE", "RDEPEND", "repository",
			"RESTRICT", "SLOT", "DEFINED_PHASES"])

		self._aux_cache = {}
		self._broken_ebuilds = set()

	def _init_cache_dirs(self):
		"""Create /var/cache/edb/dep and adjust permissions for the portage
		group."""

		dirmode  = 0o2070
		filemode =   0o60
		modemask =    0o2

		try:
			ensure_dirs(self.depcachedir, gid=portage_gid,
				mode=dirmode, mask=modemask)
		except PortageException as e:
			pass

	def close_caches(self):
		if not hasattr(self, "auxdb"):
			# unhandled exception thrown from constructor
			return
		for x in self.auxdb:
			self.auxdb[x].sync()
		self.auxdb.clear()

	def flush_cache(self):
		for x in self.auxdb.values():
			x.sync()

	def findLicensePath(self, license_name):
		mytrees = self.porttrees[:]
		mytrees.reverse()
		for x in mytrees:
			license_path = os.path.join(x, "licenses", license_name)
			if os.access(license_path, os.R_OK):
				return license_path
		return None

	def findname(self,mycpv):
		return self.findname2(mycpv)[0]

	def getRepositoryPath(self, repository_id):
		"""
		This function is required for GLEP 42 compliance; given a valid repository ID
		it must return a path to the repository
		TreeMap = { id:path }
		"""
		if repository_id in self.treemap:
			return self.treemap[repository_id]
		return None

	def getRepositoryName(self, canonical_repo_path):
		"""
		This is the inverse of getRepositoryPath().
		@param canonical_repo_path: the canonical path of a repository, as
			resolved by os.path.realpath()
		@type canonical_repo_path: String
		@returns: The repo_name for the corresponding repository, or None
			if the path does not correspond a known repository
		@rtype: String or None
		"""
		return self._repository_map.get(canonical_repo_path)

	def getRepositories(self):
		"""
		This function is required for GLEP 42 compliance; it will return a list of
		repository IDs
		TreeMap = {id: path}
		"""
		return [k for k in self.treemap if k]

	def findname2(self, mycpv, mytree=None):
		""" 
		Returns the location of the CPV, and what overlay it was in.
		Searches overlays first, then PORTDIR; this allows us to return the first
		matching file.  As opposed to starting in portdir and then doing overlays
		second, we would have to exhaustively search the overlays until we found
		the file we wanted.
		"""
		if not mycpv:
			return (None, 0)
		mysplit = mycpv.split("/")
		psplit = pkgsplit(mysplit[1])
		if psplit is None or len(mysplit) != 2:
			raise InvalidPackageName(mycpv)

		# For optimal performace in this hot spot, we do manual unicode
		# handling here instead of using the wrapped os module.
		encoding = _encodings['fs']
		errors = 'strict'

		if mytree:
			mytrees = [mytree]
		else:
			mytrees = self.porttrees[:]
			mytrees.reverse()

		relative_path = mysplit[0] + _os.sep + psplit[0] + _os.sep + \
			mysplit[1] + ".ebuild"

		for x in mytrees:
			filename = x + _os.sep + relative_path
			if _os.access(_unicode_encode(filename,
				encoding=encoding, errors=errors), _os.R_OK):
				return (filename, x)
		return (None, 0)

	def _metadata_process(self, cpv, ebuild_path, repo_path):
		"""
		Create an EbuildMetadataPhase instance to generate metadata for the
		give ebuild.
		@rtype: EbuildMetadataPhase
		@returns: A new EbuildMetadataPhase instance, or None if the
			metadata cache is already valid.
		"""
		metadata, st, emtime = self._pull_valid_cache(cpv, ebuild_path, repo_path)
		if metadata is not None:
			return None

		import _emerge
		process = _emerge.EbuildMetadataPhase(cpv=cpv, ebuild_path=ebuild_path,
			ebuild_mtime=emtime, metadata_callback=self._metadata_callback,
			portdb=self, repo_path=repo_path, settings=self.doebuild_settings)
		return process

	def _metadata_callback(self, cpv, ebuild_path, repo_path, metadata, mtime):

		i = metadata
		if hasattr(metadata, "items"):
			i = iter(metadata.items())
		metadata = dict(i)

		if metadata.get("INHERITED", False):
			metadata["_eclasses_"] = self._repo_info[repo_path
				].eclass_db.get_eclass_data(metadata["INHERITED"].split())
		else:
			metadata["_eclasses_"] = {}

		metadata.pop("INHERITED", None)
		metadata["_mtime_"] = mtime

		eapi = metadata.get("EAPI")
		if not eapi or not eapi.strip():
			eapi = "0"
			metadata["EAPI"] = eapi
		if not eapi_is_supported(eapi):
			for k in set(metadata).difference(("_mtime_", "_eclasses_")):
				metadata[k] = ""
			metadata["EAPI"] = "-" + eapi.lstrip("-")

		self.auxdb[repo_path][cpv] = metadata
		return metadata

	def _pull_valid_cache(self, cpv, ebuild_path, repo_path):
		try:
			# Don't use unicode-wrapped os module, for better performance.
			st = _os.stat(_unicode_encode(ebuild_path,
				encoding=_encodings['fs'], errors='strict'))
			emtime = st[stat.ST_MTIME]
		except OSError:
			writemsg(_("!!! aux_get(): ebuild for " \
				"'%s' does not exist at:\n") % (cpv,), noiselevel=-1)
			writemsg("!!!            %s\n" % ebuild_path, noiselevel=-1)
			raise KeyError(cpv)

		# Pull pre-generated metadata from the metadata/cache/
		# directory if it exists and is valid, otherwise fall
		# back to the normal writable cache.
		auxdbs = []
		pregen_auxdb = self._pregen_auxdb.get(repo_path)
		if pregen_auxdb is not None:
			auxdbs.append(pregen_auxdb)
		auxdbs.append(self.auxdb[repo_path])
		eclass_db = self._repo_info[repo_path].eclass_db

		doregen = True
		for auxdb in auxdbs:
			try:
				metadata = auxdb[cpv]
			except KeyError:
				pass
			except CacheError:
				if auxdb is not pregen_auxdb:
					try:
						del auxdb[cpv]
					except KeyError:
						pass
					except CacheError:
						pass
			else:
				eapi = metadata.get('EAPI', '').strip()
				if not eapi:
					eapi = '0'
				if not (eapi[:1] == '-' and eapi_is_supported(eapi[1:])) and \
					emtime == metadata['_mtime_'] and \
					eclass_db.is_eclass_data_valid(metadata['_eclasses_']):
					doregen = False

			if not doregen:
				break

		if doregen:
			metadata = None

		return (metadata, st, emtime)

	def aux_get(self, mycpv, mylist, mytree=None):
		"stub code for returning auxilliary db information, such as SLOT, DEPEND, etc."
		'input: "sys-apps/foo-1.0",["SLOT","DEPEND","HOMEPAGE"]'
		'return: ["0",">=sys-libs/bar-1.0","http://www.foo.com"] or raise KeyError if error'
		cache_me = False
		if not mytree:
			cache_me = True
		if not mytree and not self._known_keys.intersection(
			mylist).difference(self._aux_cache_keys):
			aux_cache = self._aux_cache.get(mycpv)
			if aux_cache is not None:
				return [aux_cache.get(x, "") for x in mylist]
			cache_me = True
		global auxdbkeys, auxdbkeylen
		try:
			cat, pkg = mycpv.split("/", 1)
		except ValueError:
			# Missing slash. Can't find ebuild so raise KeyError.
			raise KeyError(mycpv)

		myebuild, mylocation = self.findname2(mycpv, mytree)

		if not myebuild:
			writemsg("!!! aux_get(): %s\n" % \
				_("ebuild not found for '%s'") % mycpv, noiselevel=1)
			raise KeyError(mycpv)

		mydata, st, emtime = self._pull_valid_cache(mycpv, myebuild, mylocation)
		doregen = mydata is None

		if doregen:
			if myebuild in self._broken_ebuilds:
				raise KeyError(mycpv)
			if not self._have_root_eclass_dir:
				raise KeyError(mycpv)

			self.doebuild_settings.setcpv(mycpv)
			mydata = {}
			eapi = None

			if eapi is None and \
				'parse-eapi-ebuild-head' in self.doebuild_settings.features:
				eapi = portage._parse_eapi_ebuild_head(codecs.open(
					_unicode_encode(myebuild,
					encoding=_encodings['fs'], errors='strict'),
					mode='r', encoding=_encodings['repo.content'],
					errors='replace'))

			if eapi is not None:
				self.doebuild_settings.configdict['pkg']['EAPI'] = eapi

			if eapi is not None and not portage.eapi_is_supported(eapi):
				mydata['EAPI'] = eapi
			else:
				myret = doebuild(myebuild, "depend",
					self.doebuild_settings["ROOT"], self.doebuild_settings,
					dbkey=mydata, tree="porttree", mydbapi=self)
				if myret != os.EX_OK:
					self._broken_ebuilds.add(myebuild)
					raise KeyError(mycpv)

			self._metadata_callback(
				mycpv, myebuild, mylocation, mydata, emtime)

			if mydata.get("INHERITED", False):
				mydata["_eclasses_"] = self._repo_info[mylocation
					].eclass_db.get_eclass_data(mydata["INHERITED"].split())
			else:
				mydata["_eclasses_"] = {}

		# do we have a origin repository name for the current package
		mydata["repository"] = self._repository_map.get(mylocation, "")

		mydata["INHERITED"] = ' '.join(mydata.get("_eclasses_", []))
		mydata["_mtime_"] = st[stat.ST_MTIME]

		eapi = mydata.get("EAPI")
		if not eapi:
			eapi = "0"
			mydata["EAPI"] = eapi
		if not eapi_is_supported(eapi):
			for k in set(mydata).difference(("_mtime_", "_eclasses_")):
				mydata[k] = ""
			mydata["EAPI"] = "-" + eapi.lstrip("-")

		#finally, we look at our internal cache entry and return the requested data.
		returnme = [mydata.get(x, "") for x in mylist]

		if cache_me:
			aux_cache = {}
			for x in self._aux_cache_keys:
				aux_cache[x] = mydata.get(x, "")
			self._aux_cache[mycpv] = aux_cache

		return returnme

	def getFetchMap(self, mypkg, useflags=None, mytree=None):
		"""
		Get the SRC_URI metadata as a dict which maps each file name to a
		set of alternative URIs.

		@param mypkg: cpv for an ebuild
		@type mypkg: String
		@param useflags: a collection of enabled USE flags, for evaluation of
			conditionals
		@type useflags: set, or None to enable all conditionals
		@param mytree: The canonical path of the tree in which the ebuild
			is located, or None for automatic lookup
		@type mypkg: String
		@returns: A dict which maps each file name to a set of alternative
			URIs.
		@rtype: dict
		"""

		try:
			eapi, myuris = self.aux_get(mypkg,
				["EAPI", "SRC_URI"], mytree=mytree)
		except KeyError:
			# Convert this to an InvalidDependString exception since callers
			# already handle it.
			raise portage.exception.InvalidDependString(
				"getFetchMap(): aux_get() error reading "+mypkg+"; aborting.")

		if not eapi_is_supported(eapi):
			# Convert this to an InvalidDependString exception
			# since callers already handle it.
			raise portage.exception.InvalidDependString(
				"getFetchMap(): '%s' has unsupported EAPI: '%s'" % \
				(mypkg, eapi.lstrip("-")))

		myuris = paren_reduce(myuris)
		_src_uri_validate(mypkg, eapi, myuris)
		myuris = use_reduce(myuris, uselist=useflags,
			matchall=(useflags is None))
		myuris = flatten(myuris)

		uri_map = OrderedDict()

		myuris.reverse()
		while myuris:
			uri = myuris.pop()
			if myuris and myuris[-1] == "->":
				operator = myuris.pop()
				distfile = myuris.pop()
			else:
				distfile = os.path.basename(uri)
				if not distfile:
					raise portage.exception.InvalidDependString(
						("getFetchMap(): '%s' SRC_URI has no file " + \
						"name: '%s'") % (mypkg, uri))

			uri_set = uri_map.get(distfile)
			if uri_set is None:
				uri_set = set()
				uri_map[distfile] = uri_set
			uri_set.add(uri)
			uri = None
			operator = None

		return uri_map

	def getfetchsizes(self, mypkg, useflags=None, debug=0):
		# returns a filename:size dictionnary of remaining downloads
		myebuild = self.findname(mypkg)
		if myebuild is None:
			raise AssertionError("ebuild not found for '%s'" % mypkg)
		pkgdir = os.path.dirname(myebuild)
		mf = Manifest(pkgdir, self.settings["DISTDIR"])
		checksums = mf.getDigests()
		if not checksums:
			if debug: 
				writemsg("[empty/missing/bad digest]: %s\n" % (mypkg,))
			return {}
		filesdict={}
		myfiles = self.getFetchMap(mypkg, useflags=useflags)
		#XXX: maybe this should be improved: take partial downloads
		# into account? check checksums?
		for myfile in myfiles:
			try:
				fetch_size = int(checksums[myfile]["size"])
			except (KeyError, ValueError):
				if debug:
					writemsg(_("[bad digest]: missing %(file)s for %(pkg)s\n") % {"file":myfile, "pkg":mypkg})
				continue
			file_path = os.path.join(self.settings["DISTDIR"], myfile)
			mystat = None
			try:
				mystat = os.stat(file_path)
			except OSError as e:
				pass
			if mystat is None:
				existing_size = 0
				ro_distdirs = self.settings.get("PORTAGE_RO_DISTDIRS")
				if ro_distdirs is not None:
					for x in shlex_split(ro_distdirs):
						try:
							mystat = os.stat(os.path.join(x, myfile))
						except OSError:
							pass
						else:
							if mystat.st_size == fetch_size:
								existing_size = fetch_size
								break
			else:
				existing_size = mystat.st_size
			remaining_size = fetch_size - existing_size
			if remaining_size > 0:
				# Assume the download is resumable.
				filesdict[myfile] = remaining_size
			elif remaining_size < 0:
				# The existing file is too large and therefore corrupt.
				filesdict[myfile] = int(checksums[myfile]["size"])
		return filesdict

	def fetch_check(self, mypkg, useflags=None, mysettings=None, all=False):
		if all:
			useflags = None
		elif useflags is None:
			if mysettings:
				useflags = mysettings["USE"].split()
		myfiles = self.getFetchMap(mypkg, useflags=useflags)
		myebuild = self.findname(mypkg)
		if myebuild is None:
			raise AssertionError("ebuild not found for '%s'" % mypkg)
		pkgdir = os.path.dirname(myebuild)
		mf = Manifest(pkgdir, self.settings["DISTDIR"])
		mysums = mf.getDigests()

		failures = {}
		for x in myfiles:
			if not mysums or x not in mysums:
				ok     = False
				reason = _("digest missing")
			else:
				try:
					ok, reason = portage.checksum.verify_all(
						os.path.join(self.settings["DISTDIR"], x), mysums[x])
				except FileNotFound as e:
					ok = False
					reason = _("File Not Found: '%s'") % (e,)
			if not ok:
				failures[x] = reason
		if failures:
			return False
		return True

	def cpv_exists(self, mykey):
		"Tells us whether an actual ebuild exists on disk (no masking)"
		cps2 = mykey.split("/")
		cps = catpkgsplit(mykey, silent=0)
		if not cps:
			#invalid cat/pkg-v
			return 0
		if self.findname(cps[0] + "/" + cps2[1]):
			return 1
		else:
			return 0

	def cp_all(self, categories=None, trees=None):
		"""
		This returns a list of all keys in our tree or trees
		@param categories: optional list of categories to search or 
			defaults to self.settings.categories
		@param trees: optional list of trees to search the categories in or
			defaults to self.porttrees
		@rtype list of [cat/pkg,...]
		"""
		d = {}
		if categories is None:
			categories = self.settings.categories
		if trees is None:
			trees = self.porttrees
		for x in categories:
			for oroot in trees:
				for y in listdir(oroot+"/"+x, EmptyOnError=1, ignorecvs=1, dirsonly=1):
					if not self._pkg_dir_name_re.match(y) or \
						y == "CVS":
						continue
					d[x+"/"+y] = None
		l = list(d)
		l.sort()
		return l

	def cp_list(self, mycp, use_cache=1, mytree=None):
		if self.frozen and mytree is None:
			cachelist = self.xcache["cp-list"].get(mycp)
			if cachelist is not None:
				# Try to propagate this to the match-all cache here for
				# repoman since he uses separate match-all caches for each
				# profile (due to old-style virtuals). Do not propagate
				# old-style virtuals since cp_list() doesn't expand them.
				if not (not cachelist and mycp.startswith("virtual/")):
					self.xcache["match-all"][mycp] = cachelist
				return cachelist[:]
		mysplit = mycp.split("/")
		invalid_category = mysplit[0] not in self._categories
		d={}
		if mytree:
			if isinstance(mytree, str):
				mytrees = [mytree]
			elif not isinstance(mytree, list):
				raise  AssertionError("Invalid input type: %s" %str(type(mytree)))
		else:
			mytrees = self.porttrees
		for oroot in mytrees:
			try:
				file_list = os.listdir(os.path.join(oroot, mycp))
			except OSError:
				continue
			for x in file_list:
				pf = None
				if x[-7:] == '.ebuild':
					pf = x[:-7]

				if pf is not None:
					ps = pkgsplit(pf)
					if not ps:
						writemsg(_("\nInvalid ebuild name: %s\n") % \
							os.path.join(oroot, mycp, x), noiselevel=-1)
						continue
					if ps[0] != mysplit[1]:
						writemsg(_("\nInvalid ebuild name: %s\n") % \
							os.path.join(oroot, mycp, x), noiselevel=-1)
						continue
					ver_match = ver_regexp.match("-".join(ps[1:]))
					if ver_match is None or not ver_match.groups():
						writemsg(_("\nInvalid ebuild version: %s\n") % \
							os.path.join(oroot, mycp, x), noiselevel=-1)
						continue
					d[mysplit[0]+"/"+pf] = None
		if invalid_category and d:
			writemsg(_("\n!!! '%s' has a category that is not listed in " \
				"%setc/portage/categories\n") % \
				(mycp, self.settings["PORTAGE_CONFIGROOT"]), noiselevel=-1)
			mylist = []
		else:
			mylist = list(d)
		# Always sort in ascending order here since it's handy
		# and the result can be easily cached and reused.
		self._cpv_sort_ascending(mylist)
		if self.frozen and mytree is None:
			cachelist = mylist[:]
			self.xcache["cp-list"][mycp] = cachelist
			# Do not propagate old-style virtuals since
			# cp_list() doesn't expand them.
			if not (not cachelist and mycp.startswith("virtual/")):
				self.xcache["match-all"][mycp] = cachelist
		return mylist

	def freeze(self):
		for x in "bestmatch-visible", "cp-list", "list-visible", "match-all", \
			"match-visible", "minimum-all", "minimum-visible":
			self.xcache[x]={}
		self.frozen=1

	def melt(self):
		self.xcache = {}
		self.frozen = 0

	def xmatch(self,level,origdep,mydep=None,mykey=None,mylist=None):
		"caching match function; very trick stuff"
		#if no updates are being made to the tree, we can consult our xcache...
		if self.frozen:
			try:
				return self.xcache[level][origdep][:]
			except KeyError:
				pass

		if not mydep:
			#this stuff only runs on first call of xmatch()
			#create mydep, mykey from origdep
			mydep = dep_expand(origdep, mydb=self, settings=self.settings)
			mykey = mydep.cp

		if level == "list-visible":
			#a list of all visible packages, not called directly (just by xmatch())
			#myval = self.visible(self.cp_list(mykey))

			myval = self.gvisible(self.visible(self.cp_list(mykey)))
		elif level == "minimum-all":
			# Find the minimum matching version. This is optimized to
			# minimize the number of metadata accesses (improves performance
			# especially in cases where metadata needs to be generated).
			cpv_iter = iter(self.cp_list(mykey))
			if mydep != mykey:
				cpv_iter = self._iter_match(mydep, cpv_iter)
			try:
				myval = next(cpv_iter)
			except StopIteration:
				myval = ""

		elif level in ("minimum-visible", "bestmatch-visible"):
			# Find the minimum matching visible version. This is optimized to
			# minimize the number of metadata accesses (improves performance
			# especially in cases where metadata needs to be generated).
			if mydep == mykey:
				mylist = self.cp_list(mykey)
			else:
				mylist = match_from_list(mydep, self.cp_list(mykey))
			myval = ""
			settings = self.settings
			local_config = settings.local_config
			aux_keys = list(self._aux_cache_keys)
			if level == "minimum-visible":
				iterfunc = iter
			else:
				iterfunc = reversed
			for cpv in iterfunc(mylist):
				try:
					metadata = dict(zip(aux_keys,
						self.aux_get(cpv, aux_keys)))
				except KeyError:
					# ebuild masked by corruption
					continue
				if not eapi_is_supported(metadata["EAPI"]):
					continue
				if mydep.slot and mydep.slot != metadata["SLOT"]:
					continue
				if settings._getMissingKeywords(cpv, metadata):
					continue
				if settings._getMaskAtom(cpv, metadata):
					continue
				if settings._getProfileMaskAtom(cpv, metadata):
					continue
				if local_config:
					metadata["USE"] = ""
					if "?" in metadata["LICENSE"] or "?" in metadata["PROPERTIES"]:
						self.doebuild_settings.setcpv(cpv, mydb=metadata)
						metadata["USE"] = self.doebuild_settings.get("USE", "")
					try:
						if settings._getMissingLicenses(cpv, metadata):
							continue
						if settings._getMissingProperties(cpv, metadata):
							continue
					except InvalidDependString:
						continue
				if mydep.use:
					has_iuse = False
					for has_iuse in self._iter_match_use(mydep, [cpv]):
						break
					if not has_iuse:
						continue
				myval = cpv
				break
		elif level == "bestmatch-list":
			#dep match -- find best match but restrict search to sublist
			#no point in calling xmatch again since we're not caching list deps

			myval = best(list(self._iter_match(mydep, mylist)))
		elif level == "match-list":
			#dep match -- find all matches but restrict search to sublist (used in 2nd half of visible())

			myval = list(self._iter_match(mydep, mylist))
		elif level == "match-visible":
			#dep match -- find all visible matches
			#get all visible packages, then get the matching ones

			myval = list(self._iter_match(mydep,
				self.xmatch("list-visible", mykey, mydep=mykey, mykey=mykey)))
		elif level == "match-all":
			#match *all* visible *and* masked packages
			if mydep == mykey:
				myval = self.cp_list(mykey)
			else:
				myval = list(self._iter_match(mydep, self.cp_list(mykey)))
		else:
			raise AssertionError(
				"Invalid level argument: '%s'" % level)

		if self.frozen and (level not in ["match-list", "bestmatch-list"]):
			self.xcache[level][mydep] = myval
			if origdep and origdep != mydep:
				self.xcache[level][origdep] = myval
		return myval[:]

	def match(self, mydep, use_cache=1):
		return self.xmatch("match-visible", mydep)

	def visible(self, mylist):
		"""two functions in one.  Accepts a list of cpv values and uses the package.mask *and*
		packages file to remove invisible entries, returning remaining items.  This function assumes
		that all entries in mylist have the same category and package name."""
		if not mylist:
			return []

		db_keys = ["SLOT"]
		visible = []
		getMaskAtom = self.settings._getMaskAtom
		getProfileMaskAtom = self.settings._getProfileMaskAtom
		for cpv in mylist:
			try:
				metadata = dict(zip(db_keys, self.aux_get(cpv, db_keys)))
			except KeyError:
				# masked by corruption
				continue
			if not metadata["SLOT"]:
				continue
			if getMaskAtom(cpv, metadata):
				continue
			if getProfileMaskAtom(cpv, metadata):
				continue
			visible.append(cpv)
		return visible

	def gvisible(self,mylist):
		"strip out group-masked (not in current group) entries"

		if mylist is None:
			return []
		newlist=[]
		aux_keys = list(self._aux_cache_keys)
		metadata = {}
		local_config = self.settings.local_config
		chost = self.settings.get('CHOST', '')
		accept_chost = self.settings._accept_chost
		for mycpv in mylist:
			metadata.clear()
			try:
				metadata.update(zip(aux_keys, self.aux_get(mycpv, aux_keys)))
			except KeyError:
				continue
			except PortageException as e:
				writemsg("!!! Error: aux_get('%s', %s)\n" % (mycpv, aux_keys),
					noiselevel=-1)
				writemsg("!!! %s\n" % (e,), noiselevel=-1)
				del e
				continue
			eapi = metadata["EAPI"]
			if not eapi_is_supported(eapi):
				continue
			if _eapi_is_deprecated(eapi):
				continue
			if self.settings._getMissingKeywords(mycpv, metadata):
				continue
			if local_config:
				metadata['CHOST'] = chost
				if not accept_chost(mycpv, metadata):
					continue
				metadata["USE"] = ""
				if "?" in metadata["LICENSE"] or "?" in metadata["PROPERTIES"]:
					self.doebuild_settings.setcpv(mycpv, mydb=metadata)
					metadata['USE'] = self.doebuild_settings['PORTAGE_USE']
				try:
					if self.settings._getMissingLicenses(mycpv, metadata):
						continue
					if self.settings._getMissingProperties(mycpv, metadata):
						continue
				except InvalidDependString:
					continue
			newlist.append(mycpv)
		return newlist

def close_portdbapi_caches():
	for i in portdbapi.portdbapi_instances:
		i.close_caches()

class portagetree(object):
	def __init__(self, root="/", virtual=None, settings=None):
		"""
		Constructor for a PortageTree
		
		@param root: ${ROOT}, defaults to '/', see make.conf(5)
		@type root: String/Path
		@param virtual: UNUSED
		@type virtual: No Idea
		@param settings: Portage Configuration object (portage.settings)
		@type settings: Instance of portage.config
		"""

		if True:
			self.root = root
			if settings is None:
				from portage import settings
			self.settings = settings
			self.portroot = settings["PORTDIR"]
			self.virtual = virtual
			self.dbapi = portdbapi(mysettings=settings)

	def dep_bestmatch(self,mydep):
		"compatibility method"
		mymatch = self.dbapi.xmatch("bestmatch-visible",mydep)
		if mymatch is None:
			return ""
		return mymatch

	def dep_match(self,mydep):
		"compatibility method"
		mymatch = self.dbapi.xmatch("match-visible",mydep)
		if mymatch is None:
			return []
		return mymatch

	def exists_specific(self,cpv):
		return self.dbapi.cpv_exists(cpv)

	def getallnodes(self):
		"""new behavior: these are all *unmasked* nodes.  There may or may not be available
		masked package for nodes in this nodes list."""
		return self.dbapi.cp_all()

	def getname(self, pkgname):
		"returns file location for this particular package (DEPRECATED)"
		if not pkgname:
			return ""
		mysplit = pkgname.split("/")
		psplit = pkgsplit(mysplit[1])
		return "/".join([self.portroot, mysplit[0], psplit[0], mysplit[1]])+".ebuild"

	def depcheck(self, mycheck, use="yes", myusesplit=None):
		return dep_check(mycheck, self.dbapi, use=use, myuse=myusesplit)

	def getslot(self,mycatpkg):
		"Get a slot for a catpkg; assume it exists."
		myslot = ""
		try:
			myslot = self.dbapi.aux_get(mycatpkg, ["SLOT"])[0]
		except SystemExit as e:
			raise
		except Exception as e:
			pass
		return myslot

class FetchlistDict(Mapping):
	"""
	This provide a mapping interface to retrieve fetch lists. It's used
	to allow portage.manifest.Manifest to access fetch lists via a standard
	mapping interface rather than use the dbapi directly.
	"""
	def __init__(self, pkgdir, settings, mydbapi):
		"""pkgdir is a directory containing ebuilds and settings is passed into
		portdbapi.getfetchlist for __getitem__ calls."""
		self.pkgdir = pkgdir
		self.cp = os.sep.join(pkgdir.split(os.sep)[-2:])
		self.settings = settings
		self.mytree = os.path.realpath(os.path.dirname(os.path.dirname(pkgdir)))
		self.portdb = mydbapi

	def __getitem__(self, pkg_key):
		"""Returns the complete fetch list for a given package."""
		return list(self.portdb.getFetchMap(pkg_key, mytree=self.mytree))

	def __contains__(self, cpv):
		return cpv in self.__iter__()

	def has_key(self, pkg_key):
		"""Returns true if the given package exists within pkgdir."""
		return pkg_key in self

	def __iter__(self):
		return iter(self.portdb.cp_list(self.cp, mytree=self.mytree))

	def __len__(self):
		"""This needs to be implemented in order to avoid
		infinite recursion in some cases."""
		return len(self.portdb.cp_list(self.cp, mytree=self.mytree))

	def keys(self):
		"""Returns keys for all packages within pkgdir"""
		return self.portdb.cp_list(self.cp, mytree=self.mytree)

	if sys.hexversion >= 0x3000000:
		keys = __iter__
