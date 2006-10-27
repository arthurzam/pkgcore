# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2

"""
gentoo/ebuild specific triggers
"""

from pkgcore.merge import triggers, const, errors
from pkgcore.util.file import read_bash_dict, AtomicWriteFile
from pkgcore.fs import livefs
from pkgcore.util.osutils import normpath
from pkgcore.util.currying import partial
from pkgcore.restrictions import values
from pkgcore.util.osutils import listdir_files
from pkgcore.util.lists import stable_unique, iflatten_instance
import os, errno, stat


colon_parsed = frozenset(
    ["ADA_INCLUDE_PATH",  "ADA_OBJECTS_PATH", "INFODIR", "INFOPATH",
     "LDPATH", "MANPATH", "PATH", "PRELINK_PATH", "PRELINK_PATH_MASK",
     "PYTHONPATH", "PKG_CONFIG_PATH", "ROOTPATH"])

incrementals = frozenset(
    ['ADA_INCLUDE_PATH', 'ADA_OBJECTS_PATH', 'CLASSPATH', 'CONFIG_PROTECT',
     'CONFIG_PROTECT_MASK', 'INFODIR', 'INFOPATH', 'KDEDIRS', 'LDPATH',
     'MANPATH', 'PATH', 'PRELINK_PATH', 'PRELINK_PATH_MASK', 'PYTHONPATH',
     'ROOTPATH', 'PKG_CONFIG_PATH'])

default_ldpath = ('/lib', '/lib64', '/lib32',
    '/usr/lib', '/usr/lib64', '/usr/lib32')

def collapse_envd(base):
    pjoin = os.path.join

    collapsed_d = {}
    for x in sorted(listdir_files(base)):
        if x.endswith(".bak") or x.endswith("~") or x.startswith("._cfg") \
            or len(x) <= 2 or not x[0:2].isdigit():
            continue
        d = read_bash_dict(pjoin(base, x))
        # inefficient, but works.
        for k, v in d.iteritems():
            collapsed_d.setdefault(k, []).append(v)
        del d
    
    loc_incrementals = set(incrementals)
    loc_colon_parsed = set(colon_parsed)
    
    # split out env.d defined incrementals..
    # update incrementals *and* colon parsed for colon_seperated;
    # incrementals on it's own is space seperated.

    for x in collapsed_d.pop("COLON_SEPERATED", []):
        v = x.split()
        if v:
            loc_colon_parsed.update(v)

    loc_incrementals.update(loc_colon_parsed)
    
    # now space.
    for x in collapsed_d.pop("SPACE_SEPERATED", []):
        v = x.split()
        if v:
            loc_incrementals.update(v)

    # now reinterpret.
    for k, v in collapsed_d.iteritems():
        if k not in loc_incrementals:
            collapsed_d[k] = v[-1]
            continue
        if k in loc_colon_parsed:
            collapsed_d[k] = filter(None, iflatten_instance(
                x.split(':') for x in v))
        else:
            collapsed_d[k] = filter(None, iflatten_instance(
                x.split() for x in v))

    return collapsed_d, loc_incrementals, loc_colon_parsed


def string_collapse_envd(envd_dict, incrementals, colon_incrementals):
    """transform a passed in dict to strictly strings"""
    for k, v in envd_dict.iteritems():
        if k not in incrementals:
            continue
        if k in colon_incrementals:
            envd_dict[k] = ':'.join(v)
        else:
            envd_dict[k] = ' '.join(v)


def update_ldso(ld_search_path, offset='/'):
    # we do an atomic rename instead of open and write quick
    # enough (avoid the race iow)
    fp = os.path.join(offset, 'etc', 'ld.so.conf')
    new_f = AtomicWriteFile(fp)
    new_f.write("# automatically generated, edit env.d files instead\n")
    new_f.writelines(x.strip()+"\n" for x in ld_search_path)
    new_f.close()
    

class env_update(triggers.base):
    
    required_csets = ()
    _hooks = ('post_unmerge', 'post_merge')
    _priority = 5
    
    def trigger(self, engine):
        pjoin = os.path.join
        offset = engine.offset
        d, inc, colon = collapse_envd(pjoin(offset, "etc/env.d"))

        l = d.pop("LDPATH", None)
        if l is not None:
            update_ldso(l, engine.offset)

        string_collapse_envd(d, inc, colon)

        new_f = AtomicWriteFile(pjoin(offset, "etc", "profile.env"))
        new_f.write("# autogenerated.  update env.d instead\n")
        new_f.writelines('export %s="%s"\n' % (k, d[k]) for k in sorted(d))
        new_f.close()
        new_f = AtomicWriteFile(pjoin(offset, "etc", "profile.csh"))
        new_f.write("# autogenerated, update env.d instead\n")
        new_f.writelines('setenv %s="%s"\n' % (k, d[k]) for k in sorted(d))
        new_f.close()


def simple_chksum_compare(x, y):
    found = False
    for k, v in x.chksums.iteritems():
        if k == "size":
            continue
        o = y.chksums.get(k, None)
        if o is not None:
            if o != v:
                return False
            found = True
    if "size" in x.chksums and "size" in y.chksums:
        return x.chksums["size"] == y.chksums["size"]
    return found


def gen_config_protect_filter(offset, extra_protects=(), extra_disables=()):
    collapsed_d, inc, colon = collapse_envd(os.path.join(offset, "etc/env.d"))
    collapsed_d.setdefault("CONFIG_PROTECT", []).extend(extra_protects)
    collapsed_d.setdefault("CONFIG_PROTECT_MASK", []).extend(extra_disables)

    r = [values.StrGlobMatch(normpath(x).rstrip("/") + "/")
         for x in set(stable_unique(collapsed_d["CONFIG_PROTECT"] + ["/etc"]))]
    if len(r) > 1:
        r = values.OrRestriction(*r)
    else:
        r = r[0]
    neg = stable_unique(collapsed_d["CONFIG_PROTECT_MASK"])
    if neg:
        if len(neg) == 1:
            r2 = values.StrGlobMatch(normpath(neg[0]).rstrip("/") + "/",
                                     negate=True)
        else:
            r2 = values.OrRestriction(
                negate=True,
                *[values.StrGlobMatch(normpath(x).rstrip("/") + "/")
                  for x in set(neg)])
        r = values.AndRestriction(r, r2)
    return r


class ConfigProtectInstall(triggers.base):
    
    required_csets = ('install_existing', 'install')
    _hooks = ('pre_merge',)
    _priority = 90

    def __init__(self, extra_protects=(), extra_disables=()):
        triggers.base.__init__(self)
        self.renames = {}
        self.extra_protects = extra_protects
        self.extra_disables = extra_disables
    
    def register(self, engine):
        triggers.base.register(self, engine)
        t2 = ConfigProtectInstall_restore(self.renames)
        t2.register(engine)

    def trigger(self, engine, existing_cset, install_cset):
        pjoin = os.path.join

        # hackish, but it works.
        protected_filter = gen_config_protect_filter(engine.offset,
            self.extra_protects, self.extra_disables).match
        protected = {}

        for x in existing_cset.iterfiles():
            if x.location.endswith("/.keep"):
                continue
            elif protected_filter(x.location):
                replacement = install_cset[x]
                if not simple_chksum_compare(replacement, x):
                    protected.setdefault(
                        pjoin(engine.offset,
                              os.path.dirname(x.location).lstrip(os.path.sep)),
                        []).append((os.path.basename(replacement.location),
                                    replacement))

        for dir_loc, entries in protected.iteritems():
            updates = dict((x[0], []) for x in entries)
            try:
                existing = sorted(x for x in os.listdir(dir_loc)
                    if x.startswith("._cfg"))
            except OSError, oe:
                if oe.errno != errno.ENOENT:
                    raise
                # this shouldn't occur.
                continue

            for x in existing:
                try:
                    # ._cfg0000_filename
                    count = int(x[5:9])
                    if x[9] != "_":
                        raise ValueError
                    fn = x[10:]
                except (ValueError, IndexError):
                    continue
                if fn in updates:
                    updates[fn].append((count, fn))


            # now we rename.
            for fname, entry in entries:
                # check for any updates with the same chksums.
                count = 0
                for cfg_count, cfg_fname in updates[fname]:
                    if simple_chksum_compare(livefs.gen_obj(
                            pjoin(dir_loc, cfg_fname)), entry):
                        count = cfg_count
                        break
                    count = max(count, cfg_count + 1)
                try:
                    install_cset.remove(entry)
                except KeyError:
                    # this shouldn't occur...
                    continue
                new_fn = pjoin(dir_loc, "._cfg%04i_%s" % (count, fname))
                new_entry = entry.change_attributes(location=new_fn)
                install_cset.add(new_entry)
                self.renames[new_entry] = entry
            del updates


class ConfigProtectInstall_restore(triggers.base):

    required_csets = ('install',)
    _hooks = ('post_merge',)
    _priority = 10

    def __init__(self, renames_dict):
        triggers.base.__init__(self)
        self.renames = renames_dict

    def trigger(self, engine, install_cset):
        for new_entry, old_entry in self.renames.iteritems():
            try:
                install_cset.remove(new_entry)
            except KeyError:
                continue
            install_cset.add(old_entry)
        self.renames.clear()


class ConfigProtectUninstall(triggers.base):
    
    required_csets = ('uninstall_existing', 'uninstall')
    _hooks = ('pre_unmerge',)

    def trigger(self, engine, existing_cset, uninstall_cset):
        protected_restrict = gen_config_protect_filter(engine.offset)

        remove = []
        for x in existing_cset.iterfiles():
            if x.location.endswith("/.keep"):
                continue
            if protected_restrict.match(x.location):
                recorded_ent = uninstall_cset[x]
                if not simple_chksum_compare(recorded_ent, x):
                    # chksum differs.  file stays.
                    remove.append(recorded_ent)

        for x in remove:
            del uninstall_cset[x]


class preinst_contents_reset(triggers.base):
    
    required_csets = ('install',)
    _hooks = ('pre_merge',)
    _priority = 1
    
    def __init__(self, format_op):
        triggers.base.__init__(self)
        self.format_op = format_op
    
    def trigger(self, engine, cset):
        # wipe, and get data again.
        cset.clear()
        cset.update(engine.new._parent.scan_contents(self.format_op.env["D"]))


class collision_protect(triggers.base):

    required_csets = {
        const.INSTALL_MODE:('install', 'install_existing'),
        const.REPLACE_MODE:('install', 'install_existing', 'old_cset')
        }

    _hooks = ('sanity_check',)
    _engine_types = triggers.INSTALLING_MODES

    def __init__(self, extra_protects=(), extra_disables=()):
        triggers.base.__init__(self)
        self.extra_protects = extra_protects
        self.extra_disables = extra_disables

    def trigger(self, engine, install, existing, old_cset=()):
        if not existing:
            return

        # for the moment, we just care about files
        colliding = existing.difference(install.iterdirs())

        # filter out daft .keep files.

        # hackish, but it works.
        protected_filter = gen_config_protect_filter(engine.offset,
            self.extra_protects, self.extra_disables).match

        l = []
        for x in colliding:
            if x.location.endswith(".keep"):
                l.append(x)
            elif protected_filter(x.location):
                l.append(x)
        
        colliding.difference_update(l)
        del l, protected_filter
        if not colliding:
            return

        colliding.difference_update(old_cset)
        if colliding:
            raise errors.BlockModification(
                "collision-protect: file(s) already exist: ( %s )" %
                ', '.join(repr(x) for x in sorted(colliding)))


def customize_engine(domain_settings, engine):
    env_update().register(engine)

    protect = domain_settings.get('CONFIG_PROTECT', [])
    if isinstance(protect, basestring):
        protect = protect.split()
    mask = domain_settings.get('CONFIG_PROTECT_MASK', [])
    if isinstance(protect, basestring):
        protect = protect.split()

    ConfigProtectInstall(protect, mask).register(engine)
    ConfigProtectUninstall().register(engine)

    if "collision-protect" in domain_settings.get("FEATURES", []):
        collision_protect(protect, mask).register(engine)
