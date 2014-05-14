import os.path
from rez.backport.lru_cache import lru_cache
from rez.util import print_warning_once, Common, encode_filesystem_name, \
    propertycache, is_subdirectory, convert_to_user_dict, RO_AttrDictWrapper
from rez.resources import iter_resources, load_metadata
from rez.vendor.version.version import Version, VersionRange
from rez.vendor.version.requirement import VersionedObject, Requirement
from rez.exceptions import PackageNotFoundError
from rez.settings import settings, Settings


"""
PACKAGE_NAME_REGSTR = '[a-zA-Z_][a-zA-Z0-9_]*'
PACKAGE_NAME_REGEX = re.compile(PACKAGE_NAME_REGSTR + '$')
PACKAGE_NAME_SEP_REGEX = re.compile(r'[-@#]')
PACKAGE_REQ_SEP_REGEX = re.compile(r'[-@#=<>]')
"""


# cached package.* file reads
# FIXME: we're no longer caching metadata loads
@lru_cache(maxsize=1024)
def _load_metadata(path, variables):
    return load_metadata(path, variables)


def join_name(family_name, version):
    return '%s-%s' % (family_name, version)

resource_classes = {}

def iter_package_families(name=None, paths=None):
    """Iterate through top-level `PackageFamily` instances."""
    paths = settings.default(paths, "packages_path")

    pkg_iter = iter_resources(0,  # configuration version
                              ['package_family.folder',
                               'package_family.external'],
                              paths,
                              name=name)

    for resource in pkg_iter:
        yield resource_classes[resource.key](path=resource.path,
                                             **resource.variables)


def iter_packages(name, range=None, timestamp=None, paths=None):
    """Iterate over `Package` instances, sorted by version.

    Packages of the same name and version earlier in the search path take
    precedence - equivalent packages later in the paths are ignored.

    Args:
        name (str): Name of the package, eg 'maya'.
        range (VersionRange, optional): If provided, limits the versions
            returned.
        timestamp (int, optional): Any package newer than this time epoch is
            ignored.
        paths (list of str): paths to search for pkgs, defaults to
            `settings.packages_path`.

    Returns:
        Package object iterator.
    """
    packages = []
    consumed = set()
    for fam in iter_package_families(name, paths):
        for pkg in fam.iter_version_packages():
            pkgname = pkg.qualified_name
            if pkgname not in consumed:
                if (timestamp and pkg.data.timestamp > timestamp) \
                        or (range and pkg.version not in range):
                    continue

                consumed.add(pkgname)
                packages.append(pkg)

    packages = sorted(packages, key=lambda x: x.version)
    return iter(packages)


class PackageFamily(Common):
    """A package family has a single root directory, with a sub-directory for
    each version.
    """
    def __init__(self, name, path, data=None):
        self.name = name
        self.path = path
        self._metadata = data

    def __str__(self):
        return "%s@%s" % (self.name, self.path)

    @property
    def metadata(self):
        if callable(self._metadata):
            # load the metadata
            self._metadata = self._metadata()
        return self._metadata

    def iter_version_packages(self):
        pkg_iter = iter_resources(0,  # configuration version
                                  ['package.versionless', 'package.versioned'],
                                  [self.path],
                                  name=self.name)

        for resource in pkg_iter:
            yield resource_classes[resource.key](path=resource.path,
                                                 data=resource.load,
                                                 **resource.variables)

class ExternalPackageFamily(PackageFamily):
    """
    special case where the entire package is stored in one file
    """
    def __init__(self, name, path, data=None):
        super(ExternalPackageFamily, self).__init__(name, path)

    def iter_version_packages(self):
        versions = self.metadata.get('versions', None)
        if not versions:
            yield Package(name=self.name,
                          version=Version(''),
                          path=self.path,
                          data=self.metadata)  # copy this metadata?
        else:
            for ver_data in versions:
                yield Package(name=self.name,
                              version=ver_data['version'],
                              path=self.path,
                              data=ver_data)

class PackageBase(Common):
    """Abstract base class for Package and Variant."""
    def __init__(self, path, name=None, version=None, data=None):
        self.name = name
        if isinstance(version, basestring):
            self.version = Version(version)
        else:
            self.version = version
        self.metafile = path
        self.base = os.path.dirname(path)
        self._metadata = data

        if name is None or version is None:
            self.name = self.metadata["name"]
            try:
                self.version = self.metadata["version"] or Version()
            except KeyError:
                self.version = Version()

    @propertycache
    def qualified_name(self):
        o = VersionedObject.construct(self.name, self.version)
        return str(o)

    @property
    def metadata(self):
        """Dictionary of metadata for this package"""
        if callable(self._metadata):
            # load the metadata
            self._metadata = self._metadata()
        return self._metadata

    @propertycache
    def data(self):
        """Read-only dictionary of metadata for this package with nested,
        attribute-based access for the keys in the dictionary.

        Returns:
            RO_AttrDictWrapper
        """
        return convert_to_user_dict(self.metadata, RO_AttrDictWrapper)

    @propertycache
    def settings(self):
        """Packages can optionally override rez settings during build."""
        overrides = self.metadata["rezconfig"] or None
        return Settings(overrides=overrides)

    @propertycache
    def is_local(self):
        """Returns True if this package is in the local packages path."""
        return is_subdirectory(self.metafile, self.settings.local_packages_path)

    def _base_path(self):
        path = os.path.dirname(self.base)
        if not self.version:
            path = os.path.dirname(path)
        return os.path.dirname(path)

    def __str__(self):
        return "%s@%s" % (self.qualified_name, self._base_path())


class Package(PackageBase):
    """Class representing a package definition, as read from a package.* file.
    """
    def __init__(self, path, name=None, version=None, data=None):
        """Create a package.

        Args:
            path (str): Either a filepath to a package definition file, or a
                path to the directory containing the definition file.
            name (str): Name of the package, eg 'maya'.
            version (Version): version of the package.
        """
        super(Package, self).__init__(path, name, version, data)

    @property
    def num_variants(self):
        """Return the number of variants in this package. Returns zero if there
        are no variants."""
        # FIXME: move default to resources
        variants = self.metadata["variants"] or []
        return len(variants)

    def get_variant(self, index=None):
        """Return a variant from the definition.

        Note that even a package that does not contain variants will return a
        Variant object with index=None.
        """
        n = self.num_variants
        if index is None:
            if n:
                raise IndexError("there are variants, index must be non-None")
        elif index not in range(n):
            raise IndexError("variant index out of range")

        return Variant(path=self.metafile,
                       name=self.name,
                       version=self.version,
                       index=index,
                       data=self._metadata)

    def iter_variants(self):
        """Returns an iterator over the variants in this package."""
        n = self.num_variants
        if n:
            for i in range(n):
                yield self.get_variant(i)
        else:
            yield self.get_variant()

    def __eq__(self, other):
        return (self.name == other.name) \
            and (self.version == other.version) \
            and (self.metafile == other.metafile)


class Variant(PackageBase):
    """Class representing a variant of a package.

    Note that Variant is also used in packages that don't have a variant - in
    this case, index is None. This helps give a consistent interface.
    """
    def __init__(self, path, name=None, version=None, index=None, data=None):
        """Create a package variant.

        Args:
            path (str): Either a filepath to a package definition file, or a
                path to the directory containing the definition file.
            name (str): Name of the package, eg 'maya'.
            version (Version): Version of the package.
            index (int): Zero-based variant index. If the package does not
                contain variants, index should be set to None.
        """
        super(Variant, self).__init__(path, name, version, data)
        self.index = index
        self.root = self.base

        metadata = self.metadata
        # FIXME: move default to schema
        requires = metadata["requires"] or []

        if self.index is not None:
            try:
                var_requires = metadata["variants"][self.index]
            except IndexError:
                raise IndexError("variant index out of range")

            requires = requires + var_requires
            dirs = [encode_filesystem_name(x) for x in var_requires]
            self.root = os.path.join(self.base, os.path.join(*dirs))

            # backwards compatibility with rez-1
            if (not os.path.exists(self.root)) and (dirs != var_requires):
                root = os.path.join(self.base, os.path.join(*var_requires))
                if os.path.exists(root):
                    self.root = root

        self._requires = [Requirement(x) for x in requires]

    @property
    def qualified_package_name(self):
        return super(Variant, self).qualified_name

    @property
    def qualified_name(self):
        s = super(Variant, self).qualified_name
        if self.index is not None:
            s += "[%d]" % self.index
        return s

    @property
    def subpath(self):
        if self.index is None:
            return ''
        else:
            path = os.path.relpath(self.root, self.base)
            return os.path.normpath(path)

    def requires(self, build_requires=False, private_build_requires=False):
        """Get the requirements of the variant.

        Args:
            build_requires (bool): If True, include build requirements.
            private_build_requires (bool): If True, include private build
                requirements.

        Returns:
            List of `Requirement` objects.
        """
        requires = self._requires
        if build_requires:
            reqs = self.metadata["build_requires"]
            if reqs:
                requires = requires + [Requirement(x) for x in reqs]

        if private_build_requires:
            reqs = self.metadata["private_build_requires"]
            if reqs:
                requires = requires + [Requirement(x) for x in reqs]

        return requires

    def to_dict(self):
        return dict(
            name=self.name,
            version=str(self.version),
            metafile=self.metafile,
            index=self.index)

    @classmethod
    def from_dict(cls, d):
        return Variant(path=d["metafile"],
                       name=d["name"],
                       version=Version(d["version"]),
                       index=d["index"])

    def __eq__(self, other):
        return (self.name == other.name) \
            and (self.version == other.version) \
            and (self.metafile == other.metafile) \
            and (self.index == other.index)

    def __str__(self):
        return "%s@%s,%s" % (self.qualified_name, self._base_path(),
                             self.subpath)

resource_classes['package_family.folder'] = PackageFamily
resource_classes['package_family.external'] = ExternalPackageFamily
resource_classes['package.versionless'] = Package
resource_classes['package.versioned'] = Package
