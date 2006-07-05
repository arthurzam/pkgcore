#!/usr/bin/python

from pkgcore.config import load_config
from pkgcore.package.atom import atom
from pkgcore.util.lists import flatten, stable_unique
from pkgcore.util.repo_utils import get_raw_repos
from pkgcore.util.commandline import generate_restriction, collect_ops
from pkgcore.ebuild import resolver
import sys


def pop_paired_args(args, arg, msg):
	rets = []
	if not isinstance(arg, (tuple, list)):
		arg = [arg]
	for a in arg:
		try:
			while True:
				i = args.index(a)
				args.pop(i)
				if len(args) == i:
					raise Exception("%s needs to be followed by an arg: %s" % (a, msg))
				rets.append(args.pop(i))
		except ValueError:
			pass
	return rets

def pop_arg(args, *arg):

	ret = False
	for a in arg:
		try:
			while True:
				args.remove(a)
				ret = True
		except ValueError:
			pass
	return ret
	

def parse_atom(repo, ignore_failures, *tokens):
	""""
	parse a list of strings returning a list of atoms, filling in categories as needed

	@param repo: L{pkgcore.prototype.tree} instance
	@param tokens: list of strings to parse
	"""
	atoms = []
	for x in tokens:
		a = generate_restriction(x)
		if isinstance(a, atom):
			atoms.append(a)
			continue
		matches = set(pkg.key for pkg in repo.itermatch(a))
		if not matches:
			print "no matches found to %s" % x,a
			if ignore_failures:
				print "skipping %s" % x
				continue
			sys.exit(1)
		if len(matches) > 1:
			print "multiple pkg matches found for %s: %s, %s" % (x, ", ".join(sorted(matches)), a)
			if ignore_failures:
				print "skipping %s" % x
				continue
			sys.exit(2)
		# else we rebuild an atom to include category
		key = list(matches)[0]
		ops, text = collect_ops(x)
		if not ops:
			atoms.append(atom(key))
			continue
		atoms.append(atom(key))
	return atoms	


def main():
	import time
	args = sys.argv[1:]

	if pop_arg(args, "-h", "--help"):
		print "args supported, [-D || --deep], [[-u || --upgrade]] and -s (system|world) [-d || --debug] [ --ignore-failures ] [ --preload-vdb-state ]"
		print "[[-p || --pretend] || [-f || --fetchonly]]"
		print "can specify additional atoms when specifying -s, no atoms/sets available, defaults to sys-apps/portage"
		return 1

	if pop_arg(args, "-d", "--debug"):
		resolver.plan.limiters.add(None)
	
	pretend = pop_arg(args, "-p", "--pretend")
	fetchonly = pop_arg(args, "-f", "--fetchonly")
	
	trigger_pdb = pop_arg(args, "-p", "--pdb")
	empty_vdb = pop_arg(args, "-e", "--empty")
	upgrade = pop_arg(args, "-u", "--upgrade")
	preload_vdb_state = pop_arg(args, None, "--preload-vdb-state")
	ignore_failures = pop_arg(args, None, "--ignore-failures")
	if max and max == upgrade:
		print "can only choose max, or upgrade"
		return 1
	if upgrade:
		resolver_kls = resolver.upgrade_resolver
	else:
		resolver_kls = resolver.min_install_resolver

	deep = bool(pop_arg(args, "-D", "--deep"))

	conf = load_config()

	set_targets = pop_paired_args(args, ["--set", "-s"], "pkg sets to enable")
	if set_targets:
		print "using pkgset(s): %s" % (", ".join("'%s'" % x.strip() for x in set_targets))
	set_targets = [a for t in set_targets for a in conf.pkgset[t]]
	#map(atom, conf.pkgset[l]) for l in set_targets], restriction.base)
	
	domain = conf.domain["livefs domain"]
	vdb, repo = domain.vdb[0], domain.repos[0]
	if not args:
		if set_targets:
			atoms = []
		else:
			print "resolving sys-apps/portage since no atom supplied"
			atoms = [atom("sys-apps/portage")]
	else:
		atoms = parse_atom(repo, ignore_failures, *args)

	if set_targets:
		atoms += set_targets

	atoms = stable_unique(atoms)

	resolver_inst = resolver_kls(vdb, repo, verify_vdb=deep)

	if preload_vdb_state:
		vdb_time = time.time()
		resolver_inst.load_vdb_state()
		vdb_time = time.time() - vdb_time
	else:
		vdb_time = 0.0
	ret = True
	failures = []
	resolve_time = time.time()
	for restrict in atoms:
		print "\ncalling resolve for %s..." % restrict
		ret = resolver_inst.add_atom(restrict)
		if ret:
			print "ret was",ret
			print "resolution failed"
			failures.append(restrict)
			if not ignore_failures:
				break
	resolve_time = time.time() - resolve_time
	if failures:
		print "\nfailures encountered-"
		for restrict in failures:
			print "failed '%s'\npotentials-" % restrict
			match_count = 0
			for r in get_raw_repos(repo):
				l = r.match(restrict)
				if l:
					print "repo %s: [ %s ]" % (r, ", ".join(str(x) for x in l))
					match_count += len(l)
			if not match_count:
				print "no matches found in %s" % repo
			print
			if not ignore_failures:
				return 2

	print "\nbuildplan"
	for op, pkgs in resolver_inst.state.iter_pkg_ops():
		if pkgs[-1].repo.livefs:
			continue
		print "%s %s" % (op.ljust(8), ", ".join(str(y) for y in reversed(pkgs)))
	print
	
	if vdb_time:
		print "spent %.2f seconds preloading vdb state" % vdb_time
	print "result was successfull, 'parently- spent %.2f seconds resolving" % (resolve_time)

	if pretend:
		return 0
		

if __name__ == "__main__":
	main()
