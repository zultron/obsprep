#!/usr/bin/python

import argparse
import re, os, sys, shutil, urllib, subprocess, glob
import osc.conf, osc.core, yaml, md5, pycurl
from StringIO import StringIO
from debian.changelog import Changelog, Version
import deb822
from pprint import pprint


########################################################################
# Abstract class
########################################################################
class OBSBuildRuntimeError(RuntimeError):
    pass

class OBSBuild(object):

    source_tarball_url_format = None
    compression_ext = 'gz'
    debian_compression_ext = None
    tarball_strip_components = 1
    changelog_file = 'changelog'
    name = None
    registry = []
    dpkg_source_args = []
    git_rev = ''
    # Hardcoded in osc.commandline.Osc.do_repourls()
    url_tmpl = 'http://download.opensuse.org/repositories/%s'

    dpkg_source_comp_map = dict(
        gz = 'gzip',
        bz2 = 'bzip2',
        )

    class __metaclass__(type):
        def __init__(cls, name, bases, clsdict):
            type.__init__(cls, name, bases, clsdict)
            cls.registry.append(cls)

    def __init__(self, pac_dir = os.getcwd(), args = None):
        self.package_dir = os.path.abspath(pac_dir)
        self.tmp_dir = os.path.normpath("%s/../tmp/%s" % (self.package_dir,
                                                          self.name))
        self.args = args

        # Set up osc object and configuration
        osc.conf.get_config()
        self.osc = osc.core.Package(pac_dir)


    ########################################################################
    # Accessors and utilities
    ########################################################################
    @classmethod
    def package_name(cls, pac_dir = os.getcwd()):
        return osc.core.Package(pac_dir).name

    @classmethod
    def package_class(cls, pac_dir = os.getcwd()):
        for c in cls.registry:
            if c.name == cls.package_name(pac_dir):
                return c
        return None

    @classmethod
    def package_inst(cls, pac_dir = os.getcwd(), args=None):
        return cls.package_class(pac_dir)(pac_dir, args=args)

    def make_tmp_dir(self, subdir=None, clean=False, create=True):
        # If subdir specified, append to tmp_dir
        if subdir is None:
            tmp_dir = self.tmp_dir
        else:
            tmp_dir = "%s/%s" % (self.tmp_dir, subdir)
        # Remove tmp_dir if 'clean'
        if clean and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        # And create the directory
        if create and not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir)
        # Return tmp_dir for convenience
        return tmp_dir

    def remove_tmp_dir(self, subdir=None):
        return self.make_tmp_dir(subdir=subdir, clean=True, create=False)


    ########################################################################
    # Tarball operations
    ########################################################################
    @property
    def debian_tarball_url(self):
        if self.source_tarball_url_format is None:
            raise OBSBuildRuntimeError(
                "Subclasses must override `source_tarball_url_format`")
        return self.source_tarball_url_format % \
            dict(
            rev = self.upstream_version,
            git = self.git_rev,
            comp = self.compression_ext,
            )

    @property
    def debian_tarball_filename(self):
        return "%s_%s.orig.tar.%s" % \
            (self.name, self.upstream_version, self.compression_ext)

    @property
    def debian_tarball_path(self):
        return os.path.join(self.package_dir, self.debian_tarball_filename)

    @property
    def debian_tarball_is_downloaded(self):
        return os.path.exists(self.debian_tarball_path)

    @property
    def debian_tarball_md5sum(self):
        m = md5.new()
        with open(self.debian_tarball_path, 'rb') as f:
            while True:
                chunk = f.read(1024*1024)
                if len(chunk) == 0: break
                m.update(chunk)
        return m.hexdigest()

    @property
    def debian_tarball_size(self):
        return  os.path.getsize(self.debian_tarball_path)

    def debian_tarball_download(self):
        print "Debian orig tarball '%s':" % self.debian_tarball_filename

        if self.debian_tarball_is_downloaded:
            print "    Already exists; doing nothing"
            return
        print "    Downloading from URL '%s'" % self.debian_tarball_url
        link = urllib.FancyURLopener()
        link.retrieve(self.debian_tarball_url, self.debian_tarball_path)
        print "    Done; size %dk, md5sum %s" % \
            (self.debian_tarball_size/1024, self.debian_tarball_md5sum)

    ########################################################################
    # Changelog operations
    ########################################################################
    @property
    def changelog_path(self):
        return os.path.join(self.package_dir, self.changelog_file)

    def parse_changelog(self):
        c = Changelog()
        with open(self.changelog_path, 'r') as f:
            c.parse_changelog(f)
        return c

    def date_string(self,dt):
        '''Date string suitable for changelog entry'''
        import time
        from email.Utils import formatdate
        tt = dt.timetuple()  # get time.struct_time() object
        ts = time.mktime(tt)  # get timestamp
        return formatdate(ts)  # date string

    @property
    def date_string_now(self):
        import datetime
        return self.date_string(datetime.datetime.now())

    @property
    def upstream_version(self):
        return self.changelog.upstream_version
    
    @property
    def debian_version(self):
        return self.changelog.debian_version
    
    @property
    def osc_rev(self):
        return self.osc.rev or 0

    @property
    def debian_tarball_dsc_entry(self):
        return " %s %s %s" % \
            (self.debian_tarball_md5sum,
             self.debian_tarball_size,
             self.debian_tarball_filename)

    @property
    def debian_version_next(self):
        return Version('%s~%d' % (self.changelog_last.version,
                                  int(self.osc_rev)+1))

    @property
    def osc_author(self):
        apiurl = self.osc.apiurl
        osc.conf.get_config()
        userid = osc.conf.config['api_host_options'][apiurl]['user']
        user = osc.core.get_user_data(apiurl, userid, 'realname', 'email')
        return '"%s" <%s>' % tuple(user)

    @property
    def changelog(self):
        if not hasattr(self, '_changelog'):
            self._changelog = self.parse_changelog()
        return self._changelog

    def debian_changelog_init(self):
        self.changelog_last = list(self.changelog)[0]

    def debian_changelog_new(self, changes):
        self.changelog.new_block(
            package = self.name,
            version = self.debian_version_next,
            distributions = self.changelog.distributions,
            urgency = self.changelog.urgency,
            changes = tuple([''] + list(changes) + ['']),  # add blank lines
            author = self.osc_author,
            date = self.date_string_now)

    def debian_changelog_write(self, filename):
        with open(filename, 'w') as f:
            self.changelog.write_to_open_file(f)

    ########################################################################
    # Source package operations
    ########################################################################
    def debian_package_source_fetch(self):
        # Download orig tarball if needed
        if not self.debian_tarball_is_downloaded:
            self.debian_tarball_download()
        else:
            print "Fetching original source tarball:  already exists"

    def debian_package_source_unpack(self):
        # Extract debian original source tarball
        print "Unpacking original source tarball"
        tmp_dir = self.make_tmp_dir(subdir='source_tree', clean=True)
        tar_cmd = ('tar', 'xCf', tmp_dir, self.debian_tarball_path,
                   '--strip-components=%d' % self.tarball_strip_components,
                   )
        print "    Running command:  %s" % ' '.join(tar_cmd)
        tar_p = subprocess.Popen(tar_cmd)
        if tar_p.wait() != 0:
            raise OBSBuildRuntimeError(
                "Failed to extract tarball from '%s' (result %d)" %
                (self.debian_tarball_filename, tar_p.poll()))

    def debian_package_source_debianize(self):
        print "Debianizing source tree from git repository"

        # Extract tarball of the git tree into tmp directory
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        tar_cmd = ('tar', 'xCf', tmp_dir, '-')
        print "    Running (un)tar command:  %s" % ' '.join(tar_cmd)
        tar_p = subprocess.Popen(tar_cmd, stdin=subprocess.PIPE)
        # Create tarball of git tree prefixed with debian/
        git_cmd = ('git', 'archive', '--prefix=debian/', 'HEAD')
        print "    Piping 'git archive' command to (un)tar:  %s" % \
            ' '.join(git_cmd)
        git_p = subprocess.Popen(git_cmd, stdout=tar_p.stdin,
                                 cwd=self.package_dir)
        # Reap processes and check result
        tar_p.communicate()
        git_p.communicate()
        if tar_p.poll() or git_p.poll():
            raise OBSBuildRuntimeError(
                "'git archive | tar x' exited non-zero:  %d/%d" % \
                    (git_p.poll(), tar_p.poll()))

        # Copy temp changelog into tmpdir
        changelog_file = os.path.join(tmp_dir, 'debian/changelog')
        print "    Writing debian changelog to %s" % changelog_file
        self.debian_changelog_write(changelog_file)

    def debian_package_source_configure(self):
        # Override in subclasses that have an extra source package
        # configuration step
        pass

    def debian_package_dpkg_source(self):
        print "Building Debian source package"

        # Remove existing debianization and .dsc files
        files = (glob.glob("%s_*.debian.tar.%s" %
                           (self.name, self.compression_ext)) +
                 glob.glob("%s_*.dsc" % self.name))
        for f in files:
            print "    Removing existing file '%s'" % f
            os.unlink(f)

        # Create source package, including *.dsc and *.debian.tar.gz
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        dpkg_cmd = tuple(
            ['dpkg-source'] + self.dpkg_source_args + \
                ['-Z%s' % self.dpkg_source_comp_map.get(
                        self.compression_ext, self.compression_ext),
                 '-b', tmp_dir]
            )
        print "    Running command:  %s" % ' '.join(dpkg_cmd)
        dpkg_p = subprocess.Popen(dpkg_cmd, cwd=self.package_dir)
        if dpkg_p.wait():
            raise OBSBuildRuntimeError("`dpkg-source` failed")

    def debian_package_source_tree(self):
        # Init tmp dir
        self.make_tmp_dir(clean=True)

        self.debian_package_source_fetch()
        self.debian_package_source_unpack()
        self.debian_changelog_init()
        self.debian_changelog_new(('  * Rebuild in OBS',))
        self.debian_package_source_debianize()
        self.debian_package_source_configure()

    def debian_package_source_build(self):
        self.debian_package_source_tree()

        self.debian_package_dpkg_source()

        # Clean up
        self.remove_tmp_dir()


class PackageRebuildOBSBuild(OBSBuild):
    upstream_version = None   # Parent method N/A
    # New attributes for this subclass
    debian_package_release = None
    debianization_tarball_url_format = None
    debian_dsc_url_format = None

    def parse_changelog(self):
        # HACK FIXME  Need proper subclass structure
        # This overrides a method used in __init__()
        return (0,)

    def debian_package_source_unpack(self):
        pass

    def debian_package_source_debianize(self):
        pass

    def debian_changelog_new(self, changes):
        print "Not generating new changelog entry for rebuilt package"

    def format_vars(self, **kwargs):
        kwargs.update(dict(
                name = self.name,
                rev = self.upstream_version,
                comp = self.compression_ext,
                deb_comp = self.debian_compression_ext or self.compression_ext,
                deb_rel = self.debian_package_release,
                ))
        return kwargs

    @property
    def debian_package_dsc_name(self):
        return "%(name)s_%(rev)s-%(deb_rel)s.dsc" % \
            self.format_vars()

    @property
    def debian_package_dsc_url(self):
        return self.debian_dsc_url_format % self.format_vars(
            dsc = self.debian_package_dsc_name,
            )
         
    @property
    def debian_package_dsc_path(self):
        return os.path.join(self.package_dir,
                            self.debian_package_dsc_name)

    @property
    def debian_package_debianization_tarball_name(self):
        return "%(name)s_%(rev)s-%(deb_rel)s.debian.tar.%(deb_comp)s" % \
            self.format_vars()

    @property
    def debian_package_debianization_tarball_url(self):
        return self.debianization_tarball_url_format % self.format_vars(
            debzn_tb = self.debian_package_debianization_tarball_name,
            )
         
    @property
    def debian_package_debianization_tarball_path(self):
        return os.path.join(self.package_dir,
                            self.debian_package_debianization_tarball_name)

    def debian_package_dpkg_source(self):
        print "Fetch Debianization tarball"
        if os.path.exists(self.debian_package_debianization_tarball_path):
            print "    Already exists; doing nothing"
        else:
            print "    Downloading from URL '%s'" % \
                self.debian_package_debianization_tarball_url
            link = urllib.FancyURLopener()
            link.retrieve(self.debian_package_debianization_tarball_url,
                          self.debian_package_debianization_tarball_path)

        print "Fetch Debian source control (.dsc) file"
        if os.path.exists(self.debian_package_dsc_path):
            print "    Already exists; doing nothing"
        else:
            print "    Downloading from URL '%s'" % \
                self.debian_package_dsc_url
            link = urllib.FancyURLopener()
            link.retrieve(self.debian_package_dsc_url,
                          self.debian_package_dsc_path)

        
class NativePackageOBSBuild(OBSBuild):
    changelog_file = 'debian/changelog'

    @property
    def debian_tarball_filename(self):
        return "%s_%s.tar.%s" % \
            (self.name, self.upstream_version, self.compression_ext)

    def debian_package_source_debianize(self):
        print "Debianizing source tree:  not needed for native package"


class NoSourcePackageOBSBuild(OBSBuild):
    def debian_package_source_fetch(self):
        # All sources in this directory
        pass

    def debian_package_source_unpack(self):
        # All sources in this directory
        pass

    def debian_package_dpkg_source(self):
        # All sources in this directory, so remove all tarballs
        files = glob.glob("%s_*.tar.%s" % (self.name, self.compression_ext))
        for f in files:
            print "    Removing existing file '%s'" % f
            os.unlink(f)

        super(NoSourcePackageOBSBuild, self).debian_package_dpkg_source()


########################################################################
# xenomai package
########################################################################
class XenomaiOBSBuild(NativePackageOBSBuild):
    upstream_version = '2.6.3'
    compression_ext = 'bz2'
    source_tarball_url_format = \
        "http://download.gna.org/xenomai/stable/xenomai-%(rev)s.tar.%(comp)s"
    name = 'xenomai'
    dpkg_source_args = ['--format=3.0 (native)']

    @property
    def changelog_path(self):
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        return os.path.join(tmp_dir, self.changelog_file)


########################################################################
# rtai package
########################################################################
class RTAIOBSBuild(OBSBuild):
    source_tarball_url_format = \
        "https://github.com/shabbyx/rtai/archive/%(git)s.tar.%(comp)s"
    upstream_version_re = re.compile(r'(?P<rel>.*)\.(?P<gitrev>[^.]*)')
    name = 'rtai'

    @property
    def git_rev(self):
        m = self.upstream_version_re.match(self.changelog.upstream_version)
        return m.groupdict()['gitrev']


########################################################################
# linux-tools package
########################################################################
class LinuxToolsOBSBuild(OBSBuild):
    source_tarball_url_format = \
        ("https://www.kernel.org/pub/linux/kernel/v3.x/"
         "linux-%(rev)s.tar.%(comp)s")
    compression_ext = 'xz'
    configure_cruft = (
        'debian/lib/python/debian_linux/debian.pyc',
        'debian/lib/python/debian_linux/gencontrol.pyc',
        'debian/lib/python/debian_linux/utils.pyc',
        'debian/lib/python/debian_linux/__init__.pyc',
        )
    name = 'linux-tools'

    def debian_package_source_configure(self):
        # Configure source package
        config_cmd = ('debian/rules', 'debian/control')
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        config_p = subprocess.Popen(config_cmd, cwd=tmp_dir)
        config_p.wait()  # Always fails
        # Remove cruft causing dpkg-source errors
        #     error: detected 4 unwanted binary files
        for path in self.configure_cruft:
            os.remove(os.path.join(tmp_dir, path))
        print "Configured source package"

########################################################################
# linux package
########################################################################
class LinuxOBSBuild(OBSBuild):
    source_tarball_url_format = \
        ("https://www.kernel.org/pub/linux/kernel/v3.x/"
         "linux-%(rev)s.tar.%(comp)s")
    compression_ext = 'xz'
    configure_cruft = (
        'debian/lib/python/debian_linux/debian.pyc',
        'debian/lib/python/debian_linux/gencontrol.pyc',
        'debian/lib/python/debian_linux/utils.pyc',
        'debian/lib/python/debian_linux/__init__.pyc',
        'debian/lib/python/debian_linux/config.pyc',
        )
    name = 'linux'
    rtai_hal_patch_glob_pat = \
        ('../tmp/linux/rtai_source/base/arch/x86/patches/'
         'hal-linux-%s-x86-*.patch')
    xenomai_tarball_glob = '../xenomai/xenomai-*.tar.bz2'
    configure_args = []

    def debian_package_source_unpack_rtai(self):
        # Unpack RTAI tarball in tmp/linux/rtai_source
        print "    Unpacking RTAI tarball for hal patch"
        rtai_pkg = self.package_inst('../rtai')
        rtai_tarball_path = rtai_pkg.debian_tarball_path
        rtai_tmp_dir = self.make_tmp_dir(subdir='rtai_source', clean=True)
        tar_cmd = ('tar', 'xCf', rtai_tmp_dir, rtai_tarball_path,
                   '--strip-components=1')
        print "        Running command:  %s" % ' '.join(tar_cmd)
        tar_p = subprocess.Popen(tar_cmd)
        if tar_p.wait() != 0:
            raise OBSBuildRuntimeError("Extract tarball '%s' into '%s' failed" %
                                       (rtai_tarball_path, rtai_tmp_dir))

        print "    Locating RTAI hal patch"
        patch_re = re.compile(r'.*/hal-linux-%s-x86-[0-9]+.patch$' %
                              self.upstream_version)
        patch_glob = self.rtai_hal_patch_glob_pat % self.upstream_version
        for p in glob.glob(patch_glob):
            match = patch_re.match(p)
            if match is not None:
                rtai_hal_patch = os.path.abspath(p)
                break
        else:
            raise OBSBuildRuntimeError(
                "Unable to find RTAI patch for linux-%s in %s" %
                (self.upstream_version, patch_glob))

        self.configure_args.append('RTAI_PATCH_SRC=%s' % rtai_hal_patch)
        print "        Found RTAI hal patch: %s"  % rtai_hal_patch

    def debian_package_source_unpack_xenomai(self):
        # Unpack xenomai tarball in tmp/linux/xenomai_source
        print "    Unpacking Xenomai tarball for patch generation"
        files = glob.glob(self.xenomai_tarball_glob)
        if len(files) != 1:
            raise OBSBuildRuntimeError("%d files matched by glob '%s'" %
                                       (len(files), self.xenomai_tarball_glob))
        xenomai_tarball_path = files[0]
        xenomai_tmp_dir = self.make_tmp_dir(subdir='xenomai_source', clean=True)
        tar_cmd = ('tar', 'xCf', xenomai_tmp_dir, xenomai_tarball_path,
                   '--strip-components=1')
        print "        Running command:  %s" % ' '.join(tar_cmd)
        tar_p = subprocess.Popen(tar_cmd)
        if tar_p.wait() != 0:
            raise OBSBuildRuntimeError("Extract tarball '%s' into '%s' failed" %
                                       (xenomai_tarball_path, xenomai_tmp_dir))
        self.configure_args.append('XENO_SRCDIR=%s' % xenomai_tmp_dir)

    def debian_package_source_configure(self):
        print "Configuring Debian source package"

        # Prepare Xenomai sources
        self.debian_package_source_unpack_xenomai()
        # Prepare RTAI sources
        self.debian_package_source_unpack_rtai()

        # Configure source package
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        config_cmd = ['debian/rules', 'debian/control', 'NOFAIL=true'] + \
            self.configure_args
        print "    Running command:  %s" % ' '.join(config_cmd)
        config_p = subprocess.Popen(config_cmd, cwd = tmp_dir)
        if config_p.wait():
            raise OBSBuildRuntimeError("`%s` returned %d" %
                                       (' '.join(config_cmd), config_p.poll()))
        # Remove cruft causing dpkg-source errors
        #     error: detected 4 unwanted binary files
        for path in self.configure_cruft:
            os.remove(os.path.join(tmp_dir, path))


########################################################################
# linux-latest package
########################################################################
class LinuxLatestOBSBuild(NoSourcePackageOBSBuild):
    name = 'linux-latest'
    linux_subver_re = re.compile(r'^([0-9.]+)\.([0-9]+)$')

    def debian_package_source_configure(self):
        print "Configuring Debian source package"

        # Ensure the correct linux-support pkg is installed
        print "    Checking for correct linux-support package"
        linux_pkg = self.package_inst('../linux')

        # Get linux sub-version, e.g. 3.8, without minor version
        linux_version = linux_pkg.upstream_version
        match = self.linux_subver_re.match(linux_version)
        if not match:
            raise OBSBuildRuntimeError(
                "Unable to determine linux sub-version")
        linux_subversion = match.groups()[0]

        # Get linux package abiname, e.g. 1
        defines_file = "%s/config/defines" % linux_pkg.package_dir
        abiname_re = re.compile(r'abiname:\s*(.+)\n$')
        with open(defines_file, 'r') as f:
            for line in f:
                match = abiname_re.match(line)
                if match:
                    abiname = match.groups()[0]
                    break
            else:
                raise OBSBuildRuntimeError(
                    "Unable to determine linux package abiname")

        # Check for package
        linux_support = 'linux-support-%s-%s' % (linux_subversion, abiname)
        print "    Checking for package '%s'" % linux_support
        dpkg_cmd = ('dpkg-query', '-W', linux_support)
        print "        Running command:  %s" % ' '.join(dpkg_cmd)
        dpkg_p = subprocess.Popen(dpkg_cmd)
        if dpkg_p.wait():
            raise OBSBuildRuntimeError(
                "Unable to detect installed package '%s'" % linux_support)

        # Configure source package
        print "    Configuring source package"
        config_cmd = ('debian/rules', 'debian/control')
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        config_p = subprocess.Popen(config_cmd, cwd = tmp_dir)
        config_p.communicate()  # Command always fails; don't check result


########################################################################
# libsodium package
########################################################################
class LibSodiumOBSBuild(OBSBuild):
    source_tarball_url_format = \
        ("https://download.libsodium.org/libsodium/releases/"
         "libsodium-%(rev)s.tar.%(comp)s")
    name = 'libsodium'


########################################################################
# zeromq4 package
########################################################################
class ZeroMQ4OBSBuild(OBSBuild):
    source_tarball_url_format = \
        "http://download.zeromq.org/zeromq-%(rev)s.tar.%(comp)s"
    name = 'zeromq4'


########################################################################
# cython package
########################################################################
class CythonOBSBuild(PackageRebuildOBSBuild):
    upstream_version = '0.19.1+git34-gac3e3a2'
    debian_package_release = '1~bpo70+1'
    base_url = 'http://ftp.de.debian.org/debian/pool/main/c/cython'
    source_tarball_url_format = \
        '%s/cython_%%(rev)s.orig.tar.%%(comp)s' % base_url
    debianization_tarball_url_format = '%s/%%(debzn_tb)s' % base_url
    debian_dsc_url_format = '%s/%%(dsc)s' % base_url
    name = 'cython'

        
########################################################################
# dh-python package
########################################################################
class DHPythonOBSBuild(PackageRebuildOBSBuild):
    upstream_version = '1.20140511'
    debian_package_release = '1~bpo70+1'
    base_url = 'http://ftp.de.debian.org/debian/pool/main/d/dh-python'
    compression_ext = 'xz'
    debian_compression_ext = 'gz'
    source_tarball_url_format = \
        '%s/dh-python_%%(rev)s.orig.tar.%%(comp)s' % base_url
    debianization_tarball_url_format = '%s/%%(debzn_tb)s' % base_url
    debian_dsc_url_format = '%s/%%(dsc)s' % base_url
    name = 'dh-python'

        
########################################################################
# pyzmq package
########################################################################
class PyZMQOBSBuild(OBSBuild):
    source_tarball_url_format = \
        "https://github.com/zeromq/pyzmq/archive/v%(rev)s.tar.%(comp)s"
    name = 'pyzmq'


########################################################################
# czmq package
########################################################################
class CZMQOBSBuild(OBSBuild):
    source_tarball_url_format = \
        "http://download.zeromq.org/czmq-%(rev)s.tar.%(comp)s"
    name = 'czmq'


########################################################################
# libwebsockets package
########################################################################
class LibwebsocketsOBSBuild(OBSBuild):
    git_rev = '95a8abb'
    source_tarball_url_format = \
        ("http://git.libwebsockets.org/cgi-bin/cgit/libwebsockets/snapshot/" \
             "libwebsockets-%s.tar.gz" % git_rev)
    name = 'libwebsockets'


########################################################################
# jansson package
########################################################################
class JanssonOBSBuild(OBSBuild):
    compression_ext = 'bz2'
    source_tarball_url_format = \
        "http://www.digip.org/jansson/releases/jansson-%(rev)s.tar.%(comp)s"
    name = 'jansson'


########################################################################
# python-pyftpdlib package
########################################################################
class PythonPyFTPDLibOBSBuild(OBSBuild):
    source_tarball_url_format = \
        ("https://github.com/giampaolo/pyftpdlib/archive/" \
             "release-%(rev)s.tar.%(comp)s")
    name = 'python-pyftpdlib'


########################################################################
# dovetail-automata-keyring package
########################################################################
class DovetailAutomataKeyringOBSBuild(NoSourcePackageOBSBuild):
    name = 'dovetail-automata-keyring'


########################################################################
# ghdl package
########################################################################
class GHDLOBSBuild(PackageRebuildOBSBuild):
    upstream_version = '0.31'
    debian_package_release = '2wheezy1'
    base_url = ('http://downloads.sourceforge.net/ghdl-updates/Builds/' \
                    'ghdl-%(rev)s/Debian')
    source_tarball_url_format = \
        '%s/ghdl_%%(rev)s.orig.tar.%%(comp)s' % base_url
    debianization_tarball_url_format = '%s/%%(debzn_tb)s' % base_url
    debian_dsc_url_format = '%s/%%(dsc)s' % base_url
    name = 'ghdl'
    gcc48_url = \
    'http://mirrors-usa.go-parts.com/gcc/releases/gcc-4.8.4/gcc-4.8.4.tar.bz2'

        
########################################################################
# machinekit package
########################################################################
class MachinekitOBSBuild(NativePackageOBSBuild):
    # source_tarball_url_format = \
    #     "https://github.com/machinekit/machinekit/archive/%(git)s.tar.%(comp)s"
    source_tarball_url_format = \
        "https://github.com/zultron/machinekit/archive/%(git)s.tar.%(comp)s"
    git_rev = '7468d44d'  # FIXME this should come from github
    update_num = 10  # Bump this when git_rev changes for upstream update
    upstream_version = '0.2.%d.%s' % (update_num, git_rev)
    upstream_version_re = re.compile(r'(?P<rel>.*)\.(?P<gitrev>[^.]*)')
    name = 'machinekit'
    dpkg_source_args = ['--format=3.0 (native)']
    linux_package_abiver = '3.8-1'

    @property
    def debian_version_next(self):
        return self.upstream_version

    def debian_package_source_configure(self):
        # Configure source package
        config_cmd = (
            'debian/configure', '-prxD',
            '-X', self.linux_package_abiver,
            '-R', self.linux_package_abiver,
            )
        tmp_dir = self.make_tmp_dir(subdir='source_tree')
        print "    Running command:  %s" % ' '.join(config_cmd)
        config_p = subprocess.Popen(config_cmd, cwd=tmp_dir)
        if config_p.wait() != 0:
            raise OBSBuildRuntimeError(
                "Unable to configure machinekit package")
            
        # Copy temp changelog into tmpdir
        changelog_file = os.path.join(tmp_dir, 'debian/changelog')
        print "    Writing debian changelog to %s" % changelog_file
        self.debian_changelog_write(changelog_file)


        print "Configured source package"


########################################################################
# main()
########################################################################
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Prepare Debian packages for OBS build')
    parser.add_argument('--unpack', '-u', action='store_true',
                        help='Unpack Debianized source tree')
    parser.add_argument('--build', '-b', action='store_true',
                        help='Build Debian package from source tree')

    args = parser.parse_args()

    ob = OBSBuild.package_inst(args=args)

    if ob.args.unpack:
        print "Unpacking Debianized source tree"
        ob.debian_package_source_tree()
    elif ob.args.build:
        print "Building package from Debianized source tree"
        ob.debian_package_dpkg_source()
        ob.remove_tmp_dir()
    else:
        print "Building source package"
        ob.debian_package_source_build()
