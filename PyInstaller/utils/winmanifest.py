#-----------------------------------------------------------------------------
# Copyright (c) 2013, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License with exception
# for distributing bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------


# Development notes kept for documentation purposes.
#
# Currently not implemented in the Manifest class:
# * Validation (only very basic sanity checks are currently in place)
# * comClass, typelib, comInterfaceProxyStub and windowClass child elements of 
#   the file element
# * comInterfaceExternalProxyStub and windowClass child elements of the 
#   assembly element
# * Application Configuration File and Multilanguage User Interface (MUI) 
#   support when searching for assembly files
#
# Isolated Applications and Side-by-side Assemblies:
# http://msdn.microsoft.com/en-us/library/dd408052%28VS.85%29.aspx
#
# Changelog:
# 2009-12-17  fix: small glitch in toxml / toprettyxml methods (xml declaration
#                  wasn't replaced when a different encodig than UTF-8 was used)
#             chg: catch xml.parsers.expat.ExpatError and re-raise as 
#                  ManifestXMLParseError
#             chg: support initialize option in parse method also
#
# 2009-12-13  fix: fixed os import
#             fix: skip invalid / empty dependent assemblies
#
# 2009-08-21  fix: Corrected assembly searching sequence for localized 
#                  assemblies
#             fix: Allow assemblies with no dependent files
#
# 2009-07-31  chg: Find private assemblies even if unversioned
#             add: Manifest.same_id method to check if two manifests have the
#                  same assemblyIdentity
#
# 2009-07-30  fix: Potential failure in File.calc_hash method if hash
#                  algorythm not supported
#             add: Publisher configuration (policy) support when searching for 
#                  assembly files
#             fix: Private assemblies are now actually found if present (and no
#                  shared assembly exists)
#             add: Python 2.3 compatibility (oldest version supported by 
#                  pyinstaller)
#
# 2009-07-28  chg: Code cleanup, removed a bit of redundancy
#             add: silent mode (set silent attribute on module)
#             chg: Do not print messages in silent mode
#
# 2009-06-18  chg: Use glob instead of regular expression in Manifest.find_files
#
# 2009-05-04  fix: Don't fail if manifest has empty description
#             fix: Manifests created by the toxml, toprettyxml, writexml or 
#                  writeprettyxml methods are now correctly recognized by 
#                  Windows, which expects the XML declaration to be ordered
#                  version-encoding-standalone (standalone being optional)
#             add: 'encoding' keyword argument in toxml, toprettyxml, writexml 
#                  and writeprettyxml methods
#             chg: UpdateManifestResourcesFromXML and 
#                  UpdateManifestResourcesFromXMLFile: set resource name 
#                  depending on file type ie. exe or dll
#             fix: typo in __main__: UpdateManifestResourcesFromDataFile
#                  should have been UpdateManifestResourcesFromXMLFile
#
# 2009-03-21  First version


"""
Create, parse and write MS Windows Manifest files.
Find files which are part of an assembly, by searching shared and 
private assemblies.
Update or add manifest resources in Win32 PE files.

Commandline usage:
winmanifest.py <dstpath> <xmlpath>
Updates or adds manifest <xmlpath> as resource in Win32 PE file <dstpath>.
"""


import os
from glob import glob
import re
import sys
import xml
from xml.dom import Node, minidom
from xml.dom.minidom import Document, Element

from PyInstaller import compat
from PyInstaller.compat import hashlib, architecture
from PyInstaller import log as logging
logger = logging.getLogger('PyInstaller.build.winmanifest')

try:
    from PyInstaller.utils import winresource
except ImportError, detail:
    winresource = None
    logger.warn(detail)
    logger.warn("Cannot check for assembly dependencies - resource access ")
    logger.warn("unavailable. To enable resource access, please install ")
    logger.warn("http://sourceforge.net/projects/pywin32/")


LANGUAGE_NEUTRAL_NT5 = "x-ww"
LANGUAGE_NEUTRAL_NT6 = "none"
RT_MANIFEST = 24

Document.aChild = Document.appendChild
Document.cE = Document.createElement
Document.cT = Document.createTextNode
Document.getEByTN = Document.getElementsByTagName
Element.aChild = Element.appendChild
Element.getA = Element.getAttribute
Element.getEByTN = Element.getElementsByTagName
Element.remA = Element.removeAttribute
Element.setA = Element.setAttribute


def getChildElementsByTagName(self, tagName):
    """ Return child elements of type tagName if found, else [] """
    result = []
    for child in self.childNodes:
        if isinstance(child, Element):
            if child.tagName == tagName:
                result.append(child)
    return result


def getFirstChildElementByTagName(self, tagName):
    """ Return the first element of type tagName if found, else None """
    for child in self.childNodes:
        if isinstance(child, Element):
            if child.tagName == tagName:
                return child
    return None


Document.getCEByTN = getChildElementsByTagName
Document.getFCEByTN = getFirstChildElementByTagName
Element.getCEByTN = getChildElementsByTagName
Element.getFCEByTN = getFirstChildElementByTagName


class _Dummy:
    pass


if winresource:
    _File = winresource.File
else:
    _File = _Dummy

    
class File(_File):

    """ A file referenced by an assembly inside a manifest. """
    
    def __init__(self, filename="", hashalg=None, hash=None, comClasses=None, 
                 typelibs=None, comInterfaceProxyStubs=None, 
                 windowClasses=None):
        if winresource:
            winresource.File.__init__(self, filename)
        else:
            self.filename = filename
        self.name = os.path.basename(filename)
        if hashalg:
            self.hashalg = hashalg.upper()
        else:
            self.hashalg = None
        if (os.path.isfile(filename) and hashalg and hashlib and
            hasattr(hashlib, hashalg.lower())):
            self.calc_hash()
        else:
            self.hash = hash
        self.comClasses = comClasses or [] # TO-DO: implement
        self.typelibs = typelibs or [] # TO-DO: implement
        self.comInterfaceProxyStubs = comInterfaceProxyStubs or [] # TO-DO: implement
        self.windowClasses = windowClasses or [] # TO-DO: implement

    def calc_hash(self, hashalg=None):
        """
        Calculate the hash of the file.
        
        Will be called automatically from the constructor if the file exists 
        and hashalg is given (and supported), but may also be called manually 
        e.g. to update the hash if the file has changed.
        
        """
        fd = open(self.filename, "rb")
        buf = fd.read()
        fd.close()
        if hashalg:
            self.hashalg = hashalg.upper()
        self.hash = getattr(hashlib, self.hashalg.lower())(buf).hexdigest()

    def find(self, searchpath):
        logger.info("Searching for file %s", self.name)
        fn = os.path.join(searchpath, self.name)
        if os.path.isfile(fn):
            logger.info("Found file %s", fn)
            return fn
        else:
            logger.warn("No such file %s", fn)
            return None


class InvalidManifestError(Exception):
    pass


class ManifestXMLParseError(InvalidManifestError):
    pass


class Manifest(object):
    
    # Manifests:
    # http://msdn.microsoft.com/en-us/library/aa375365%28VS.85%29.aspx

    """
    Manifest constructor.
    
    To build a basic manifest for your application:
      mf = Manifest(type='win32', name='YourAppName', language='*', 
                    processorArchitecture='x86', version=[1, 0, 0, 0])
    
    To write the XML to a manifest file:
      mf.writexml("YourAppName.exe.manifest")
    or
      mf.writeprettyxml("YourAppName.exe.manifest")
    
    """

    def __init__(self, manifestVersion=None, noInheritable=False, 
                 noInherit=False, type_=None, name=None, language=None, 
                 processorArchitecture=None, version=None, 
                 publicKeyToken=None, description=None, 
                 requestedExecutionLevel=None, uiAccess=None, 
                 dependentAssemblies=None, files=None, 
                 comInterfaceExternalProxyStubs=None):
        self.filename = None
        self.optional = None
        self.manifestType = "assembly"
        self.manifestVersion = manifestVersion or [1, 0]
        self.noInheritable = noInheritable
        self.noInherit = noInherit
        self.type = type_
        self.name = name
        self.language = language
        self.processorArchitecture = processorArchitecture
        self.version = version
        self.publicKeyToken = publicKeyToken
        # publicKeyToken:
        # A 16-character hexadecimal string that represents the last 8 bytes 
        # of the SHA-1 hash of the public key under which the assembly is 
        # signed. The public key used to sign the catalog must be 2048 bits 
        # or greater. Required for all shared side-by-side assemblies.
        # http://msdn.microsoft.com/en-us/library/aa375692(VS.85).aspx
        self.applyPublisherPolicy = None
        self.description = None
        self.requestedExecutionLevel = requestedExecutionLevel
        self.uiAccess = uiAccess
        self.dependentAssemblies = dependentAssemblies or []
        self.bindingRedirects = []
        self.files = files or []
        self.comInterfaceExternalProxyStubs = comInterfaceExternalProxyStubs or [] # TO-DO: implement
    
    def add_dependent_assembly(self, manifestVersion=None, noInheritable=False, 
                 noInherit=False, type_=None, name=None, language=None, 
                 processorArchitecture=None, version=None, 
                 publicKeyToken=None, description=None, 
                 requestedExecutionLevel=None, uiAccess=None, 
                 dependentAssemblies=None, files=None, 
                 comInterfaceExternalProxyStubs=None):
        """
        Shortcut for self.dependentAssemblies.append(Manifest(*args, **kwargs))
        """
        self.dependentAssemblies.append(Manifest(manifestVersion, 
                                        noInheritable, noInherit, type_, name, 
                                        language, processorArchitecture, 
                                        version, publicKeyToken, description, 
                                        requestedExecutionLevel, uiAccess, 
                                        dependentAssemblies, files, 
                                        comInterfaceExternalProxyStubs))
        if self.filename:
            # Enable search for private assembly by assigning bogus filename
            # (only the directory has to be correct)
            self.dependentAssemblies[-1].filename = ":".join((self.filename, 
                                                              name))
    
    def add_file(self, name="", hashalg="", hash="", comClasses=None, 
                 typelibs=None, comInterfaceProxyStubs=None, 
                 windowClasses=None):
        """ Shortcut for manifest.files.append """
        self.files.append(File(name, hashalg, hash, comClasses, 
                          typelibs, comInterfaceProxyStubs, windowClasses))
    
    def find_files(self, ignore_policies=True):
        """ Search shared and private assemblies and return a list of files.
        
        If any files are not found, return an empty list.
        
        IMPORTANT NOTE: For the purpose of getting the dependent assembly 
        files of an executable, the publisher configuration (aka policy)
        should be ignored (which is the default). Setting ignore_policies=False 
        is only useful to find out which files are actually loaded at
        runtime.
        
        """
        
        # Shared Assemblies:
        # http://msdn.microsoft.com/en-us/library/aa375996%28VS.85%29.aspx
        #
        # Private Assemblies:
        # http://msdn.microsoft.com/en-us/library/aa375674%28VS.85%29.aspx
        #
        # Assembly Searching Sequence:
        # http://msdn.microsoft.com/en-us/library/aa374224%28VS.85%29.aspx
        #
        # NOTE:
        # Multilanguage User Interface (MUI) support not yet implemented
        
        files = []
        
        languages = []
        if self.language not in (None, "", "*", "neutral"):
            languages.append(self.getlanguage())
            if "-" in self.language:
                # language-culture syntax, e.g. en-us
                # Add only the language part
                languages.append(self.language.split("-")[0])
            if self.language not in ("en-us", "en"):
                languages.append("en-us")
            if self.language != "en":
                languages.append("en")
        languages.append(self.getlanguage("*"))
        
        winsxs = os.path.join(compat.getenv("SystemRoot"), "WinSxS")
        if not os.path.isdir(winsxs):
            logger.warn("No such dir %s", winsxs)
        manifests = os.path.join(winsxs, "Manifests")
        if not os.path.isdir(manifests):
            logger.warn("No such dir %s", manifests)
        if not ignore_policies and self.version:
            if sys.getwindowsversion() < (6, ):
                # Windows XP
                pcfiles = os.path.join(winsxs, "Policies")
                if not os.path.isdir(pcfiles):
                    logger.warn("No such dir %s", pcfiles)
            else:
                # Vista or later
                pcfiles = manifests
        
        for language in languages:
            version = self.version
            
            # Search for publisher configuration
            if not ignore_policies and version:
                # Publisher Configuration (aka policy)
                # A publisher configuration file globally redirects 
                # applications and assemblies having a dependence on one 
                # version of a side-by-side assembly to use another version of 
                # the same assembly. This enables applications and assemblies 
                # to use the updated assembly without having to rebuild all of 
                # the affected applications.
                # http://msdn.microsoft.com/en-us/library/aa375680%28VS.85%29.aspx
                #
                # Under Windows XP and 2003, policies are stored as 
                # <version>.policy files inside 
                # %SystemRoot%\WinSxS\Policies\<name>
                # Under Vista and later, policies are stored as 
                # <name>.manifest files inside %SystemRoot%\winsxs\Manifests
                redirected = False
                if os.path.isdir(pcfiles):
                    logger.info("Searching for publisher configuration %s ...",
                                self.getpolicyid(True, language=language))
                    if sys.getwindowsversion() < (6, ):
                        # Windows XP
                        policies = os.path.join(pcfiles, 
                                                self.getpolicyid(True,
                                                                 language=language) + 
                                                ".policy")
                    else:
                        # Vista or later
                        policies = os.path.join(pcfiles, 
                                                self.getpolicyid(True,
                                                                 language=language) + 
                                                ".manifest")
                    for manifestpth in glob(policies):
                        if not os.path.isfile(manifestpth):
                            logger.warn("Not a file %s", manifestpth)
                            continue
                        logger.info("Found %s", manifestpth)
                        try:
                            policy = ManifestFromXMLFile(manifestpth)
                        except Exception, exc:
                            logger.error("Could not parse file %s", manifestpth)
                            logger.exception(exc)
                        else:
                            logger.info("Checking publisher policy for "
                                        "binding redirects")
                            for assembly in policy.dependentAssemblies:
                                if (not assembly.same_id(self, True) or
                                    assembly.optional):
                                    continue
                                for redirect in assembly.bindingRedirects:
                                    if logger.isEnabledFor(logging.INFO):
                                        old = "-".join([".".join([str(i) 
                                                                  for i in 
                                                                  part]) 
                                                        for part in 
                                                        redirect[0]])
                                        new = ".".join([str(i) 
                                                        for i in
                                                        redirect[1]])
                                        logger.info("Found redirect for "
                                                    "version(s) %s -> %n",
                                                    old, new)
                                    if (version >= redirect[0][0] and
                                        version <= redirect[0][-1] and
                                        version != redirect[1]):
                                        logger.info("Applying redirect "
                                                    "%s -> %s",
                                                    ".".join([str(i) 
                                                              for i in
                                                              version]),
                                                    new)
                                        version = redirect[1]
                                        redirected = True
                    if not redirected:
                        logger.info("Publisher configuration not used")
            
            # Search for assemblies according to assembly searching sequence
            paths = []
            if os.path.isdir(manifests):
                # Add winsxs search paths
                paths.extend(glob(os.path.join(manifests, 
                                               self.getid(language=language, 
                                                          version=version) + 
                                               "_*.manifest")))
            if self.filename:
                # Add private assembly search paths
                dirnm = os.path.dirname(self.filename)
                if language in (LANGUAGE_NEUTRAL_NT5, 
                                LANGUAGE_NEUTRAL_NT6):
                    for ext in (".dll", ".manifest"):
                        paths.extend(glob(os.path.join(dirnm, self.name + ext)))
                        paths.extend(glob(os.path.join(dirnm, self.name, 
                                                       self.name + ext)))
                else:
                    for ext in (".dll", ".manifest"):
                        paths.extend(glob(os.path.join(dirnm, language, 
                                                       self.name + ext)))
                    for ext in (".dll", ".manifest"):
                        paths.extend(glob(os.path.join(dirnm, language, 
                                                       self.name, 
                                                       self.name + ext)))
            logger.info("Searching for assembly %s ...",
                        self.getid(language=language, version=version))
            for manifestpth in paths:
                if not os.path.isfile(manifestpth):
                    logger.warn("Not a file %s", manifestpth)
                    continue
                assemblynm = os.path.basename(
                    os.path.splitext(manifestpth)[0])
                try:
                    if manifestpth.endswith(".dll"):
                        logger.info("Found manifest in %s", manifestpth)
                        manifest = ManifestFromResFile(manifestpth, [1])
                    else:
                        logger.info("Found manifest %s", manifestpth)
                        manifest = ManifestFromXMLFile(manifestpth)
                except Exception, exc:
                    logger.error("Could not parse manifest %s", manifestpth)
                    logger.exception(exc)
                else:
                    if manifestpth.startswith(winsxs):
                        assemblydir = os.path.join(winsxs, assemblynm)
                        if not os.path.isdir(assemblydir):
                            logger.warn("No such dir %s", assemblydir)
                            logger.warn("Assembly incomplete")
                            return []
                    else:
                        assemblydir = os.path.dirname(manifestpth)
                    files.append(manifestpth)
                    for file_ in self.files or manifest.files:
                        fn = file_.find(assemblydir)
                        if fn:
                            files.append(fn)
                        else:
                            # If any of our files does not exist,
                            # the assembly is incomplete
                            logger.warn("Assembly incomplete")
                            return []
                return files

        logger.warn("Assembly not found")
        return []

    def getid(self, language=None, version=None):
        """
        Return an identification string which uniquely names a manifest.

        This string is a combination of the manifest's processorArchitecture, 
        name, publicKeyToken, version and language.
        
        Arguments:
        version (tuple or list of integers) - If version is given, use it 
                                              instead of the manifest's 
                                              version.
        
        """
        if not self.name:
            logger.warn("Assembly metadata incomplete")
            return ""
        id = []
        if self.processorArchitecture:
            id.append(self.processorArchitecture)
        id.append(self.name)
        if self.publicKeyToken:
            id.append(self.publicKeyToken)
        if version or self.version:
            id.append(".".join([str(i) for i in version or self.version]))
        if not language:
            language = self.getlanguage()
        if language:
            id.append(language)
        return "_".join(id)
    
    def getlanguage(self, language=None, windowsversion=None):
        """
        Get and return the manifest's language as string.
        
        Can be either language-culture e.g. 'en-us' or a string indicating 
        language neutrality, e.g. 'x-ww' on Windows XP or 'none' on Vista 
        and later.
        
        """
        if not language:
            language = self.language
        if language in (None, "", "*", "neutral"):
            return (LANGUAGE_NEUTRAL_NT5,
                    LANGUAGE_NEUTRAL_NT6)[(windowsversion or 
                                           sys.getwindowsversion()) >= (6, )]
        return language
    
    def getpolicyid(self, fuzzy=True, language=None, windowsversion=None):
        """
        Return an identification string which can be used to find a policy.

        This string is a combination of the manifest's processorArchitecture, 
        major and minor version, name, publicKeyToken and language.
        
        Arguments:
        fuzzy (boolean)             - If False, insert the full version in 
                                      the id string. Default is True (omit).
        windowsversion              - If not specified (or None), default to 
        (tuple or list of integers)   sys.getwindowsversion().
        
        """
        if not self.name:
            logger.warn("Assembly metadata incomplete")
            return ""
        id = []
        if self.processorArchitecture:
            id.append(self.processorArchitecture)
        name = []
        name.append("policy")
        if self.version:
            name.append(str(self.version[0]))
            name.append(str(self.version[1]))
        name.append(self.name)
        id.append(".".join(name))
        if self.publicKeyToken:
            id.append(self.publicKeyToken)
        if self.version and (windowsversion or sys.getwindowsversion()) >= (6, ):
            # Vista and later
            if fuzzy:
                id.append("*")
            else:
                id.append(".".join([str(i) for i in self.version]))
        if not language:
            language = self.getlanguage(windowsversion=windowsversion)
        if language:
            id.append(language)
        id.append("*")
        id = "_".join(id)
        if self.version and (windowsversion or sys.getwindowsversion()) < (6, ):
            # Windows XP
            if fuzzy:
                id = os.path.join(id, "*")
            else:
                id = os.path.join(id, ".".join([str(i) for i in self.version]))
        return id

    def load_dom(self, domtree, initialize=True):
        """
        Load manifest from DOM tree.
        
        If initialize is True (default), reset existing attributes first.
        
        """
        if domtree.nodeType == Node.DOCUMENT_NODE:
            rootElement = domtree.documentElement
        elif domtree.nodeType == Node.ELEMENT_NODE:
            rootElement = domtree
        else:
            raise InvalidManifestError("Invalid root element node type " + 
                                       str(rootElement.nodeType) + 
                                       " - has to be one of (DOCUMENT_NODE, "
                                       "ELEMENT_NODE)")
        allowed_names = ("assembly", "assemblyBinding", "configuration", 
                         "dependentAssembly")
        if rootElement.tagName not in allowed_names:
            raise InvalidManifestError(
                "Invalid root element <%s> - has to be one of <%s>" % 
                (rootElement.tagName, ">, <".join(allowed_names)))
        # logger.info("loading manifest metadata from element <%s>", rootElement.tagName)
        if rootElement.tagName == "configuration":
            for windows in rootElement.getCEByTN("windows"):
                for assemblyBinding in windows.getCEByTN("assemblyBinding"):
                    self.load_dom(assemblyBinding, initialize)
        else:
            if initialize:
                self.__init__()
            self.manifestType = rootElement.tagName
            self.manifestVersion = [int(i) 
                                    for i in 
                                    (rootElement.getA("manifestVersion") or 
                                     "1.0").split(".")]
            self.noInheritable = bool(rootElement.getFCEByTN("noInheritable"))
            self.noInherit = bool(rootElement.getFCEByTN("noInherit"))
            for assemblyIdentity in rootElement.getCEByTN("assemblyIdentity"):
                self.type = assemblyIdentity.getA("type") or None
                self.name = assemblyIdentity.getA("name") or None
                self.language = assemblyIdentity.getA("language") or None
                self.processorArchitecture = assemblyIdentity.getA(
                    "processorArchitecture") or None
                version = assemblyIdentity.getA("version")
                if version:
                    self.version = [int(i) for i in version.split(".")]
                self.publicKeyToken = assemblyIdentity.getA("publicKeyToken") or None
            for publisherPolicy in rootElement.getCEByTN("publisherPolicy"):
                self.applyPublisherPolicy = (publisherPolicy.getA("apply") or 
                                             "").lower() == "yes"
            for description in rootElement.getCEByTN("description"):
                if description.firstChild:
                    self.description = description.firstChild.wholeText
            for trustInfo in rootElement.getCEByTN("trustInfo"):
                for security in trustInfo.getCEByTN("security"):
                    for reqPriv in security.getCEByTN("requestedPrivileges"):
                        for reqExeLev in reqPriv.getCEByTN("requestedExecutionLevel"):
                            self.requestedExecutionLevel = reqExeLev.getA("level")
                            self.uiAccess = (reqExeLev.getA("uiAccess") or 
                                             "").lower() == "true"
            if rootElement.tagName == "assemblyBinding":
                dependencies = [rootElement]
            else:
                dependencies = rootElement.getCEByTN("dependency")
            for dependency in dependencies:
                for dependentAssembly in dependency.getCEByTN(
                    "dependentAssembly"):
                    manifest = ManifestFromDOM(dependentAssembly)
                    if not manifest.name:
                        # invalid, skip
                        continue
                    manifest.optional = (dependency.getA("optional") or 
                                         "").lower() == "yes"
                    self.dependentAssemblies.append(manifest)
                    if self.filename:
                        # Enable search for private assembly by assigning bogus 
                        # filename (only the directory has to be correct)
                        self.dependentAssemblies[-1].filename = ":".join(
                            (self.filename, manifest.name.encode(sys.getdefaultencoding())))
            for bindingRedirect in rootElement.getCEByTN("bindingRedirect"):
                oldVersion = [[int(i) for i in part.split(".")] 
                              for part in 
                              bindingRedirect.getA("oldVersion").split("-")]
                newVersion = [int(i) 
                              for i in 
                              bindingRedirect.getA("newVersion").split(".")]
                self.bindingRedirects.append((oldVersion, newVersion))
            for file_ in rootElement.getCEByTN("file"):
                self.add_file(name=file_.getA("name"),
                              hashalg=file_.getA("hashalg"),
                              hash=file_.getA("hash"))
    
    def parse(self, filename_or_file, initialize=True):
        """ Load manifest from file or file object """
        if isinstance(filename_or_file, (str, unicode)):
            filename = filename_or_file
        else:
            filename = filename_or_file.name
        try:
            domtree = minidom.parse(filename_or_file)
        except xml.parsers.expat.ExpatError, e:
            args = [e.args[0]]
            if isinstance(filename, unicode):
                filename = filename.encode(sys.getdefaultencoding(), "replace")
            args.insert(0, '\n  File "%s"\n   ' % filename)
            raise ManifestXMLParseError(" ".join([str(arg) for arg in args]))
        if initialize:
            self.__init__()
        self.filename = filename
        self.load_dom(domtree, False)
    
    def parse_string(self, xmlstr, initialize=True):
        """ Load manifest from XML string """
        try:
            domtree = minidom.parseString(xmlstr)
        except xml.parsers.expat.ExpatError, e:
            raise ManifestXMLParseError(e)
        self.load_dom(domtree, initialize)
    
    def same_id(self, manifest, skip_version_check=False):
        """
        Return a bool indicating if another manifest has the same identitiy.
        
        This is done by comparing language, name, processorArchitecture, 
        publicKeyToken, type and version.
        
        """
        if skip_version_check:
            version_check = True
        else:
            version_check = self.version == manifest.version
        return (self.language == manifest.language and
                self.name == manifest.name and
                self.processorArchitecture == manifest.processorArchitecture and
                self.publicKeyToken == manifest.publicKeyToken and
                self.type == manifest.type and
                version_check)
    
    def todom(self):
        """ Return the manifest as DOM tree """
        doc = Document()
        docE = doc.cE(self.manifestType)
        if self.manifestType == "assemblyBinding":
            cfg = doc.cE("configuration")
            win = doc.cE("windows")
            win.aChild(docE)
            cfg.aChild(win)
            doc.aChild(cfg)
        else:
            doc.aChild(docE)
        if self.manifestType != "dependentAssembly":
            docE.setA("xmlns", "urn:schemas-microsoft-com:asm.v1")
            if self.manifestType != "assemblyBinding":
                docE.setA("manifestVersion", 
                          ".".join([str(i) for i in self.manifestVersion]))
        if self.noInheritable:
            docE.aChild(doc.cE("noInheritable"))
        if self.noInherit:
            docE.aChild(doc.cE("noInherit"))
        aId = doc.cE("assemblyIdentity")
        if self.type:
            aId.setAttribute("type", self.type)
        if self.name:
            aId.setAttribute("name", self.name)
        if self.language:
            aId.setAttribute("language", self.language)
        if self.processorArchitecture:
            aId.setAttribute("processorArchitecture", 
                             self.processorArchitecture)
        if self.version:
            aId.setAttribute("version", 
                             ".".join([str(i) for i in self.version]))
        if self.publicKeyToken:
            aId.setAttribute("publicKeyToken", self.publicKeyToken)
        if aId.hasAttributes():
            docE.aChild(aId)
        else:
            aId.unlink()
        if self.applyPublisherPolicy != None:
            ppE = doc.cE("publisherPolicy")
            if self.applyPublisherPolicy:
                ppE.setA("apply", "yes")
            else:
                ppE.setA("apply", "no")
            docE.aChild(ppE)
        if self.description:
            descE = doc.cE("description")
            descE.aChild(doc.cT(self.description))
            docE.aChild(descE)
        if self.requestedExecutionLevel in ("asInvoker", "highestAvailable", 
                                            "requireAdministrator"):
            tE = doc.cE("trustInfo")
            tE.setA("xmlns", "urn:schemas-microsoft-com:asm.v3")
            sE = doc.cE("security")
            rpE = doc.cE("requestedPrivileges")
            relE = doc.cE("requestedExecutionLevel")
            relE.setA("level", self.requestedExecutionLevel)
            if self.uiAccess:
                relE.setA("uiAccess", "true")
            else:
                relE.setA("uiAccess", "false")
            rpE.aChild(relE)
            sE.aChild(rpE)
            tE.aChild(sE)
            docE.aChild(tE)
        if self.dependentAssemblies:
            for assembly in self.dependentAssemblies:
                if self.manifestType != "assemblyBinding":
                    dE = doc.cE("dependency")
                    if assembly.optional:
                        dE.setAttribute("optional", "yes")
                daE = doc.cE("dependentAssembly")
                adom = assembly.todom()
                for child in adom.documentElement.childNodes:
                    daE.aChild(child.cloneNode(False))
                adom.unlink()
                if self.manifestType != "assemblyBinding":
                    dE.aChild(daE)
                    docE.aChild(dE)
                else:
                    docE.aChild(daE)
        if self.bindingRedirects:
            for bindingRedirect in self.bindingRedirects:
                brE = doc.cE("bindingRedirect")
                brE.setAttribute("oldVersion", 
                                 "-".join([".".join([str(i) 
                                                     for i in 
                                                     part]) 
                                           for part in 
                                           bindingRedirect[0]]))
                brE.setAttribute("newVersion", 
                                 ".".join([str(i) for i in bindingRedirect[1]]))
                docE.aChild(brE)
        if self.files:
            for file_ in self.files:
                fE = doc.cE("file")
                for attr in ("name", "hashalg", "hash"):
                    val = getattr(file_, attr)
                    if val:
                        fE.setA(attr, val)
                docE.aChild(fE)
        return doc
    
    def toprettyxml(self, indent="  ", newl=os.linesep, encoding="UTF-8"):
        """ Return the manifest as pretty-printed XML """
        domtree = self.todom()
        # WARNING: The XML declaration has to follow the order 
        # version-encoding-standalone (standalone being optional), otherwise 
        # if it is embedded in an exe the exe will fail to launch! 
        # ('application configuration incorrect')
        if sys.version_info >= (2,3):
            xmlstr = domtree.toprettyxml(indent, newl, encoding)
        else:
            xmlstr = domtree.toprettyxml(indent, newl)
        xmlstr = xmlstr.strip(os.linesep).replace(
                '<?xml version="1.0" encoding="%s"?>' % encoding, 
                '<?xml version="1.0" encoding="%s" standalone="yes"?>' % 
                encoding)
        domtree.unlink()
        return xmlstr
    
    def toxml(self, encoding="UTF-8"):
        """ Return the manifest as XML """
        domtree = self.todom()
        # WARNING: The XML declaration has to follow the order 
        # version-encoding-standalone (standalone being optional), otherwise 
        # if it is embedded in an exe the exe will fail to launch! 
        # ('application configuration incorrect')
        xmlstr = domtree.toxml(encoding).replace(
            '<?xml version="1.0" encoding="%s"?>' % encoding, 
            '<?xml version="1.0" encoding="%s" standalone="yes"?>' % encoding)
        domtree.unlink()
        return xmlstr

    def update_resources(self, dstpath, names=None, languages=None):
        """ Update or add manifest resource in dll/exe file dstpath """
        UpdateManifestResourcesFromXML(dstpath, self.toprettyxml(), names, 
                                       languages)
    
    def writeprettyxml(self, filename_or_file=None, indent="  ", newl=os.linesep, 
                       encoding="UTF-8"):
        """ Write the manifest as XML to a file or file object """
        if not filename_or_file:
            filename_or_file = self.filename
        if isinstance(filename_or_file, (str, unicode)):
            filename_or_file = open(filename_or_file, "wb")
        xmlstr = self.toprettyxml(indent, newl, encoding)
        filename_or_file.write(xmlstr)
        filename_or_file.close()
    
    def writexml(self, filename_or_file=None, indent="  ", newl=os.linesep, 
                 encoding="UTF-8"):
        """ Write the manifest as XML to a file or file object """
        if not filename_or_file:
            filename_or_file = self.filename
        if isinstance(filename_or_file, (str, unicode)):
            filename_or_file = open(filename_or_file, "wb")
        xmlstr = self.toxml(indent, newl, encoding)
        filename_or_file.write(xmlstr)
        filename_or_file.close()


def ManifestFromResFile(filename, names=None, languages=None):
    """ Create and return manifest instance from resource in dll/exe file """
    res = GetManifestResources(filename, names, languages)
    pth = []
    if res and res[RT_MANIFEST]:
        while isinstance(res, dict) and res.keys():
            key = res.keys()[0]
            pth.append(str(key))
            res = res[key]
    if isinstance(res, dict):
        raise InvalidManifestError("No matching manifest resource found in '%s'" % 
                                   filename)
    manifest = Manifest()
    manifest.filename = ":".join([filename] + pth)
    manifest.parse_string(res, False)
    return manifest


def ManifestFromDOM(domtree):
    """ Create and return manifest instance from DOM tree """
    manifest = Manifest()
    manifest.load_dom(domtree)
    return manifest


def ManifestFromXML(xmlstr):
    """ Create and return manifest instance from XML """
    manifest = Manifest()
    manifest.parse_string(xmlstr)
    return manifest


def ManifestFromXMLFile(filename_or_file):
    """ Create and return manifest instance from file """
    manifest = Manifest()
    manifest.parse(filename_or_file)
    return manifest


def GetManifestResources(filename, names=None, languages=None):
    """ Get manifest resources from file """
    return winresource.GetResources(filename, [RT_MANIFEST], names, languages)


def UpdateManifestResourcesFromXML(dstpath, xmlstr, names=None, 
                                   languages=None):
    """ Update or add manifest XML as resource in dstpath """
    logger.info("Updating manifest in %s", dstpath)
    if dstpath.lower().endswith(".exe"):
        name = 1 
    else:
        name = 2
    winresource.UpdateResources(dstpath, xmlstr, RT_MANIFEST, names or [name], 
                             languages or [0, "*"])


def UpdateManifestResourcesFromXMLFile(dstpath, srcpath, names=None, 
                                       languages=None):
    """ Update or add manifest XML from srcpath as resource in dstpath """
    logger.info("Updating manifest from %s in %s", srcpath, dstpath)
    if dstpath.lower().endswith(".exe"):
        name = 1 
    else:
        name = 2
    winresource.UpdateResourcesFromDataFile(dstpath, srcpath, RT_MANIFEST, 
                                         names or [name], 
                                         languages or [0, "*"])


def create_manifest(filename, manifest, console):
    """
    Create assembly manifest.
    """
    if not manifest:
        manifest = ManifestFromXMLFile(filename)
        # /path/NAME.exe.manifest - split extension twice to get NAME.
        name = os.path.basename(filename)
        manifest.name = os.path.splitext(os.path.splitext(name)[0])[0]
    elif isinstance(manifest, basestring) and "<" in manifest:
        # Assume XML string
        manifest = ManifestFromXML(manifest)
    elif not isinstance(manifest, Manifest):
        # Assume filename
        manifest = ManifestFromXMLFile(manifest)
    dep_names = set([dep.name for dep in manifest.dependentAssemblies])
    if manifest.filename != filename:
        # Update dependent assemblies
        depmanifest = ManifestFromXMLFile(filename)
        for assembly in depmanifest.dependentAssemblies:
            if not assembly.name in dep_names:
                manifest.dependentAssemblies.append(assembly)
                dep_names.add(assembly.name)
    if (not console and
        not "Microsoft.Windows.Common-Controls" in dep_names):
        # Add Microsoft.Windows.Common-Controls to dependent assemblies
        manifest.dependentAssemblies.append(
            Manifest(type_="win32",
                 name="Microsoft.Windows.Common-Controls",
                 language="*",
                 processorArchitecture=processor_architecture(),
                 version=(6, 0, 0, 0),
                 publicKeyToken="6595b64144ccf1df")
            )
    manifest.writeprettyxml(filename)
    return manifest


def processor_architecture():
    """
    Detect processor architecture for assembly manifest.

    According to:
    http://msdn.microsoft.com/en-us/library/windows/desktop/aa374219(v=vs.85).aspx
    item processorArchitecture in assembly manifest is

    'x86' - 32bit Windows
    'amd64' - 64bit Windows
    """
    if architecture() == '32bit':
        return 'x86'
    else:
        return 'amd64'


if __name__ == "__main__":    
    dstpath = sys.argv[1]
    srcpath = sys.argv[2]
    UpdateManifestResourcesFromXMLFile(dstpath, srcpath)
