# Copyright: 2005 Brian Harring <ferringb@gmail.com>
# License: GPL2

# pre merge triggers
# post merge triggers
# ordering?

import os, errno

from pkgcore.fs import contents
from pkgcore.fs import gen_obj as gen_fs_obj
from pkgcore.util.mappings import LazyValDict, ImmutableDict, StackedDict
from pkgcore.util import currying
from pkgcore.merge.triggers import merge_trigger, unmerge_trigger, ldconfig_trigger
from pkgcore.merge import errors

def scan_livefs(cset):
	for x in cset:
		try:
			yield gen_fs_obj(x.location)
		except OSError, oe:
			if oe.errno != errno.ENOENT:
				raise
			del oe


def alias_cset(alias, engine, csets):
	return csets[alias]

class MergeEngine(object):
	REPLACING_MODE = 0
	INSTALL_MODE = 1
	UNINSTALL_MODE = 2

	install_hooks = dict((x, []) for x in ["sanity_check", "pre_merge", "merge", "post_merge", "final"])
	uninstall_hooks = dict((x, []) for x in ["sanity_check", "pre_unmerge", "unmerge", "post_unmerge", "final"])
	replace_hooks = dict((x, []) for x in set(install_hooks.keys() + uninstall_hooks.keys()))

	install_hooks["merge"].append(merge_trigger)
	uninstall_hooks["unmerge"].append(unmerge_trigger)
	replace_hooks["merge"].append(merge_trigger)
	replace_hooks["unmerge"].append(unmerge_trigger)
	install_hooks["post_merge"].append(ldconfig_trigger)
	uninstall_hooks["post_unmerge"].append(ldconfig_trigger)

	# this should be a symlink update only
	replace_hooks["post_merge"].append(ldconfig_trigger)
	replace_hooks["post_unmerge"].append(ldconfig_trigger)


	install_csets = {"install_existing":"get_livefs_intersect_cset"}
	uninstall_csets = dict(install_csets)
	replace_csets = dict(install_csets)

	install_csets.update({}.fromkeys(["install", "replace", "modifying"],
		currying.pre_curry(alias_cset, "new_cset")))
	uninstall_csets.update({}.fromkeys(["uninstall", "modifying"],
		currying.pre_curry(alias_cset, "old_cset")))
	replace_csets["install"] = currying.pre_curry(alias_cset, "new_cset")
	replace_csets["modifying"] = lambda e, c: c["install"].intersection(c["uninstall"])
	replace_csets["uninstall"] = "get_remove_cset"
	replace_csets["replace"] = "get_replace_cset"
	replace_csets["install_existing"] = "get_livefs_intersect_cset"

	install_csets_preserve = ["new_cset"]
	uninstall_csets_preserve = ["old_cset"]
	replace_csets_preserve = ["new_cset", "old_cset"]

	def __init__(self, mode, hooks, csets, preserves, offset=None):
		self.mode = mode
		self.hooks = ImmutableDict((x, []) for x in hooks)

		self.preserve_csets = []
		self.cset_sources = {}
		# instantiate these seperately so their values are preserved
		self.preserved_csets = LazyValDict(self.preserve_csets, self._get_cset_source)
		for k,v in csets.iteritems():
			if isinstance(v, basestring):
				v = getattr(self, v, v)
			if not callable(v):
				raise TypeError("cset values must be either the string name of existing methods, or callables "
				"(got %s)" % v)
			if k in preserves:
				self.add_preserved_cset(k, v)
			else:
				self.add_cset(k, v)

		self.offset = offset
		for k, v in hooks.iteritems():
			self.add_triggers(k, *v)

		self.regenerate_csets()
		for x in hooks.keys():
			setattr(self, x, currying.pre_curry(self.execute_hook, x))

	@classmethod
	def install(cls, pkg, offset=None):
		hooks = dict((k, [y() for y in v]) for (k,v) in cls.install_hooks.iteritems())
		csets = dict(cls.install_csets)
		if "new_cset" not in csets:
			csets["new_cset"] = currying.post_curry(cls.get_pkg_contents, pkg)
		o = cls(cls.INSTALL_MODE, hooks, csets, cls.install_csets_preserve, offset=offset)

		if offset:
			# wrap the results of new_cset to pass through an offset generator
			o.cset_sources["new_cset"] = currying.post_curry(o.generate_offset_cset, o.cset_sources["new_cset"])

		o.new = pkg
		return o

	@classmethod
	def uninstall(cls, pkg, offset=None):
		hooks = dict((k, [y() for y in v]) for (k,v) in cls.uninstall_hooks.iteritems())
		csets = dict(cls.uninstall_csets)
		if "old_cset" not in csets:
			csets["old_cset"] = currying.post_curry(cls.get_pkg_contents, pkg)
		o = cls(cls.UNINSTALL_MODE, hooks, csets, cls.uninstall_csets_preserve, offset=offset)

		if offset:
			# wrap the results of new_cset to pass through an offset generator
			o.cset_sources["old_cset"] = currying.post_curry(o.generate_offset_cset, o.cset_sources["old_cset"])

		o.old = pkg
		return o

	@classmethod
	def replace(cls, old, new, offset=None):
		hooks = dict((k, [y() for y in v]) for (k,v) in cls.replace_hooks.iteritems())
		csets = dict(cls.replace_csets)

		for v,k in ((old, "old_cset"), (new, "new_cset")):
			if k not in csets:
				csets[k] = currying.post_curry(cls.get_pkg_contents, v)

		o = cls(cls.UNINSTALL_MODE, hooks, csets, cls.replace_csets_preserve, offset=offset)

		if offset:
			for k in ("old_cset", "new_cset"):
				# wrap the results of new_cset to pass through an offset generator
				o.cset_sources[k] = currying.post_curry(o.generate_offset_cset, o.cset_sources[k])

		o.old = old
		o.new = new
		return o

	def execute_hook(self, hook):
		self.regenerate_csets()
		for x in self.hooks[hook]:
			# error checking needed here.
			x(self, self.csets)

	def regenerate_csets(self):
		self.csets = StackedDict(self.preserved_csets,
			LazyValDict(self.cset_sources, self._get_cset_source))

	def _get_cset_source(self, key):
		return self.cset_sources[key](self, self.csets)

	def add_preserved_cset(self, cset_name, func):
		self.add_cset(cset_name, func)
		self.preserve_csets.append(cset_name)

	def add_cset(self, cset_name, func):
		if not callable(func):
			raise TypeError("func must be a callable")
		if not isinstance(cset_name, basestring):
			raise TypeError("cset_name must be a string")
		self.cset_sources[cset_name] = func


	def add_triggers(self, hook_name, *triggers):
		if hook_name not in self.hooks:
			raise KeyError("%s isn't a known hook" % hook_name)

		for x in triggers:
			for rcs in x.required_csets:
				if rcs not in self.cset_sources:
					if isinstance(rcs, basestring):
						raise errors.TriggerUnknownCset(rcs)
#					elif isinstance(rcs, (tuple, list)):
#						updates.update([rcs])
#					elif not callable(rcs):
#						raise TriggerUnknownCset(rcs)

		for x in triggers:
			x.register(hook_name, self.hooks[hook_name])

	@staticmethod
	def generate_offset_cset(engine, csets, cset_generator):
		return contents.contentsSet(x.change_location(os.path.join(engine.offset,
			x.location.lstrip(os.path.sep))) for x in cset_generator(engine, csets))

	@staticmethod
	def get_pkg_contents(engine, csets, pkg):
		"""generate the cset of what files shall be merged to the livefs"""
		return pkg.contents

	@staticmethod
	def get_remove_cset(engine, csets):
		"""generate the cset of what files shall be removed from the livefs"""
		return csets["old_cset"].difference(csets["new_cset"])

	@staticmethod
	def get_replace_cset(engine, csets):
		"""generates the cset of what will be replaced going from old -> new pkg"""
		return csets["new_cset"].intersection(csets["old_cset"])

	@staticmethod
	def get_livefs_intersect_cset(engine, csets, default_cset="modifying"):
		"""generates the livefs intersection against a cset"""
		return contents.contentsSet(scan_livefs(csets[default_cset]))


