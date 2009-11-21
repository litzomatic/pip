import sys
import os
import shutil
import re
import zipfile
import tarfile
import pkg_resources
import tempfile
import mimetypes
import urlparse
import urllib2
import urllib
import ConfigParser
from email.FeedParser import FeedParser
from pip.locations import bin_py, site_packages
from pip.exceptions import InstallationError, UninstallationError
from pip.vcs import vcs
from pip.log import logger
from pip.util import display_path, rmtree, format_size
from pip.util import splitext, ask, backup_dir
from pip.util import url_to_filename, filename_to_url
from pip.util import is_url, is_filename, strip_prefix
from pip.util import make_path_relative, is_svn_page, file_contents
from pip.util import has_leading_dir, split_leading_dir
from pip.util import get_file_content
from pip import call_subprocess
from pip.backwardcompat import any, md5
from pip.index import Link

class InstallRequirement(object):

    def __init__(self, req, comes_from, source_dir=None, editable=False,
                 url=None, update=True):
        if isinstance(req, basestring):
            req = pkg_resources.Requirement.parse(req)
        self.req = req
        self.comes_from = comes_from
        self.source_dir = source_dir
        self.editable = editable
        self.url = url
        self._egg_info_path = None
        # This holds the pkg_resources.Distribution object if this requirement
        # is already available:
        self.satisfied_by = None
        # This hold the pkg_resources.Distribution object if this requirement
        # conflicts with another installed distribution:
        self.conflicts_with = None
        self._temp_build_dir = None
        self._is_bundle = None
        # True if the editable should be updated:
        self.update = update
        # Set to True after successful installation
        self.install_succeeded = None
        # UninstallPathSet of uninstalled distribution (for possible rollback)
        self.uninstalled = None

    @classmethod
    def from_editable(cls, editable_req, comes_from=None, default_vcs=None):
        name, url = parse_editable(editable_req, default_vcs)
        if url.startswith('file:'):
            source_dir = url_to_filename(url)
        else:
            source_dir = None
        return cls(name, comes_from, source_dir=source_dir, editable=True, url=url)

    @classmethod
    def from_line(cls, name, comes_from=None):
        """Creates an InstallRequirement from a name, which might be a
        requirement, filename, or URL.
        """
        url = None
        name = name.strip()
        req = name
        if is_url(name):
            url = name
            ## FIXME: I think getting the requirement here is a bad idea:
            #req = get_requirement_from_url(url)
            req = None
        elif is_filename(name):
            if not os.path.exists(name):
                logger.warn('Requirement %r looks like a filename, but the file does not exist'
                            % name)
            url = filename_to_url(name)
            #req = get_requirement_from_url(url)
            req = None
        return cls(req, comes_from, url=url)

    def __str__(self):
        if self.req:
            s = str(self.req)
            if self.url:
                s += ' from %s' % self.url
        else:
            s = self.url
        if self.satisfied_by is not None:
            s += ' in %s' % display_path(self.satisfied_by.location)
        if self.comes_from:
            if isinstance(self.comes_from, basestring):
                comes_from = self.comes_from
            else:
                comes_from = self.comes_from.from_path()
            if comes_from:
                s += ' (from %s)' % comes_from
        return s

    def from_path(self):
        if self.req is None:
            return None
        s = str(self.req)
        if self.comes_from:
            if isinstance(self.comes_from, basestring):
                comes_from = self.comes_from
            else:
                comes_from = self.comes_from.from_path()
            if comes_from:
                s += '->' + comes_from
        return s

    def build_location(self, build_dir, unpack=True):
        if self._temp_build_dir is not None:
            return self._temp_build_dir
        if self.req is None:
            self._temp_build_dir = tempfile.mkdtemp('-build', 'pip-')
            self._ideal_build_dir = build_dir
            return self._temp_build_dir
        if self.editable:
            name = self.name.lower()
        else:
            name = self.name
        # FIXME: Is there a better place to create the build_dir? (hg and bzr need this)
        if not os.path.exists(build_dir):
            os.makedirs(build_dir)
        return os.path.join(build_dir, name)

    def correct_build_location(self):
        """If the build location was a temporary directory, this will move it
        to a new more permanent location"""
        if self.source_dir is not None:
            return
        assert self.req is not None
        assert self._temp_build_dir
        old_location = self._temp_build_dir
        new_build_dir = self._ideal_build_dir
        del self._ideal_build_dir
        if self.editable:
            name = self.name.lower()
        else:
            name = self.name
        new_location = os.path.join(new_build_dir, name)
        if not os.path.exists(new_build_dir):
            logger.debug('Creating directory %s' % new_build_dir)
            os.makedirs(new_build_dir)
        if os.path.exists(new_location):
            raise InstallationError(
                'A package already exists in %s; please remove it to continue'
                % display_path(new_location))
        logger.debug('Moving package %s from %s to new location %s'
                     % (self, display_path(old_location), display_path(new_location)))
        shutil.move(old_location, new_location)
        self._temp_build_dir = new_location
        self.source_dir = new_location
        self._egg_info_path = None

    @property
    def name(self):
        if self.req is None:
            return None
        return self.req.project_name

    @property
    def url_name(self):
        if self.req is None:
            return None
        return urllib.quote(self.req.unsafe_name)

    @property
    def setup_py(self):
        return os.path.join(self.source_dir, 'setup.py')

    def run_egg_info(self, force_root_egg_info=False):
        assert self.source_dir
        if self.name:
            logger.notify('Running setup.py egg_info for package %s' % self.name)
        else:
            logger.notify('Running setup.py egg_info for package from %s' % self.url)
        logger.indent += 2
        try:
            script = self._run_setup_py
            script = script.replace('__SETUP_PY__', repr(self.setup_py))
            script = script.replace('__PKG_NAME__', repr(self.name))
            # We can't put the .egg-info files at the root, because then the source code will be mistaken
            # for an installed egg, causing problems
            if self.editable or force_root_egg_info:
                egg_base_option = []
            else:
                egg_info_dir = os.path.join(self.source_dir, 'pip-egg-info')
                if not os.path.exists(egg_info_dir):
                    os.makedirs(egg_info_dir)
                egg_base_option = ['--egg-base', 'pip-egg-info']
            call_subprocess(
                [sys.executable, '-c', script, 'egg_info'] + egg_base_option,
                cwd=self.source_dir, filter_stdout=self._filter_install, show_stdout=False,
                command_level=logger.VERBOSE_DEBUG,
                command_desc='python setup.py egg_info')
        finally:
            logger.indent -= 2
        if not self.req:
            self.req = pkg_resources.Requirement.parse(self.pkg_info()['Name'])
            self.correct_build_location()

    ## FIXME: This is a lame hack, entirely for PasteScript which has
    ## a self-provided entry point that causes this awkwardness
    _run_setup_py = """
__file__ = __SETUP_PY__
from setuptools.command import egg_info
def replacement_run(self):
    self.mkpath(self.egg_info)
    installer = self.distribution.fetch_build_egg
    for ep in egg_info.iter_entry_points('egg_info.writers'):
        # require=False is the change we're making:
        writer = ep.load(require=False)
        if writer:
            writer(self, ep.name, egg_info.os.path.join(self.egg_info,ep.name))
    self.find_sources()
egg_info.egg_info.run = replacement_run
execfile(__file__)
"""

    def egg_info_data(self, filename):
        if self.satisfied_by is not None:
            if not self.satisfied_by.has_metadata(filename):
                return None
            return self.satisfied_by.get_metadata(filename)
        assert self.source_dir
        filename = self.egg_info_path(filename)
        if not os.path.exists(filename):
            return None
        fp = open(filename, 'r')
        data = fp.read()
        fp.close()
        return data

    def egg_info_path(self, filename):
        if self._egg_info_path is None:
            if self.editable:
                base = self.source_dir
            else:
                base = os.path.join(self.source_dir, 'pip-egg-info')
            filenames = os.listdir(base)
            if self.editable:
                filenames = []
                for root, dirs, files in os.walk(base):
                    for dir in vcs.dirnames:
                        if dir in dirs:
                            dirs.remove(dir)
                    filenames.extend([os.path.join(root, dir)
                                     for dir in dirs])
                filenames = [f for f in filenames if f.endswith('.egg-info')]
            assert filenames, "No files/directories in %s (from %s)" % (base, filename)
            assert len(filenames) == 1, "Unexpected files/directories in %s: %s" % (base, ' '.join(filenames))
            self._egg_info_path = os.path.join(base, filenames[0])
        return os.path.join(self._egg_info_path, filename)

    def egg_info_lines(self, filename):
        data = self.egg_info_data(filename)
        if not data:
            return []
        result = []
        for line in data.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            result.append(line)
        return result

    def pkg_info(self):
        p = FeedParser()
        data = self.egg_info_data('PKG-INFO')
        if not data:
            logger.warn('No PKG-INFO file found in %s' % display_path(self.egg_info_path('PKG-INFO')))
        p.feed(data or '')
        return p.close()

    @property
    def dependency_links(self):
        return self.egg_info_lines('dependency_links.txt')

    _requirements_section_re = re.compile(r'\[(.*?)\]')

    def requirements(self, extras=()):
        in_extra = None
        for line in self.egg_info_lines('requires.txt'):
            match = self._requirements_section_re.match(line)
            if match:
                in_extra = match.group(1)
                continue
            if in_extra and in_extra not in extras:
                # Skip requirement for an extra we aren't requiring
                continue
            yield line

    @property
    def absolute_versions(self):
        for qualifier, version in self.req.specs:
            if qualifier == '==':
                yield version

    @property
    def installed_version(self):
        return self.pkg_info()['version']

    def assert_source_matches_version(self):
        assert self.source_dir
        if self.comes_from is None:
            # We don't check the versions of things explicitly installed.
            # This makes, e.g., "pip Package==dev" possible
            return
        version = self.installed_version
        if version not in self.req:
            logger.fatal(
                'Source in %s has the version %s, which does not match the requirement %s'
                % (display_path(self.source_dir), version, self))
            raise InstallationError(
                'Source in %s has version %s that conflicts with %s'
                % (display_path(self.source_dir), version, self))
        else:
            logger.debug('Source in %s has version %s, which satisfies requirement %s'
                         % (display_path(self.source_dir), version, self))

    def update_editable(self, obtain=True):
        if not self.url:
            logger.info("Cannot update repository at %s; repository location is unknown" % self.source_dir)
            return
        assert self.editable
        assert self.source_dir
        if self.url.startswith('file:'):
            # Static paths don't get updated
            return
        assert '+' in self.url, "bad url: %r" % self.url
        if not self.update:
            return
        vc_type, url = self.url.split('+', 1)
        backend = vcs.get_backend(vc_type)
        if backend:
            vcs_backend = backend(self.url)
            if obtain:
                vcs_backend.obtain(self.source_dir)
            else:
                vcs_backend.export(self.source_dir)
        else:
            assert 0, (
                'Unexpected version control type (in %s): %s'
                % (self.url, vc_type))

    def uninstall(self, auto_confirm=False):
        """
        Uninstall the distribution currently satisfying this requirement.

        Prompts before removing or modifying files unless
        ``auto_confirm`` is True.

        Refuses to delete or modify files outside of ``sys.prefix`` -
        thus uninstallation within a virtual environment can only
        modify that virtual environment, even if the virtualenv is
        linked to global site-packages.

        """
        if not self.check_if_exists():
            raise UninstallationError("Cannot uninstall requirement %s, not installed" % (self.name,))
        dist = self.satisfied_by or self.conflicts_with
        paths_to_remove = UninstallPathSet(dist, sys.prefix)

        pip_egg_info_path = os.path.join(dist.location,
                                         dist.egg_name()) + '.egg-info'
        easy_install_egg = dist.egg_name() + '.egg'
        # This won't find a globally-installed develop egg if
        # we're in a virtualenv.
        # (There doesn't seem to be any metadata in the
        # Distribution object for a develop egg that points back
        # to its .egg-link and easy-install.pth files).  That's
        # OK, because we restrict ourselves to making changes
        # within sys.prefix anyway.
        develop_egg_link = os.path.join(site_packages,
                                        dist.project_name) + '.egg-link'
        if os.path.exists(pip_egg_info_path):
            # package installed by pip
            paths_to_remove.add(pip_egg_info_path)
            if dist.has_metadata('installed-files.txt'):
                for installed_file in dist.get_metadata('installed-files.txt').splitlines():
                    path = os.path.normpath(os.path.join(pip_egg_info_path, installed_file))
                    if os.path.exists(path):
                        paths_to_remove.add(path)
            if dist.has_metadata('top_level.txt'):
                for top_level_pkg in [p for p
                                      in dist.get_metadata('top_level.txt').splitlines()
                                      if p]:
                    path = os.path.join(dist.location, top_level_pkg)
                    if os.path.exists(path):
                        paths_to_remove.add(path)
                    elif os.path.exists(path + '.py'):
                        paths_to_remove.add(path + '.py')
                        if os.path.exists(path + '.pyc'):
                            paths_to_remove.add(path + '.pyc')

        elif dist.location.endswith(easy_install_egg):
            # package installed by easy_install
            paths_to_remove.add(dist.location)
            easy_install_pth = os.path.join(os.path.dirname(dist.location),
                                            'easy-install.pth')
            paths_to_remove.add_pth(easy_install_pth, './' + easy_install_egg)

        elif os.path.isfile(develop_egg_link):
            # develop egg
            fh = open(develop_egg_link, 'r')
            link_pointer = os.path.normcase(fh.readline().strip())
            fh.close()
            assert (link_pointer == dist.location), 'Egg-link %s does not match installed location of %s (at %s)' % (link_pointer, self.name, dist.location)
            paths_to_remove.add(develop_egg_link)
            easy_install_pth = os.path.join(os.path.dirname(develop_egg_link),
                                            'easy-install.pth')
            paths_to_remove.add_pth(easy_install_pth, dist.location)
            # fix location (so we can uninstall links to sources outside venv)
            paths_to_remove.location = develop_egg_link

        # find distutils scripts= scripts
        if dist.has_metadata('scripts') and dist.metadata_isdir('scripts'):
            for script in dist.metadata_listdir('scripts'):
                paths_to_remove.add(os.path.join(bin_py, script))
                if sys.platform == 'win32':
                    paths_to_remove.add(os.path.join(bin_py, script) + '.bat')

        # find console_scripts
        if dist.has_metadata('entry_points.txt'):
            config = ConfigParser.SafeConfigParser()
            config.readfp(FakeFile(dist.get_metadata_lines('entry_points.txt')))
            if config.has_section('console_scripts'):
                for name, value in config.items('console_scripts'):
                    paths_to_remove.add(os.path.join(bin_py, name))
                    if sys.platform == 'win32':
                        paths_to_remove.add(os.path.join(bin_py, name) + '.exe')
                        paths_to_remove.add(os.path.join(bin_py, name) + '-script.py')

        paths_to_remove.remove(auto_confirm)
        self.uninstalled = paths_to_remove

    def rollback_uninstall(self):
        if self.uninstalled:
            self.uninstalled.rollback()
        else:
            logger.error("Can't rollback %s, nothing uninstalled."
                         % (self.project_name,))

    def archive(self, build_dir):
        assert self.source_dir
        create_archive = True
        archive_name = '%s-%s.zip' % (self.name, self.installed_version)
        archive_path = os.path.join(build_dir, archive_name)
        if os.path.exists(archive_path):
            response = ask('The file %s exists. (i)gnore, (w)ipe, (b)ackup '
                           % display_path(archive_path), ('i', 'w', 'b'))
            if response == 'i':
                create_archive = False
            elif response == 'w':
                logger.warn('Deleting %s' % display_path(archive_path))
                os.remove(archive_path)
            elif response == 'b':
                dest_file = backup_dir(archive_path)
                logger.warn('Backing up %s to %s'
                            % (display_path(archive_path), display_path(dest_file)))
                shutil.move(archive_path, dest_file)
        if create_archive:
            zip = zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED)
            dir = os.path.normcase(os.path.abspath(self.source_dir))
            for dirpath, dirnames, filenames in os.walk(dir):
                if 'pip-egg-info' in dirnames:
                    dirnames.remove('pip-egg-info')
                for dirname in dirnames:
                    dirname = os.path.join(dirpath, dirname)
                    name = self._clean_zip_name(dirname, dir)
                    zipdir = zipfile.ZipInfo(self.name + '/' + name + '/')
                    zipdir.external_attr = 0755 << 16L
                    zip.writestr(zipdir, '')
                for filename in filenames:
                    if filename == 'pip-delete-this-directory.txt':
                        continue
                    filename = os.path.join(dirpath, filename)
                    name = self._clean_zip_name(filename, dir)
                    zip.write(filename, self.name + '/' + name)
            zip.close()
            logger.indent -= 2
            logger.notify('Saved %s' % display_path(archive_path))

    def _clean_zip_name(self, name, prefix):
        assert name.startswith(prefix+'/'), (
            "name %r doesn't start with prefix %r" % (name, prefix))
        name = name[len(prefix)+1:]
        name = name.replace(os.path.sep, '/')
        return name

    def install(self, install_options):
        if self.editable:
            self.install_editable()
            return
        temp_location = tempfile.mkdtemp('-record', 'pip-')
        record_filename = os.path.join(temp_location, 'install-record.txt')
        ## FIXME: I'm not sure if this is a reasonable location; probably not
        ## but we can't put it in the default location, as that is a virtualenv symlink that isn't writable
        header_dir = os.path.join(os.path.dirname(os.path.dirname(self.source_dir)), 'lib', 'include')
        logger.notify('Running setup.py install for %s' % self.name)
        logger.indent += 2
        try:
            call_subprocess(
                [sys.executable, '-c',
                 "import setuptools; __file__=%r; execfile(%r)" % (self.setup_py, self.setup_py),
                 'install', '--single-version-externally-managed', '--record', record_filename,
                 '--install-headers', header_dir] + install_options,
                cwd=self.source_dir, filter_stdout=self._filter_install, show_stdout=False)
        finally:
            logger.indent -= 2
        self.install_succeeded = True
        f = open(record_filename)
        for line in f:
            line = line.strip()
            if line.endswith('.egg-info'):
                egg_info_dir = line
                break
        else:
            logger.warn('Could not find .egg-info directory in install record for %s' % self)
            ## FIXME: put the record somewhere
            return
        f.close()
        new_lines = []
        f = open(record_filename)
        for line in f:
            filename = line.strip()
            if os.path.isdir(filename):
                filename += os.path.sep
            new_lines.append(make_path_relative(filename, egg_info_dir))
        f.close()
        f = open(os.path.join(egg_info_dir, 'installed-files.txt'), 'w')
        f.write('\n'.join(new_lines)+'\n')
        f.close()

    def remove_temporary_source(self):
        """Remove the source files from this requirement, if they are marked
        for deletion"""
        if self.is_bundle or os.path.exists(self.delete_marker_filename):
            logger.info('Removing source in %s' % self.source_dir)
            if self.source_dir:
                rmtree(self.source_dir)
            self.source_dir = None
            if self._temp_build_dir and os.path.exists(self._temp_build_dir):
                rmtree(self._temp_build_dir)
            self._temp_build_dir = None

    def install_editable(self):
        logger.notify('Running setup.py develop for %s' % self.name)
        logger.indent += 2
        try:
            ## FIXME: should we do --install-headers here too?
            call_subprocess(
                [sys.executable, '-c',
                 "import setuptools; __file__=%r; execfile(%r)" % (self.setup_py, self.setup_py),
                 'develop', '--no-deps'], cwd=self.source_dir, filter_stdout=self._filter_install,
                show_stdout=False)
        finally:
            logger.indent -= 2
        self.install_succeeded = True

    def _filter_install(self, line):
        level = logger.NOTIFY
        for regex in [r'^running .*', r'^writing .*', '^creating .*', '^[Cc]opying .*',
                      r'^reading .*', r"^removing .*\.egg-info' \(and everything under it\)$",
                      r'^byte-compiling ',
                      # Not sure what this warning is, but it seems harmless:
                      r"^warning: manifest_maker: standard file '-c' not found$"]:
            if re.search(regex, line.strip()):
                level = logger.INFO
                break
        return (level, line)

    def check_if_exists(self):
        """Find an installed distribution that satisfies or conflicts
        with this requirement, and set self.satisfied_by or
        self.conflicts_with appropriately."""
        if self.req is None:
            return False
        try:
            self.satisfied_by = pkg_resources.get_distribution(self.req)
        except pkg_resources.DistributionNotFound:
            return False
        except pkg_resources.VersionConflict:
            self.conflicts_with = pkg_resources.get_distribution(self.req.project_name)
        return True

    @property
    def is_bundle(self):
        if self._is_bundle is not None:
            return self._is_bundle
        base = self._temp_build_dir
        if not base:
            ## FIXME: this doesn't seem right:
            return False
        self._is_bundle = (os.path.exists(os.path.join(base, 'pip-manifest.txt'))
                           or os.path.exists(os.path.join(base, 'pyinstall-manifest.txt')))
        return self._is_bundle

    def bundle_requirements(self):
        for dest_dir in self._bundle_editable_dirs:
            package = os.path.basename(dest_dir)
            ## FIXME: svnism:
            for vcs_backend in vcs.backends:
                url = rev = None
                vcs_bundle_file = os.path.join(
                    dest_dir, vcs_backend.bundle_file)
                if os.path.exists(vcs_bundle_file):
                    vc_type = vcs_backend.name
                    fp = open(vcs_bundle_file)
                    content = fp.read()
                    fp.close()
                    url, rev = vcs_backend().parse_vcs_bundle_file(content)
                    break
            if url:
                url = '%s+%s@%s' % (vc_type, url, rev)
            else:
                url = None
            yield InstallRequirement(
                package, self, editable=True, url=url,
                update=False, source_dir=dest_dir)
        for dest_dir in self._bundle_build_dirs:
            package = os.path.basename(dest_dir)
            yield InstallRequirement(
                package, self,
                source_dir=dest_dir)

    def move_bundle_files(self, dest_build_dir, dest_src_dir):
        base = self._temp_build_dir
        assert base
        src_dir = os.path.join(base, 'src')
        build_dir = os.path.join(base, 'build')
        bundle_build_dirs = []
        bundle_editable_dirs = []
        for source_dir, dest_dir, dir_collection in [
            (src_dir, dest_src_dir, bundle_editable_dirs),
            (build_dir, dest_build_dir, bundle_build_dirs)]:
            if os.path.exists(source_dir):
                for dirname in os.listdir(source_dir):
                    dest = os.path.join(dest_dir, dirname)
                    dir_collection.append(dest)
                    if os.path.exists(dest):
                        logger.warn('The directory %s (containing package %s) already exists; cannot move source from bundle %s'
                                    % (dest, dirname, self))
                        continue
                    if not os.path.exists(dest_dir):
                        logger.info('Creating directory %s' % dest_dir)
                        os.makedirs(dest_dir)
                    shutil.move(os.path.join(source_dir, dirname), dest)
                if not os.listdir(source_dir):
                    os.rmdir(source_dir)
        self._temp_build_dir = None
        self._bundle_build_dirs = bundle_build_dirs
        self._bundle_editable_dirs = bundle_editable_dirs

    @property
    def delete_marker_filename(self):
        assert self.source_dir
        return os.path.join(self.source_dir, 'pip-delete-this-directory.txt')

DELETE_MARKER_MESSAGE = '''\
This file is placed here by pip to indicate the source was put
here by pip.

Once this package is successfully installed this source code will be
deleted (unless you remove this file).
'''

class RequirementSet(object):

    def __init__(self, build_dir, src_dir, download_dir, download_cache=None,
                 upgrade=False, ignore_installed=False,
                 ignore_dependencies=False):
        self.build_dir = build_dir
        self.src_dir = src_dir
        self.download_dir = download_dir
        self.download_cache = download_cache
        self.upgrade = upgrade
        self.ignore_installed = ignore_installed
        self.requirements = {}
        # Mapping of alias: real_name
        self.requirement_aliases = {}
        self.unnamed_requirements = []
        self.ignore_dependencies = ignore_dependencies
        self.successfully_downloaded = []
        self.successfully_installed = []

    def __str__(self):
        reqs = [req for req in self.requirements.values()
                if not req.comes_from]
        reqs.sort(key=lambda req: req.name.lower())
        return ' '.join([str(req.req) for req in reqs])

    def add_requirement(self, install_req):
        name = install_req.name
        if not name:
            self.unnamed_requirements.append(install_req)
        else:
            if self.has_requirement(name):
                raise InstallationError(
                    'Double requirement given: %s (aready in %s, name=%r)'
                    % (install_req, self.get_requirement(name), name))
            self.requirements[name] = install_req
            ## FIXME: what about other normalizations?  E.g., _ vs. -?
            if name.lower() != name:
                self.requirement_aliases[name.lower()] = name

    def has_requirement(self, project_name):
        for name in project_name, project_name.lower():
            if name in self.requirements or name in self.requirement_aliases:
                return True
        return False

    @property
    def is_download(self):
        if self.download_dir:
            self.download_dir = os.path.expanduser(self.download_dir)
            if os.path.exists(self.download_dir):
                return True
            else:
                logger.fatal('Could not find download directory')
                raise InstallationError(
                    "Could not find or access download directory '%s'"
                    % display_path(self.download_dir))
        return False

    def get_requirement(self, project_name):
        for name in project_name, project_name.lower():
            if name in self.requirements:
                return self.requirements[name]
            if name in self.requirement_aliases:
                return self.requirements[self.requirement_aliases[name]]
        raise KeyError("No project with the name %r" % project_name)

    def uninstall(self, auto_confirm=False):
        for req in self.requirements.values():
            req.uninstall(auto_confirm=auto_confirm)

    def install_files(self, finder, force_root_egg_info=False):
        unnamed = list(self.unnamed_requirements)
        reqs = self.requirements.values()
        while reqs or unnamed:
            if unnamed:
                req_to_install = unnamed.pop(0)
            else:
                req_to_install = reqs.pop(0)
            install = True
            if not self.ignore_installed and not req_to_install.editable:
                req_to_install.check_if_exists()
                if req_to_install.satisfied_by:
                    if self.upgrade:
                        req_to_install.conflicts_with = req_to_install.satisfied_by
                        req_to_install.satisfied_by = None
                    else:
                        install = False
                if req_to_install.satisfied_by:
                    logger.notify('Requirement already satisfied '
                                  '(use --upgrade to upgrade): %s'
                                  % req_to_install)
            if req_to_install.editable:
                logger.notify('Obtaining %s' % req_to_install)
            elif install:
                if req_to_install.url and req_to_install.url.lower().startswith('file:'):
                    logger.notify('Unpacking %s' % display_path(url_to_filename(req_to_install.url)))
                else:
                    logger.notify('Downloading/unpacking %s' % req_to_install)
            logger.indent += 2
            is_bundle = False
            try:
                if req_to_install.editable:
                    if req_to_install.source_dir is None:
                        location = req_to_install.build_location(self.src_dir)
                        req_to_install.source_dir = location
                    else:
                        location = req_to_install.source_dir
                    if not os.path.exists(self.build_dir):
                        os.makedirs(self.build_dir)
                    req_to_install.update_editable(not self.is_download)
                    if self.is_download:
                        req_to_install.run_egg_info()
                        req_to_install.archive(self.download_dir)
                    else:
                        req_to_install.run_egg_info()
                elif install:
                    location = req_to_install.build_location(self.build_dir, not self.is_download)
                    ## FIXME: is the existance of the checkout good enough to use it?  I don't think so.
                    unpack = True
                    if not os.path.exists(os.path.join(location, 'setup.py')):
                        ## FIXME: this won't upgrade when there's an existing package unpacked in `location`
                        if req_to_install.url is None:
                            url = finder.find_requirement(req_to_install, upgrade=self.upgrade)
                        else:
                            ## FIXME: should req_to_install.url already be a link?
                            url = Link(req_to_install.url)
                            assert url
                        if url:
                            try:
                                self.unpack_url(url, location, self.is_download)
                            except urllib2.HTTPError, e:
                                logger.fatal('Could not install requirement %s because of error %s'
                                             % (req_to_install, e))
                                raise InstallationError(
                                    'Could not install requirement %s because of HTTP error %s for URL %s'
                                    % (req_to_install, e, url))
                        else:
                            unpack = False
                    if unpack:
                        is_bundle = req_to_install.is_bundle
                        url = None
                        if is_bundle:
                            req_to_install.move_bundle_files(self.build_dir, self.src_dir)
                            for subreq in req_to_install.bundle_requirements():
                                reqs.append(subreq)
                                self.add_requirement(subreq)
                        elif self.is_download:
                            req_to_install.source_dir = location
                            if url and url.scheme in vcs.all_schemes:
                                req_to_install.run_egg_info()
                                req_to_install.archive(self.download_dir)
                        else:
                            req_to_install.source_dir = location
                            req_to_install.run_egg_info()
                            if force_root_egg_info:
                                # We need to run this to make sure that the .egg-info/
                                # directory is created for packing in the bundle
                                req_to_install.run_egg_info(force_root_egg_info=True)
                            req_to_install.assert_source_matches_version()
                            f = open(req_to_install.delete_marker_filename, 'w')
                            f.write(DELETE_MARKER_MESSAGE)
                            f.close()
                if not is_bundle and not self.is_download:
                    ## FIXME: shouldn't be globally added:
                    finder.add_dependency_links(req_to_install.dependency_links)
                    ## FIXME: add extras in here:
                    if not self.ignore_dependencies:
                        for req in req_to_install.requirements():
                            try:
                                name = pkg_resources.Requirement.parse(req).project_name
                            except ValueError, e:
                                ## FIXME: proper warning
                                logger.error('Invalid requirement: %r (%s) in requirement %s' % (req, e, req_to_install))
                                continue
                            if self.has_requirement(name):
                                ## FIXME: check for conflict
                                continue
                            subreq = InstallRequirement(req, req_to_install)
                            reqs.append(subreq)
                            self.add_requirement(subreq)
                    if req_to_install.name not in self.requirements:
                        self.requirements[req_to_install.name] = req_to_install
                else:
                    req_to_install.remove_temporary_source()
                if install:
                    self.successfully_downloaded.append(req_to_install)
            finally:
                logger.indent -= 2

    def unpack_url(self, link, location, only_download=False):
        if only_download:
            location = self.download_dir
        for backend in vcs.backends:
            if link.scheme in backend.schemes:
                vcs_backend = backend(link.url)
                if only_download:
                    vcs_backend.export(location)
                else:
                    vcs_backend.unpack(location)
                return
        dir = tempfile.mkdtemp()
        if link.url.lower().startswith('file:'):
            source = url_to_filename(link.url)
            content_type = mimetypes.guess_type(source)[0]
            self.unpack_file(source, location, content_type, link)
            return
        md5_hash = link.md5_hash
        target_url = link.url.split('#', 1)[0]
        target_file = None
        if self.download_cache:
            if not os.path.isdir(self.download_cache):
                logger.indent -= 2
                logger.notify('Creating supposed download cache at %s' % self.download_cache)
                logger.indent += 2
                os.makedirs(self.download_cache)
            target_file = os.path.join(self.download_cache,
                                       urllib.quote(target_url, ''))
        if (target_file and os.path.exists(target_file)
            and os.path.exists(target_file+'.content-type')):
            fp = open(target_file+'.content-type')
            content_type = fp.read().strip()
            fp.close()
            if md5_hash:
                download_hash = md5()
                fp = open(target_file, 'rb')
                while 1:
                    chunk = fp.read(4096)
                    if not chunk:
                        break
                    download_hash.update(chunk)
                fp.close()
            temp_location = target_file
            logger.notify('Using download cache from %s' % target_file)
        else:
            try:
                resp = urllib2.urlopen(target_url)
            except urllib2.HTTPError, e:
                logger.fatal("HTTP error %s while getting %s" % (e.code, link))
                raise
            except IOError, e:
                # Typically an FTP error
                logger.fatal("Error %s while getting %s" % (e, link))
                raise
            content_type = resp.info()['content-type']
            filename = link.filename
            ext = splitext(filename)[1]
            if not ext:
                ext = mimetypes.guess_extension(content_type)
                if ext:
                    filename += ext
            if not ext and link.url != resp.geturl():
                ext = os.path.splitext(resp.geturl())[1]
                if ext:
                    filename += ext
            temp_location = os.path.join(dir, filename)
            fp = open(temp_location, 'wb')
            if md5_hash:
                download_hash = md5()
            try:
                total_length = int(resp.info()['content-length'])
            except (ValueError, KeyError):
                total_length = 0
            downloaded = 0
            show_progress = total_length > 40*1000 or not total_length
            show_url = link.show_url
            try:
                if show_progress:
                    ## FIXME: the URL can get really long in this message:
                    if total_length:
                        logger.start_progress('Downloading %s (%s): ' % (show_url, format_size(total_length)))
                    else:
                        logger.start_progress('Downloading %s (unknown size): ' % show_url)
                else:
                    logger.notify('Downloading %s' % show_url)
                logger.debug('Downloading from URL %s' % link)
                while 1:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if show_progress:
                        if not total_length:
                            logger.show_progress('%s' % format_size(downloaded))
                        else:
                            logger.show_progress('%3i%%  %s' % (100*downloaded/total_length, format_size(downloaded)))
                    if md5_hash:
                        download_hash.update(chunk)
                    fp.write(chunk)
                fp.close()
            finally:
                if show_progress:
                    logger.end_progress('%s downloaded' % format_size(downloaded))
        if md5_hash:
            download_hash = download_hash.hexdigest()
            if download_hash != md5_hash:
                logger.fatal("MD5 hash of the package %s (%s) doesn't match the expected hash %s!"
                             % (link, download_hash, md5_hash))
                raise InstallationError('Bad MD5 hash for package %s' % link)
        if only_download:
            self.copy_file(temp_location, location, content_type, link)
        else:
            self.unpack_file(temp_location, location, content_type, link)
        if target_file and target_file != temp_location:
            logger.notify('Storing download in cache at %s' % display_path(target_file))
            shutil.copyfile(temp_location, target_file)
            fp = open(target_file+'.content-type', 'w')
            fp.write(content_type)
            fp.close()
            os.unlink(temp_location)
        if target_file is None:
            os.unlink(temp_location)

    def copy_file(self, filename, location, content_type, link):
        copy = True
        download_location = os.path.join(location, link.filename)
        if os.path.exists(download_location):
            response = ask('The file %s exists. (i)gnore, (w)ipe, (b)ackup '
                           % display_path(download_location), ('i', 'w', 'b'))
            if response == 'i':
                copy = False
            elif response == 'w':
                logger.warn('Deleting %s' % display_path(download_location))
                os.remove(download_location)
            elif response == 'b':
                dest_file = backup_dir(download_location)
                logger.warn('Backing up %s to %s'
                            % (display_path(download_location), display_path(dest_file)))
                shutil.move(download_location, dest_file)
        if copy:
            shutil.copy(filename, download_location)
            logger.indent -= 2
            logger.notify('Saved %s' % display_path(download_location))

    def unpack_file(self, filename, location, content_type, link):
        if (content_type == 'application/zip'
            or filename.endswith('.zip')
            or filename.endswith('.pybundle')
            or zipfile.is_zipfile(filename)):
            self.unzip_file(filename, location, flatten=not filename.endswith('.pybundle'))
        elif (content_type == 'application/x-gzip'
              or tarfile.is_tarfile(filename)
              or splitext(filename)[1].lower() in ('.tar', '.tar.gz', '.tar.bz2', '.tgz', '.tbz')):
            self.untar_file(filename, location)
        elif (content_type and content_type.startswith('text/html')
              and is_svn_page(file_contents(filename))):
            # We don't really care about this
            from pip.vcs.subversion import Subversion
            Subversion('svn+' + link.url).unpack(location)
        else:
            ## FIXME: handle?
            ## FIXME: magic signatures?
            logger.fatal('Cannot unpack file %s (downloaded from %s, content-type: %s); cannot detect archive format'
                         % (filename, location, content_type))
            raise InstallationError('Cannot determine archive format of %s' % location)

    def unzip_file(self, filename, location, flatten=True):
        """Unzip the file (zip file located at filename) to the destination
        location"""
        if not os.path.exists(location):
            os.makedirs(location)
        zipfp = open(filename, 'rb')
        try:
            zip = zipfile.ZipFile(zipfp)
            leading = has_leading_dir(zip.namelist()) and flatten
            for name in zip.namelist():
                data = zip.read(name)
                fn = name
                if leading:
                    fn = split_leading_dir(name)[1]
                fn = os.path.join(location, fn)
                dir = os.path.dirname(fn)
                if not os.path.exists(dir):
                    os.makedirs(dir)
                if fn.endswith('/') or fn.endswith('\\'):
                    # A directory
                    if not os.path.exists(fn):
                        os.makedirs(fn)
                else:
                    fp = open(fn, 'wb')
                    try:
                        fp.write(data)
                    finally:
                        fp.close()
        finally:
            zipfp.close()

    def untar_file(self, filename, location):
        """Untar the file (tar file located at filename) to the destination location"""
        if not os.path.exists(location):
            os.makedirs(location)
        if filename.lower().endswith('.gz') or filename.lower().endswith('.tgz'):
            mode = 'r:gz'
        elif filename.lower().endswith('.bz2') or filename.lower().endswith('.tbz'):
            mode = 'r:bz2'
        elif filename.lower().endswith('.tar'):
            mode = 'r'
        else:
            logger.warn('Cannot determine compression type for file %s' % filename)
            mode = 'r:*'
        tar = tarfile.open(filename, mode)
        try:
            leading = has_leading_dir([member.name for member in tar.getmembers()])
            for member in tar.getmembers():
                fn = member.name
                if leading:
                    fn = split_leading_dir(fn)[1]
                path = os.path.join(location, fn)
                if member.isdir():
                    if not os.path.exists(path):
                        os.makedirs(path)
                else:
                    try:
                        fp = tar.extractfile(member)
                    except (KeyError, AttributeError), e:
                        # Some corrupt tar files seem to produce this
                        # (specifically bad symlinks)
                        logger.warn(
                            'In the tar file %s the member %s is invalid: %s'
                            % (filename, member.name, e))
                        continue
                    if not os.path.exists(os.path.dirname(path)):
                        os.makedirs(os.path.dirname(path))
                    destfp = open(path, 'wb')
                    try:
                        shutil.copyfileobj(fp, destfp)
                    finally:
                        destfp.close()
                    fp.close()
        finally:
            tar.close()

    def install(self, install_options):
        """Install everything in this set (after having downloaded and unpacked the packages)"""
        to_install = sorted([r for r in self.requirements.values()
                             if self.upgrade or not r.satisfied_by],
                            key=lambda p: p.name.lower())
        if to_install:
            logger.notify('Installing collected packages: %s' % (', '.join([req.name for req in to_install])))
        logger.indent += 2
        try:
            for requirement in to_install:
                if requirement.conflicts_with:
                    logger.notify('Found existing installation: %s'
                                  % requirement.conflicts_with)
                    logger.indent += 2
                    try:
                        requirement.uninstall(auto_confirm=True)
                    finally:
                        logger.indent -= 2
                try:
                    requirement.install(install_options)
                except:
                    # if install did not succeed, rollback previous uninstall
                    if requirement.conflicts_with and not requirement.install_succeeded:
                        requirement.rollback_uninstall()
                    raise
                requirement.remove_temporary_source()
        finally:
            logger.indent -= 2
        self.successfully_installed = to_install

    def create_bundle(self, bundle_filename):
        ## FIXME: can't decide which is better; zip is easier to read
        ## random files from, but tar.bz2 is smaller and not as lame a
        ## format.

        ## FIXME: this file should really include a manifest of the
        ## packages, maybe some other metadata files.  It would make
        ## it easier to detect as well.
        zip = zipfile.ZipFile(bundle_filename, 'w', zipfile.ZIP_DEFLATED)
        vcs_dirs = []
        for dir, basename in (self.build_dir, 'build'), (self.src_dir, 'src'):
            dir = os.path.normcase(os.path.abspath(dir))
            for dirpath, dirnames, filenames in os.walk(dir):
                for backend in vcs.backends:
                    vcs_backend = backend()
                    vcs_url = vcs_rev = None
                    if vcs_backend.dirname in dirnames:
                        for vcs_dir in vcs_dirs:
                            if dirpath.startswith(vcs_dir):
                                # vcs bundle file already in parent directory
                                break
                        else:
                            vcs_url, vcs_rev = vcs_backend.get_info(
                                os.path.join(dir, dirpath))
                            vcs_dirs.append(dirpath)
                        vcs_bundle_file = vcs_backend.bundle_file
                        vcs_guide = vcs_backend.guide % {'url': vcs_url,
                                                         'rev': vcs_rev}
                        dirnames.remove(vcs_backend.dirname)
                        break
                if 'pip-egg-info' in dirnames:
                    dirnames.remove('pip-egg-info')
                for dirname in dirnames:
                    dirname = os.path.join(dirpath, dirname)
                    name = self._clean_zip_name(dirname, dir)
                    zip.writestr(basename + '/' + name + '/', '')
                for filename in filenames:
                    if filename == 'pip-delete-this-directory.txt':
                        continue
                    filename = os.path.join(dirpath, filename)
                    name = self._clean_zip_name(filename, dir)
                    zip.write(filename, basename + '/' + name)
                if vcs_url:
                    name = os.path.join(dirpath, vcs_bundle_file)
                    name = self._clean_zip_name(name, dir)
                    zip.writestr(basename + '/' + name, vcs_guide)

        zip.writestr('pip-manifest.txt', self.bundle_requirements())
        zip.close()
        # Unlike installation, this will always delete the build directories
        logger.info('Removing temporary build dir %s and source dir %s'
                    % (self.build_dir, self.src_dir))
        for dir in self.build_dir, self.src_dir:
            if os.path.exists(dir):
                ## FIXME: should this use pip.util.rmtree?
                shutil.rmtree(dir)


    BUNDLE_HEADER = '''\
# This is a pip bundle file, that contains many source packages
# that can be installed as a group.  You can install this like:
#     pip this_file.zip
# The rest of the file contains a list of all the packages included:
'''

    def bundle_requirements(self):
        parts = [self.BUNDLE_HEADER]
        for req in sorted(
            [req for req in self.requirements.values()
             if not req.comes_from],
            key=lambda x: x.name):
            parts.append('%s==%s\n' % (req.name, req.installed_version))
        parts.append('# These packages were installed to satisfy the above requirements:\n')
        for req in sorted(
            [req for req in self.requirements.values()
             if req.comes_from],
            key=lambda x: x.name):
            parts.append('%s==%s\n' % (req.name, req.installed_version))
        ## FIXME: should we do something with self.unnamed_requirements?
        return ''.join(parts)

    def _clean_zip_name(self, name, prefix):
        assert name.startswith(prefix+'/'), (
            "name %r doesn't start with prefix %r" % (name, prefix))
        name = name[len(prefix)+1:]
        name = name.replace(os.path.sep, '/')
        return name

_scheme_re = re.compile(r'^(http|https|file):', re.I)

def parse_requirements(filename, finder=None, comes_from=None, options=None):
    skip_match = None
    skip_regex = options.skip_requirements_regex
    if skip_regex:
        skip_match = re.compile(skip_regex)
    filename, content = get_file_content(filename, comes_from=comes_from)
    for line_number, line in enumerate(content.splitlines()):
        line_number += 1
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if skip_match and skip_match.search(line):
            continue
        if line.startswith('-r') or line.startswith('--requirement'):
            if line.startswith('-r'):
                req_url = line[2:].strip()
            else:
                req_url = line[len('--requirement'):].strip().strip('=')
            if _scheme_re.search(filename):
                # Relative to a URL
                req_url = urlparse.urljoin(req_url, filename)
            elif not _scheme_re.search(req_url):
                req_url = os.path.join(os.path.dirname(filename), req_url)
            for item in parse_requirements(req_url, finder, comes_from=filename, options=options):
                yield item
        elif line.startswith('-Z') or line.startswith('--always-unzip'):
            # No longer used, but previously these were used in
            # requirement files, so we'll ignore.
            pass
        elif finder and line.startswith('-f') or line.startswith('--find-links'):
            if line.startswith('-f'):
                line = line[2:].strip()
            else:
                line = line[len('--find-links'):].strip().lstrip('=')
            ## FIXME: it would be nice to keep track of the source of
            ## the find_links:
            finder.find_links.append(line)
        elif line.startswith('-i') or line.startswith('--index-url'):
            if line.startswith('-i'):
                line = line[2:].strip()
            else:
                line = line[len('--index-url'):].strip().lstrip('=')
            finder.index_urls = [line]
        elif line.startswith('--extra-index-url'):
            line = line[len('--extra-index-url'):].strip().lstrip('=')
            finder.index_urls.append(line)
        else:
            comes_from = '-r %s (line %s)' % (filename, line_number)
            if line.startswith('-e') or line.startswith('--editable'):
                if line.startswith('-e'):
                    line = line[2:].strip()
                else:
                    line = line[len('--editable'):].strip()
                req = InstallRequirement.from_editable(
                    line, comes_from=comes_from, default_vcs=options.default_vcs)
            else:
                req = InstallRequirement.from_line(line, comes_from)
            yield req

def parse_editable(editable_req, default_vcs=None):
    """Parses svn+http://blahblah@rev#egg=Foobar into a requirement
    (Foobar) and a URL"""
    url = editable_req
    if os.path.isdir(url) and os.path.exists(os.path.join(url, 'setup.py')):
        # Treating it as code that has already been checked out
        url = filename_to_url(url)
    if url.lower().startswith('file:'):
        return None, url
    for version_control in vcs:
        if url.lower().startswith('%s:' % version_control):
            url = '%s+%s' % (version_control, url)
    if '+' not in url:
        if default_vcs:
            url = default_vcs + '+' + url
        else:
            raise InstallationError(
                '--editable=%s should be formatted with svn+URL, git+URL, hg+URL or bzr+URL' % editable_req)
    vc_type = url.split('+', 1)[0].lower()
    if not vcs.get_backend(vc_type):
        raise InstallationError(
            'For --editable=%s only svn (svn+URL), Git (git+URL), Mercurial (hg+URL) and Bazaar (bzr+URL) is currently supported' % editable_req)
    match = re.search(r'(?:#|#.*?&)egg=([^&]*)', editable_req)
    if (not match or not match.group(1)) and vcs.get_backend(vc_type):
        parts = [p for p in editable_req.split('#', 1)[0].split('/') if p]
        if parts[-2] in ('tags', 'branches', 'tag', 'branch'):
            req = parts[-3]
        elif parts[-1] == 'trunk':
            req = parts[-2]
        else:
            raise InstallationError(
                '--editable=%s is not the right format; it must have #egg=Package'
                % editable_req)
    else:
        req = match.group(1)
    ## FIXME: use package_to_requirement?
    match = re.search(r'^(.*?)(?:-dev|-\d.*)', req)
    if match:
        # Strip off -dev, -0.2, etc.
        req = match.group(1)
    return req, url

class UninstallPathSet(object):
    """A set of file paths to be removed in the uninstallation of a
    requirement."""
    def __init__(self, dist, restrict_to_prefix):
        self.paths = set()
        self._refuse = set()
        self.pth = {}
        self.prefix = os.path.normcase(os.path.realpath(restrict_to_prefix))
        self.dist = dist
        self.location = dist.location
        self.save_dir = None
        self._moved_paths = []

    def _can_uninstall(self):
        prefix, stripped = strip_prefix(self.location, self.prefix)
        if not stripped:
            logger.notify("Not uninstalling %s at %s, outside environment %s"
                          % (self.dist.project_name, self.dist.location,
                             self.prefix))
            return False
        return True

    def add(self, path):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return
        prefix, stripped = strip_prefix(os.path.normcase(path), self.prefix)
        if stripped:
            self.paths.add((prefix, stripped))
        else:
            self._refuse.add((prefix, path))

    def add_pth(self, pth_file, entry):
        prefix, stripped = strip_prefix(os.path.normcase(pth_file), self.prefix)
        if stripped:
            entry = os.path.normcase(entry)
            if stripped not in self.pth:
                self.pth[stripped] = UninstallPthEntries(os.path.join(prefix, stripped))
            self.pth[stripped].add(os.path.normcase(entry))
        else:
            self._refuse.add((prefix, pth_file))

    def compact(self, paths):
        """Compact a path set to contain the minimal number of paths
        necessary to contain all paths in the set. If /a/path/ and
        /a/path/to/a/file.txt are both in the set, leave only the
        shorter path."""
        short_paths = set()
        def sort_set(x, y):
            prefix_x, path_x = x
            prefix_y, path_y = y
            return cmp(len(path_x), len(path_y))
        for prefix, path in sorted(paths, sort_set):
            if not any([(path.startswith(shortpath) and
                         path[len(shortpath.rstrip(os.path.sep))] == os.path.sep)
                        for shortprefix, shortpath in short_paths]):
                short_paths.add((prefix, path))
        return short_paths

    def remove(self, auto_confirm=False):
        """Remove paths in ``self.paths`` with confirmation (unless
        ``auto_confirm`` is True)."""
        if not self._can_uninstall():
            return
        logger.notify('Uninstalling %s:' % self.dist.project_name)
        logger.indent += 2
        paths = sorted(self.compact(self.paths))
        try:
            if auto_confirm:
                response = 'y'
            else:
                for prefix, path in paths:
                    logger.notify(os.path.join(prefix, path))
                response = ask('Proceed (y/n)? ', ('y', 'n'))
            if self._refuse:
                logger.notify('Not removing or modifying (outside of prefix):')
                for prefix, path in self.compact(self._refuse):
                    logger.notify(os.path.join(prefix, path))
            if response == 'y':
                self.save_dir = tempfile.mkdtemp('-uninstall', 'pip-')
                for prefix, path in paths:
                    full_path = os.path.join(prefix, path)
                    new_path = os.path.join(self.save_dir, path)
                    new_dir = os.path.dirname(new_path)
                    logger.info('Removing file or directory %s' % full_path)
                    self._moved_paths.append((prefix, path))
                    os.renames(full_path, new_path)
                for pth in self.pth.values():
                    pth.remove()
                logger.notify('Successfully uninstalled %s' % self.dist.project_name)

        finally:
            logger.indent -= 2

    def rollback(self):
        """Rollback the changes previously made by remove()."""
        if self.save_dir is None:
            logger.error("Can't roll back %s; was not uninstalled" % self.dist.project_name)
            return False
        logger.notify('Rolling back uninstall of %s' % self.dist.project_name)
        for prefix, path in self._moved_paths:
            tmp_path = os.path.join(self.save_dir, path)
            real_path = os.path.join(prefix, path)
            logger.info('Replacing %s' % real_path)
            os.renames(tmp_path, real_path)
        for pth in self.pth:
            pth.rollback()

    def commit(self):
        """Remove temporary save dir: rollback will no longer be possible."""
        if self.save_dir is not None:
            shutil.rmtree(self.save_dir)
            self.save_dir = None
            self._moved_paths = []


class UninstallPthEntries(object):
    def __init__(self, pth_file):
        if not os.path.isfile(pth_file):
            raise UninstallationError("Cannot remove entries from nonexistent file %s" % pth_file)
        self.file = pth_file
        self.entries = set()
        self._saved_lines = None

    def add(self, entry):
        self.entries.add(entry)

    def remove(self):
        logger.info('Removing pth entries from %s:' % self.file)
        fh = open(self.file, 'r')
        lines = fh.readlines()
        self._saved_lines = lines
        fh.close()
        try:
            for entry in self.entries:
                logger.info('Removing entry: %s' % entry)
            try:
                lines.remove(entry + '\n')
            except ValueError:
                pass
        finally:
            pass
        fh = open(self.file, 'w')
        fh.writelines(lines)
        fh.close()

    def rollback(self):
        if self._saved_lines is None:
            logger.error('Cannot roll back changes to %s, none were made' % self.file)
            return False
        logger.info('Rolling %s back to previous state' % self.file)
        fh = open(self.file, 'w')
        fh.writelines(self._saved_lines)
        fh.close()
        return True

class FakeFile(object):
    """Wrap a list of lines in an object with readline() to make
    ConfigParser happy."""
    def __init__(self, lines):
        self._gen = (l for l in lines)

    def readline(self):
        try:
            return self._gen.next()
        except StopIteration:
            return ''